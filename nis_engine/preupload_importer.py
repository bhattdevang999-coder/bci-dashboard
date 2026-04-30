"""Read a TLG-style Sage/Volcom 'Pre-Upload' .xlsx and convert each style row
into an NIS form-state dict that the rule engine can evaluate.

Designed to be schema-tolerant: column headers are matched by name (lowercased,
punctuation-insensitive) so R0 vs R1 column shuffles don't break it.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

try:
    import openpyxl
except ImportError:
    openpyxl = None


# Brand-aware vendor-code lookup. Add brands here as they sign on.
_BRAND_VENDOR_CODES = {
    "Sage Collective": "QT5G8",
}


# Color-code -> Amazon standardized color
_COLOR_MAP = {
    "BEIGE": "Beige", "BLACK": "Black", "BROWN": "Brown", "BLUE": "Blue",
    "RED": "Red", "GREEN": "Green", "GRAY": "Gray", "GREY": "Gray",
    "WHITE": "White", "IVORY": "Off White", "NAVY": "Blue", "TAN": "Brown",
    "CAMEL": "Brown", "OLIVE": "Green", "CREAM": "Off White", "CHARCOAL": "Gray",
    "BURGUNDY": "Red", "WINE": "Red", "PINK": "Pink", "PURPLE": "Purple",
    "TAUPE": "Brown", "COGNAC": "Brown", "TRUFFLE": "Brown", "BEG": "Beige",
    "BLK": "Black", "NVY": "Blue",
}


# Sub-class label -> (product_category, product_subcategory, item_type_keyword, item_type_name)
# COAT cascades known to Amazon NIS — verified against engine bundles.
_SUBCLASS_TAXONOMY = {
    "Puffer":              ("Women's Outerwear", "Down and Parkas", "down-and-parkas-coats", "Puffer Coat"),
    "Faux Wool Outerwear": ("Women's Outerwear", "Wool",            "wool-outerwear-coats",  "Wool Coat"),
    "Wool Outerwear":      ("Women's Outerwear", "Wool",            "wool-outerwear-coats",  "Wool Coat"),
    "Anorak":              ("Women's Outerwear", "Lightweight Jackets and Windbreakers",
                                                                    "anorak-jackets",        "Anorak"),
    "Vest":                ("Women's Outerwear", "Lightweight Jackets and Windbreakers",
                                                                    "outerwear-vests",       "Vest"),
    "Parka":               ("Women's Outerwear", "Down and Parkas", "parkas",                "Parka"),
}


def _norm_header(s: Optional[str]) -> str:
    """Lowercase, strip non-alphanumerics. Used for tolerant header matching."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


# Map of normalized header tokens to canonical field key. The list of aliases
# captures the variations we've seen across Sage/Volcom pre-upload files.
_HEADER_ALIASES = {
    "season":          ["seasoncode", "season"],
    "tlgdiv":          ["tlgdivname", "brandcode"],
    "division":        ["division"],
    "sub_class":       ["subclassname"],
    "sub_sub":         ["subsubclassname"],
    "style":           ["style", "styleno", "stylenumber"],
    "name":            ["basicstylename", "stylename", "tlgstylename"],
    "keywords":        ["relatedkeywords", "tlgstyledesc"],
    "color_code":      ["colorcode"],
    "color":           ["colorname"],
    "model":           ["modelnumber"],
    "size":            ["productsize", "size"],
    "upc":             ["upccode", "upc"],
    "asin":            ["childasin"],
    "sku":             ["sku"],
    "due_date":        ["duedateearliestshipdate", "duedate"],
    "amazon_cost":     ["amazoncost", "amznwholesale", "wholesale", "cost"],
    "list_price":      ["amazonlistprice", "amznretail", "retail", "listprice"],
    "department":      ["department"],
    "type_jacket":     ["typeofjacket", "typeofjacket"],
    "coo":             ["coo", "countryoforigin"],
    "care":            ["careinstructions", "care"],
    "fabric":          ["fabriccontentpercentage", "fabric", "fabriccontent"],
    "closure":         ["closuretype", "closure"],
    "length":          ["centerbacklengthcbl", "length", "cbl"],
    "pockets":         ["numberofpockets"],
    "hood":            ["removablehood"],
    "addl":            ["additionaldetailsstandoutscalloutsfeatures",
                        "additionaldetails", "standouts"],
}


def _build_column_map(header_row: List[Any]) -> Dict[str, int]:
    """Given the first row of a sheet, return a map of canonical_key -> 1-based column index."""
    out: Dict[str, int] = {}
    for col_idx, val in enumerate(header_row, start=1):
        norm = _norm_header(val)
        for canonical, aliases in _HEADER_ALIASES.items():
            if norm in aliases:
                if canonical not in out:  # first match wins
                    out[canonical] = col_idx
                break
    return out


def parse_preupload(xlsx_path: str, brand_hint: Optional[str] = None) -> Dict[str, Any]:
    """Parse the pre-upload xlsx into a structured per-style dict.

    Returns:
        {
          "brand":   "Sage Collective" (inferred from TLGDIV NAME or brand_hint),
          "styles": {
              "107010295": {
                "style":       "107010295",
                "sub_class":   "Puffer",
                "department":  "Women's",
                "name":        "Long Stretch Quilted Puffer Coat ...",
                "raw":         {...all parsed columns...},
                "colors":      {"Black", "Truffle"},
                "sizes":       {"Small", "Medium", "Large", "X-Large"},
                "upcs":        ["199...", ...],
                "skus":        ["F26-...", ...]
              },
              ...
          },
          "errors": [...]
        }
    """
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse pre-upload files")

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    # Find the UPC sheet — name varies; look for "Upload Template UPC" or similar
    target_ws = None
    for sn in wb.sheetnames:
        if "upload" in sn.lower() and "upc" in sn.lower():
            target_ws = wb[sn]
            break
    if target_ws is None:
        # Fallback: first sheet
        target_ws = wb[wb.sheetnames[0]]

    header = [target_ws.cell(row=1, column=c).value for c in range(1, target_ws.max_column + 1)]
    cmap = _build_column_map(header)

    if "style" not in cmap:
        return {"brand": None, "styles": {}, "errors": ["No 'STYLE#' column found in sheet"]}

    styles: Dict[str, Dict[str, Any]] = {}
    inferred_brand = brand_hint
    errors: List[str] = []

    for r in range(2, target_ws.max_row + 1):
        sn_raw = target_ws.cell(row=r, column=cmap["style"]).value
        if not sn_raw:
            continue
        sn = str(sn_raw).strip()
        if not sn:
            continue

        # Extract every known field for this row
        row_data: Dict[str, Any] = {}
        for k, col in cmap.items():
            v = target_ws.cell(row=r, column=col).value
            if v is not None and v != "":
                row_data[k] = v

        # Infer brand from TLGDIV NAME the first time we see a non-empty one
        if not inferred_brand:
            tlgdiv = row_data.get("tlgdiv") or ""
            if isinstance(tlgdiv, str):
                upper = tlgdiv.upper()
                if "SAGE" in upper:
                    inferred_brand = "Sage Collective"
                elif "VOLCOM" in upper:
                    inferred_brand = "Volcom"
                elif "SPYDER" in upper:
                    inferred_brand = "Spyder"
                elif "STELLA" in upper:
                    inferred_brand = "Stella Parker"

        if sn not in styles:
            styles[sn] = {
                "style":      sn,
                "sub_class":  row_data.get("sub_class"),
                "sub_sub":    row_data.get("sub_sub"),
                "department": row_data.get("department"),
                "name":       row_data.get("name"),
                "tlgdiv":     row_data.get("tlgdiv"),
                "model":      row_data.get("model"),
                "coo":        row_data.get("coo"),
                "care":       row_data.get("care"),
                "fabric":     row_data.get("fabric"),
                "closure":    row_data.get("closure"),
                "length":     row_data.get("length"),
                "list_price": row_data.get("list_price"),
                "amazon_cost":row_data.get("amazon_cost"),
                "addl":       row_data.get("addl"),
                "keywords":   row_data.get("keywords"),
                "due_date":   row_data.get("due_date"),
                "type_jacket":row_data.get("type_jacket"),
                "raw":        dict(row_data),
                "colors":     set(),
                "sizes":      set(),
                "upcs":       [],
                "skus":       [],
            }
        if "color" in row_data:
            styles[sn]["colors"].add(row_data["color"])
        if "size" in row_data:
            styles[sn]["sizes"].add(row_data["size"])
        if "upc" in row_data:
            styles[sn]["upcs"].append(str(row_data["upc"]))
        if "sku" in row_data:
            styles[sn]["skus"].append(str(row_data["sku"]))

    # Convert sets to sorted lists for JSON-friendliness
    for s in styles.values():
        s["colors"] = sorted(s["colors"])
        s["sizes"]  = sorted(s["sizes"])

    return {
        "brand":  inferred_brand,
        "styles": styles,
        "errors": errors,
    }


def style_to_form_state(style: Dict[str, Any], brand: str) -> Dict[str, Any]:
    """Convert one parsed pre-upload style row into an NIS form_state dict."""
    s = style
    dept_raw = (s.get("department") or "").strip()
    dept = "womens" if "women" in dept_raw.lower() else (
           "mens"   if "men"   in dept_raw.lower() else "womens")
    gender = "Female" if dept == "womens" else "Male"

    primary_color = s["colors"][0] if s.get("colors") else ""
    color_value = primary_color.title()
    color_map = _COLOR_MAP.get((primary_color or "").upper(), color_value)

    # Pick a sensible variant size
    sizes = s.get("sizes") or []
    primary_size = "Medium" if "Medium" in sizes else (
                   "M"      if "M"      in sizes else (sizes[0] if sizes else "Medium"))

    sub_class = s.get("sub_class") or ""
    cat, subcat, itk, itn = _SUBCLASS_TAXONOMY.get(
        sub_class,
        ("Women's Outerwear", "Wool", "wool-outerwear-coats", "Coat")
    )
    if dept == "mens":
        cat = cat.replace("Women's", "Men's")

    vendor_code = _BRAND_VENDOR_CODES.get(brand, "")

    state = {
        "rtip_vendor_code#1.value":      vendor_code,
        "vendor_sku#1.value":            str(s.get("model") or s.get("style") or ""),
        "product_type#1.value":          "COAT",
        "parentage_level#1.value":       "Parent",
        "item_name#1.value":             (
            f"{brand} {gender_word(dept)} {s.get('name') or ''}"
        ).strip(),
        "brand#1.value":                 brand,
        "external_product_id#1.type":    "UPC",
        "product_category#1.value":      cat,
        "product_subcategory#1.value":   subcat,
        "item_type_keyword#1.value":     itk,
        "item_type_name#1.value":        itn,
        "model_number#1.value":          str(s.get("model") or ""),
        "model_name#1.value":            s.get("name") or "",
        "style#1.value":                 s.get("name") or "",
        "department#1.value":            dept,
        "target_gender#1.value":         gender,
        "country_of_origin#1.value":     str(s.get("coo") or "").title(),
        "color#1.value":                 color_value,
        "color#1.standardized_values#1": color_map,
        "care_instructions#1.value":     s.get("care") or "",
        "material#1.value":              s.get("fabric") or "",
        "fabric_type#1.value":           s.get("fabric") or "",
        "closure#1.type#1.value":        s.get("closure") or "",
        "fit_type#1.value":              "Regular",
        "apparel_size#1.size":           primary_size,
        "bullet_point#1.value":          ((s.get("addl") or "")[:200]).strip(),
        "rtip_product_description#1.value":
            ((s.get("addl") or "") + " " + (s.get("keywords") or "")).strip(),
        "list_price#1.value":            str(s.get("list_price") or ""),
        "item_length_description#1.value":
            f"{s.get('length')}-inch" if s.get("length") else "",
    }
    # Clean blanks
    return {k: v for k, v in state.items() if v not in (None, "", " ")}


def gender_word(dept: str) -> str:
    return "Women's" if dept == "womens" else "Men's"
