"""High-level API the dashboard uses at runtime.

Takes a product-type bundle (from nis_rules/{TYPE}.json) + current form state
and returns per-field verdicts:

    {
      "A": {
        "field_key": "rtip_vendor_code#1.value",
        "label":     "Vendor Code",
        "section":   "Listing Identity",
        "base_requirement": "CONDITIONALLY REQUIRED",
        "verdict":   "required_missing" | "required_ok" | "optional" | "hidden" | "error" | "review",
        "value":     "<current user value>",
        "dropdown_source":  ["A","B","C"] or None,
        "rule_trail": [
          {"rule_id": "cf_0001", "kind": "required_missing", "verdict": "required_missing",
           "source": "...", "trigger_cells": ["A7","D7"]},
          ...
        ],
        "trigger_cells": ["A7","D7"],   # union across all firing rules
        "errors": []
      },
      ...
    }

The engine is pure Python, fast, and deterministic. Single entry point
for the Flask endpoint we add in app.py.
"""

import json
import os
from typing import Any, Dict, List, Optional, Set

try:
    # Relative import when used as a package
    from .nis_rule_evaluator import rule_verdict, evaluate, EvalContext, is_error
except ImportError:
    from nis_rule_evaluator import rule_verdict, evaluate, EvalContext, is_error


# In-memory cache of loaded bundles keyed by product_type.
_BUNDLE_CACHE: Dict[str, dict] = {}
_BUNDLE_DIR: Optional[str] = None
_DEFAULTS_CACHE: Optional[dict] = None
_PACKAGING_CACHE: Optional[dict] = None


def set_bundle_dir(path: str) -> None:
    """Point the engine at the directory holding {TYPE}.json bundle files."""
    global _BUNDLE_DIR, _DEFAULTS_CACHE, _PACKAGING_CACHE
    _BUNDLE_DIR = path
    _BUNDLE_CACHE.clear()
    _DEFAULTS_CACHE = None
    _PACKAGING_CACHE = None


def _engine_dir() -> str:
    """Directory where this module lives — the JSON helper files sit alongside."""
    return os.path.dirname(os.path.abspath(__file__))


def _load_apparel_defaults() -> dict:
    """Load apparel_defaults.json once and cache."""
    global _DEFAULTS_CACHE
    if _DEFAULTS_CACHE is not None:
        return _DEFAULTS_CACHE
    path = os.path.join(_engine_dir(), "apparel_defaults.json")
    if not os.path.exists(path):
        _DEFAULTS_CACHE = {"defaults": {}, "_applies_to": []}
        return _DEFAULTS_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            _DEFAULTS_CACHE = json.load(f)
    except Exception as e:
        print(f"[nis_engine] failed to load apparel_defaults.json: {e}")
        _DEFAULTS_CACHE = {"defaults": {}, "_applies_to": []}
    return _DEFAULTS_CACHE


def _load_packaging_memory() -> dict:
    """Load brand_packaging_memory.json (operator-confirmed package dims per brand+subclass)."""
    global _PACKAGING_CACHE
    if _PACKAGING_CACHE is not None:
        return _PACKAGING_CACHE
    path = os.path.join(_engine_dir(), "brand_packaging_memory.json")
    if not os.path.exists(path):
        _PACKAGING_CACHE = {"entries": {}}
        return _PACKAGING_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            _PACKAGING_CACHE = json.load(f)
    except Exception:
        _PACKAGING_CACHE = {"entries": {}}
    return _PACKAGING_CACHE


def _save_packaging_memory(data: dict) -> None:
    global _PACKAGING_CACHE
    path = os.path.join(_engine_dir(), "brand_packaging_memory.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _PACKAGING_CACHE = data


def get_packaging_for(brand: str, product_type: str, sub_class: str) -> Optional[dict]:
    """Return the saved package dims for (brand, product_type, sub_class) or None."""
    if not brand or not product_type:
        return None
    mem = _load_packaging_memory()
    key = f"{brand}|{product_type.upper()}|{sub_class or ''}"
    return (mem.get("entries") or {}).get(key)


def save_packaging_for(brand: str, product_type: str, sub_class: str, dims: dict) -> dict:
    """Save package dims and return the updated entry. dims should be a dict of field_key -> value."""
    if not brand or not product_type:
        raise ValueError("brand and product_type are required")
    mem = _load_packaging_memory()
    if "entries" not in mem:
        mem["entries"] = {}
    key = f"{brand}|{product_type.upper()}|{sub_class or ''}"
    from datetime import datetime
    mem["entries"][key] = {**dims, "_updated_at": datetime.utcnow().isoformat() + "Z"}
    mem["_updated_at"] = datetime.utcnow().isoformat() + "Z"
    _save_packaging_memory(mem)
    return mem["entries"][key]


def list_packaging_memory() -> dict:
    return _load_packaging_memory()


def load_bundle(product_type: str) -> Optional[dict]:
    """Load a product-type bundle from disk (or memory cache). Returns None if missing."""
    if not product_type:
        return None
    pt = product_type.upper().strip()
    if pt in _BUNDLE_CACHE:
        return _BUNDLE_CACHE[pt]
    if not _BUNDLE_DIR:
        return None
    path = os.path.join(_BUNDLE_DIR, f"{pt}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
    _BUNDLE_CACHE[pt] = bundle
    return bundle


def list_product_types() -> List[str]:
    """Return the list of product types with rule bundles available."""
    if not _BUNDLE_DIR or not os.path.isdir(_BUNDLE_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(_BUNDLE_DIR)
        if f.endswith(".json") and not f.startswith("__")
    )


def get_index() -> dict:
    """Return the __index__.json summary, or empty dict if missing."""
    if not _BUNDLE_DIR:
        return {}
    path = os.path.join(_BUNDLE_DIR, "__index__.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# State normalisation
# ============================================================================

def _field_to_cell_state(bundle: dict, form_state: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a field-key-keyed or column-keyed form_state into cell-ref state (row 7).

    Accepts any of:
      {"rtip_vendor_code#1.value": "AMZN4"}   # field_key
      {"A": "AMZN4"}                          # column letter
      {"A7": "AMZN4"}                         # cell ref (pass-through)
    Output is always cell-ref keyed: {"A7": "AMZN4", ...}.
    """
    data_row = bundle.get("data_row", 7)
    fields = bundle.get("fields", {})

    # Build a field_key -> column map
    key_to_col = {f["field_key"]: col for col, f in fields.items()}

    cell_state: Dict[str, Any] = {}
    for k, v in (form_state or {}).items():
        if not k:
            continue
        # Already a cell ref?
        if k and k[-1].isdigit() and k[0].isalpha():
            cell_state[k] = v
            continue
        # Column letter?
        if all(c.isalpha() and c.isupper() for c in k) and k in fields:
            cell_state[f"{k}{data_row}"] = v
            continue
        # Field key?
        if k in key_to_col:
            cell_state[f"{key_to_col[k]}{data_row}"] = v
            continue
        # Unknown — keep as-is so the evaluator doesn't silently lose it
        cell_state[k] = v
    return cell_state


# ============================================================================
# Per-field evaluation
# ============================================================================

def evaluate_form(
    product_type: str,
    form_state: Dict[str, Any],
    include_dropdowns: bool = True,
    apply_apparel_defaults: bool = True,
    brand: Optional[str] = None,
    sub_class: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate all rules for `product_type` against `form_state`.

    `apply_apparel_defaults`: when True (default), merge the universal apparel defaults
        from apparel_defaults.json into the form state before evaluation. Operator-supplied
        values always win — defaults only fill in fields the operator left blank.
    `brand`/`sub_class`: when provided, also merge the saved package dimensions for that
        (brand, product_type, sub_class) tuple from brand_packaging_memory.json.

    Returns a dict keyed by column letter with verdict info for each field.
    """
    bundle = load_bundle(product_type)
    if not bundle:
        return {"error": f"no rule bundle for product_type={product_type!r}"}

    # Merge defaults into form_state. Operator values always win.
    merged_state: Dict[str, Any] = {}
    if apply_apparel_defaults:
        defaults_doc = _load_apparel_defaults()
        applies_to = defaults_doc.get("_applies_to") or []
        if not applies_to or product_type.upper() in [a.upper() for a in applies_to]:
            merged_state.update(defaults_doc.get("defaults") or {})
    if brand:
        pkg = get_packaging_for(brand, product_type, sub_class or "")
        if pkg:
            for k, v in pkg.items():
                if k.startswith("_"):
                    continue
                merged_state[k] = v
    # Operator-supplied state wins over both
    for k, v in (form_state or {}).items():
        if v is None or v == "":
            continue
        merged_state[k] = v

    cell_state = _field_to_cell_state(bundle, merged_state)
    fields = bundle["fields"]
    rules  = bundle["rules"]

    ctx_named    = bundle.get("named_ranges") or {}
    ctx_vlookup  = bundle.get("vlookup_tables") or {}
    ctx_indirect = set(bundle.get("indirect_names") or [])

    # Per-column aggregation
    result: Dict[str, dict] = {}
    for col, fmeta in fields.items():
        # Read current value from cell state, with row 7 as data row
        data_row = bundle.get("data_row", 7)
        value = cell_state.get(f"{col}{data_row}", "")
        result[col] = {
            "field_key":    fmeta["field_key"],
            "label":        fmeta["label"],
            "section":      fmeta["section"],
            "base_requirement": fmeta["base_requirement"],
            "column":       col,
            "value":        value,
            "verdict":      "optional",          # default
            "rule_trail":   [],
            "trigger_cells": [],
            "dropdown_source": None,
            "errors":       [],
        }

    # Walk every rule, attach its verdict to the fields it applies to
    for r in rules:
        applies = r.get("applies_to") or []
        if not applies:
            continue
        ast = r.get("ast")
        kind = r.get("kind", "valid")
        if r.get("needs_review"):
            for col in applies:
                if col in result:
                    result[col]["rule_trail"].append({
                        "rule_id": r["rule_id"], "kind": kind, "verdict": "review",
                        "source": r["source"][:200], "trigger_cells": r.get("trigger_cells", []),
                    })
            continue

        # Evaluate with the appropriate rule_kind
        mapped_kind = _map_kind_to_evaluator(kind)
        v = rule_verdict(
            ast, state=cell_state,
            named_ranges=ctx_named,
            vlookup_tables=ctx_vlookup,
            indirect_names=ctx_indirect,
            rule_kind=mapped_kind,
        )
        for col in applies:
            if col not in result:
                continue
            trail_entry = {
                "rule_id": r["rule_id"],
                "kind":    kind,
                "verdict": v["verdict"],
                "source":  r["source"][:200],
                "trigger_cells": r.get("trigger_cells", []),
            }
            result[col]["rule_trail"].append(trail_entry)
            # Union trigger cells
            for tc in r.get("trigger_cells", []):
                if tc not in result[col]["trigger_cells"]:
                    result[col]["trigger_cells"].append(tc)

            if v["error"]:
                result[col]["errors"].append({
                    "rule_id": r["rule_id"], "error": v["error"],
                })

            # Handle dropdown source for DV rules
            if kind == "raw" and include_dropdowns and v["value"] is not None and not v["error"]:
                dd = _extract_dropdown_values(v["value"], ctx_named)
                if dd:
                    result[col]["dropdown_source"] = dd

    # Finalise per-field verdict by combining rule verdicts
    for col, info in result.items():
        info["verdict"] = _final_verdict(info, fields[col])

    return {
        "product_type": bundle.get("product_type"),
        "version":      bundle.get("version"),
        "data_row":     bundle.get("data_row", 7),
        "fields":       result,
        "summary":      _summarise(result),
        "defaults_applied": apply_apparel_defaults,
        "packaging_applied": bool(brand and get_packaging_for(brand, product_type, sub_class or "")),
    }


def _map_kind_to_evaluator(kind: str) -> str:
    """Map the extractor's classification to rule_verdict's rule_kind."""
    if kind == "required_missing":
        # formula TRUE => field is required but empty (red)
        return "required"
    if kind == "filled":
        # formula TRUE => value is filled (informational — no verdict shift)
        return "raw"
    if kind == "valid":
        # formula TRUE => requirement satisfied (green)
        return "valid"
    if kind == "hidden":
        return "hidden"
    return "raw"


def _extract_dropdown_values(val: Any, named_ranges: Dict[str, List[Any]]) -> Optional[List[Any]]:
    """If `val` is an INDIRECT/named-range reference, return its values list."""
    if isinstance(val, dict):
        if "__named__" in val:
            name = val["__named__"]
            vs = val.get("values")
            if vs is not None:
                return vs
            return named_ranges.get(name)
        if "__indirect__" in val:
            name = val["__indirect__"]
            vs = val.get("values")
            if vs is not None:
                return vs
            return named_ranges.get(name)
    return None


def _final_verdict(info: dict, fmeta: dict) -> str:
    """Combine all rule verdicts for a field into a single dashboard verdict.

    Priority:
      1. review (any rule needs human review) > error (unresolvable)
      2. required_missing (any required_missing rule fires)
      3. base_requirement REQUIRED + field empty -> required_missing
      4. valid (any valid rule fires) + field filled -> required_ok
      5. base_requirement REQUIRED/CONDITIONALLY REQUIRED + field filled -> required_ok
      6. default -> optional
    """
    has_review = any(t["verdict"] == "review" for t in info["rule_trail"])
    if has_review:
        return "review"

    has_error = any(t["verdict"] == "error" for t in info["rule_trail"])
    value = info["value"]
    is_filled = value not in (None, "", 0) or (isinstance(value, (int, float)) and value != 0)
    # Keep numeric-0 detection reasonable: treat explicit 0 as filled
    is_filled = value not in (None, "")

    # Check for required_missing CF rules that fired
    missing_fires = [t for t in info["rule_trail"]
                     if t["kind"] == "required_missing" and t["verdict"] == "required"]
    if missing_fires:
        return "required_missing"

    # Check for satisfied-requirement rules that fired
    valid_fires = [t for t in info["rule_trail"]
                   if t["kind"] == "valid" and t["verdict"] == "valid"]
    if valid_fires and is_filled:
        return "required_ok"
    if valid_fires and not is_filled:
        # Rule says "requirement is satisfied" but value is empty — this is a
        # CF that fires when the field correctly has its conditional trigger
        # un-met (e.g., Parentage Level is not Parent, so Vendor Code is OK to be blank).
        # Treat as optional.
        return "optional"

    # Base requirement fallback
    br = (fmeta.get("base_requirement") or "").upper()
    if br == "REQUIRED":
        return "required_ok" if is_filled else "required_missing"
    if br == "CONDITIONALLY REQUIRED":
        # Without a firing rule we can't judge; treat as optional unless filled
        return "required_ok" if is_filled else "optional"

    if has_error and not is_filled:
        return "error"
    return "optional"


def _summarise(result: Dict[str, dict]) -> dict:
    counts = {}
    for info in result.values():
        counts[info["verdict"]] = counts.get(info["verdict"], 0) + 1
    return counts
