"""Amazon NIS rule extractor — reads a .xlsm template and emits a JSON rule file
the dashboard can load for runtime evaluation.

Output schema (per template):
{
  "product_type":   "COAT",
  "template_file":  "Jackets_and_Coats_2026-04-15T21_23-2.xlsm",
  "version":        "Version=1.3",
  "label_row":      3,
  "key_row":        4,
  "requirement_row":5,
  "data_row":       7,
  "fields": {
    "A": {
      "field_key":     "rtip_vendor_code#1.value",
      "label":         "Vendor Code",
      "section":       "Listing Identity",
      "base_requirement": "CONDITIONALLY REQUIRED",  # from row 5
      "column":        "A"
    },
    ...
  },
  "rules": [
    {
      "rule_id":    "cf_001",
      "kind":       "required" | "hidden" | "valid" | "raw",
      "applies_to": ["A"],          # column letters
      "field_keys": ["rtip_vendor_code#1.value"],
      "trigger_cells": ["A7","D7"], # cells the formula reads
      "named_used":   ["CONDITION_LIST_3"],
      "source":      "<original Excel formula>",
      "ast":         { ... parsed AST ... },
      "needs_review": false
    },
    ...
  ],
  "named_ranges":     { "CONDITION_LIST_3": ["a","b",...], ... },
  "indirect_names":   ["COATparentage_level1.value", ...],   # all defined names usable by INDIRECT
  "vlookup_tables":   { "'Dropdown Lists'!$A$1:$B$16000": [["NFL","nfl"],...] },
  "coverage": {
    "total_formulas":  775,
    "parsed_clean":    775,
    "needs_review":    0,
    "fields_with_rules": 142
  }
}

Run from CLI:
    python nis_rule_extractor.py path/to/Jackets_and_Coats.xlsm out_dir/
or import and call extract_rules().
"""

import json
import os
import re
import sys
import zipfile
from typing import Any, Dict, List, Optional, Set, Tuple
from xml.etree import ElementTree as ET

# Make sibling modules importable when run as a script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nis_formula_parser import parse_formula, collect_cell_refs, collect_named_refs, has_unknowns

NS  = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
NSR = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


# ============================================================================
# XLSX helpers
# ============================================================================

def _load_shared_strings(z: zipfile.ZipFile) -> List[str]:
    if 'xl/sharedStrings.xml' not in z.namelist():
        return []
    root = ET.fromstring(z.read('xl/sharedStrings.xml').decode('utf-8'))
    out = []
    for si in root.iter(NS + 'si'):
        parts = []
        for t in si.iter(NS + 't'):
            if t.text:
                parts.append(t.text)
        out.append(''.join(parts))
    return out


def _cell_value(cell: ET.Element, strings: List[str]) -> Optional[Any]:
    v = cell.find(NS + 'v')
    t = cell.attrib.get('t', '')
    if v is None or v.text is None:
        if t == 'inlineStr':
            inl = cell.find(NS + 'is')
            if inl is not None:
                return ''.join(t.text or '' for t in inl.iter(NS + 't'))
        return None
    if t == 's':
        idx = int(v.text)
        return strings[idx] if 0 <= idx < len(strings) else None
    if t == 'b':
        return v.text == '1'
    # default — number-as-string. Convert if it looks numeric.
    txt = v.text
    try:
        if '.' in txt:
            return float(txt)
        return int(txt)
    except (ValueError, TypeError):
        return txt


def _col_letters(cell_ref: str) -> str:
    m = re.match(r'^([A-Z]+)\d+$', cell_ref)
    return m.group(1) if m else ''


def _split_sqref(sqref: str) -> List[str]:
    """Split an sqref like 'A7:A1048576 C7:C1048576' into individual ref strings."""
    if not sqref:
        return []
    return [s for s in sqref.split() if s]


def _columns_in_range(rng: str) -> List[str]:
    """Given 'A7:C9' return ['A','B','C']. Given 'A7' return ['A']."""
    if ':' in rng:
        a, b = rng.split(':', 1)
        ca, cb = _col_letters(a), _col_letters(b)
        if not ca or not cb:
            return []
        # Convert column letters to numbers, range, back to letters
        def col_to_num(c):
            n = 0
            for ch in c:
                n = n * 26 + (ord(ch) - ord('A') + 1)
            return n
        def num_to_col(n):
            s = ''
            while n > 0:
                n, r = divmod(n - 1, 26)
                s = chr(ord('A') + r) + s
            return s
        a_n, b_n = col_to_num(ca), col_to_num(cb)
        return [num_to_col(i) for i in range(min(a_n, b_n), max(a_n, b_n) + 1)]
    return [_col_letters(rng)]


# ============================================================================
# Sheet-name lookup + product type detection
# ============================================================================

def _find_template_sheet(z: zipfile.ZipFile) -> Tuple[str, str]:
    """Return (sheet_xml_path, product_type) for the 'Template-{TYPE}' sheet."""
    wb = ET.fromstring(z.read('xl/workbook.xml').decode('utf-8'))
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels').decode('utf-8'))
    rel_map = {r.attrib['Id']: r.attrib['Target']
               for r in rels.iter('{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')}

    template_sheet_path = None
    product_type = None
    for sheet in wb.iter(NS + 'sheet'):
        name = sheet.attrib.get('name', '')
        rid = sheet.attrib.get(NSR + 'id')
        if name.startswith('Template-') and rid in rel_map:
            target = rel_map[rid]
            template_sheet_path = _normalize_target(target)
            product_type = name.split('Template-', 1)[1].strip()
            break
    if not template_sheet_path:
        # Fallback: assume sheet3.xml
        template_sheet_path = 'xl/worksheets/sheet3.xml'
    return template_sheet_path, product_type


def _find_dropdown_sheet(z: zipfile.ZipFile) -> Optional[str]:
    """Return sheet xml path for 'Dropdown Lists' sheet, or None."""
    wb = ET.fromstring(z.read('xl/workbook.xml').decode('utf-8'))
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels').decode('utf-8'))
    rel_map = {r.attrib['Id']: r.attrib['Target']
               for r in rels.iter('{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')}
    for sheet in wb.iter(NS + 'sheet'):
        if sheet.attrib.get('name', '') == 'Dropdown Lists':
            rid = sheet.attrib.get(NSR + 'id')
            target = rel_map.get(rid)
            if target:
                return _normalize_target(target)
    return None


def _normalize_target(target: str) -> str:
    """Convert a relationship Target to a zip-internal path.
    Handles three observed formats:
      - 'worksheets/sheet3.xml'    (relative; needs xl/ prefix)
      - '/xl/worksheets/sheet3.xml' (absolute with leading /)
      - 'xl/worksheets/sheet3.xml'  (absolute already)
    """
    t = target.lstrip('/')
    if not t.startswith('xl/'):
        t = 'xl/' + t
    return t


# ============================================================================
# Defined names + named-range resolution
# ============================================================================

# Pattern for a sheet-qualified range/cell: 'Dropdown Lists'!$A$1:$B$3 or 'Sheet'!$A$1
_DEFNAME_PATTERN = re.compile(
    r"^'([^']+)'!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$"
)


def _parse_defined_names(z: zipfile.ZipFile) -> Dict[str, dict]:
    """Return a dict of name -> {sheet, col_start, row_start, col_end, row_end}."""
    wb = ET.fromstring(z.read('xl/workbook.xml').decode('utf-8'))
    out = {}
    for d in wb.iter(NS + 'definedName'):
        name = d.attrib.get('name', '')
        ref  = d.text or ''
        m = _DEFNAME_PATTERN.match(ref.strip())
        if not m:
            continue
        sheet, c1, r1, c2, r2 = m.groups()
        out[name] = {
            "sheet": sheet,
            "col_start": c1, "row_start": int(r1),
            "col_end":   c2 or c1, "row_end": int(r2) if r2 else int(r1),
        }
    return out


def _read_sheet_grid(z: zipfile.ZipFile, sheet_path: str, strings: List[str]) -> Dict[str, Any]:
    """Read every cell on a sheet into a dict keyed by ref (e.g., 'A1')."""
    if sheet_path not in z.namelist():
        return {}
    root = ET.fromstring(z.read(sheet_path).decode('utf-8'))
    grid = {}
    for row in root.iter(NS + 'row'):
        for c in row.iter(NS + 'c'):
            ref = c.attrib.get('r', '')
            grid[ref] = _cell_value(c, strings)
    return grid


def _resolve_named_range(grid: Dict[str, Any], spec: dict) -> List[Any]:
    """Given a defined-name spec on the 'Dropdown Lists' grid, return its values as a flat list."""
    if not spec:
        return []
    c1, c2 = spec["col_start"], spec["col_end"]
    r1, r2 = spec["row_start"], spec["row_end"]
    # If single-column range, flatten down the column
    out = []
    # Convert col letter to num
    def col_to_num(c):
        n = 0
        for ch in c:
            n = n * 26 + (ord(ch) - ord('A') + 1)
        return n
    def num_to_col(n):
        s = ''
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(ord('A') + r) + s
        return s
    n1, n2 = col_to_num(c1), col_to_num(c2)
    for r in range(r1, r2 + 1):
        for n in range(n1, n2 + 1):
            ref = f"{num_to_col(n)}{r}"
            v = grid.get(ref)
            if v is not None and v != "":
                out.append(v)
    return out


# ============================================================================
# Field metadata extraction (rows 2/3/4/5)
# ============================================================================

def _extract_fields(grid: Dict[str, Any]) -> Dict[str, dict]:
    """Read rows 2 (section), 3 (label), 4 (field_key), 5 (base requirement)."""
    fields = {}
    # Find every column that has a row-4 value (the canonical field key)
    cols = sorted({_col_letters(ref) for ref in grid if _col_letters(ref)})
    # Collect section spans (row 2): a section header sits in some column and applies right
    # until the next section header. Build (col_letter -> section) by sweeping.
    section_map: Dict[str, str] = {}
    current_section = ""
    for col in cols:
        s = grid.get(f"{col}2")
        if s:
            current_section = str(s)
        section_map[col] = current_section

    for col in cols:
        key = grid.get(f"{col}4")
        if not key:
            continue
        fields[col] = {
            "field_key": str(key),
            "label":     str(grid.get(f"{col}3", "") or ""),
            "section":   section_map.get(col, ""),
            "base_requirement": str(grid.get(f"{col}5", "") or ""),
            "column":    col,
        }
    return fields


# ============================================================================
# Conditional formatting + data validation
# ============================================================================

def _extract_cf_rules(sheet_xml: ET.Element) -> List[dict]:
    """Yield raw CF rule dicts: {sqref, formula, type, dxfId}."""
    rules = []
    for cf in sheet_xml.iter(NS + 'conditionalFormatting'):
        sqref = cf.attrib.get('sqref', '')
        for rule in cf.iter(NS + 'cfRule'):
            f = rule.find(NS + 'formula')
            if f is None or not f.text:
                continue
            rules.append({
                "sqref": sqref,
                "formula": f.text,
                "type": rule.attrib.get('type', ''),
                "dxfId": rule.attrib.get('dxfId', ''),
                "priority": rule.attrib.get('priority', ''),
            })
    return rules


def _extract_dv_rules(sheet_xml: ET.Element) -> List[dict]:
    """Yield raw DV rule dicts: {sqref, formula1, type}."""
    rules = []
    for dv in sheet_xml.iter(NS + 'dataValidation'):
        sqref = dv.attrib.get('sqref', '')
        f1 = dv.find(NS + 'formula1')
        if f1 is None or not f1.text:
            continue
        rules.append({
            "sqref": sqref,
            "formula": f1.text,
            "type": dv.attrib.get('type', ''),
            "operator": dv.attrib.get('operator', ''),
        })
    return rules


# dxfId -> rule-kind mapping, based on verified semantics across all 31 NIS templates:
#   dxfId 0/3 = 'requirement satisfied' (green when rule TRUE)
#   dxfId 1   = simple 'filled' indicator (TRUE when any value entered)
#   dxfId 2   = 'required but missing' (red when TRUE — this is the operator signal)
CF_DXF_KIND_MAP = {
    "0": "valid",             # green: requirement is met
    "1": "filled",            # green: value is present
    "2": "required_missing",  # red: field required but empty
    "3": "valid",             # green variant for conditionally-required
}


def _classify_cf_rule(rule: dict, dxf_styles: Dict[str, dict]) -> str:
    """Map a CF rule to a semantic kind using dxfId.

    Falls back to structural pattern matching if dxfId isn't in our known map.
    Only 'required_missing' rules fire the red operator badge on the dashboard.
    """
    dxf_id = str(rule.get("dxfId", ""))
    if dxf_id in CF_DXF_KIND_MAP:
        return CF_DXF_KIND_MAP[dxf_id]
    # Fallback: pattern match the formula (rare edge cases for non-standard dxfIds)
    src = rule.get("formula", "") or ""
    if "NOT(LEN(" in src and src.startswith("AND(NOT(LEN"):
        return "required_missing"
    if src.startswith("IF(LEN(") and src.endswith(",1,0)"):
        return "filled"
    return "valid"


# ============================================================================
# DXF style parsing (to enable future colour-based rule classification)
# ============================================================================

def _load_dxf_styles(z: zipfile.ZipFile) -> Dict[str, dict]:
    """Best-effort: parse xl/styles.xml dxfs entries and pull fill colour."""
    if 'xl/styles.xml' not in z.namelist():
        return {}
    root = ET.fromstring(z.read('xl/styles.xml').decode('utf-8'))
    dxfs = root.find(NS + 'dxfs')
    if dxfs is None:
        return {}
    out = {}
    for i, dxf in enumerate(dxfs.iter(NS + 'dxf')):
        fill = dxf.find(NS + 'fill')
        rgb = None
        if fill is not None:
            pf = fill.find(NS + 'patternFill')
            if pf is not None:
                fg = pf.find(NS + 'fgColor')
                if fg is not None:
                    rgb = fg.attrib.get('rgb') or fg.attrib.get('theme')
        out[str(i)] = {"fill_rgb": rgb}
    return out


# ============================================================================
# Public extraction API
# ============================================================================

def extract_rules(xlsm_path: str) -> dict:
    """Extract a complete rule bundle from one NIS .xlsm template."""
    if not os.path.exists(xlsm_path):
        raise FileNotFoundError(xlsm_path)

    with zipfile.ZipFile(xlsm_path) as z:
        strings = _load_shared_strings(z)
        template_sheet, product_type = _find_template_sheet(z)
        dropdown_sheet = _find_dropdown_sheet(z)
        defined_names = _parse_defined_names(z)
        dxf_styles = _load_dxf_styles(z)

        # Read the template sheet grid (for fields + cell context)
        template_grid = _read_sheet_grid(z, template_sheet, strings)

        # Read the dropdown sheet grid (for resolving named ranges)
        dropdown_grid = _read_sheet_grid(z, dropdown_sheet, strings) if dropdown_sheet else {}

        # Re-parse the sheet XML for CF/DV blocks
        sheet_xml_root = ET.fromstring(z.read(template_sheet).decode('utf-8'))
        cf_rules_raw = _extract_cf_rules(sheet_xml_root)
        dv_rules_raw = _extract_dv_rules(sheet_xml_root)

    # ---- Fields ----
    fields = _extract_fields(template_grid)

    # ---- Resolve named ranges & VLOOKUP tables ----
    named_ranges: Dict[str, List[Any]] = {}
    indirect_names: Set[str] = set()
    for name, spec in defined_names.items():
        indirect_names.add(name)
        # Materialise short condition lists (≤ a few hundred values). Cap to avoid bloat.
        if name.startswith('CONDITION_LIST_') or name.startswith('COAT') or \
           name.startswith('BLAZER') or name.startswith('DRESS') or name.startswith('SWIMWEAR'):
            vals = _resolve_named_range(dropdown_grid, spec)
            if vals and len(vals) <= 1024:
                named_ranges[name] = vals

    # ---- VLOOKUP tables on Dropdown Lists sheet ----
    vlookup_tables: Dict[str, List[List[Any]]] = {}
    # The standard cascade range used in templates
    standard_range = "'Dropdown Lists'!$A$1:$B$16000"
    if dropdown_grid:
        rows = []
        for r in range(1, 16001):
            a = dropdown_grid.get(f"A{r}")
            if a is None or a == "":
                # Stop early: rows after the first block of empties don't add value
                # but the cascade table may have gaps. Cap at first 100 consecutive empties.
                continue
            b = dropdown_grid.get(f"B{r}")
            rows.append([a, b])
        if rows:
            vlookup_tables[standard_range] = rows

    # ---- Parse CF rules ----
    rule_records = []
    needs_review_count = 0
    parsed_clean_count = 0
    rule_id_counter = 0

    def _make_rule_record(prefix: str, raw: dict, kind: str) -> Optional[dict]:
        nonlocal rule_id_counter, needs_review_count, parsed_clean_count
        rule_id_counter += 1
        rule_id = f"{prefix}_{rule_id_counter:04d}"

        ast = parse_formula(raw["formula"])
        review = has_unknowns(ast)
        if review:
            needs_review_count += 1
        else:
            parsed_clean_count += 1

        # Map sqref to columns
        cols: Set[str] = set()
        for r in _split_sqref(raw["sqref"]):
            cols.update(_columns_in_range(r))
        applies_to = sorted(cols)

        field_keys = [fields[c]["field_key"] for c in applies_to if c in fields]
        trigger_cells = collect_cell_refs(ast)
        named_used   = collect_named_refs(ast)

        rec = {
            "rule_id":      rule_id,
            "kind":         kind,
            "applies_to":   applies_to,
            "field_keys":   field_keys,
            "trigger_cells": trigger_cells,
            "named_used":   named_used,
            "source":       raw["formula"],
            "ast":          ast,
            "needs_review": review,
        }
        # Carry CF-specific style info
        if "dxfId" in raw and raw["dxfId"]:
            rec["dxf_fill"] = dxf_styles.get(raw["dxfId"], {}).get("fill_rgb")
        if "type" in raw:
            rec["xml_type"] = raw["type"]
        return rec

    for raw in cf_rules_raw:
        kind = _classify_cf_rule(raw, dxf_styles)
        rec = _make_rule_record("cf", raw, kind)
        if rec:
            rule_records.append(rec)

    for raw in dv_rules_raw:
        # DV is dropdown-source ("INDIRECT(...)") or list — kind='valid' (truthy means value is in allowed set)
        # We model it as 'raw' so the dashboard can use the resolved value as the dropdown source.
        rec = _make_rule_record("dv", raw, "raw")
        if rec:
            rule_records.append(rec)

    fields_with_rules = len({c for r in rule_records for c in r["applies_to"]} & set(fields.keys()))

    # ---- Version metadata from row 1 ----
    version = ""
    for col in 'ABCDEFGHIJKLMNOP':
        v = template_grid.get(f"{col}1")
        if isinstance(v, str) and v.startswith("Version="):
            version = v.split("=", 1)[1]
            break

    bundle = {
        "product_type":  product_type,
        "template_file": os.path.basename(xlsm_path),
        "version":       version,
        "label_row":     3,
        "key_row":       4,
        "requirement_row": 5,
        "data_row":      7,
        "fields":        fields,
        "rules":         rule_records,
        "named_ranges":  named_ranges,
        "indirect_names": sorted(indirect_names),
        "vlookup_tables": vlookup_tables,
        "coverage": {
            "total_formulas":  len(rule_records),
            "parsed_clean":    parsed_clean_count,
            "needs_review":    needs_review_count,
            "fields_with_rules": fields_with_rules,
            "field_count":     len(fields),
        },
    }
    return bundle


def write_bundle(bundle: dict, out_dir: str) -> str:
    """Write a bundle to {out_dir}/{PRODUCT_TYPE}.json. Returns the path.

    If a bundle already exists for this product type (e.g., a brand-specific
    NIS like Stella Parker that targets DRESS), merge them: union the rules
    (dedup by formula+sqref+kind), union fields, union named_ranges.
    Source templates are tracked in `merged_from`.
    """
    os.makedirs(out_dir, exist_ok=True)
    pt = bundle.get("product_type") or "UNKNOWN"
    out_path = os.path.join(out_dir, f"{pt}.json")

    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        bundle = _merge_bundles(existing, bundle)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    return out_path


def _merge_bundles(a: dict, b: dict) -> dict:
    """Merge bundle b into a (a wins on metadata; rules and fields are unioned)."""
    out = dict(a)

    # Track sources
    sources = list(a.get("merged_from") or [a.get("template_file")])
    if b.get("template_file") and b.get("template_file") not in sources:
        sources.append(b.get("template_file"))
    out["merged_from"] = [s for s in sources if s]

    # Fields: prefer existing, fill in missing columns from b
    fields = dict(a.get("fields") or {})
    for col, meta in (b.get("fields") or {}).items():
        if col not in fields:
            fields[col] = meta
    out["fields"] = fields

    # Rules: dedupe by (kind, sqref-applies_to, source)
    seen = set()
    rules = []
    for r in (a.get("rules") or []) + (b.get("rules") or []):
        key = (r.get("kind"), tuple(r.get("applies_to", [])), r.get("source"))
        if key in seen:
            continue
        seen.add(key)
        rules.append(r)
    # Re-id rules to keep IDs unique and ordered
    for i, r in enumerate(rules):
        prefix = r["rule_id"].split("_", 1)[0] if "_" in r.get("rule_id", "") else "r"
        r["rule_id"] = f"{prefix}_{i+1:04d}"
    out["rules"] = rules

    # Named ranges + indirect names: union
    nr = dict(a.get("named_ranges") or {})
    for k, v in (b.get("named_ranges") or {}).items():
        if k not in nr:
            nr[k] = v
    out["named_ranges"] = nr

    indirect = sorted(set((a.get("indirect_names") or []) + (b.get("indirect_names") or [])))
    out["indirect_names"] = indirect

    # VLOOKUP tables: union by range name (a wins on conflict)
    vt = dict(a.get("vlookup_tables") or {})
    for k, v in (b.get("vlookup_tables") or {}).items():
        if k not in vt:
            vt[k] = v
    out["vlookup_tables"] = vt

    # Recompute coverage
    fields_with_rules = len(
        {c for r in rules for c in r.get("applies_to", [])} & set(fields.keys())
    )
    needs_review = sum(1 for r in rules if r.get("needs_review"))
    out["coverage"] = {
        "total_formulas":  len(rules),
        "parsed_clean":    len(rules) - needs_review,
        "needs_review":    needs_review,
        "fields_with_rules": fields_with_rules,
        "field_count":     len(fields),
    }
    return out


def write_index(bundles: List[dict], out_dir: str) -> str:
    """Write an __index__.json mapping product types to summary metadata.

    Reads final on-disk bundles (post-merge) so totals are accurate.
    """
    os.makedirs(out_dir, exist_ok=True)
    index = {}
    for fname in sorted(os.listdir(out_dir)):
        if not fname.endswith(".json") or fname.startswith("__"):
            continue
        path = os.path.join(out_dir, fname)
        with open(path, "r", encoding="utf-8") as f:
            b = json.load(f)
        pt = b.get("product_type") or os.path.splitext(fname)[0]
        index[pt] = {
            "template_file":  b.get("template_file"),
            "merged_from":    b.get("merged_from") or [b.get("template_file")],
            "version":        b.get("version"),
            "field_count":    b["coverage"]["field_count"],
            "rule_count":     b["coverage"]["total_formulas"],
            "needs_review":   b["coverage"]["needs_review"],
            "fields_with_rules": b["coverage"]["fields_with_rules"],
        }
    out_path = os.path.join(out_dir, "__index__.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return out_path


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python nis_rule_extractor.py <template.xlsm> <out_dir>")
        print("  or:  python nis_rule_extractor.py --all <templates_dir> <out_dir>")
        sys.exit(1)

    if sys.argv[1] == "--all":
        templates_dir = sys.argv[2]
        out_dir = sys.argv[3]
        bundles = []
        import glob
        for f in sorted(glob.glob(os.path.join(templates_dir, "*.xlsm"))):
            print(f"Extracting {os.path.basename(f)}...")
            try:
                b = extract_rules(f)
                p = write_bundle(b, out_dir)
                print(f"  -> {p}  ({b['coverage']['total_formulas']} rules, "
                      f"{b['coverage']['needs_review']} need review, "
                      f"{b['coverage']['field_count']} fields)")
                bundles.append(b)
            except Exception as e:
                print(f"  ! failed: {e}")
        idx = write_index(bundles, out_dir)
        print(f"\nWrote index: {idx}  ({len(bundles)} templates)")
    else:
        xlsm = sys.argv[1]
        out_dir = sys.argv[2]
        b = extract_rules(xlsm)
        p = write_bundle(b, out_dir)
        print(f"Wrote {p}  ({b['coverage']['total_formulas']} rules, "
              f"{b['coverage']['needs_review']} need review, "
              f"{b['coverage']['field_count']} fields)")
