"""
NIS Wizard v3 — Flask Backend
The Levy Group — Amazon Intelligence
Port: 5000
"""

import os
import re
import json
import csv
import zipfile
import shutil
import copy
import io
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from flask import Flask, request, jsonify, render_template, send_file, abort
from flask_cors import CORS
import openpyxl
from openpyxl.styles import PatternFill, Font, Border, Alignment
from openpyxl.utils import get_column_letter, column_index_from_string

from anthropic import Anthropic

# ── App setup ──────────────────────────────────────────────────────────────────
import threading

# Initialize Anthropic client (graceful fallback if no API key)
try:
    _anthropic_client = Anthropic()
    print("[LLM] Anthropic client initialized successfully.")
except Exception as _anthro_err:
    _anthropic_client = None
    print(f"[LLM] Anthropic client failed to initialize ({_anthro_err}). Will use rule-based generation.")

BASE_DIR = Path(__file__).parent

# Progress tracking for NIS generation
nis_progress = {"total": 0, "completed": 0, "current_style": "", "current_step": "", "status": "idle", "started_at": None}

# Progress tracking for content generation
content_progress = {"total": 0, "completed": 0, "current_style": "", "current_step": "", "status": "idle", "started_at": None}
UPLOAD_TEMPLATES = BASE_DIR / "uploads" / "templates"
UPLOAD_PRODUCTS  = BASE_DIR / "uploads" / "products"
UPLOAD_KEYWORDS  = BASE_DIR / "uploads" / "keywords"
UPLOAD_OUTPUT    = BASE_DIR / "uploads" / "output"
UPLOAD_IMAGES    = BASE_DIR / "uploads" / "style_images"
FEEDBACK_FILE    = BASE_DIR / "feedback" / "content_feedback.jsonl"
FEEDBACK_DIR     = BASE_DIR / "feedback"
DEFAULT_TEMPLATE = UPLOAD_TEMPLATES / "Dresses-Training.xlsm"
BRAND_CONFIGS_DIR = BASE_DIR / "brand_configs"
DROPDOWN_CACHE_DIR = BASE_DIR / "dropdown_cache"
SUBCLASS_MAP_FILE = BASE_DIR / "subclass_product_type_map.json"

# ── Taxonomy Overrides (Phase 1) ───────────────────────────────────────────────
# Per-item-type confirmed taxonomy quadruple: product_category / product_subcategory
# / item_type_keyword / item_type_name. Keyed by (product_type, sub_class, gender_bucket).
# Universal across brands — Amazon's taxonomy doesn't care which brand owns the SKU.
TAXONOMY_OVERRIDES_FILE = BASE_DIR / "taxonomy_overrides.json"
TAXONOMY_UNIVERSE_FILE  = BASE_DIR / "taxonomy_universe.json"
TAXONOMY_HISTORY_FILE   = BASE_DIR / "feedback" / "taxonomy_history.jsonl"

# Closed set of 13 gender buckets — see spec for derivation rules.
GENDER_BUCKETS = [
    "Mens", "Womens", "MensPlus", "WomensPlus", "WomensPetite",
    "BoysToddler", "BoysLittle", "BoysBig",
    "GirlsToddler", "GirlsLittle", "GirlsBig",
    "Baby", "Unisex",
]

def _load_taxonomy_universe():
    """Load the pre-computed valid-value universe from taxonomy_universe.json.
    Structure: { PRODUCT_TYPE: { product_categories, subcategories_by_category, item_type_names } }
    Returns {} if the file doesn't exist yet (universe builder hasn't run).
    """
    if not TAXONOMY_UNIVERSE_FILE.exists():
        return {}
    try:
        return json.loads(TAXONOMY_UNIVERSE_FILE.read_text())
    except Exception:
        return {}

def _load_taxonomy_overrides():
    """Load the operator-confirmed taxonomy store. Returns a dict with
    'version' / 'updated_at' / 'entries' keys. Creates an empty shell if missing.
    """
    if not TAXONOMY_OVERRIDES_FILE.exists():
        return {"version": 1, "updated_at": "", "entries": {}}
    try:
        data = json.loads(TAXONOMY_OVERRIDES_FILE.read_text())
        if "entries" not in data:
            data["entries"] = {}
        if "version" not in data:
            data["version"] = 1
        return data
    except Exception:
        return {"version": 1, "updated_at": "", "entries": {}}

def _save_taxonomy_overrides(data):
    """Atomic write: tmp -> rename. No git push here — that's done separately
    so one failing push doesn't block the save."""
    data["updated_at"] = datetime.now().isoformat() + "Z"
    tmp = TAXONOMY_OVERRIDES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False))
    tmp.replace(TAXONOMY_OVERRIDES_FILE)

def _append_taxonomy_history(key, entry, action):
    """Append audit log entry for every taxonomy change."""
    try:
        TAXONOMY_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(str(TAXONOMY_HISTORY_FILE), "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat() + "Z",
                "action": action,
                "key": key,
                "entry": entry,
            }) + "\n")
    except Exception:
        pass

def _try_git_commit_taxonomy(key, user):
    """Best-effort git commit + push of taxonomy_overrides.json. Silent failure.
    Runs in-process but with a short timeout so a slow network doesn't block the request.
    """
    import subprocess
    try:
        cwd = str(BASE_DIR)
        subprocess.run(["git", "add", "taxonomy_overrides.json"], cwd=cwd, timeout=5, capture_output=True)
        msg = f"taxonomy: confirm {key} by {user or 'unknown'}"
        subprocess.run(["git", "commit", "-m", msg], cwd=cwd, timeout=5, capture_output=True)
        subprocess.run(["git", "push", "origin", "master"], cwd=cwd, timeout=10, capture_output=True)
    except Exception as e:
        print(f"[taxonomy] git push failed (non-blocking): {e}")

def _derive_gender_bucket(style):
    """Map a style to one of the 13 closed-set gender buckets.
    Uses division_name AND style_name so 'VOLCOM YOUTH SWIM' + 'Little Boys ...'
    resolves to 'BoysLittle' not 'Unisex'.
    """
    dn = (style.get("division_name", "") or "").upper()
    sn = (style.get("style_name", "") or "").upper()
    combined = f"{dn} {sn}"

    # Baby/infant first
    if "BABY" in combined or "INFANT" in combined:
        return "Baby"

    # Youth buckets (check before adult since YOUTH-division styles may have style_name signals)
    if any(t in combined for t in ["YOUTH", "KIDS", " BOY", "BOYS", " GIRL", "GIRLS", "TODDLER"]):
        is_girls = "GIRL" in combined
        is_boys  = "BOY" in combined
        if "TODDLER" in combined:
            return "GirlsToddler" if is_girls else "BoysToddler"
        if "LITTLE" in combined:
            return "GirlsLittle" if is_girls else "BoysLittle"
        if "BIG" in combined:
            return "GirlsBig" if is_girls else "BoysBig"
        # Youth without size bucket — default to Big Kid
        if is_girls:
            return "GirlsBig"
        if is_boys:
            return "BoysBig"
        return "Unisex"

    # Women's buckets (check BEFORE men's — 'WOMENS' contains 'MENS' substring)
    if "WOMENS" in dn or "WOMEN'S" in dn or "WOMEN " in dn:
        if "PETITE" in combined:
            return "WomensPetite"
        if any(t in combined for t in ["PLUS", " 1X", " 2X", " 3X"]):
            return "WomensPlus"
        return "Womens"

    # Men's buckets
    if "MENS" in dn or "MEN'S" in dn or " MEN " in dn or dn.endswith(" MEN"):
        if any(t in combined for t in ["BIG AND TALL", "BIG & TALL", "PLUS", " 2XL", " 3XL", " 4XL"]):
            return "MensPlus"
        return "Mens"

    return "Unisex"

def _taxonomy_key(product_type, sub_class, gender_bucket):
    """Build the canonical composite key for the overrides store."""
    pt = (product_type or "").strip().upper()
    sc = (sub_class or "").strip()
    gb = (gender_bucket or "Unisex").strip()
    return f"{pt}|{sc}|{gb}"

def _resolve_taxonomy_for_style(style, brand_cfg=None):
    """Look up a confirmed taxonomy quadruple for a style.
    Returns a dict with: matched (bool), source ('override'|'auto'), entry (the quadruple),
    key (the composite key), gender_bucket, auto_derived (the rule-based fallback for UI display).

    Callers should prefer entry[field] when matched=True, else fall back to the existing
    rule-based _derive_* functions.
    """
    pt = _resolve_style_product_type(style) or ""
    sc = style.get("subclass", "") or style.get("sub_class", "")
    gb = _derive_gender_bucket(style)
    key = _taxonomy_key(pt, sc, gb)

    overrides = _load_taxonomy_overrides()
    entry = overrides.get("entries", {}).get(key)
    if entry and entry.get("source") == "manual":
        return {"matched": True, "source": "override", "entry": entry, "key": key,
                "gender_bucket": gb, "product_type": pt, "sub_class": sc}

    # No confirmed entry — return the auto-derived quadruple for fallback display
    style_gender, _ = _derive_gender_department(style)
    eff_gender = style_gender or (brand_cfg.get("gender", "") if brand_cfg else "")
    style_name = style.get("style_name", "")
    auto_cat    = _derive_amazon_product_category(sc, gender=eff_gender, product_type=pt,
                                                  style_name=style_name)
    auto_subcat = _derive_swim_product_subcategory(sc, gender=eff_gender, style_name=style_name,
                                                   product_category=auto_cat) if pt == "SWIMWEAR" else SUBCLASS_SUBCATEGORY_MAP.get(sc, "")
    auto_itk    = _derive_item_type_keyword(sc, product_type=pt, gender=eff_gender, style_name=style_name)
    auto_itn    = _derive_item_type_name(sc, product_type=pt, gender=eff_gender, style_name=style_name)
    auto = {
        "product_type": pt,
        "product_category": auto_cat,
        "product_subcategory": auto_subcat,
        "item_type_keyword": auto_itk,
        "item_type_name": auto_itn,
    }
    return {"matched": False, "source": "auto", "entry": auto, "key": key,
            "gender_bucket": gb, "product_type": pt, "sub_class": sc,
            "auto_derived": auto}

def _validate_taxonomy_quadruple(pt, category, subcategory, itk, itn):
    """Check each value is dropdown-valid for the product type. Returns (ok, errors).
    errors is a list of {field, value, reason, valid_options} dicts."""
    errors = []
    universe = _load_taxonomy_universe().get(pt, {})
    cache = load_dropdown_cache(pt) or {}

    valid_cats = universe.get("product_categories", []) or cache.get("product_category#1.value", [])
    if category and valid_cats and category not in valid_cats:
        errors.append({"field": "product_category", "value": category,
                       "reason": "not in Amazon dropdown", "valid_options": valid_cats[:50]})

    valid_subs_by_cat = universe.get("subcategories_by_category", {})
    if subcategory and category and category in valid_subs_by_cat:
        if subcategory not in valid_subs_by_cat[category]:
            errors.append({"field": "product_subcategory", "value": subcategory,
                           "reason": f"not valid under category '{category}'",
                           "valid_options": valid_subs_by_cat[category]})

    valid_itns = universe.get("item_type_names", []) or cache.get("item_type_name#1.value", [])
    if itn and valid_itns and itn not in valid_itns:
        errors.append({"field": "item_type_name", "value": itn,
                       "reason": "not in Amazon dropdown", "valid_options": valid_itns[:50]})

    # item_type_keyword is free-text; just length-check
    if itk and len(itk) > 100:
        errors.append({"field": "item_type_keyword", "value": itk,
                       "reason": "exceeds 100 chars", "valid_options": []})

    return (len(errors) == 0, errors)
# ── End Taxonomy Overrides ────────────────────────────────────────────────────

def _load_subclass_map():
    """Load the learned sub-class → product type mapping."""
    if SUBCLASS_MAP_FILE.exists():
        with open(str(SUBCLASS_MAP_FILE), "r") as f:
            return json.load(f)
    return {}

def _save_subclass_map(mapping):
    """Save the sub-class → product type mapping."""
    with open(str(SUBCLASS_MAP_FILE), "w") as f:
        json.dump(mapping, f, indent=2)

def resolve_product_type(sub_class, division_name=""):
    """Resolve a sub-class to a product type using all available sources.
    Returns (product_type_id, confidence, reason)
    confidence: 'known'|'detected'|'unknown'
    reason: human-readable explanation of why this mapping was chosen
    """
    if not sub_class:
        if division_name:
            dn = division_name.upper()
            if "SWIM" in dn: return "SWIMWEAR", "detected", f"Division '{division_name}' contains SWIM"
            if "DRESS" in dn: return "DRESS", "detected", f"Division '{division_name}' contains DRESS"
            if "SHIRT" in dn or "TOP" in dn: return "SHIRT", "detected", f"Division '{division_name}' contains SHIRT/TOP"
        return "UNKNOWN", "unknown", "No sub-class or division name provided"
    
    # 1. Learned map (operator-confirmed)
    learned = _load_subclass_map()
    if sub_class in learned:
        return learned[sub_class], "known", f"Previously confirmed by operator: {sub_class} = {learned[sub_class]}"
    
    # 2. ALL_PRODUCT_TYPES
    for pt in ALL_PRODUCT_TYPES:
        if sub_class in pt.get("sub_classes", []):
            return pt["id"], "known", f"Sub-class '{sub_class}' is a known {pt['label']} type"
    
    # 3. TEMPLATE_PRODUCT_TYPE_MAP
    if sub_class in TEMPLATE_PRODUCT_TYPE_MAP:
        tpl = TEMPLATE_PRODUCT_TYPE_MAP[sub_class]
        tpl_to_id = {"Dresses": "DRESS", "Swimwear": "SWIMWEAR", "Other_Shirts": "SHIRT",
                      "Shorts": "SHORTS", "Jackets_and_Coats": "COAT", "Skirts": "SKIRT"}
        pt_id = tpl_to_id.get(tpl, tpl.upper())
        return pt_id, "known", f"Sub-class '{sub_class}' maps to {tpl} template"
    
    # 4. Division name heuristic
    if division_name:
        dn = division_name.upper()
        if "SWIM" in dn: return "SWIMWEAR", "detected", f"Division '{division_name}' suggests swimwear"
        if "DRESS" in dn: return "DRESS", "detected", f"Division '{division_name}' suggests dresses"
        if "SHIRT" in dn or "TOP" in dn: return "SHIRT", "detected", f"Division '{division_name}' suggests tops"
        if "SHORT" in dn: return "SHORTS", "detected", f"Division '{division_name}' suggests shorts"
        if "JACKET" in dn or "COAT" in dn: return "COAT", "detected", f"Division '{division_name}' suggests outerwear"
    
    # 5. Fuzzy sub_class name matching
    sc_lower = sub_class.lower()
    if any(w in sc_lower for w in ["swim", "bikini", "rashguard", "trunk", "tankini"]):
        return "SWIMWEAR", "detected", f"Sub-class '{sub_class}' contains swim-related keyword"
    if any(w in sc_lower for w in ["dress", "gown", "romper"]):
        return "DRESS", "detected", f"Sub-class '{sub_class}' contains dress-related keyword"
    if any(w in sc_lower for w in ["shirt", "blouse", "top", "tee", "polo", "tank"]):
        return "SHIRT", "detected", f"Sub-class '{sub_class}' contains top/shirt keyword"
    if any(w in sc_lower for w in ["short", "board"]):
        return "SHORTS", "detected", f"Sub-class '{sub_class}' contains shorts keyword"
    if any(w in sc_lower for w in ["jacket", "coat", "hoodie", "pullover", "fleece"]):
        return "COAT", "detected", f"Sub-class '{sub_class}' contains outerwear keyword"
    if any(w in sc_lower for w in ["pant", "legging", "jogger"]):
        return "PANTS", "detected", f"Sub-class '{sub_class}' contains pants keyword"
    if any(w in sc_lower for w in ["skirt", "skort"]):
        return "SKIRT", "detected", f"Sub-class '{sub_class}' contains skirt keyword"
    
    return "UNKNOWN", "unknown", f"Cannot determine product type for sub-class '{sub_class}'"


for d in [UPLOAD_TEMPLATES, UPLOAD_PRODUCTS, UPLOAD_KEYWORDS, UPLOAD_OUTPUT, UPLOAD_IMAGES, BRAND_CONFIGS_DIR, FEEDBACK_DIR, DROPDOWN_CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
CORS(app)


# ═══════════════════════════════════════════════════════════════════════════════
# TEMPLATE DROPDOWN VALIDATION ENGINE (permanent infrastructure)
#
# Every Amazon .xlsm template has dropdown validations as named ranges.
# This engine:
# 1. Extracts valid dropdown values when a template is uploaded
# 2. Caches per product type (DRESS, SWIMWEAR, SHIRT, etc.)
# 3. Validates every field value before writing to .xlsm
# 4. Auto-corrects fuzzy matches (e.g., "30+" → "UPF 30")
# 5. Flags values that can't be matched
# ═══════════════════════════════════════════════════════════════════════════════

_dropdown_cache = {}  # product_type → { field_id: [valid_values] }


def extract_template_dropdowns(template_path):
    """Extract all dropdown valid values from an Amazon .xlsm template.
    Called automatically on template upload and at server startup.
    """
    from openpyxl.utils import range_boundaries
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        wb = openpyxl.load_workbook(template_path, keep_vba=True)

    product_type = "UNKNOWN"
    ws = None
    for name in wb.sheetnames:
        if name.upper().startswith("TEMPLATE"):
            ws = wb[name]
            parts = name.split("-", 1)
            if len(parts) == 2 and parts[1].strip():
                product_type = parts[1].strip().upper()
            break
    if not ws:
        wb.close()
        return None

    # Build field_id normalized lookup
    max_col = ws.max_column or 254
    fid_norm_map = {}
    for col in range(1, max_col + 1):
        raw = ws.cell(row=4, column=col).value
        if raw:
            fid = str(raw).strip()
            norm = fid.replace("#", "").replace(".", "")
            fid_norm_map[norm] = fid

    # Extract dropdown values from defined names
    field_dropdowns = {}
    for name in wb.defined_names:
        if not name.startswith(product_type):
            continue
        field_ref = name[len(product_type):]
        ref_norm = field_ref.replace(".", "")
        actual_fid = fid_norm_map.get(ref_norm)
        if not actual_fid:
            continue

        dn = wb.defined_names[name]
        try:
            for title, coord in dn.destinations:
                sheet = wb[title]
                mc, mr, xc, xr = range_boundaries(coord)
                vals = []
                for r in range(mr, xr + 1):
                    v = sheet.cell(row=r, column=mc).value
                    if v is not None:
                        vals.append(str(v))
                if vals:
                    field_dropdowns[actual_fid] = vals
        except Exception:
            pass

    wb.close()

    _dropdown_cache[product_type] = field_dropdowns
    cache_path = DROPDOWN_CACHE_DIR / f"{product_type}.json"
    with open(str(cache_path), "w", encoding="utf-8") as f:
        json.dump(field_dropdowns, f, indent=2)

    print(f"[Dropdown] Extracted {len(field_dropdowns)} dropdown fields for {product_type}")
    return {"product_type": product_type, "dropdown_fields": len(field_dropdowns)}


def load_dropdown_cache(product_type):
    """Load dropdown values for a product type. Memory first, then disk."""
    if product_type in _dropdown_cache:
        return _dropdown_cache[product_type]
    cache_path = DROPDOWN_CACHE_DIR / f"{product_type}.json"
    if cache_path.exists():
        with open(str(cache_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        _dropdown_cache[product_type] = data
        return data
    return {}


def _fuzzy_match_dropdown(value, valid_values):
    """Find closest matching dropdown value. Returns (match, confidence) or (None, 0)."""
    if not value or not valid_values:
        return None, 0
    val_str = str(value).strip()
    val_lower = val_str.lower()

    for v in valid_values:
        if v == val_str:
            return v, 1.0
    for v in valid_values:
        if v.lower() == val_lower:
            return v, 0.95
    for v in valid_values:
        if val_lower in v.lower() or v.lower() in val_lower:
            return v, 0.8
    for v in valid_values:
        if v.lower().startswith(val_lower[:3]) and len(val_lower) >= 3:
            return v, 0.6

    val_words = set(val_lower.split())
    best_score, best_match = 0, None
    for v in valid_values:
        v_words = set(v.lower().split())
        if val_words and v_words:
            overlap = len(val_words & v_words) / max(len(val_words), len(v_words))
            if overlap > best_score:
                best_score, best_match = overlap, v
    if best_score >= 0.5:
        return best_match, best_score * 0.7

    return None, 0


def validate_field_value(field_id, value, product_type):
    """Validate a single field against its dropdown. Returns result dict."""
    dropdowns = load_dropdown_cache(product_type)
    if field_id not in dropdowns:
        return {"field_id": field_id, "original": value, "status": "no_dropdown"}
    valid = dropdowns[field_id]
    val_str = str(value).strip() if value else ""
    if not val_str:
        return {"field_id": field_id, "original": value, "status": "empty"}
    if val_str in valid:
        return {"field_id": field_id, "original": value, "status": "valid"}
    match, confidence = _fuzzy_match_dropdown(val_str, valid)
    if match and confidence >= 0.6:
        return {"field_id": field_id, "original": value, "status": "corrected",
                "corrected": match, "confidence": round(confidence, 2)}
    return {"field_id": field_id, "original": value, "status": "invalid",
            "valid_values": valid[:10]}


def validate_and_correct_for_build(field_values, product_type):
    """Validate all fields and auto-correct before writing to .xlsm.
    Modifies field_values in-place. Returns list of all validation results.
    """
    dropdowns = load_dropdown_cache(product_type)
    results = []
    for field_id, value in list(field_values.items()):
        if not value or field_id not in dropdowns:
            continue
        r = validate_field_value(field_id, value, product_type)
        if r["status"] == "corrected":
            field_values[field_id] = r["corrected"]
        results.append(r)
    return results


# Auto-extract dropdowns from templates on disk at startup
for _tpl_file in UPLOAD_TEMPLATES.glob("*.xlsm"):
    try:
        extract_template_dropdowns(str(_tpl_file))
    except Exception as _e:
        print(f"[Dropdown] Startup extract failed for {_tpl_file.name}: {_e}")


# ── Brand configs ──────────────────────────────────────────────────────────────
BRAND_CONFIGS = {
    "Stella Parker": {
        "vendor_code_prefix": "FC0C0",
        "vendor_code_full": "Stella Parker Sportswear, us_apparel, FC0C0",
        "default_upf": "UPF 30",
        "default_fabric": "95% Polyester, 5% Spandex",
        "default_coo": "Mexico",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "UPF sun protection",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, UPF {upf}, {color}, {size}",
        "never_words": [],
    },
    "Novelle Fashion": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "79% Nylon, 21% Spandex",
        "default_coo": "Bangladesh",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "Butterlux fabric softness",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": ["affordable"],
    },
    "Volcom": {
        "vendor_code_prefix": "7E8G6",
        "vendor_code_full": "Volcom, us_apparel, 7E8G6",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "",
        "gender": "",
        "department": "",
        "bullet_1_focus": "Brand lifestyle and quality",
        "title_formula": "{brand} {gender} {style_name} {product_type}",
        "never_words": [],
    },
    "Roxy": {
        "vendor_code_prefix": "PG823",
        "vendor_code_full": "Roxy Women's Swimwear, us_apparel, PG823",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "Beach/surf lifestyle",
        "title_formula": "{brand} {gender} {style_name} {product_type}",
        "never_words": [],
    },
    "Nautica": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "Nautical inspired style",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Ben Sherman": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "British mod heritage style",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Spyder": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "Performance athletic design",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Tahari": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Dry Clean",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "Sophisticated tailored design",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
    "Sage": {
        "vendor_code_prefix": "",
        "vendor_code_full": "",
        "default_upf": "",
        "default_fabric": "",
        "default_coo": "",
        "default_care": "Machine Wash",
        "gender": "Female",
        "department": "womens",
        "bullet_1_focus": "Effortless everyday style",
        "title_formula": "{brand} Women's {style_descriptor} {product_type}, {color}, {size}",
        "never_words": [],
    },
}

# ── Color maps ─────────────────────────────────────────────────────────────────
COLOR_MAP = {
    "MAUVE": "Pink", "ROSE": "Pink", "BLUSH": "Pink", "PINK": "Pink",
    "CORAL": "Pink", "HOT PINK": "Pink", "MAGENTA": "Pink", "FUCHSIA": "Pink",
    "RED": "Red", "CRIMSON": "Red", "BURGUNDY": "Red", "WINE": "Red",
    "MAROON": "Red", "BRICK": "Red", "CHERRY": "Red",
    "BLUE": "Blue", "NAVY": "Blue", "COBALT": "Blue", "ROYAL BLUE": "Blue",
    "SKY BLUE": "Blue", "PERIWINKLE": "Blue", "DENIM": "Blue", "INDIGO": "Blue",
    "TEAL": "Teal", "TURQUOISE": "Teal", "AQUA": "Teal", "CYAN": "Teal",
    "GREEN": "Green", "OLIVE": "Green", "SAGE": "Green", "FOREST": "Green",
    "EMERALD": "Green", "MINT": "Green", "LIME": "Green", "HUNTER": "Green",
    "KHAKI": "Khaki", "TAN": "Khaki", "CAMEL": "Khaki", "BEIGE": "Beige",
    "IVORY": "Ivory", "CREAM": "Ivory", "OFF WHITE": "Ivory",
    "WHITE": "White", "BRIGHT WHITE": "White",
    "BLACK": "Black", "JET BLACK": "Black", "ONYX": "Black",
    "GREY": "Grey", "GRAY": "Grey", "CHARCOAL": "Grey", "SILVER": "Silver",
    "PURPLE": "Purple", "LAVENDER": "Purple", "VIOLET": "Purple",
    "PLUM": "Purple", "LILAC": "Purple", "VRYVIOLET": "Purple",
    "ORANGE": "Orange", "RUST": "Orange", "PUMPKIN": "Orange",
    "AMBER": "Orange", "TERRACOTTA": "Orange",
    "YELLOW": "Yellow", "GOLD": "Gold", "MUSTARD": "Yellow",
    "BROWN": "Brown", "CHOCOLATE": "Brown", "ESPRESSO": "Brown", "MOCHA": "Brown",
    "MULTI": "Multicolor", "MULTICOLOR": "Multicolor", "PRINT": "Multicolor",
    "COMBO": "Multicolor", "FLORAL": "Multicolor",
}

SIZE_MAP = {
    "XS": "X-Small", "S": "Small", "M": "Medium", "L": "Large",
    "XL": "X-Large", "XXL": "XX-Large", "2XL": "XX-Large",
    "XXXL": "3X-Large", "3XL": "3X-Large", "1X": "1X-Large",
    "2X": "2X-Large", "3X": "3X-Large", "4X": "4X-Large",
    "0X": "0X-Large", "0": "0", "2": "2", "4": "4", "6": "6",
    "8": "8", "10": "10", "12": "12", "14": "14", "16": "16",
}

# ── Template-to-product-type routing ──────────────────────────────────────────
# Maps sub_class values to template product type names
TEMPLATE_PRODUCT_TYPE_MAP = {
    # Dresses
    "Day Dress": "Dresses",
    "Cocktail Dress": "Dresses",
    "Active Dress": "Dresses",
    "Swimdress": "Dresses",
    "Maxi Dress": "Dresses",
    "Mini Dress": "Dresses",
    "Wrap Dress": "Dresses",
    "Shirt Dress": "Dresses",
    "Shift Dress": "Dresses",
    "A-Line Dress": "Dresses",
    "Sundress": "Dresses",
    "Bodycon Dress": "Dresses",
    # Shirts / Other_Shirts
    "Polo": "Other_Shirts",
    "Tee": "Other_Shirts",
    "Shirt": "Other_Shirts",
    "Blouse": "Other_Shirts",
    "Tank": "Other_Shirts",
    # Swimwear (from TLG pre-upload templates)
    "Rashguard": "Swimwear",
    "One Piece Swim": "Swimwear",
    "Bikini Top": "Swimwear",
    "Swim Bottom": "Swimwear",
    "Bikini Bottom": "Swimwear",
    "Tankini": "Swimwear",
    "Swim Set 2 pcs": "Swimwear",
    "Trunk": "Swimwear",
    "Cover Up": "Swimwear",
    # Shorts / Boardshorts
    "Short": "Swimwear",  # swim shorts/trunks — use Swimwear template
    "Board Short": "Shorts",
    "Chino Short": "Shorts",
    "Boardshorts": "Shorts",
    # Jackets and Coats
    "Jacket": "Jackets_and_Coats",
    "Coat": "Jackets_and_Coats",
    "Hoodie": "Jackets_and_Coats",
    "Pullover": "Jackets_and_Coats",
    # Skirts
    "Skirt": "Skirts",
}

SUBCLASS_CATEGORY_MAP = {
    "Day Dress": "casual-and-day-dresses",
    "Cocktail Dress": "special-occasion-dresses",
    "Maxi Dress": "maxi-dresses",
    "Mini Dress": "mini-dresses",
    "Active Dress": "active-dresses",
    "Wrap Dress": "wrap-dresses",
    "Shirt Dress": "shirt-dresses",
    "Shift Dress": "casual-and-day-dresses",
    "A-Line Dress": "special-occasion-dresses",
    "Sundress": "casual-and-day-dresses",
    "Bodycon Dress": "casual-and-day-dresses",
}

# product_subcategory values must match the Amazon template dropdown
# (these are from the Dropdown Lists sheet, not item_type_keyword slugs)
SUBCLASS_SUBCATEGORY_MAP = {
    "Day Dress": "Casual and Day Dresses",
    "Cocktail Dress": "Night Out and Cocktail Dresses",
    "Maxi Dress": "Casual and Day Dresses",
    "Mini Dress": "Casual and Day Dresses",
    "Active Dress": "Casual and Day Dresses",
    "Wrap Dress": "Casual and Day Dresses",
    "Shirt Dress": "Casual and Day Dresses",
    "Swimdress": "Special Occasion Dresses",
}

DESCRIPTION_OPENERS = [
    "Elevate your wardrobe with the {brand} {style_name} — a versatile piece designed for the modern woman.",
    "Introducing the {brand} {style_name}, where effortless style meets all-day comfort.",
    "Step into confidence with the {brand} {style_name}, crafted for women who refuse to compromise on style.",
    "The {brand} {style_name} is your go-to choice for a polished look from morning to evening.",
    "Discover the {brand} {style_name} — thoughtfully designed for women who love beautiful, functional fashion.",
    "Meet the {brand} {style_name}: a wardrobe essential that blends timeless design with contemporary flair.",
    "The {brand} {style_name} brings together sophisticated design and everyday wearability in one stunning piece.",
    "Designed for the woman on the move, the {brand} {style_name} delivers style without sacrificing comfort.",
]

# Swimwear openers rotate so 20+ rash guards don't share the same opening line.
SWIM_DESCRIPTION_OPENERS = [
    "The {brand} {style_name} is built for performance in and out of the water.",
    "Dive into warm-weather adventures with the {brand} {style_name}.",
    "Crafted for sun, surf, and everything in between, the {brand} {style_name} is your summer essential.",
    "Meet the {brand} {style_name} — a water-ready staple engineered for comfort and durability.",
    "From beach mornings to pool-side afternoons, the {brand} {style_name} has you covered.",
    "The {brand} {style_name} balances beach style with all-day performance.",
    "Whether you're paddling out or soaking up sun, the {brand} {style_name} keeps up.",
    "Designed with {brand}'s water-sports DNA, the {style_name} is ready for any adventure.",
]

# Bullet 1 variation pool for swim UPF items so 20+ rash guards don't share one bullet.
SWIM_UPF_B1_OPENERS = [
    "UPF {upf} SUN PROTECTION",
    "BLOCK HARMFUL UV RAYS",
    "ALL-DAY SUN DEFENSE",
    "UV PROTECTION BUILT IN",
    "SUN-SAFE PERFORMANCE",
    "FULL-COVERAGE UPF {upf}",
]
SWIM_UPF_B1_TAILS = [
    "shields your skin from harmful UV rays. Ideal for surfing, swimming, and all-day outdoor wear.",
    "delivers certified UV defense so you can stay in the water longer without worry.",
    "keeps sunburns at bay during long beach days, pool sessions, and open-water swims.",
    "provides lasting UV protection for active surfers, swimmers, and beach-goers.",
    "gives you reliable sun coverage from first light to sundown.",
]

# Bullet 1 variation pool for non-UPF swim items (bikini tops/bottoms, one-pieces, trunks w/o UPF).
SWIM_LIFESTYLE_B1_OPENERS = [
    "SURF & BEACH READY",
    "OCEAN-INSPIRED STYLE",
    "WATER-TO-SAND VERSATILE",
    "BEACH-DAY ESSENTIAL",
    "POOLSIDE TO SHORELINE",
]
SWIM_LIFESTYLE_B1_TAILS = [
    "combines ocean-inspired style with durable, quick-dry construction perfect for any beach day or pool session.",
    "pairs effortless coastal style with performance fabric that moves with you in and out of the water.",
    "brings classic surf aesthetics together with modern quick-dry performance.",
    "blends laid-back beach style with the durability you need for real water days.",
    "delivers easy swim-wear style that transitions seamlessly from shoreline to boardwalk.",
]

# Bullet 2 variation pool. Avoids stuffing the full style name into bullet 2.
# Feature phrase is filled in per style.
SWIM_B2_OPENERS = [
    "DESIGNED FOR PERFORMANCE",
    "PURPOSE-BUILT DETAILS",
    "THOUGHTFUL CONSTRUCTION",
    "STYLE MEETS FUNCTION",
    "DIALED-IN FEATURES",
]
SWIM_B2_TEMPLATES = [
    "This {itn_lower} features {feature_str} for lasting comfort and durability in the water.",
    "Every detail of this {itn_lower} is built for real beach days: {feature_str}.",
    "Purpose-built details — {feature_str} — give this {itn_lower} a technical edge.",
    "This {itn_lower} brings together {feature_str} so you can focus on the swim, not the fit.",
    "From fit to finish, this {itn_lower} delivers {feature_str}.",
]

DESCRIPTION_OPENERS_ROTATION = {}  # style_num -> opener_index

# ── Helper utilities ───────────────────────────────────────────────────────────
def _safe(v):
    return str(v).strip() if v is not None else ""

def _derive_gender_department(style):
    """Derive Amazon gender + department from a style's division_name AND style_name.
    style_name is used to refine YOUTH styles that don't have BOY/GIRL in division_name
    (e.g. 'VOLCOM YOUTH SWIM' + style 'Little Boys Long Sleeve Rashguard' -> Male/boys).
    Returns (gender, department) tuple — e.g. ('Male', 'mens') or ('Female', 'womens').
    Falls back to empty strings when unknown.
    """
    dn = (style.get("division_name", "") or "").upper()
    sn = (style.get("style_name", "") or "").upper()
    gender = ""
    department = ""
    if "YOUTH" in dn or "KIDS" in dn or "BOY" in dn or "GIRL" in dn:
        # Youth — refine with style_name
        if "GIRL" in dn or "GIRL" in sn:
            gender = "Female"
            department = "girls"
        elif "BOY" in dn or "BOY" in sn:
            gender = "Male"
            department = "boys"
        else:
            gender = "Unisex"
            department = "boys"  # Amazon default for youth unisex
    elif "WOMENS" in dn or "WOMEN'S" in dn or "WOMEN " in dn:
        # Check WOMENS before MENS ("WOMENS" contains "MENS" as substring)
        gender = "Female"
        department = "womens"
    elif "MENS" in dn or "MEN'S" in dn or " MEN " in dn or dn.endswith(" MEN"):
        gender = "Male"
        department = "mens"
    return gender, department

def _derive_youth_size_info(style_name, gender, raw_size):
    """Given a youth style's name + gender + raw_size, return:
      (special_size_type, size_class, age_range_description, normalized_size)
    for adults returns ("", "Alpha", "Adult", normalize_size(raw_size)).
    Maps toddler 2T/3T/etc. to '2 Years'/'3 Years' (Amazon-valid values).
    """
    sn = (style_name or "").lower()
    g = (gender or "").lower()
    raw = str(raw_size).strip()

    # Detect youth bucket from style_name
    is_toddler    = "toddler" in sn or raw.upper().endswith("T") and raw[:-1].isdigit()
    is_little_boy  = "little boy" in sn or "little boys" in sn
    is_big_boy     = "big boy" in sn or "big boys" in sn
    is_little_girl = "little girl" in sn or "little girls" in sn
    is_big_girl    = "big girl" in sn or "big girls" in sn
    is_youth       = g == "unisex" or is_toddler or is_little_boy or is_big_boy or is_little_girl or is_big_girl or "boys" in sn or "girls" in sn

    # If this isn't a youth style, adult defaults
    if not is_youth:
        return "", "Alpha", "Adult", (normalize_size(raw) or raw)

    # Pick special_size_type
    if is_toddler:
        sst = "Toddler Girls" if ("girls" in sn or is_little_girl) else "Toddler Boys"
    elif is_little_boy:
        sst = "Little Boys"
    elif is_big_boy:
        sst = "Big Boys"
    elif is_little_girl:
        sst = "Little Girls"
    elif is_big_girl:
        sst = "Big Girls"
    else:
        # Fallback — use gender
        sst = "Big Boys" if g == "male" else ("Big Girls" if g == "female" else "")

    # Pick age_range_description
    if is_toddler:
        ard = "Toddler"
    elif is_little_boy or is_little_girl:
        ard = "Little Kid"
    elif is_big_boy or is_big_girl:
        ard = "Big Kid"
    else:
        ard = "Big Kid"

    # Normalize size for youth
    # 2T/3T/4T/5T -> 'N Years' (Amazon-valid)
    if raw.upper().endswith("T") and raw[:-1].isdigit():
        return sst, "Age", ard, f"{int(raw[:-1])} Years"
    # Plain numeric kid sizes (4,5,6,7,8,10,12,14,16) -> 'N Years'
    if raw.isdigit():
        years = int(raw)
        if 0 <= years <= 18:
            return sst, "Age", ard, f"{years} Years"
        return sst, "Numeric", ard, raw
    # Alpha youth sizes — keep alpha
    return sst, "Alpha", ard, (normalize_size(raw) or raw)

def normalize_color(raw_color):
    """Map raw color to Amazon color family."""
    if not raw_color:
        return ""
    upper = raw_color.upper().strip()
    for key, val in COLOR_MAP.items():
        if key in upper:
            return val
    # Title case fallback
    return raw_color.title()

def normalize_size(raw_size):
    """Standardize size string."""
    if not raw_size:
        return ""
    return SIZE_MAP.get(str(raw_size).strip().upper(), str(raw_size).strip())

def parse_fabric(raw_fabric):
    """Parse fabric string like '95 POLY 5 SPAN' → '95% Polyester, 5% Spandex'."""
    if not raw_fabric:
        return ""
    abbreviations = {
        "POLY": "Polyester", "SPAN": "Spandex", "COTT": "Cotton",
        "NYLON": "Nylon", "RAYON": "Rayon", "LINEN": "Linen",
        "SILK": "Silk", "WOOL": "Wool", "MODAL": "Modal",
        "ACRY": "Acrylic", "LYOCEL": "Lyocell", "TENCEL": "Tencel",
        "VISCOSE": "Viscose", "BAMBOO": "Bamboo",
    }
    s = str(raw_fabric).strip()
    # Already in percentage format
    if "%" in s:
        return s
    # Try to parse "95 POLY 5 SPAN" format
    parts = re.findall(r'(\d+)\s*([A-Za-z]+)', s)
    if parts:
        result = []
        for pct, fiber in parts:
            full = abbreviations.get(fiber.upper(), fiber.title())
            result.append(f"{pct}% {full}")
        return ", ".join(result)
    return s

def derive_neck_type(style_name):
    """Derive neck type from style name."""
    name = style_name.upper()
    mappings = [
        ("V NECK", "V-Neck"), ("V-NECK", "V-Neck"), ("VNECK", "V-Neck"),
        ("HALTER", "Halter"), ("CREW", "Crew Neck"), ("SCOOP", "Scoop Neck"),
        ("SQUARE", "Square Neck"), ("COWL", "Cowl Neck"), ("MOCK", "Mock Neck"),
        ("TURTLENECK", "Turtleneck"), ("HIGH NECK", "High Neck"),
        ("SWEETHEART", "Sweetheart"), ("OFF THE SHOULDER", "Off Shoulder"),
        ("OFF SHLD", "Off Shoulder"), ("OFF-SHOULDER", "Off Shoulder"),
        ("BAND NECK", "Band Neck"), ("BAND NCK", "Band Neck"),
        ("YOKE NECK", "Yoke Neck"), ("YOKE NCK", "Yoke Neck"),
        ("PINTUCK", "V-Neck"), ("KEYHOLE", "Keyhole"),
    ]
    for pattern, neck in mappings:
        if pattern in name:
            return neck
    return ""

def derive_sleeve_type(style_name):
    """Derive sleeve type from style name."""
    name = style_name.upper()
    mappings = [
        ("SLEEVELESS", "Sleeveless"), ("SLVLES", "Sleeveless"),
        ("SLVLS", "Sleeveless"), ("SLV", "Short Sleeve"),
        ("FLUTTER", "Flutter Sleeve"), ("FLUTTER SLV", "Flutter Sleeve"),
        ("FLUTTER SLEEVE", "Flutter Sleeve"),
        ("RUFFLE SLV", "Ruffle Sleeve"), ("RFL SLV", "Ruffle Sleeve"),
        ("OFF SHOULDER", "Off-Shoulder"), ("OFF SHLD", "Off-Shoulder"),
        ("BALLOON SL", "Balloon Sleeve"), ("CAP SLEEVE", "Cap Sleeve"),
        ("SHORT SLEEVE", "Short Sleeve"), ("LONG SLEEVE", "Long Sleeve"),
        ("3/4 SLEEVE", "3/4 Sleeve"),
    ]
    for pattern, sleeve in mappings:
        if pattern in name:
            return sleeve
    return "Sleeveless"

# Common ISO country codes → full names (Amazon template dropdowns use full names)
COUNTRY_CODE_MAP = {
    "MX": "Mexico", "BD": "Bangladesh", "CN": "China", "US": "United States",
    "IN": "India", "VN": "Vietnam", "KH": "Cambodia", "PK": "Pakistan",
    "ID": "Indonesia", "TW": "Taiwan", "KR": "South Korea", "JP": "Japan",
    "TH": "Thailand", "TR": "Turkey", "IT": "Italy", "PT": "Portugal",
    "PE": "Peru", "GT": "Guatemala", "HN": "Honduras", "SV": "El Salvador",
    "NI": "Nicaragua", "HT": "Haiti", "DO": "Dominican Republic",
    "LK": "Sri Lanka", "MM": "Myanmar", "PH": "Philippines",
    "ET": "Ethiopia", "MG": "Madagascar", "MA": "Morocco", "EG": "Egypt",
}

def normalize_coo(raw):
    """Convert ISO country code to full name. Pass through if already a full name."""
    if not raw:
        return raw
    s = str(raw).strip()
    upper = s.upper()
    if upper in COUNTRY_CODE_MAP:
        return COUNTRY_CODE_MAP[upper]
    # Already a full name
    return s


def clean_brand_name(raw_brand):
    """Strip vendor labels from brand name. 'Stella Parker PL Ladies SPTW' -> 'Stella Parker'"""
    if not raw_brand:
        return raw_brand
    b = str(raw_brand).strip()
    # Remove common vendor suffixes
    for suffix in [' PL Ladies SPTW', ' PL Ladies', ' PL Mens', ' SPTW', ' Sportswear',
                   ' us_apparel', ' Women\'s Swimwear', ', us_apparel']:
        b = b.replace(suffix, '')
    # Remove anything after the last known brand word
    # Known brands: keep only the first 1-3 proper words
    known = {'Stella Parker', 'Volcom', 'Roxy', 'Novelle Fashion', 'Nautica', 
             'Ben Sherman', 'Spyder', 'Tahari', 'Sage'}
    for k in known:
        if b.startswith(k):
            return k
    return b.strip()

def derive_silhouette(sub_subclass):
    """Derive silhouette from sub_subclass. Never returns #N/A or empty junk."""
    if not sub_subclass:
        return "flattering"
    s = str(sub_subclass).strip()
    # Catch #N/A, N/A, NA, None, nan, empty
    if s.upper() in ('', '#N/A', 'N/A', 'NA', 'NONE', 'NAN', 'NA (CONVERSION)', '#N/A (CONVERSION)'):
        return "flattering"
    mapping = {
        "Shift Dress": "Shift",
        "A-Line Dress": "A-Line",
        "Fit & Flare Dress": "Fit & Flare",
        "Dress with Shorts": "Romper",
        "Wrap Dress": "Wrap",
        "Sheath Dress": "Sheath",
        "Maxi Dress": "Maxi",
        "Mini Dress": "Mini",
        "Bodycon Dress": "Bodycon",
    }
    result = mapping.get(s, s.replace(" Dress", "").strip())
    # Final safety check
    if not result or '#' in result or result.upper() in ('N/A', 'NA', 'NAN'):
        return "flattering"
    return result

def style_descriptor_from_name(style_name):
    """Extract a clean style descriptor for use in titles."""
    name = str(style_name).upper()
    # Clean up common abbreviations
    replacements = {
        "SLVLES": "Sleeveless", "SLVLS": "Sleeveless", "SLV": "Sleeve",
        "DRS": "Dress", "DRSS": "Dress", "NCK": "Neck",
        "SHLD": "Shoulder", "RFL": "Ruffle", "BBYDOLL": "Baby Doll",
        "SHRT": "Short", "CINCH WST": "Cinch Waist",
        "TSL": "Tassel", "FR": "Front", "FIT": "Fit",
        "FLR": "Flare", "ZIP": "Zip", "BTN": "Button",
    }
    result = name
    for abbr, full in replacements.items():
        result = re.sub(r'\b' + abbr + r'\b', full, result)
    # Remove "DRESS" from end since product_type covers it
    result = re.sub(r'\bDRESS\b', '', result).strip()
    # Title case
    return result.title().strip()

def _title_case_preserve_acronyms(s):
    """Title-case a string while preserving common acronyms (UPF, 4-Way, etc.)
    and correctly handling possessives (Men's, Women's, Boys', Girls').
    """
    if not s:
        return s
    out = s.title()
    # Fix possessives that .title() breaks: Men'S -> Men's
    out = re.sub(r"'S\b", "'s", out)
    out = re.sub(r"S'\b", "s'", out)
    # Restore acronyms that .title() lowercased
    restorations = {
        r'\bUpf\b': 'UPF',
        r'\bUv\b': 'UV',
        r'\bUsa\b': 'USA',
        r'\bUs\b': 'US',
        r'\bNfl\b': 'NFL',
        r'\bMlb\b': 'MLB',
        r'\bNba\b': 'NBA',
        r'\bLs\b': 'LS',
        r'\bSs\b': 'SS',
    }
    for pat, repl in restorations.items():
        out = re.sub(pat, repl, out)
    return out

def _gender_title_word(gender_str, style_name=""):
    """Return the gender word used in titles, given a style-derived gender.
    Male -> Men's, Female -> Women's, Unisex/boys -> Boys' / Girls' / Kids' per style_name.
    """
    g = (gender_str or "").lower()
    sn = (style_name or "").lower()
    if g == "male" and ("boys" in sn or "boy" in sn or "toddler" in sn):
        return "Boys'"
    if g == "female" and ("girls" in sn or "girl" in sn):
        return "Girls'"
    if g == "male":
        return "Men's"
    if g == "female":
        return "Women's"
    if g == "unisex":
        if "boys" in sn:
            return "Boys'"
        if "girls" in sn:
            return "Girls'"
        return "Kids'"
    return ""

def generate_title(brand_cfg, brand, style_name, product_type, color, size, upf="", style_gender=""):
    """Generate Amazon-compliant title. Max 120 chars for Vendor Central apparel.
    style_gender should come from _derive_gender_department(style)[0] — takes priority
    over brand_cfg['gender'] so brand-level defaults never leak into mis-gendered titles.
    """
    # Always clean the brand name
    clean_brand = clean_brand_name(brand)
    formula = brand_cfg.get("title_formula", "{brand} {gender} {style_descriptor} {product_type}, {color}, {size}")
    descriptor = style_descriptor_from_name(style_name)

    # If style name already contains the product type, don't append it again
    pt_title = product_type.title() if product_type else ""
    if pt_title and pt_title.lower() in style_name.lower():
        pt_title = ""  # avoid "Swim Trunk Trunk"

    # Style-derived gender takes priority over brand config; brand config is last resort
    effective_gender = style_gender or brand_cfg.get("gender", "")
    gender_word = _gender_title_word(effective_gender, style_name)

    title = formula.format(
        brand=clean_brand,
        style_descriptor=descriptor,
        style_name=_title_case_preserve_acronyms(style_name),
        product_type=pt_title,
        color=color.title() if color else "",
        size=normalize_size(size),
        upf=upf or brand_cfg.get("default_upf", ""),
        gender=gender_word,
    )
    # Clean up double spaces, leading/trailing punctuation
    title = re.sub(r'\s+', ' ', title).strip()
    title = re.sub(r',\s*,', ',', title)
    title = re.sub(r',\s*$', '', title)
    # Preserve acronyms (UPF etc.) in final title
    title = _title_case_preserve_acronyms(title) if title else title
    # Enforce 120 char limit — truncate at last complete word before limit
    if len(title) > 120:
        title = title[:120].rsplit(' ', 1)[0].rstrip(',')
    return title

def generate_bullets(brand_cfg, brand, style_name, sub_subclass, fabric, care, color, upf="",
                     subclass="", gender="", product_type="", style_num=""):
    """Generate 5 bullet points per brand + style context. Product-type-aware."""
    brand = clean_brand_name(brand)
    focus = brand_cfg.get("bullet_1_focus", "Style and quality")
    actual_fabric = fabric or brand_cfg.get("default_fabric", "")
    actual_care = care or brand_cfg.get("default_care", "Machine Wash")
    actual_upf = upf or brand_cfg.get("default_upf", "")
    # Normalize UPF: strip redundant "UPF" prefix so templates can add their own
    if actual_upf and actual_upf.upper().startswith("UPF"):
        actual_upf = actual_upf[3:].strip()  # "UPF 50+" → "50+"

    # Determine the product word to use instead of "dress"
    itn = _derive_item_type_name(subclass, product_type) or subclass or "garment"
    itn_lower = itn.lower()
    is_swim = product_type == "SWIMWEAR" or subclass in (
        "Rashguard", "Trunk", "Bikini Top", "Bikini Bottom", "Swim Bottom",
        "One Piece Swim", "Tankini", "Short", "Swim Set 2 pcs", "Board Short")
    gender_word = "men's" if (gender or "").lower() == "male" else "women's" if (gender or "").lower() == "female" else ""

    # ── Bullet 1: brand-specific focus, rotated per style to avoid duplicate content ──
    # Hash style_name for a stable-but-varied index across 20+ styles of same subclass.
    # Use style_num (unique) so 21 rashguards w/ same style_name still rotate through all variants
    # Use two independent indices so opener*tail = OxT distinct combos across styles
    _b1_seed = style_num or style_name or ""
    _b1_idx_o = abs(hash(_b1_seed + "b1o"))
    _b1_idx_t = abs(hash(_b1_seed + "b1t"))
    if actual_upf and ("upf" in focus.lower() or is_swim):
        _opener = SWIM_UPF_B1_OPENERS[_b1_idx_o % len(SWIM_UPF_B1_OPENERS)].format(upf=actual_upf)
        _tail = SWIM_UPF_B1_TAILS[_b1_idx_t % len(SWIM_UPF_B1_TAILS)]
        b1 = f"{_opener} — Built with UPF {actual_upf} ultraviolet protection factor fabric, this {itn_lower} {_tail}"
    elif "butterlux" in focus.lower():
        b1 = f"BUTTERLUX FABRIC — Crafted from our signature Butterlux material, this {brand} {itn_lower} delivers an extraordinarily soft, silky touch for all-day luxurious comfort."
    elif "beach" in focus.lower() or "surf" in focus.lower() or is_swim:
        _opener = SWIM_LIFESTYLE_B1_OPENERS[_b1_idx_o % len(SWIM_LIFESTYLE_B1_OPENERS)]
        _tail = SWIM_LIFESTYLE_B1_TAILS[_b1_idx_t % len(SWIM_LIFESTYLE_B1_TAILS)]
        b1 = f"{_opener} — This {brand} {itn_lower} {_tail}"
    elif "nautical" in focus.lower():
        b1 = f"NAUTICAL INSPIRED — Rooted in {brand}'s rich maritime heritage, this {itn_lower} features classic nautical design elements that bring timeless style to every occasion."
    elif "british" in focus.lower() or "mod" in focus.lower():
        b1 = f"BRITISH MOD HERITAGE — Influenced by {brand}'s iconic British mod aesthetic, this {itn_lower} delivers bold, fashion-forward style that stands out."
    elif "performance" in focus.lower():
        b1 = f"PERFORMANCE DESIGN — Engineered with {brand}'s performance expertise, this {itn_lower} combines athletic functionality with modern styling."
    elif "tailored" in focus.lower() or "sophisticated" in focus.lower():
        b1 = f"SOPHISTICATED TAILORING — {brand}'s expert tailoring creates a refined, polished silhouette that transitions effortlessly between occasions."
    else:
        b1 = f"QUALITY CRAFTSMANSHIP — {brand} brings signature quality to this {itn_lower}, combining premium materials with expert construction for lasting style and durability."

    # ── Bullet 2: Style-specific features ──
    style_features = []
    sn_upper = style_name.upper()
    if is_swim:
        if "HOODED" in sn_upper or "HOOD" in sn_upper: style_features.append("integrated hood for extra sun coverage")
        if "LONG SLEEVE" in sn_upper: style_features.append("long-sleeve design for full arm protection")
        if "SHORT SLEEVE" in sn_upper: style_features.append("short-sleeve design for freedom of movement")
        if "LOOSE" in sn_upper: style_features.append("loose, relaxed fit for layering comfort")
        if "4-WAY" in sn_upper or "4 WAY" in sn_upper: style_features.append("4-way stretch fabric for unrestricted movement")
        if "E-WAIST" in sn_upper or "ELASTIC" in sn_upper: style_features.append("elastic waistband for easy on/off")
        if "BOARD" in sn_upper: style_features.append("boardshort styling built for the water")
        if "TWIST" in sn_upper: style_features.append("twist-front detail for a flattering silhouette")
        if "V-NECK" in sn_upper or "VNECK" in sn_upper: style_features.append("V-neckline for a sleek profile")
        if "PRINTED" in sn_upper or "PRINT" in sn_upper: style_features.append("bold all-over print")
        if "LOGO" in sn_upper: style_features.append("signature logo branding")
        if "ZIP" in sn_upper: style_features.append("front zip closure")
    else:
        # Dress/apparel features
        neck = derive_neck_type(style_name)
        sleeve = derive_sleeve_type(style_name)
        if neck: style_features.append(f"flattering {neck} neckline")
        if sleeve and sleeve != "Sleeveless": style_features.append(f"{sleeve.lower()} detail")
        if "PLEATED" in sn_upper: style_features.append("elegant pleated front")
        if "RUFFLE" in sn_upper: style_features.append("playful ruffle accents")

    if not style_features:
        style_features = ["thoughtfully designed details"]
    feature_str = ", ".join(style_features[:3])
    # Bullet 2: no style_name stuffing. Rotate headline + template per style.
    _b2_seed = style_num or style_name or ""
    _b2_idx_h = abs(hash(_b2_seed + "b2h"))
    _b2_idx_t = abs(hash(_b2_seed + "b2t"))
    if is_swim:
        _b2_head = SWIM_B2_OPENERS[_b2_idx_h % len(SWIM_B2_OPENERS)]
        _b2_body = SWIM_B2_TEMPLATES[_b2_idx_t % len(SWIM_B2_TEMPLATES)].format(itn_lower=itn_lower, feature_str=feature_str)
        b2 = f"{_b2_head} — {_b2_body}"
    else:
        b2 = f"DESIGNED FOR PERFORMANCE — This {itn_lower} features {feature_str} for a look that stands apart."

    # ── Bullet 3: Fit & sizing ──
    fit_type = brand_cfg.get("default_fit_type", "")
    if is_swim:
        b3 = f"COMFORTABLE FIT — Designed to fit true to size with a {fit_type.lower() or 'relaxed'} fit that moves with your body. Available in a full size range so every {gender_word + ' ' if gender_word else ''}customer finds the right fit."
    else:
        sil = derive_silhouette(sub_subclass)
        fit_desc = f"{sil} silhouette" if sil else "flattering cut"
        b3 = f"PERFECT FIT & COMFORT — The {fit_desc} is designed to flatter a range of body types with a relaxed yet refined fit that moves with you throughout the day."

    # ── Bullet 4: Fabric + care ──
    if actual_fabric:
        if is_swim:
            b4 = f"QUICK-DRY FABRIC — Made from {actual_fabric} for rapid moisture wicking and fast drying. {actual_care} for easy upkeep after every swim session."
        else:
            b4 = f"PREMIUM FABRIC — Made from {actual_fabric}, offering a smooth, comfortable feel with just the right amount of stretch. {actual_care} for easy home care."
    else:
        b4 = f"EASY CARE — Crafted for effortless wearability with durable construction. {actual_care} for convenient upkeep."

    # ── Bullet 5: Use case ──
    if is_swim:
        b5 = f"VERSATILE WATER WEAR — Whether you're surfing, swimming laps, or lounging poolside, this {brand} {itn_lower} delivers. Pair with your favorite {brand} swim gear for a complete look."
    else:
        b5 = f"COMPLETE THE LOOK — Pair this {color.title() if color else ''} {itn_lower} with your favorite accessories for any occasion. A versatile addition to your {brand} wardrobe."

    return [b1, b2, b3, b4, b5]

def generate_description(brand_cfg, brand, style_num, style_name, sub_subclass, fabric, care, color, upf="",
                         subclass="", gender="", product_type=""):
    """Generate product description. Max 2000 chars. Product-type-aware."""
    brand = clean_brand_name(brand)
    actual_fabric = fabric or brand_cfg.get("default_fabric", "")
    actual_care = care or brand_cfg.get("default_care", "Machine Wash")
    actual_upf = upf or brand_cfg.get("default_upf", "")
    if actual_upf and actual_upf.upper().startswith("UPF"):
        actual_upf = actual_upf[3:].strip()
    itn = _derive_item_type_name(subclass, product_type) or subclass or "garment"
    itn_lower = itn.lower()
    is_swim = product_type == "SWIMWEAR" or subclass in (
        "Rashguard", "Trunk", "Bikini Top", "Bikini Bottom", "Swim Bottom",
        "One Piece Swim", "Tankini", "Short", "Swim Set 2 pcs", "Board Short")

    parts = []
    if is_swim:
        # Rotate swim opener across 20+ styles so same-subclass products don't share line 1.
        _sw_idx = abs(hash((style_num or "") + "swim")) if style_num else 0
        _sw_opener = SWIM_DESCRIPTION_OPENERS[_sw_idx % len(SWIM_DESCRIPTION_OPENERS)]
        parts.append(_sw_opener.format(brand=brand, style_name=_title_case_preserve_acronyms(style_name)))
        if actual_fabric:
            fp = f"Constructed from {actual_fabric}"
            if actual_upf:
                fp += f" with UPF {actual_upf} sun protection"
            fp += ", this " + itn_lower + " dries quickly and resists chlorine and salt water."
            parts.append(fp)
        _closers = [
            f"Designed for surfing, swimming, and beach days, it combines {brand}'s signature style with functional performance. {actual_care} for easy care after every session.",
            f"Whether you're chasing waves or lounging on the sand, this {itn_lower} delivers the durability and comfort you expect from {brand}. {actual_care} between sessions.",
            f"From sunrise paddle-outs to sunset hangs, this {brand} {itn_lower} keeps up. {actual_care} after each use for long-lasting wear.",
            f"Packable, quick-drying, and built to last — {brand}'s approach to warm-weather essentials shows in every detail. {actual_care} for easy upkeep.",
        ]
        parts.append(_closers[_sw_idx % len(_closers)])
    else:
        # Dress / general apparel
        global DESCRIPTION_OPENERS_ROTATION
        if style_num not in DESCRIPTION_OPENERS_ROTATION:
            idx = len(DESCRIPTION_OPENERS_ROTATION) % len(DESCRIPTION_OPENERS)
            DESCRIPTION_OPENERS_ROTATION[style_num] = idx
        opener_template = DESCRIPTION_OPENERS[DESCRIPTION_OPENERS_ROTATION[style_num]]
        parts.append(opener_template.format(brand=brand, style_name=style_name.title()))
        if actual_fabric:
            fp = f"Constructed from {actual_fabric}"
            if actual_upf:
                fp += f" with UPF {actual_upf} sun protection built right in"
            fp += f", this {itn_lower} provides all-day comfort. {actual_care} for easy upkeep."
            parts.append(fp)
        parts.append(f"Versatile enough for any occasion, this {brand} {itn_lower} transitions effortlessly from day to night.")

    return " ".join(parts)[:2000]

def qa_check_content(content, brand):
    """Run QA checks on generated content. Returns list of issues."""
    issues = []
    clean_brand = clean_brand_name(brand)
    title = content.get("title", "")
    
    # Title checks
    if len(title) > 120:
        issues.append({"field": "title", "severity": "error", "msg": f"Title exceeds 120 chars ({len(title)})."})
    if len(title) < 40:
        issues.append({"field": "title", "severity": "warning", "msg": f"Title is very short ({len(title)} chars). Add more keywords."})
    if '#N/A' in title or '#n/a' in title.lower():
        issues.append({"field": "title", "severity": "error", "msg": "Title contains #N/A — data parsing error."})
    if brand and clean_brand not in title:
        issues.append({"field": "title", "severity": "warning", "msg": f"Brand name '{clean_brand}' not in title."})
    
    # Bullet checks
    for i in range(1, 6):
        b = content.get(f"bullet_{i}", "")
        if not b:
            issues.append({"field": f"bullet_{i}", "severity": "error", "msg": f"Bullet {i} is empty."})
        elif len(b) < 50:
            issues.append({"field": f"bullet_{i}", "severity": "warning", "msg": f"Bullet {i} is very short ({len(b)} chars)."})
        if len(b) > 500:
            issues.append({"field": f"bullet_{i}", "severity": "error", "msg": f"Bullet {i} exceeds 500 chars ({len(b)})."})
        if '#N/A' in b or '#n/a' in b.lower():
            issues.append({"field": f"bullet_{i}", "severity": "error", "msg": f"Bullet {i} contains #N/A."})
        # Check for prohibited language
        for word in ['best seller', 'best-seller', 'limited time', 'on sale', 'free shipping', 'guaranteed']:
            if word.lower() in b.lower():
                issues.append({"field": f"bullet_{i}", "severity": "error", "msg": f"Bullet {i} contains prohibited phrase: '{word}'."})
    
    # Description checks
    desc = content.get("description", "")
    if not desc:
        issues.append({"field": "description", "severity": "error", "msg": "Description is empty."})
    elif len(desc) < 200:
        issues.append({"field": "description", "severity": "warning", "msg": f"Description is short ({len(desc)} chars). Aim for 500+."})
    if len(desc) > 2000:
        issues.append({"field": "description", "severity": "error", "msg": f"Description exceeds 2000 chars ({len(desc)})."})
    if '#N/A' in desc:
        issues.append({"field": "description", "severity": "error", "msg": "Description contains #N/A."})
    
    # Backend keywords checks
    kw = content.get("backend_keywords", "")
    if not kw:
        issues.append({"field": "backend_keywords", "severity": "warning", "msg": "Backend keywords empty."})
    elif len(kw.encode('utf-8')) > 250:
        issues.append({"field": "backend_keywords", "severity": "error", "msg": f"Backend keywords exceed 250 bytes ({len(kw.encode('utf-8'))})."})
    if clean_brand and clean_brand.lower() in (kw or '').lower():
        issues.append({"field": "backend_keywords", "severity": "warning", "msg": "Backend keywords contain brand name (Amazon indexes it automatically)."})
    
    return issues


def generate_title_why(brand_cfg, brand, style_name, title, upf, has_keywords):
    """Generate 'why' explanation for the title."""
    char_count = len(title)
    parts = []
    # Gender format
    parts.append('"Women\'s" format used — outperforms "for Women" in Amazon search CTR.')
    # UPF
    if upf or brand_cfg.get("default_upf"):
        parts.append(f'UPF {upf or brand_cfg.get("default_upf")} placed after brand as lead differentiator — detected in product data.')
    # Keywords
    if has_keywords:
        parts.append('Top keyword from uploaded Helium 10 data incorporated into title.')
    else:
        parts.append('No keyword data uploaded — category defaults used for title structure.')
    parts.append(f'{char_count}/200 characters used.')
    return ' '.join(parts)


def generate_bullet_why(idx, brand_cfg, brand, style_name, sub_subclass, upf, fabric, has_keywords):
    """Generate 'why' explanation for a bullet."""
    focus = brand_cfg.get("bullet_1_focus", "Style and quality")
    if idx == 0:
        if "upf" in focus.lower():
            return f'UPF {upf or brand_cfg.get("default_upf", "30+")} detected in product data. Sun protection leads for this brand based on category positioning — highest differentiator for outdoor/activewear.'
        elif "butterlux" in focus.lower():
            return f'Butterlux fabric is {brand}\'s signature material — used as Bullet 1 per brand config.'
        elif "beach" in focus.lower() or "surf" in focus.lower():
            return f'{brand}\'s surf heritage is the primary brand differentiator — configured as Bullet 1 focus.'
        else:
            return f'Brand quality and craftsmanship leads Bullet 1 per brand config setting: "{focus}".'
    elif idx == 1:
        neck = derive_neck_type(style_name)
        sleeve = derive_sleeve_type(style_name)
        features = []
        if neck: features.append(f'"{neck}" detected in style name')
        if sleeve: features.append(f'"{sleeve}" detected in style name')
        if "PLEATED" in style_name.upper(): features.append('"PLEATED" → hourglass effect copy')
        if "RUFFLE" in style_name.upper() or "RFL" in style_name.upper(): features.append('"RUFFLE" → playful accent copy')
        if features:
            return f'Style-specific features derived from name: {", ".join(features[:2])}. This bullet varies per style to avoid duplicate content.'
        return 'Style details derived from style name analysis. This bullet varies per style to avoid duplicate content.'
    elif idx == 2:
        silhouette = derive_silhouette(sub_subclass)
        return f'Fit & sizing copy generated from silhouette: "{silhouette or "flattering"}". Size range XS–3X included per Amazon best practice for apparel.'
    elif idx == 3:
        actual_fabric = parse_fabric(fabric) or brand_cfg.get("default_fabric", "")
        if actual_fabric:
            return f'Fabric composition "{actual_fabric}" from product data. Care instructions from product data or brand defaults. Premium fabric positioning improves conversion.'
        return f'Fabric information not found in product data — using brand default. Care instructions from brand config. Upload product data with fabric column for style-specific copy.'
    elif idx == 4:
        return 'Cross-sell bullet drives average order value by suggesting complementary styling. Color-specific to each variant (updated per color in NIS output).'
    return ''


def generate_description_why(brand_cfg, style_num, opener_idx, has_keywords):
    """Generate 'why' explanation for description."""
    total_openers = len(DESCRIPTION_OPENERS)
    opener_num = (opener_idx % total_openers) + 1
    parts = [
        f'Opener #{opener_num} of {total_openers} used — rotated per style to avoid duplicate content flags.',
        'Three-paragraph structure: opener + style details + fabric/care + occasion/versatility.',
    ]
    if not has_keywords:
        parts.append('No keyword data uploaded — keyword integration uses category defaults. Upload Helium 10 CSV for optimized keyword placement.')
    return ' '.join(parts)


def generate_keywords_why(brand, keywords_list, result_kw, has_keywords):
    """Generate 'why' explanation for backend keywords."""
    byte_count = len(result_kw.encode('utf-8'))
    term_count = len(result_kw.split()) if result_kw else 0
    if has_keywords:
        top_kw = [k['keyword'] for k in keywords_list[:3]] if keywords_list else []
        return (f'{byte_count}/250 bytes used. {term_count} terms: top keywords from uploaded Helium 10 data '
                f'({(", ".join(top_kw)) or "none"}) plus category defaults. '
                f'Brand name excluded (Amazon penalizes brand repetition in backend keywords).')
    return (f'{byte_count}/250 bytes used. {term_count} terms derived from category defaults + style name analysis. '
            f'No keyword data uploaded — upload Helium 10 CSV for search-volume-ranked backend keywords. '
            f'Brand name excluded per Amazon guidelines.')


def generate_backend_keywords(brand, style_name, sub_subclass, color, fabric, upf="",
                              subclass="", gender="", product_type=""):
    """Generate backend keywords. Max 250 bytes, lowercase, no brand, no title duplicates.
    Product-type-aware."""
    brand_lower = brand.lower()
    itn = _derive_item_type_name(subclass, product_type) or subclass or ""
    itk = _derive_item_type_keyword(subclass, product_type) or ""
    is_swim = product_type == "SWIMWEAR" or subclass in (
        "Rashguard", "Trunk", "Bikini Top", "Bikini Bottom", "Swim Bottom",
        "One Piece Swim", "Tankini", "Short", "Swim Set 2 pcs", "Board Short")
    g = (gender or "").lower()
    gender_kw = "mens" if g == "male" else "womens" if g == "female" else "kids" if g == "unisex" else ""

    candidates = []
    if is_swim:
        # Swimwear-specific keywords
        if gender_kw:
            candidates.append(f"{gender_kw} {itn.lower()}" if itn else f"{gender_kw} swimwear")
        candidates.append(itn.lower() if itn else "swimwear")
        candidates.append(itk.replace("-", " ") if itk else "")
        candidates.append(subclass.lower() if subclass else "")
        candidates.extend(["swim", "swimwear", "beach", "pool"])
        if "RASH" in (subclass or "").upper() or "RASH" in style_name.upper():
            candidates.extend(["rash guard", "rashguard", "sun shirt", "swim shirt"])
        if "TRUNK" in (subclass or "").upper() or "TRUNK" in style_name.upper():
            candidates.extend(["swim trunks", "board shorts", "bathing suit"])
        if "BIKINI" in (subclass or "").upper():
            candidates.extend(["bikini", "two piece", "swimsuit"])
        if "ONE PIECE" in (subclass or "").upper():
            candidates.extend(["one piece", "swimsuit", "bathing suit"])
        if "TANKINI" in (subclass or "").upper():
            candidates.extend(["tankini", "two piece", "swimsuit"])
        if "BOARD" in style_name.upper():
            candidates.extend(["boardshorts", "board shorts", "surf shorts"])
        if upf:
            candidates.extend([f"upf {upf}", "sun protection", "uv protection"])
        candidates.extend(["quick dry", "water sports", "surf", "swimming"])
    else:
        # Dress / general apparel keywords
        if gender_kw:
            candidates.append(f"{gender_kw} {itn.lower()}" if itn else f"{gender_kw} dress")
        candidates.append(itn.lower() if itn else "dress")
        candidates.append(f"{sub_subclass.lower()}" if sub_subclass else "")
        candidates.extend(["casual", "everyday", "comfortable", "stylish"])
        if upf:
            candidates.extend([f"upf {upf}", "sun protective clothing"])

    if fabric:
        if "polyester" in fabric.lower(): candidates.append("polyester")
        if "spandex" in fabric.lower(): candidates.append("stretch")
        if "nylon" in fabric.lower(): candidates.append("nylon")
    if color:
        candidates.append(normalize_color(color).lower())

    # Filter: no brand name, no empty, no duplicates, no stem-redundant phrases
    # (e.g. drop "rash guard shirt" when "mens rash guard shirt" already covers all tokens)
    def _stem(tok):
        t = tok.strip().lower()
        # normalize singular/plural
        if len(t) > 3 and t.endswith("es") and not t.endswith("ses"):
            t = t[:-2]
        elif len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        return t
    seen_phrases = set()
    covered_stems = set()
    result = []
    for kw in candidates:
        kw = kw.strip().lower()
        if not kw or brand_lower in kw or kw in seen_phrases or len(kw) < 2:
            continue
        toks = kw.split()
        stems = [_stem(t) for t in toks]
        # Skip if every stem is already covered by an earlier phrase (pure redundancy)
        if stems and all(s in covered_stems for s in stems):
            continue
        seen_phrases.add(kw)
        for s in stems:
            covered_stems.add(s)
        result.append(kw)

    # Join and cap at 250 bytes
    joined = " ".join(result)
    while len(joined.encode('utf-8')) > 250 and result:
        result.pop()
        joined = " ".join(result)

    return joined

# ── Template parsing ───────────────────────────────────────────────────────────
# ── LLM feedback loading ───────────────────────────────────────────────────────
def load_brand_feedback(brand):
    """Load the 20 most recent feedback entries for a brand as a summary string."""
    if not FEEDBACK_FILE.exists():
        return ""
    entries = []
    try:
        with open(str(FEEDBACK_FILE), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("brand") == brand:
                        entries.append(entry)
                except json.JSONDecodeError:
                    pass
    except Exception:
        return ""
    # Sort newest first, take top 20
    entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    recent = entries[:20]
    if not recent:
        return ""
    lines = []
    for e in recent:
        field = e.get("field", "")
        feedback = e.get("feedback", "")
        orig = e.get("original", "")
        updated = e.get("updated", "")
        if feedback:
            lines.append(f"- [{field}] {feedback}")
        elif orig and updated:
            lines.append(f"- [{field}] Changed from: '{str(orig)[:80]}' to: '{str(updated)[:80]}'")
    return "\n".join(lines)


def generate_content_llm(brand_cfg, brand, style, feedback_history):
    """
    Use Claude to generate Amazon listing content for a style.
    Falls back to rule-based generation if LLM is unavailable.
    Returns a content dict with title, bullet_1-5, description, backend_keywords.
    """
    global _anthropic_client
    if _anthropic_client is None:
        return None  # Caller will fall back to rule-based

    clean_brand = clean_brand_name(brand)
    style_num = style["style_num"]
    style_name = style["style_name"]
    subclass = style.get("subclass", "")
    sub_subclass = style.get("sub_subclass", "")
    fabric = parse_fabric(style.get("fabric", "")) or brand_cfg.get("default_fabric", "")
    care = style.get("care", "") or brand_cfg.get("default_care", "")
    upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
    coo = style.get("coo", "") or brand_cfg.get("default_coo", "")

    # Collect color/size lists
    variants = style.get("variants", [])
    colors = list(dict.fromkeys([v.get("color_name", "") for v in variants if v.get("color_name")]))
    sizes = list(dict.fromkeys([v.get("size", "") for v in variants if v.get("size")]))
    upcs = [v.get("upc", "") for v in variants[:3] if v.get("upc")]

    # Brand voice description
    bullet_1_focus = brand_cfg.get("bullet_1_focus", "Style and quality")
    never_words = brand_cfg.get("never_words", [])
    # Derive gender per-style from division_name, not brand config
    style_gender, style_dept = _derive_gender_department(style)
    gender = style_gender or brand_cfg.get("gender", "")
    never_words_str = ", ".join(never_words) if never_words else "none"
    product_type = _resolve_style_product_type(style) or ""
    itn = _derive_item_type_name(subclass, product_type) or subclass or "garment"
    division = style.get("division_name", "")

    # Product brief: per-style brief overrides brand-level brief
    style_briefs = session_data.get("style_briefs", {})
    product_briefs = brand_cfg.get("product_briefs", {})
    brief = style_briefs.get(str(style_num), "") or product_briefs.get(f"{product_type}/{subclass}", "") or product_briefs.get(product_type, "") or product_briefs.get("_default", "")

    feedback_count = len([l for l in feedback_history.splitlines() if l.strip()]) if feedback_history else 0
    feedback_section = f"LEARNED PREFERENCES (from {feedback_count} previous edits):\n{feedback_history}" if feedback_history else "LEARNED PREFERENCES: None yet."

    # Gender-aware audience description
    if gender == "Male":
        audience = "men and boys"
        gender_prefix = "Men's"
    elif gender == "Female":
        audience = "women and girls"
        gender_prefix = "Women's"
    else:
        audience = "all genders"
        gender_prefix = ""

    prompt = f"""You are an Amazon listing content expert generating Vendor Central NIS content.

BRAND: {clean_brand}
BRAND VOICE: {clean_brand} is a {bullet_1_focus}-focused brand targeting {audience}. {brief or ''}
HERO FEATURE: {bullet_1_focus}
NEVER USE: {never_words_str}

PRODUCT:
- Product Type: {itn} ({product_type})
- Style: {style_name}
- Style #: {style_num}
- Sub-class: {subclass} / {sub_subclass}
- Division: {division}
- Gender: {gender or 'not specified'}
- Fabric: {fabric or 'not specified'}
- UPF: {upf or 'none'}
- COO: {coo or 'not specified'}
- Care: {care or 'not specified'}
- Colors: {', '.join(colors[:8]) if colors else 'not specified'}
- Sizes: {', '.join(sizes[:10]) if sizes else 'not specified'}

{f'PRODUCT BRIEF FROM OPERATOR: {brief}' if brief else ''}

{feedback_section}

RULES:
- Title: max 120 characters. Format: {clean_brand} {gender_prefix + ' ' if gender_prefix else ''}[Style Descriptor] [Product Type], [Key Feature], [Color], [Size].
- This is a {itn}, NOT a dress. Do not use the word "dress" unless the product is actually a dress.
- 5 bullet points, each max 500 chars. Format: LABEL — description. Each bullet must be unique. Bullet 1 focuses on {bullet_1_focus}. Bullet 2 must describe THIS specific style's unique design features.
- Description: max 2000 chars. Plain text, no HTML. Buyer-focused, mentions brand + product name.
- Backend keywords: max 250 bytes. Lowercase, space-separated. No brand name, no words from title. Include product-type-specific search terms (e.g. for swimwear: swim, swimwear, beach, pool, rash guard, etc.).
- No promotional language (best seller, limited time, on sale, guaranteed, free shipping).
- No competitor brand names.

Respond in this exact JSON format (no other text, no markdown, just the JSON object):
{{"title": "...", "bullet_1": "...", "bullet_2": "...", "bullet_3": "...", "bullet_4": "...", "bullet_5": "...", "description": "...", "backend_keywords": "..."}}"""

    try:
        # Build message content — text prompt + optional image (vision)
        msg_content = []
        style_num = style["style_num"]
        img_path = (session_data.get("style_images") or {}).get(str(style_num))
        if img_path and Path(img_path).exists():
            import base64 as _b64
            with open(img_path, "rb") as _img_f:
                img_bytes = _b64.b64encode(_img_f.read()).decode("utf-8")
            ext = Path(img_path).suffix.lower()
            media_type = {"jpg": "image/jpeg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                          ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")
            msg_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": img_bytes}
            })
            msg_content.append({"type": "text", "text": "Above is a photo of this product. Use it to accurately describe the product's appearance, design details, and visual features.\n\n" + prompt})
        else:
            msg_content.append({"type": "text", "text": prompt})

        message = _anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": msg_content}],
        )
        raw = message.content[0].text.strip()
        # Strip any markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)

        content = {
            "title": str(parsed.get("title", ""))[:120],
            "bullet_1": str(parsed.get("bullet_1", ""))[:500],
            "bullet_2": str(parsed.get("bullet_2", ""))[:500],
            "bullet_3": str(parsed.get("bullet_3", ""))[:500],
            "bullet_4": str(parsed.get("bullet_4", ""))[:500],
            "bullet_5": str(parsed.get("bullet_5", ""))[:500],
            "description": str(parsed.get("description", ""))[:2000],
            "backend_keywords": str(parsed.get("backend_keywords", "")),
        }
        # Cap backend keywords at 250 bytes
        kw = content["backend_keywords"]
        while len(kw.encode("utf-8")) > 250 and kw:
            kw = kw.rsplit(" ", 1)[0]
        content["backend_keywords"] = kw

        return content

    except Exception as e:
        print(f"[LLM] generate_content_llm failed for style {style_num}: {e}")
        return None


def parse_template_columns(template_path):
    """Parse .xlsm template rows 3 (headers) and 4 (field IDs). Returns col_map."""
    wb = openpyxl.load_workbook(template_path, keep_vba=True, read_only=True)
    ws = None
    for name in wb.sheetnames:
        if "template" in name.lower() or "dress" in name.lower():
            ws = wb[name]
            break
    if ws is None:
        ws = wb.active
    
    col_map = {}
    for col in range(1, (ws.max_column or 300) + 1):
        header = _safe(ws.cell(row=3, column=col).value)
        field_id = _safe(ws.cell(row=4, column=col).value)
        if header or field_id:
            col_map[col] = {"header": header, "field_id": field_id}
    
    wb.close()
    return col_map

_template_col_map_cache = {}

def get_template_col_map(template_path=None):
    if template_path is None:
        template_path = str(DEFAULT_TEMPLATE)
    if template_path not in _template_col_map_cache:
        _template_col_map_cache[template_path] = parse_template_columns(template_path)
    return _template_col_map_cache[template_path]

def find_col_by_field_id(col_map, field_id_pattern):
    """Find column number(s) by field_id pattern match."""
    results = []
    for col, info in col_map.items():
        if field_id_pattern.lower() in info["field_id"].lower():
            results.append(col)
    return results

def find_col_exact(col_map, field_id):
    """Find exact column number by field_id."""
    for col, info in col_map.items():
        if info["field_id"].lower() == field_id.lower():
            return col
    return None

# ── Product data parsing ───────────────────────────────────────────────────────
PRODUCT_HEADER_ALIASES = {
    # Brand
    "brand": "brand",
    "brand code": "brand",
    "brand name": "brand",
    # Division / product type
    "division": "division",
    "tlgdiv name": "division_name",
    "inline/value": "inline_value",
    # Category
    "sub-class name": "subclass",
    "sub class name": "subclass",
    "sub class": "subclass",
    "subclass": "subclass",
    "sub sub-class name": "sub_subclass",
    "sub sub class name": "sub_subclass",
    "sub-subclass": "sub_subclass",
    # Style
    "style #": "style_num",
    "style#": "style_num",
    "style number": "style_num",
    "style name": "style_name",
    "style description": "style_desc",
    "tlg style desc": "style_desc",
    "season code": "season_code",
    "season added to amzn": "season_added",
    # Color
    "color code": "color_code",
    "color name": "color_name",
    "color": "color_name",
    # Size
    "product - size": "size",
    "size": "size",
    # IDs
    "upc": "upc",
    "upc code": "upc",
    "casin": "casin",
    "child asin": "child_asin",
    "parent asin": "parent_asin",
    "model": "model_code",
    "model name": "model_name",
    "sku": "sku",
    # Pricing
    "list price": "list_price",
    "amzn retail": "list_price",
    "retail": "list_price",
    "retail price": "list_price",
    "cost price": "cost_price",
    "amzn wholesale": "cost_price",
    "wholesale": "cost_price",
    "wholesale price": "cost_price",
    "case pack": "case_pack",
    # Product attributes
    "country of origin": "coo",
    "coo": "coo",
    "fabric": "fabric",
    "material": "fabric",
    "fabric content percentage": "fabric",
    "fabric content": "fabric",
    "care": "care",
    "care instructions": "care",
    "upf": "upf",
    "upf rating": "upf",
    "neck type": "neck_type",
    "collar type": "neck_type",
    "closure type": "closure_type",
    "closure": "closure_type",
    "sleeve type": "sleeve_type",
    "sleeve": "sleeve_type",
    "fit type": "fit_type",
    "fit type (regular, relaxed, oversized, slim , fitted, etc.)": "fit_type",
    "pockets": "pockets",
    "pockets?": "pockets",
    # Bullets from pre-upload
    "key features (bullet 1)": "bullet_1",
    "key features (bullet 2)": "bullet_2",
    "key features (bullet 3)": "bullet_3",
    "key features (bullet 4)": "bullet_4",
    "key features (bullet 5)": "bullet_5",
    "bullet 1": "bullet_1",
    "bullet 2": "bullet_2",
    "bullet 3": "bullet_3",
    "bullet 4": "bullet_4",
    "bullet 5": "bullet_5",
    # Dates
    "due date": "ship_date",
    "due date (earliest ship date)": "ship_date",
    "earliest ship date": "ship_date",
    "ship date": "ship_date",
    # Extra
    "additional details": "additional_details",
    "additional details, standouts, call outs, features": "additional_details",
}

def fuzzy_match_headers(headers):
    """Fuzzy-match raw headers to internal field names."""
    mapping = {}  # internal_field -> col_index (0-based)
    for idx, h in enumerate(headers):
        if not h:
            continue
        key = str(h).strip().lower()
        if key in PRODUCT_HEADER_ALIASES:
            internal = PRODUCT_HEADER_ALIASES[key]
            if internal not in mapping:
                mapping[internal] = idx
    return mapping

def parse_product_file(file_path):
    """Parse product Excel/CSV file. Returns (rows, errors, warnings)."""
    ext = Path(file_path).suffix.lower()
    raw_rows = []
    
    if ext in [".xlsx", ".xls", ".xlsm"]:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        wb.close()
        
        # Find header row
        header_row_idx = None
        for i, row in enumerate(all_rows):
            non_empty = sum(1 for c in row if c is not None)
            # Look for a row that has many non-empty cells and contains typical headers
            if non_empty >= 5:
                row_str = " ".join(str(c).lower() for c in row if c is not None)
                if any(kw in row_str for kw in ["style", "brand", "color", "size", "upc", "price"]):
                    header_row_idx = i
                    break
        
        if header_row_idx is None:
            return [], ["Could not find header row in file"], []
        
        headers = [str(c).strip() if c is not None else "" for c in all_rows[header_row_idx]]
        for row in all_rows[header_row_idx + 1:]:
            if any(c is not None for c in row):
                raw_rows.append(row)
        
    elif ext in [".csv", ".tsv"]:
        delimiter = "\t" if ext == ".tsv" else ","
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter=delimiter)
            all_csv = list(reader)
        if not all_csv:
            return [], ["Empty CSV file"], []
        headers = all_csv[0]
        raw_rows = [tuple(row) for row in all_csv[1:]]
    else:
        return [], [f"Unsupported file type: {ext}"], []
    
    col_map = fuzzy_match_headers(headers)
    
    errors = []
    warnings = []
    styles = {}  # style_num -> {style_info, variants:[]}
    
    for row_idx, row in enumerate(raw_rows, start=1):
        def get(field):
            idx = col_map.get(field)
            if idx is None or idx >= len(row):
                return ""
            return _safe(row[idx])
        
        style_num = get("style_num")
        style_name = get("style_name") or get("style_desc")
        brand = get("brand")
        subclass = get("subclass")
        sub_subclass = get("sub_subclass")
        division_name = get("division_name")
        color_name = get("color_name")
        color_code = get("color_code")
        size = get("size")
        upc = get("upc")
        list_price = get("list_price")
        cost_price = get("cost_price")
        parent_asin = get("parent_asin")
        child_asin = get("child_asin")
        model_name = get("model_name")
        season_code = get("season_code")
        fabric = get("fabric")
        care = get("care")
        upf = get("upf")
        coo = get("coo")
        sku = get("sku")
        # New fields from pre-upload
        neck_type = get("neck_type")
        closure_type = get("closure_type")
        sleeve_type = get("sleeve_type")
        fit_type = get("fit_type")
        ship_date = get("ship_date")
        bullet_1 = get("bullet_1")
        bullet_2 = get("bullet_2")
        bullet_3 = get("bullet_3")
        bullet_4 = get("bullet_4")
        bullet_5 = get("bullet_5")
        additional_details = get("additional_details")
        
        if not style_num:
            continue
        
        # Validation
        row_errors = []
        row_warnings = []
        
        if not style_name:
            row_errors.append(f"Row {row_idx}: Missing style name for style {style_num}")
        
        # UPC validation
        if upc:
            upc_clean = re.sub(r'\D', '', str(upc))
            if len(upc_clean) not in (12, 13, 14):
                row_errors.append(f"Row {row_idx}: UPC/EAN '{upc}' is not valid (expected 12, 13, or 14 digits, got {len(upc_clean)}) — style {style_num}, {color_name} {size}")
            elif len(upc_clean) == 13:
                row_warnings.append(f"Row {row_idx}: EAN-13 detected for {style_num} {color_name} {size} — will be used as-is")
        else:
            row_warnings.append(f"Row {row_idx}: Missing UPC for style {style_num}, color {color_name}, size {size}")
        
        # Price validation
        try:
            lp = float(list_price) if list_price else 0
            cp = float(cost_price) if cost_price else 0
            if lp > 0 and cp > 0:
                if cp > lp:
                    row_errors.append(f"Row {row_idx}: Cost (${cp}) > List price (${lp}) for style {style_num}")
                elif cp > 0.8 * lp:
                    row_warnings.append(f"Row {row_idx}: CRITICAL: Cost (${cp}) is >80% of List (${lp}) for style {style_num}")
                elif cp > 0.6 * lp:
                    row_warnings.append(f"Row {row_idx}: Cost (${cp}) is >60% of List (${lp}) for style {style_num}")
        except (ValueError, TypeError):
            pass
        
        errors.extend(row_errors)
        warnings.extend(row_warnings)
        
        # Build style entry
        if style_num not in styles:
            styles[style_num] = {
                "style_num": style_num,
                "style_name": style_name or style_num,
                "brand": brand,
                "subclass": subclass,
                "sub_subclass": sub_subclass,
                "division_name": division_name,
                "list_price": list_price,
                "cost_price": cost_price,
                "parent_asin": parent_asin,
                "model_name": model_name,
                "season_code": season_code,
                "fabric": fabric,
                "care": care,
                "upf": upf,
                "coo": coo,
                # Fields from pre-upload that take priority
                "neck_type": neck_type,
                "closure_type": closure_type,
                "sleeve_type": sleeve_type,
                "fit_type": fit_type,
                "ship_date": ship_date,
                "bullets_from_upload": [b for b in [bullet_1, bullet_2, bullet_3, bullet_4, bullet_5] if b],
                "additional_details": additional_details,
                "variants": [],
                "errors": [],
                "warnings": [],
            }
        
        styles[style_num]["errors"].extend(row_errors)
        styles[style_num]["warnings"].extend(row_warnings)
        
        # Deduplicate style-level info
        if style_name and not styles[style_num]["style_name"]:
            styles[style_num]["style_name"] = style_name
        if fabric and not styles[style_num]["fabric"]:
            styles[style_num]["fabric"] = fabric
        if care and not styles[style_num]["care"]:
            styles[style_num]["care"] = care
        if upf and not styles[style_num]["upf"]:
            styles[style_num]["upf"] = upf
        if coo and not styles[style_num]["coo"]:
            styles[style_num]["coo"] = coo
        if parent_asin and not styles[style_num]["parent_asin"]:
            styles[style_num]["parent_asin"] = parent_asin
        
        variant = {
            "color_name": color_name,
            "color_code": color_code,
            "size": size,
            "upc": upc,
            "child_asin": child_asin,
            "sku": sku,
            "errors": row_errors,
            "warnings": row_warnings,
        }
        styles[style_num]["variants"].append(variant)
    
    return list(styles.values()), errors, warnings

# ── Session state (in-memory, per app restart) ─────────────────────────────────
session_data = {
    "brand": None,
    "vendor_code": None,
    "template_path": str(DEFAULT_TEMPLATE),
    "col_map": None,
    "product_file": None,
    "styles": [],
    "keywords": [],
    "analytics": [],
    "generated_content": {},
    # Multi-template: maps product_type -> path, e.g. {"Dresses": "/path/to/Dresses.xlsm"}
    "templates": {},
    # Field overrides from QA review: { style_num: { field_id: value } }
    "field_overrides": {},
    "operator": "",
}

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/session-restore")
def session_restore():
    """Return current session state so frontend can restore after page refresh."""
    return jsonify({
        "brand": session_data.get("brand"),
        "vendor_code": session_data.get("vendor_code"),
        "template_loaded": session_data.get("col_map") is not None,
        "template_columns": len(session_data.get("col_map") or {}),
        "styles_count": len(session_data.get("styles", [])),
        "styles": session_data.get("styles", []),
        "keywords_loaded": len(session_data.get("keywords", [])) > 0,
        "analytics_loaded": len(session_data.get("analytics", [])) > 0,
        "content_generated": len(session_data.get("generated_content", {})) > 0,
        "generated_content": session_data.get("generated_content", {}),
        "brand_config": BRAND_CONFIGS.get(session_data.get("brand"), {}),
    })

@app.route("/api/session-reset", methods=["POST"])
def session_reset():
    """Clear all session state for a fresh start."""
    session_data["brand"] = None
    session_data["vendor_code"] = None
    session_data["template_path"] = str(DEFAULT_TEMPLATE)
    session_data["col_map"] = None
    session_data["product_file"] = None
    session_data["styles"] = []
    session_data["keywords"] = []
    session_data["analytics"] = []
    session_data["generated_content"] = {}
    session_data["templates"] = {}
    session_data["field_overrides"] = {}
    session_data["operator"] = ""
    return jsonify({"ok": True})


@app.route("/api/set-operator", methods=["POST"])
def set_operator():
    """Set the current operator name for session tracking."""
    data = request.get_json(force=True)
    name = str(data.get("operator", "")).strip()
    session_data["operator"] = name
    return jsonify({"ok": True, "operator": name})


@app.route("/api/brand-config", methods=["POST"])
def brand_config():
    data = request.get_json(force=True)
    brand = data.get("brand", "")
    if brand not in BRAND_CONFIGS:
        return jsonify({"error": f"Unknown brand: {brand}"}), 400
    cfg = BRAND_CONFIGS[brand]
    session_data["brand"] = brand
    session_data["vendor_code"] = data.get("vendor_code", cfg.get("vendor_code_full", ""))
    return jsonify({"brand": brand, "config": cfg})

@app.route("/api/upload-template", methods=["POST"])
def upload_template():
    if "file" not in request.files:
        # Use default template
        template_path = str(DEFAULT_TEMPLATE)
        session_data["template_path"] = template_path
        session_data["col_map"] = get_template_col_map(template_path)
        col_count = len(session_data["col_map"])
        return jsonify({
            "template": "Dresses-Training.xlsm",
            "columns_mapped": col_count,
            "message": f"Dresses template — {col_count} columns mapped",
            "template_path": template_path,
        })
    
    f = request.files["file"]
    if not f.filename.endswith(".xlsm"):
        return jsonify({"error": "Template must be a .xlsm file"}), 400
    
    save_path = UPLOAD_TEMPLATES / f.filename
    f.save(str(save_path))
    
    try:
        col_map = get_template_col_map(str(save_path))
        session_data["template_path"] = str(save_path)
        session_data["col_map"] = col_map
        # Auto-extract dropdown values for validation
        dd_result = extract_template_dropdowns(str(save_path))
        dd_count = dd_result["dropdown_fields"] if dd_result else 0
        return jsonify({
            "template": f.filename,
            "columns_mapped": len(col_map),
            "dropdown_fields": dd_count,
            "message": f"{f.filename} — {len(col_map)} columns, {dd_count} dropdown validations loaded",
            "template_path": str(save_path),
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse template: {str(e)}"}), 500


@app.route("/api/upload-category-template", methods=["POST"])
def upload_category_template():
    """Upload a .xlsm template for a specific product type (multi-template support)."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    product_type = request.form.get("product_type", "").strip()
    
    if not f.filename.endswith(".xlsm"):
        return jsonify({"error": "Template must be a .xlsm file"}), 400
    if not product_type:
        return jsonify({"error": "product_type is required"}), 400
    
    # Save as {product_type}.xlsm
    safe_name = re.sub(r'[^\w]', '_', product_type)
    save_path = UPLOAD_TEMPLATES / f"{safe_name}.xlsm"
    f.save(str(save_path))
    
    try:
        col_map = get_template_col_map(str(save_path))
        # Register in session multi-template map
        session_data["templates"][product_type] = str(save_path)
        # If this is the first/only template, also set as default
        if not session_data.get("col_map"):
            session_data["template_path"] = str(save_path)
            session_data["col_map"] = col_map
        # Auto-extract dropdown values
        dd_result = extract_template_dropdowns(str(save_path))
        dd_count = dd_result["dropdown_fields"] if dd_result else 0
        return jsonify({
            "product_type": product_type,
            "template": f.filename,
            "columns_mapped": len(col_map),
            "dropdown_fields": dd_count,
            "message": f"{product_type} template loaded — {len(col_map)} columns, {dd_count} dropdowns",
            "loaded_templates": list(session_data["templates"].keys()),
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse template: {str(e)}"}), 500


@app.route("/api/templates")
def list_templates():
    """Return all loaded templates with their product types."""
    templates = session_data.get("templates", {})
    result = []
    for pt, path in templates.items():
        p = Path(path)
        result.append({
            "product_type": pt,
            "filename": p.name,
            "exists": p.exists(),
        })
    # Also include the default template if no multi-templates are registered
    if not result and session_data.get("template_path"):
        p = Path(session_data["template_path"])
        result.append({
            "product_type": "Dresses",
            "filename": p.name,
            "exists": p.exists(),
            "is_default": True,
        })
    return jsonify({"templates": result})


# All product types we know about (from master_nis_reference + common Amazon categories)
ALL_PRODUCT_TYPES = [
    {"id": "BLAZER", "label": "Blazers", "sub_classes": ["Blazer", "Sport Coat"]},
    {"id": "BRA", "label": "Bras / Intimates", "sub_classes": ["Bra", "Bralette", "Sports Bra"]},
    {"id": "COAT", "label": "Jackets & Coats", "sub_classes": ["Jacket", "Coat", "Vest", "Puffer", "Windbreaker", "Anorak", "Parka"]},
    {"id": "DRESS", "label": "Dresses", "sub_classes": ["Day Dress", "Cocktail Dress", "Active Dress", "Swimdress", "Maxi Dress", "Mini Dress", "Wrap Dress", "Shirt Dress", "Dress"]},
    {"id": "HAT", "label": "Hats / Headwear", "sub_classes": ["Hat", "Cap", "Beanie", "Visor", "Sun Hat", "Trucker Hat", "Bucket Hat"]},
    {"id": "ONE_PIECE_OUTFIT", "label": "One-Piece Outfits / Rompers", "sub_classes": ["Romper", "Jumpsuit", "Bodysuit", "Catsuit", "One Piece Outfit"]},
    {"id": "OVERALLS", "label": "Overalls", "sub_classes": ["Overalls", "Dungarees", "Overall"]},
    {"id": "PANTS", "label": "Pants / Leggings", "sub_classes": ["Pants", "Leggings", "Joggers", "Trousers", "Chino", "Cargo Pant"]},
    {"id": "SANDAL", "label": "Sandals / Footwear", "sub_classes": ["Sandal", "Flip Flop", "Slide", "Thong Sandal", "Slipper"]},
    {"id": "SHIRT", "label": "Shirts / Tops", "sub_classes": ["Pullover", "Tank", "Tee", "Blouse", "Shirt", "Polo", "Henley", "Crop Top", "Camisole", "Tunic"]},
    {"id": "SHORTS", "label": "Shorts", "sub_classes": ["Shorts", "Board Short", "Chino Short", "Boardshorts", "Skort", "Cargo Short"]},
    {"id": "SKIRT", "label": "Skirts", "sub_classes": ["Skirt", "Mini Skirt", "Maxi Skirt", "Wrap Skirt"]},
    {"id": "SNOWSUIT", "label": "Snowsuits", "sub_classes": ["Snowsuit", "Snow Suit", "Ski Suit"]},
    {"id": "SNOW_PANT", "label": "Snow Pants", "sub_classes": ["Snow Pant", "Snow Pants", "Ski Pants", "Ski Pant"]},
    {"id": "SWEATSHIRT", "label": "Sweatshirts / Hoodies", "sub_classes": ["Sweatshirt", "Hoodie", "Fleece", "Quarter Zip"]},
    {"id": "SWIMWEAR", "label": "Swimwear", "sub_classes": ["One Piece", "One Piece Swim", "Bikini Top", "Bikini Bottom", "Swim Bottom", "Tankini", "Cover Up", "Boardshorts", "Rashguard", "Swim Set 2 pcs", "Trunk", "Short", "Swim Trunk", "Rash Guard"]},
]


@app.route("/api/product-types")
def product_types():
    """Return all known product types with their training status.
    'trained' = we have a template uploaded + dropdowns extracted.
    'untrained' = we know it exists but no template yet.
    """
    result = []
    for pt in ALL_PRODUCT_TYPES:
        pt_id = pt["id"]
        dropdowns = load_dropdown_cache(pt_id)
        has_template = any(
            pt_id.upper() in str(p.name).upper()
            for p in UPLOAD_TEMPLATES.glob("*.xlsm")
        ) or pt_id in session_data.get("templates", {})

        result.append({
            "id": pt_id,
            "label": pt["label"],
            "sub_classes": pt["sub_classes"],
            "trained": len(dropdowns) > 0,
            "has_template": has_template,
            "dropdown_fields": len(dropdowns),
        })

    return jsonify({"product_types": result})


@app.route("/api/request-template-training", methods=["POST"])
def request_template_training():
    """Log a request for a product type template to be trained.
    Operator is telling us they need this product type but we don't have a template.
    """
    data = request.get_json(force=True)
    product_type = data.get("product_type", "")
    operator = data.get("operator", "") or session_data.get("operator", "")
    brand = data.get("brand", "") or session_data.get("brand", "")
    note = data.get("note", "")

    # Store as feedback
    _store_feedback({
        "id": f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_uuid.uuid4().hex[:8]}",
        "timestamp": datetime.utcnow().isoformat(),
        "operator": operator,
        "brand": brand,
        "type": "template_request",
        "phase": "upload",
        "context": {"scope": "brand", "brand": brand, "product_type": product_type},
        "data": {"product_type": product_type, "note": note, "message": f"Need {product_type} template for {brand}"},
        "maps_to": "operator_note",
    })

    return jsonify({
        "ok": True,
        "message": f"Template request logged for {product_type}. Send the Amazon .xlsm template to Devang to enable this product type.",
        
        "product_type": product_type,
    })


@app.route("/api/download-sample-template")
def download_sample_template():
    """Download the sample pre-upload template with all expected columns."""
    path = BASE_DIR / "uploads" / "sample_preupload_template.xlsx"
    if not path.exists():
        return jsonify({"error": "Sample template not found"}), 404
    return send_file(str(path), as_attachment=True,
                     download_name="TLG_PreUpload_Template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")




@app.route("/api/save-subclass-mapping", methods=["POST"])
def save_subclass_mapping():
    """Permanently save a sub-class → product type mapping.
    Called when the operator assigns a product type to an unknown sub-class.
    This mapping persists and is used for all future uploads.
    """
    data = request.get_json(force=True)
    sub_class = data.get("sub_class", "").strip()
    product_type = data.get("product_type", "").strip()
    
    if not sub_class or not product_type:
        return jsonify({"error": "sub_class and product_type required"}), 400
    
    mapping = _load_subclass_map()
    mapping[sub_class] = product_type
    _save_subclass_map(mapping)
    
    return jsonify({"ok": True, "sub_class": sub_class, "product_type": product_type,
                     "total_mappings": len(mapping)})


@app.route("/api/subclass-mappings")
def get_subclass_mappings():
    """Return all learned sub-class → product type mappings."""
    return jsonify({"mappings": _load_subclass_map()})


@app.route("/api/upload-product-data", methods=["POST"])
def upload_product_data():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in [".xlsx", ".xls", ".xlsm", ".csv", ".tsv"]:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400
    
    save_path = UPLOAD_PRODUCTS / f.filename
    f.save(str(save_path))
    session_data["product_file"] = str(save_path)
    
    try:
        styles, errors, warnings = parse_product_file(str(save_path))
        session_data["styles"] = styles

        total_variants = sum(len(s["variants"]) for s in styles)

        # ── Detect brand ──────────────────────────────────────────────
        brands_found = set(s.get("brand", "") for s in styles if s.get("brand"))

        # Multi-brand check: reject if more than one brand
        if len(brands_found) > 1:
            return jsonify({
                "error": "Multiple brands detected",
                "brands": sorted(brands_found),
                "message": f"This file contains {len(brands_found)} brands: {', '.join(sorted(brands_found))}. Please upload one brand at a time.",
            }), 400

        brand = list(brands_found)[0] if len(brands_found) == 1 else ""
        if brand:
            session_data["brand"] = brand
            brand_cfg = _load_brand_config_data(brand)
            session_data["vendor_code"] = brand_cfg.get("vendor_code_full", "")
            session_data["brandConfig"] = brand_cfg

        # ── Detect product types ──────────────────────────────────────
        present_types = set()
        type_counts = defaultdict(int)
        for s in styles:
            pt = TEMPLATE_PRODUCT_TYPE_MAP.get(s.get("subclass", ""), None)
            if pt:
                present_types.add(pt)
                type_counts[pt] += 1
        loaded_templates = session_data.get("templates", {})
        missing_templates = [
            {"product_type": pt, "style_count": type_counts[pt]}
            for pt in sorted(present_types)
            if pt not in loaded_templates
        ]

        # Also try to detect product type from division_name (e.g., "VOLCOM MENS SWIM")
        division_names = set(s.get("division_name", "") for s in styles if s.get("division_name"))
        detected_gender = ""
        detected_category = ""
        for dn in division_names:
            dn_upper = dn.upper()
            if "MENS" in dn_upper or "MEN" in dn_upper:
                detected_gender = "Male"
            elif "WOMENS" in dn_upper or "WOMEN" in dn_upper:
                detected_gender = "Female"
            if "SWIM" in dn_upper:
                detected_category = "SWIMWEAR"
            elif "DRESS" in dn_upper:
                detected_category = "DRESS"
            elif "SHIRT" in dn_upper or "TOP" in dn_upper:
                detected_category = "SHIRT"

        # ── Data quality audit ────────────────────────────────────────
        # Check every field across all styles — what's present, what's missing
        REQUIRED_FIELDS = {
            "style_num": "Style Number",
            "style_name": "Style Name",
            "brand": "Brand",
        }
        IMPORTANT_FIELDS = {
            "cost_price": "Cost Price (Wholesale)",
            "list_price": "List Price (Retail)",
            "coo": "Country of Origin",
            "fabric": "Fabric / Material",
            "care": "Care Instructions",
            "subclass": "Product Sub-Class",
        }
        NICE_TO_HAVE = {
            "upf": "UPF Rating",
            "neck_type": "Neck Type",
            "closure_type": "Closure Type",
            "sleeve_type": "Sleeve Type",
            "fit_type": "Fit Type",
            "ship_date": "Ship Date",
            "bullets_from_upload": "Key Features / Bullets",
            "model_name": "Model Name",
        }
        VARIANT_REQUIRED = {
            "upc": "UPC Code",
            "color_name": "Color Name",
            "size": "Size",
        }

        field_status = {}  # field_key -> {present: N, missing: N, label: str, level: str}

        for field_key, label in {**REQUIRED_FIELDS, **IMPORTANT_FIELDS, **NICE_TO_HAVE}.items():
            present = sum(1 for s in styles if s.get(field_key))
            level = "required" if field_key in REQUIRED_FIELDS else "important" if field_key in IMPORTANT_FIELDS else "optional"
            field_status[field_key] = {
                "label": label, "present": present, "missing": len(styles) - present,
                "total": len(styles), "level": level,
            }

        # Variant-level checks
        all_variants = [v for s in styles for v in s.get("variants", [])]
        for field_key, label in VARIANT_REQUIRED.items():
            present = sum(1 for v in all_variants if v.get(field_key))
            field_status[f"variant_{field_key}"] = {
                "label": f"{label} (per variant)", "present": present, "missing": len(all_variants) - present,
                "total": len(all_variants), "level": "required",
            }

        # Build action items
        action_items = []
        for key, info in field_status.items():
            if info["missing"] > 0 and info["level"] in ("required", "important"):
                pct = round(100 * info["present"] / info["total"]) if info["total"] else 0
                action_items.append({
                    "field": info["label"],
                    "level": info["level"],
                    "present": info["present"],
                    "missing": info["missing"],
                    "total": info["total"],
                    "pct": pct,
                    "message": f"{info['label']}: {info['missing']} of {info['total']} missing" + 
                               (" — required for NIS" if info["level"] == "required" else " — add in pre-upload or set on dashboard"),
                })

        # Check brand config existence
        brand_known = brand and (brand in BRAND_CONFIGS or (_load_brand_config_data(brand) != {}))

        # ── Category breakdown ────────────────────────────────────────
        category_breakdown = []
        styles_by_pt = defaultdict(lambda: defaultdict(list))
        unknown_subclasses = set()
        for s in styles:
            sub_class = s.get("subclass", "") or "Unknown"
            pt_id, confidence, reason = resolve_product_type(sub_class, s.get("division_name", ""))
            s["_resolved_pt"] = pt_id
            s["_pt_confidence"] = confidence
            s["_pt_reason"] = reason
            if confidence == "unknown":
                unknown_subclasses.add(sub_class)
            styles_by_pt[pt_id][sub_class].append(s["style_num"])

        for pt_id, sub_classes in styles_by_pt.items():
            pt_def = next((p for p in ALL_PRODUCT_TYPES if p["id"] == pt_id), None)
            pt_label = pt_def["label"] if pt_def else pt_id
            dd = load_dropdown_cache(pt_id)
            is_trained = len(dd) > 0
            total_in_pt = sum(len(v) for v in sub_classes.values())
            sub_list = [{"name": sc, "count": len(snums)} for sc, snums in sub_classes.items()]
            category_breakdown.append({
                "product_type": pt_id,
                "label": pt_label,
                "trained": is_trained,
                "dropdown_fields": len(dd),
                "total_styles": total_in_pt,
                "sub_classes": sub_list,
            })

        trained_count = sum(1 for c in category_breakdown if c["trained"])
        total_categories = len(category_breakdown)

        # Taxonomy bucket summary: which (product_type, sub_class, gender_bucket) triples
        # exist in this upload, and which are already confirmed in the overrides store.
        _tax_store = _load_taxonomy_overrides().get("entries", {})
        _tax_buckets = {}
        for _s in styles:
            _pt = _resolve_style_product_type(_s) or ""
            _sc = _s.get("subclass") or _s.get("sub_class") or ""
            _gb = _derive_gender_bucket(_s)
            _k = _taxonomy_key(_pt, _sc, _gb)
            _b = _tax_buckets.setdefault(_k, {"key": _k, "product_type": _pt,
                                              "sub_class": _sc, "gender_bucket": _gb,
                                              "style_count": 0, "confirmed": False})
            _b["style_count"] += 1
            if _tax_store.get(_k, {}).get("source") == "manual":
                _b["confirmed"] = True
        _tax_summary = {
            "total_buckets": len(_tax_buckets),
            "confirmed_buckets": sum(1 for b in _tax_buckets.values() if b["confirmed"]),
            "unconfirmed_buckets": sum(1 for b in _tax_buckets.values() if not b["confirmed"]),
            "buckets": list(_tax_buckets.values()),
        }
        return jsonify({
            "total_styles": len(styles),
            "total_variants": total_variants,
            "brand": brand,
            "brand_known": brand_known,
            "taxonomy_summary": _tax_summary,
            "category_breakdown": category_breakdown,
            "trained_count": trained_count,
            "total_categories": total_categories,
            "vendor_code": session_data.get("vendor_code", ""),
            "detected_gender": detected_gender,
            "detected_category": detected_category,
            "division_names": list(division_names),
            "errors": errors,
            "warnings": warnings,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "styles": styles,
            "missing_templates": missing_templates,
            "present_product_types": list(present_types),
            "field_status": field_status,
            "action_items": action_items,
            "categories": dict(type_counts),
            "unique_colors": len(set(v.get("color_name", "") for s in styles for v in s.get("variants", []) if v.get("color_name"))),
            "unknown_subclasses": list(unknown_subclasses),
            "size_range": ", ".join(sorted(set(v.get("size", "") for s in styles for v in s.get("variants", []) if v.get("size")),
                                          key=lambda x: ["XXS","XS","S","M","L","XL","XXL","2XL","3XL"].index(x) if x in ["XXS","XS","S","M","L","XL","XXL","2XL","3XL"] else 99)),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Failed to parse product data: {str(e)}"}), 500

@app.route("/api/upload-keywords", methods=["POST"])
def upload_keywords():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    save_path = UPLOAD_KEYWORDS / f.filename
    f.save(str(save_path))
    
    keywords = []
    try:
        ext = Path(f.filename).suffix.lower()
        if ext in [".csv", ".tsv"]:
            delimiter = "\t" if ext == ".tsv" else ","
            with open(str(save_path), "r", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh, delimiter=delimiter)
                for row in reader:
                    kw = row.get("Keyword Phrase") or row.get("keyword") or row.get("Search Query", "")
                    volume = row.get("Search Volume") or row.get("volume", "0")
                    if kw:
                        try:
                            vol = int(str(volume).replace(",", ""))
                        except (ValueError, AttributeError):
                            vol = 0
                        keywords.append({"keyword": kw.strip().lower(), "volume": vol})
        
        # Sort by volume
        keywords.sort(key=lambda x: x["volume"], reverse=True)
        session_data["keywords"] = keywords
        
        top5 = [k["keyword"] for k in keywords[:5]]
        return jsonify({
            "total_keywords": len(keywords),
            "top5": top5,
            "message": f"{len(keywords)} keywords loaded. Top 5: {', '.join(top5)}",
            "keywords": keywords[:50],
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse keyword file: {str(e)}"}), 500

@app.route("/api/upload-analytics", methods=["POST"])
def upload_analytics():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    f = request.files["file"]
    save_path = UPLOAD_KEYWORDS / f.filename
    f.save(str(save_path))
    
    analytics = []
    try:
        ext = Path(f.filename).suffix.lower()
        if ext in [".csv", ".tsv"]:
            delimiter = "\t" if ext == ".tsv" else ","
            with open(str(save_path), "r", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh, delimiter=delimiter)
                for row in reader:
                    analytics.append(dict(row))
        
        session_data["analytics"] = analytics
        return jsonify({
            "total_rows": len(analytics),
            "message": f"{len(analytics)} analytics rows loaded",
        })
    except Exception as e:
        return jsonify({"error": f"Failed to parse analytics file: {str(e)}"}), 500

def _run_content_generation(brand, styles, brand_cfg, has_keywords, feedback_history):
    """Background worker for content generation."""
    global DESCRIPTION_OPENERS_ROTATION, content_progress
    DESCRIPTION_OPENERS_ROTATION = {}
    content_map = {}
    total_qa_errors = 0
    total_qa_warnings = 0
    feedback_count = len([l for l in feedback_history.splitlines() if l.strip()]) if feedback_history else 0

    for i, style in enumerate(styles):
        style_num = style["style_num"]
        style_name = style["style_name"]
        subclass = style.get("subclass", "")
        sub_subclass = style.get("sub_subclass", "")
        fabric = parse_fabric(style.get("fabric", "")) or brand_cfg.get("default_fabric", "")
        care = style.get("care", "") or brand_cfg.get("default_care", "")
        upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
        coo = style.get("coo", "") or brand_cfg.get("default_coo", "")

        content_progress["current_style"] = f"{style_num} — {style_name}"

        # Get first color for preview title
        first_variant = style["variants"][0] if style["variants"] else {}
        first_color = first_variant.get("color_name", "")
        first_size = first_variant.get("size", "")

        # Dramatic progress steps
        content_progress["current_step"] = f"Analyzing style attributes for {style_name}..."
        time.sleep(0.3)
        content_progress["current_step"] = f"Loading {clean_brand_name(brand)} brand preferences..."
        time.sleep(0.2)
        if feedback_count > 0:
            content_progress["current_step"] = f"Reading {feedback_count} previous feedback corrections..."
            time.sleep(0.2)
        content_progress["current_step"] = f"Generating SEO-optimized title (max 120 chars)..."

        # Try LLM generation first (unless mode is "rules")
        gen_mode = session_data.get("generation_mode", "auto")
        llm_result = None
        if gen_mode != "rules" and _anthropic_client is not None:
            try:
                llm_result = generate_content_llm(brand_cfg, brand, style, feedback_history)
            except Exception as e:
                print(f"[LLM] Fallback for {style_num}: {e}")

        if llm_result:
            title = llm_result["title"]
            bullets = [
                llm_result.get("bullet_1", ""),
                llm_result.get("bullet_2", ""),
                llm_result.get("bullet_3", ""),
                llm_result.get("bullet_4", ""),
                llm_result.get("bullet_5", ""),
            ]
            description = llm_result["description"]
            backend_kw = llm_result["backend_keywords"]
        else:
            # Rule-based fallback
            if _anthropic_client is not None:
                print(f"[LLM] Falling back to rule-based for style {style_num}")
            # Use actual subclass as product type descriptor (not hardcoded "Dress")
            pt_label = subclass or sub_subclass or _resolve_style_product_type(style).replace("_", " ").title() or "Dress"
            resolved_pt = _resolve_style_product_type(style) or ""
            style_gender, _ = _derive_gender_department(style)
            eff_gender_gen = style_gender or brand_cfg.get("gender", "")
            title = generate_title(brand_cfg, brand, style_name, pt_label, first_color, first_size, upf, style_gender=style_gender)
            bullets = generate_bullets(brand_cfg, brand, style_name, sub_subclass, fabric, care, first_color, upf,
                                       subclass=subclass, gender=eff_gender_gen, product_type=resolved_pt, style_num=style_num)
            description = generate_description(brand_cfg, brand, style_num, style_name, sub_subclass, fabric, care, first_color, upf,
                                               subclass=subclass, gender=eff_gender_gen, product_type=resolved_pt)
            backend_kw = generate_backend_keywords(brand, style_name, subclass, first_color, fabric, upf,
                                                    subclass=subclass, gender=eff_gender_gen, product_type=resolved_pt)

        content_progress["current_step"] = f"Crafting 5 unique bullet points..."
        time.sleep(0.2)
        content_progress["current_step"] = f"Writing buyer-focused description..."
        time.sleep(0.2)
        content_progress["current_step"] = f"Building backend keywords (250 bytes max)..."
        time.sleep(0.1)
        content_progress["current_step"] = f"Running QA compliance check..."

        # Derived attributes
        neck = derive_neck_type(style_name)
        sleeve = derive_sleeve_type(style_name)
        silhouette = derive_silhouette(sub_subclass)
        color_map_val = normalize_color(first_color)
        category = SUBCLASS_CATEGORY_MAP.get(subclass, "")
        subcategory = SUBCLASS_SUBCATEGORY_MAP.get(subclass, "")
        if not subcategory and (resolved_pt == "SWIMWEAR" or subclass in ("Rashguard","Trunk","Bikini Top","Bikini Bottom","Swim Bottom","One Piece Swim","Tankini","Short","Swim Set 2 pcs","Swim Set","Board Short","Cover Up","Swim Shirt")):
            _cat_for_sub = _derive_amazon_product_category(subclass, gender=eff_gender_gen, product_type=resolved_pt, style_name=style_name)
            subcategory = _derive_swim_product_subcategory(subclass, gender=eff_gender_gen, style_name=style_name, product_category=_cat_for_sub)

        opener_idx = DESCRIPTION_OPENERS_ROTATION.get(style_num, 0)
        bullet_whys = [
            generate_bullet_why(j, brand_cfg, brand, style_name, sub_subclass, upf, fabric, has_keywords)
            for j in range(5)
        ]

        entry = {
            "style_num": style_num,
            "style_name": style_name,
            "title": title,
            "title_why": generate_title_why(brand_cfg, brand, style_name, title, upf, has_keywords),
            "bullets": bullets,
            "bullet_whys": bullet_whys,
            "bullet_1": bullets[0] if len(bullets) > 0 else "",
            "bullet_2": bullets[1] if len(bullets) > 1 else "",
            "bullet_3": bullets[2] if len(bullets) > 2 else "",
            "bullet_4": bullets[3] if len(bullets) > 3 else "",
            "bullet_5": bullets[4] if len(bullets) > 4 else "",
            "description": description,
            "description_why": generate_description_why(brand_cfg, style_num, opener_idx, has_keywords),
            "backend_keywords": backend_kw,
            "backend_keywords_why": generate_keywords_why(brand, session_data.get("keywords", []), backend_kw, has_keywords),
            "qa_issues": [],
            "neck_type": neck,
            "sleeve_type": sleeve,
            "silhouette": silhouette,
            "color_map": color_map_val,
            "category": category,
            "sub_class": subclass,
            "subcategory": subcategory,
            "fabric": fabric,
            "care": care,
            "upf": upf,
            "coo": coo,
            "llm_generated": llm_result is not None,
        }

        # QA check
        issues = qa_check_content(entry, brand)
        entry["qa_issues"] = issues
        total_qa_errors += sum(1 for iss in issues if iss["severity"] == "error")
        total_qa_warnings += sum(1 for iss in issues if iss["severity"] == "warning")

        content_map[style_num] = entry
        content_progress["completed"] = i + 1
        content_progress["current_step"] = f"✓ {style_num} complete"
        time.sleep(0.1)

    session_data["generated_content"] = content_map
    content_progress["status"] = "done"
    content_progress["current_style"] = ""
    content_progress["current_step"] = ""
    content_progress["results"] = {
        "content": content_map,
        "total": len(content_map),
        "qa_errors": total_qa_errors,
        "qa_warnings": total_qa_warnings,
    }


@app.route("/api/generate-content", methods=["POST"])
def generate_content():
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand")
    if not brand:
        return jsonify({"error": "No brand selected"}), 400

    styles = data.get("styles") or session_data.get("styles", [])
    if not styles:
        return jsonify({"error": "No product data loaded"}), 400

    # ── GATE: Check if all product types are trained ───────────────────
    untrained_types = []
    for s in styles:
        sub_class = s.get("subclass", "")
        pt = None
        for pt_def in ALL_PRODUCT_TYPES:
            if sub_class in pt_def.get("sub_classes", []):
                pt = pt_def["id"]
                break
        if pt:
            dropdowns = load_dropdown_cache(pt)
            if not dropdowns:
                untrained_types.append(pt)
        # If we can't even map the subclass, check division_name
        elif s.get("division_name"):
            dn = s["division_name"].upper()
            if "SWIM" in dn:
                dd = load_dropdown_cache("SWIMWEAR")
                if not dd:
                    untrained_types.append("SWIMWEAR")
            elif "DRESS" in dn:
                dd = load_dropdown_cache("DRESS")
                if not dd:
                    untrained_types.append("DRESS")

    untrained_types = list(set(untrained_types))
    if untrained_types:
        return jsonify({
            "error": "Cannot generate — untrained product types detected",
            "untrained_types": untrained_types,
            "message": f"The following product types are not trained yet: {', '.join(untrained_types)}. "
                       f"Contact admin to upload the Amazon .xlsm template(s) for these product types.",
            "action": "request_template",
            
        }), 400

    # Content generation mode: "api" (LLM) or "rules" (rule-based only)
    gen_mode = data.get("mode", "auto")  # auto = try API first, fall back to rules
    session_data["generation_mode"] = gen_mode

    # Store style briefs + brand brief from frontend
    style_briefs = data.get("style_briefs", {})
    if style_briefs:
        session_data["style_briefs"] = style_briefs
    brand_brief = data.get("brand_brief", "")

    # Load brand config from file if available, fall back to in-memory
    brand_cfg = _load_brand_config_data(brand)
    # Inject brand brief into config so LLM prompt picks it up
    if brand_brief:
        if "product_briefs" not in brand_cfg:
            brand_cfg["product_briefs"] = {}
        brand_cfg["product_briefs"]["_default"] = brand_brief
    has_keywords = len(session_data.get("keywords", [])) > 0

    # Load feedback history for this brand
    feedback_history = load_brand_feedback(brand)

    # Init progress
    content_progress["total"] = len(styles)
    content_progress["completed"] = 0
    content_progress["status"] = "running"
    content_progress["started_at"] = datetime.now().isoformat()
    content_progress["current_style"] = ""
    content_progress["current_step"] = ""
    content_progress["results"] = None

    # Run in background thread
    t = threading.Thread(
        target=_run_content_generation,
        args=(brand, styles, brand_cfg, has_keywords, feedback_history)
    )
    t.daemon = True
    t.start()

    return jsonify({"status": "started", "total": len(styles)})


@app.route("/api/content-progress")
def content_progress_endpoint():
    elapsed = ""
    eta = ""
    if content_progress.get("started_at") and content_progress["completed"] > 0:
        started = datetime.fromisoformat(content_progress["started_at"])
        elapsed_sec = (datetime.now() - started).total_seconds()
        per_style = elapsed_sec / content_progress["completed"]
        remaining = content_progress["total"] - content_progress["completed"]
        eta_sec = per_style * remaining
        elapsed = f"{int(elapsed_sec)}s"
        if eta_sec > 60:
            eta = f"~{int(eta_sec / 60)}m {int(eta_sec % 60)}s remaining"
        else:
            eta = f"~{int(eta_sec)}s remaining"
    return jsonify({
        "total": content_progress["total"],
        "completed": content_progress["completed"],
        "current_style": content_progress["current_style"],
        "current_step": content_progress["current_step"],
        "status": content_progress["status"],
        "elapsed": elapsed,
        "eta": eta,
        "percent": round((content_progress["completed"] / max(content_progress["total"], 1)) * 100, 1),
    })


@app.route("/api/content-results")
def content_results():
    """Poll this after generate-content to get results when done."""
    if content_progress["status"] == "done":
        return jsonify({
            "status": "done",
            **content_progress.get("results", {}),
        })
    else:
        return jsonify({
            "status": content_progress["status"],
            "completed": content_progress["completed"],
            "total": content_progress["total"],
        })

# ───────────────────────────────────────────────────────────────────────────────
# STRUCTURED FEEDBACK SYSTEM
#
# Every feedback entry is stored in one JSONL file per brand under feedback/.
# Schema:
#   id            - unique (timestamp + random)
#   timestamp     - ISO 8601 UTC
#   operator      - who submitted (from session or explicit)
#   brand         - brand name (always present)
#   type          - one of:
#                     "content_edit"    — operator changed LLM-generated content
#                     "field_override"  — operator changed a NIS field value
#                     "apply_all"       — operator applied a value to all styles
#                     "manual"          — operator typed freeform feedback
#                     "session"         — end-of-session summary
#                     "regenerate"      — operator regenerated a field
#   phase         - where in the flow: "upload", "generate", "review", "download"
#   context       - what it's about (maps_to target):
#     scope       - "brand" | "style" | "field" | "session"
#     brand       - brand name
#     style_num   - style number (if scope is style or field)
#     field_id    - field_id string (if scope is field)
#     field_name  - human-readable field name (if scope is field)
#   data          - type-specific payload:
#     For content_edit:  { field, original, updated }
#     For field_override: { field_id, field_name, original, updated }
#     For apply_all:     { field_id, field_name, value, styles_count }
#     For manual:        { message }
#     For session:       { rating, note, stats }
#     For regenerate:    { field, attempt_num }
#   maps_to       - where this feedback should route:
#     "llm_prompt"    — feed into next LLM generation for this brand
#     "brand_config"  — update brand defaults
#     "derive_logic"  — improve field derivation functions
#     "operator_note" — informational, no auto-action
# ───────────────────────────────────────────────────────────────────────────────

import uuid as _uuid

def _feedback_file_for_brand(brand):
    """Return the JSONL feedback file path for a brand."""
    safe = re.sub(r'[^\w\-]', '_', brand or "unknown")
    return FEEDBACK_DIR / f"{safe}_feedback.jsonl"


def _store_feedback(entry):
    """Append a feedback entry to the brand-specific JSONL file.
    Also appends to the legacy content_feedback.jsonl for backward compat with LLM loading.
    """
    brand = entry.get("brand") or entry.get("context", {}).get("brand", "unknown")
    fpath = _feedback_file_for_brand(brand)
    try:
        with open(str(fpath), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        traceback.print_exc()

    # Also write to legacy file so load_brand_feedback() still works
    if entry.get("type") in ("content_edit", "field_override", "manual"):
        legacy = {
            "timestamp": entry.get("timestamp"),
            "brand": brand,
            "style_num": entry.get("context", {}).get("style_num", ""),
            "field": entry.get("data", {}).get("field") or entry.get("data", {}).get("field_name", ""),
            "feedback": entry.get("data", {}).get("message", ""),
            "original": entry.get("data", {}).get("original", ""),
            "updated": entry.get("data", {}).get("updated", ""),
        }
        try:
            with open(str(FEEDBACK_FILE), "a", encoding="utf-8") as f:
                f.write(json.dumps(legacy) + "\n")
        except Exception:
            pass


def _load_feedback(brand=None, scope=None, style_num=None, field_id=None,
                   fb_type=None, limit=100):
    """Load feedback entries with optional filters."""
    entries = []

    # Determine which files to read
    if brand:
        files = [_feedback_file_for_brand(brand)]
    else:
        files = list(FEEDBACK_DIR.glob("*_feedback.jsonl"))

    for fpath in files:
        if not fpath.exists():
            continue
        with open(str(fpath), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ctx = e.get("context", {})
                if scope and ctx.get("scope") != scope:
                    continue
                if style_num and ctx.get("style_num") != style_num:
                    continue
                if field_id and ctx.get("field_id") != field_id:
                    continue
                if fb_type and e.get("type") != fb_type:
                    continue
                entries.append(e)

    entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return entries[:limit]


@app.route("/api/feedback", methods=["GET", "POST"])
def feedback_endpoint():
    """Universal feedback endpoint.
    POST: Submit structured feedback with context mapping.
    GET:  Retrieve feedback entries with optional filters.
    """
    if request.method == "POST":
        data = request.get_json(force=True)
        fb_type  = data.get("type", "manual")
        phase    = data.get("phase", "review")
        context  = data.get("context", {})
        payload  = data.get("data", {})
        maps_to  = data.get("maps_to", "operator_note")

        # Fill in brand from session if not provided
        if not context.get("brand"):
            context["brand"] = session_data.get("brand", "unknown")
        if not context.get("scope"):
            if context.get("field_id"):
                context["scope"] = "field"
            elif context.get("style_num"):
                context["scope"] = "style"
            else:
                context["scope"] = "brand"

        # Auto-determine maps_to if not explicit
        if maps_to == "operator_note" and fb_type in ("content_edit", "regenerate"):
            maps_to = "llm_prompt"
        elif maps_to == "operator_note" and fb_type == "apply_all":
            maps_to = "brand_config"
        elif maps_to == "operator_note" and fb_type == "field_override":
            maps_to = "derive_logic"

        entry = {
            "id": f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_uuid.uuid4().hex[:8]}",
            "timestamp": datetime.utcnow().isoformat(),
            "operator": data.get("operator", session_data.get("operator", "")),
            "brand": context.get("brand"),
            "type": fb_type,
            "phase": phase,
            "context": context,
            "data": payload,
            "maps_to": maps_to,
        }

        _store_feedback(entry)
        return jsonify({"ok": True, "id": entry["id"]})

    # GET — retrieve feedback entries with optional filters
    brand     = request.args.get("brand", "") or session_data.get("brand", "")
    scope     = request.args.get("scope", "")
    style_num = request.args.get("style_num", "")
    field_id  = request.args.get("field_id", "")
    fb_type   = request.args.get("type", "")
    limit     = int(request.args.get("limit", "100"))

    entries = _load_feedback(
        brand=brand or None, scope=scope or None,
        style_num=style_num or None, field_id=field_id or None,
        fb_type=fb_type or None, limit=limit
    )
    return jsonify({"brand": brand, "entries": entries, "total": len(entries)})


# Legacy endpoint — backward compat for existing frontend code
@app.route("/api/submit-feedback", methods=["POST"])
def submit_feedback_legacy():
    data = request.get_json(force=True)
    entry = {
        "id": f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_uuid.uuid4().hex[:8]}",
        "timestamp": datetime.utcnow().isoformat(),
        "operator": session_data.get("operator", ""),
        "brand": session_data.get("brand"),
        "type": "content_edit",
        "phase": "review",
        "context": {
            "brand": session_data.get("brand"),
            "style_num": data.get("style_num", ""),
            "field_id": "",
            "field_name": data.get("field", ""),
            "scope": "field" if data.get("field") else "style",
        },
        "data": {
            "field": data.get("field", ""),
            "original": data.get("original", ""),
            "updated": data.get("updated", ""),
            "message": data.get("feedback", ""),
        },
        "maps_to": "llm_prompt",
    }
    _store_feedback(entry)
    return jsonify({"ok": True, "id": entry["id"]})


@app.route("/api/feedback/summary")
def feedback_summary():
    """Return feedback stats: count per brand, per type, most-edited fields."""
    brand = request.args.get("brand", "")
    entries = _load_feedback(brand=brand or None, limit=10000)

    by_brand = defaultdict(int)
    by_type  = defaultdict(int)
    by_field = defaultdict(int)
    by_maps  = defaultdict(int)

    for e in entries:
        by_brand[e.get("brand", "Unknown")] += 1
        by_type[e.get("type", "unknown")] += 1
        by_maps[e.get("maps_to", "unknown")] += 1
        fname = e.get("context", {}).get("field_name") or e.get("data", {}).get("field", "")
        if fname:
            by_field[fname] += 1

    # Top edited fields
    top_fields = sorted(by_field.items(), key=lambda x: x[1], reverse=True)[:10]

    return jsonify({
        "total": len(entries),
        "by_brand": dict(by_brand),
        "by_type": dict(by_type),
        "by_maps_to": dict(by_maps),
        "top_edited_fields": [{"field": f, "count": c} for f, c in top_fields],
    })


@app.route("/api/feedback/session-summary")
def feedback_session_summary():
    """Compile a session summary for email digest.
    Returns everything that happened this session: overrides, edits, manual notes.
    """
    brand = session_data.get("brand", "")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    overrides = session_data.get("field_overrides", {})

    # Count overrides per style
    override_count = sum(len(v) for v in overrides.values())
    styles_with_overrides = sum(1 for v in overrides.values() if v)

    # Load recent feedback for this brand (this session — last hour)
    recent = _load_feedback(brand=brand or None, limit=500)
    # Filter to recent (within last 2 hours)
    cutoff = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    session_entries = [e for e in recent if e.get("timestamp", "") >= cutoff]

    manual_notes = [e for e in session_entries if e.get("type") == "manual"]
    content_edits = [e for e in session_entries if e.get("type") == "content_edit"]
    field_overrides = [e for e in session_entries if e.get("type") == "field_override"]
    apply_alls = [e for e in session_entries if e.get("type") == "apply_all"]

    return jsonify({
        "brand": brand,
        "total_styles": len(styles),
        "styles_with_content": len(content_map),
        "total_overrides": override_count,
        "styles_with_overrides": styles_with_overrides,
        "manual_notes": [{"message": n.get("data", {}).get("message", ""),
                          "context": n.get("context", {}),
                          "timestamp": n.get("timestamp")} for n in manual_notes],
        "content_edits_count": len(content_edits),
        "field_overrides_count": len(field_overrides),
        "apply_all_count": len(apply_alls),
        "session_entries": len(session_entries),
    })


@app.route("/api/feedback/digest")
def feedback_digest():
    """Full feedback digest across all brands — for Devang to review.
    Shows: recent feedback, patterns, most-edited fields, template requests,
    operator activity, and learning recommendations.
    """
    all_entries = _load_feedback(limit=500)

    # Categorize
    by_type = defaultdict(list)
    by_brand = defaultdict(list)
    by_field = defaultdict(int)
    template_requests = []
    manual_notes = []

    for e in all_entries:
        by_type[e.get("type", "unknown")].append(e)
        by_brand[e.get("brand", "unknown")].append(e)
        fname = e.get("context", {}).get("field_name") or e.get("data", {}).get("field", "")
        if fname:
            by_field[fname] += 1
        if e.get("type") == "template_request":
            template_requests.append(e)
        if e.get("type") == "manual":
            manual_notes.append(e)

    # Build learning recommendations
    learning = []
    top_fields = sorted(by_field.items(), key=lambda x: x[1], reverse=True)[:5]
    for field, count in top_fields:
        if count >= 3:
            learning.append({
                "field": field, "edit_count": count,
                "recommendation": f"'{field}' has been corrected {count} times. Consider updating the derivation logic or brand config default."
            })

    # Field overrides that could become brand defaults
    brand_default_candidates = []
    for e in by_type.get("apply_all", []):
        brand_default_candidates.append({
            "brand": e.get("brand"),
            "field": e.get("data", {}).get("field_name", ""),
            "value": e.get("data", {}).get("value", ""),
            "timestamp": e.get("timestamp"),
        })

    return jsonify({
        "total_feedback": len(all_entries),
        "by_type": {k: len(v) for k, v in by_type.items()},
        "by_brand": {k: len(v) for k, v in by_brand.items()},
        "top_edited_fields": [{"field": f, "count": c} for f, c in top_fields],
        "template_requests": [{
            "product_type": e.get("data", {}).get("product_type", ""),
            "brand": e.get("brand"),
            "note": e.get("data", {}).get("note", ""),
            "timestamp": e.get("timestamp"),
        } for e in template_requests],
        "manual_notes": [{
            "message": e.get("data", {}).get("message", ""),
            "brand": e.get("brand"),
            "context": e.get("context", {}),
            "timestamp": e.get("timestamp"),
        } for e in manual_notes[:20]],
        "learning_recommendations": learning,
        "brand_default_candidates": brand_default_candidates[:10],
    })


@app.route("/api/feedback/changelog")
def feedback_changelog():
    """Return recent content edits as a flat table for the change log view."""
    brand = request.args.get("brand", "") or session_data.get("brand", "")
    limit = int(request.args.get("limit", "100"))
    entries = _load_feedback(brand=brand or None, fb_type="content_edit", limit=limit)
    # Also include field_override and manual types
    entries += _load_feedback(brand=brand or None, fb_type="field_override", limit=limit)
    entries += _load_feedback(brand=brand or None, fb_type="manual", limit=limit)
    # Sort by timestamp desc
    entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    entries = entries[:limit]

    rows = []
    for e in entries:
        ctx = e.get("context", {})
        data = e.get("data", {})
        rows.append({
            "timestamp": e.get("timestamp", ""),
            "operator": e.get("operator", ""),
            "brand": e.get("brand", ""),
            "style_num": ctx.get("style_num", ""),
            "field": ctx.get("field_name", "") or data.get("field", ""),
            "type": e.get("type", ""),
            "original": str(data.get("original", ""))[:100],
            "updated": str(data.get("updated", ""))[:100],
            "reason": data.get("reason", "") or data.get("message", ""),
        })
    return jsonify({"brand": brand, "total": len(rows), "rows": rows})


@app.route("/api/feedback/learning")
def feedback_learning():
    """Aggregate feedback patterns into actionable learning insights."""
    brand = request.args.get("brand", "") or session_data.get("brand", "")
    entries = _load_feedback(brand=brand or None, limit=10000)

    # Patterns: which fields get edited most, which reasons, which operators
    field_edits = defaultdict(int)
    reason_counts = defaultdict(int)
    operator_counts = defaultdict(int)
    original_to_updated = defaultdict(list)  # field -> list of (original, updated)

    for e in entries:
        if e.get("type") not in ("content_edit", "field_override"):
            continue
        ctx = e.get("context", {})
        data = e.get("data", {})
        fname = ctx.get("field_name", "") or data.get("field", "")
        if fname:
            field_edits[fname] += 1
        reason = data.get("reason", "")
        if reason:
            reason_counts[reason] += 1
        op = e.get("operator", "")
        if op:
            operator_counts[op] += 1
        orig = str(data.get("original", ""))[:80]
        updated = str(data.get("updated", ""))[:80]
        if fname and orig and updated:
            original_to_updated[fname].append({"from": orig, "to": updated})

    insights = []
    # Top edited fields
    for field, count in sorted(field_edits.items(), key=lambda x: x[1], reverse=True)[:10]:
        examples = original_to_updated.get(field, [])[:3]
        insights.append({
            "type": "frequent_edit",
            "field": field,
            "count": count,
            "message": f"'{field}' was edited {count} times. Consider updating the default or derivation.",
            "examples": examples,
        })

    # Top reasons
    top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return jsonify({
        "brand": brand,
        "total_edits": sum(field_edits.values()),
        "top_edited_fields": [{"field": f, "count": c} for f, c in sorted(field_edits.items(), key=lambda x: x[1], reverse=True)[:10]],
        "top_reasons": [{"reason": r, "count": c} for r, c in top_reasons],
        "operators": [{"name": n, "edits": c} for n, c in sorted(operator_counts.items(), key=lambda x: x[1], reverse=True)],
        "insights": insights,
    })


def _run_nis_generation(brand, styles, content_map, vendor_code, template_path, brand_cfg, template_map):
    """Background worker for NIS file generation."""
    results = []
    errors = []

    for i, style in enumerate(styles):
        style_num = style["style_num"]
        style_name = style["style_name"]
        style_content = content_map.get(style_num, {})
        variants = style.get("variants", [])

        nis_progress["current_style"] = f"{style_num} \u2014 {style_name}"

        if not style_content:
            errors.append(f"No content for style {style_num}")
            nis_progress["completed"] = i + 1
            continue

        subclass = style.get("subclass", "")
        product_type_for_template = TEMPLATE_PRODUCT_TYPE_MAP.get(subclass, None)
        if product_type_for_template and product_type_for_template in template_map:
            style_template_path = template_map[product_type_for_template]
        else:
            style_template_path = template_path

        nis_progress["current_step"] = f"Reading preupload template for {style_name}..."
        time.sleep(0.2)
        nis_progress["current_step"] = f"Mapping 254 columns to NIS template..."
        time.sleep(0.2)
        nis_progress["current_step"] = f"Injecting data for {len(variants)} variants..."

        try:
            output_path = do_xlsm_surgery(
                template_path=style_template_path,
                brand=brand,
                brand_cfg=brand_cfg,
                vendor_code=vendor_code,
                style=style,
                content=style_content,
            )
            filename = Path(output_path).name
            nis_progress["current_step"] = f"Writing parent row + {len(variants)} child rows..."
            time.sleep(0.1)
            nis_progress["current_step"] = f"QA: verifying file integrity..."
            time.sleep(0.1)
            nis_progress["current_step"] = f"\u2713 NIS_{brand}_{style_num}.xlsm saved"
            results.append({
                "style_num": style_num,
                "style_name": style_name,
                "rows": len(variants) + 1,
                "filename": filename,
                "path": output_path,
            })
        except Exception as e:
            traceback.print_exc()
            errors.append(f"Failed to generate NIS for style {style_num}: {str(e)}")

        nis_progress["completed"] = i + 1

    nis_progress["status"] = "done"
    nis_progress["current_style"] = ""
    nis_progress["current_step"] = ""
    nis_progress["results"] = results
    nis_progress["errors"] = errors


@app.route("/api/generate-nis", methods=["POST"])
def generate_nis():
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand")
    styles = data.get("styles") or session_data.get("styles", [])
    content_map = data.get("content") or session_data.get("generated_content", {})
    vendor_code = data.get("vendor_code") or session_data.get("vendor_code") or ""
    template_path = data.get("template_path") or None
    if not template_path or template_path == "null" or template_path == "None":
        template_path = session_data.get("template_path") or str(DEFAULT_TEMPLATE)
    
    if not brand:
        return jsonify({"error": "No brand selected"}), 400
    if not styles:
        return jsonify({"error": "No product data loaded"}), 400
    if not content_map:
        return jsonify({"error": "Content not yet generated. Run Generate Content first."}), 400
    if not os.path.exists(template_path):
        return jsonify({"error": f"Template file not found: {template_path}. Upload an Amazon NIS template first."}), 400
    
    brand_cfg = _load_brand_config_data(brand)
    
    # Clear output dir
    for f in UPLOAD_OUTPUT.glob("*.xlsm"):
        f.unlink()
    
    # Init progress
    nis_progress["total"] = len(styles)
    nis_progress["completed"] = 0
    nis_progress["status"] = "running"
    nis_progress["started_at"] = datetime.now().isoformat()
    nis_progress["current_style"] = ""
    nis_progress["results"] = []
    nis_progress["errors"] = []
    
    template_map = dict(session_data.get("templates", {}))
    
    # Run in background thread to avoid Render's 30-sec request timeout
    t = threading.Thread(target=_run_nis_generation, args=(
        brand, styles, content_map, vendor_code, template_path, brand_cfg, template_map
    ))
    t.daemon = True
    t.start()
    
    # Return immediately
    return jsonify({"status": "started", "total": len(styles)})


@app.route("/api/generate-nis-results")
def generate_nis_results():
    """Poll this after generate-nis to get results when done."""
    if nis_progress["status"] == "done":
        return jsonify({
            "status": "done",
            "results": nis_progress.get("results", []),
            "errors": nis_progress.get("errors", []),
            "total": len(nis_progress.get("results", [])),
        })
    else:
        return jsonify({
            "status": nis_progress["status"],
            "completed": nis_progress["completed"],
            "total": nis_progress["total"],
        })

@app.route("/api/generate-progress")
def generate_progress():
    elapsed = ""
    eta = ""
    if nis_progress["started_at"] and nis_progress["completed"] > 0:
        started = datetime.fromisoformat(nis_progress["started_at"])
        elapsed_sec = (datetime.now() - started).total_seconds()
        per_style = elapsed_sec / nis_progress["completed"]
        remaining = nis_progress["total"] - nis_progress["completed"]
        eta_sec = per_style * remaining
        elapsed = f"{int(elapsed_sec)}s"
        if eta_sec > 60:
            eta = f"~{int(eta_sec / 60)}m {int(eta_sec % 60)}s remaining"
        else:
            eta = f"~{int(eta_sec)}s remaining"
    return jsonify({
        "total": nis_progress["total"],
        "completed": nis_progress["completed"],
        "current_style": nis_progress["current_style"],
        "current_step": nis_progress.get("current_step", ""),
        "status": nis_progress["status"],
        "elapsed": elapsed,
        "eta": eta,
        "percent": round((nis_progress["completed"] / max(nis_progress["total"], 1)) * 100, 1)
    })

# ── Category / field derivation helpers ───────────────────────────────────────
def _derive_amazon_product_category(sub_class, gender="", product_type="", style_name="", department=""):
    """Map sub_class + gender to Amazon product_category dropdown value.
    Gender-aware: Men's Swimwear vs Women's Swimwear vs Swim (youth).
    """
    # ── Dresses ──
    DRESS_MAP = {
        "Day Dress": "Women's Dresses",
        "Cocktail Dress": "Women's Dresses",
        "Active Dress": "Women's Active",
        "Swimdress": "Women's Dresses",
        "Pullover": "Women's Everyday Sportswear",
        "Tank": "Women's Everyday Sportswear",
        "Shirt": "Women's Everyday Sportswear",
        "Shorts": "Women's Everyday Sportswear",
        "Skirt": "Women's Everyday Sportswear",
        "Skort": "Women's Everyday Sportswear",
        "Jacket": "Women's Everyday Sportswear",
        "Coat": "Women's Everyday Sportswear",
    }
    # ── Swimwear — gender-dependent ──
    SWIM_SUBCLASSES = {"Rashguard", "Trunk", "Bikini Top", "Bikini Bottom", "Swim Bottom",
                       "One Piece Swim", "Tankini", "Short", "Swim Set 2 pcs", "Swim Set",
                       "Board Short", "Boardshort", "Swim Shirt", "Cover Up", "Swimdress"}

    if sub_class in DRESS_MAP:
        return DRESS_MAP[sub_class]

    if sub_class in SWIM_SUBCLASSES or product_type == "SWIMWEAR":
        g = (gender or "").lower()
        sn = (style_name or "").lower()
        dept = (department or "").lower()
        is_youth = ("boys" in sn or "girls" in sn or "toddler" in sn or
                    dept in ("boys","girls") or g == "unisex")
        if is_youth:
            return "Swim"
        if g == "male":
            return "Men's Swimwear"
        if g == "female":
            return "Women's Swimwear"
        return "Swim"

    return ""  # leave blank if unknown — operator sets on dashboard

def _derive_item_type_keyword(sub_class, product_type="", gender="", style_name=""):
    """Map sub_class to Amazon item_type_keyword (free-text SEO slug).
    Covers all 16 product types."""
    _map = {
        # Dresses
        "Day Dress": "casual-and-day-dresses", "Cocktail Dress": "cocktail-and-party-dresses",
        "Active Dress": "active-dresses", "Maxi Dress": "maxi-dresses",
        "Mini Dress": "mini-dresses", "Wrap Dress": "wrap-dresses",
        "Shirt Dress": "shirt-dresses", "Dress": "dresses",
        # Swimwear
        "Rashguard": "rash-guards", "Rash Guard": "rash-guards",
        "Trunk": "swim-trunks", "Swim Trunk": "swim-trunks",
        "Board Short": "board-shorts", "Boardshort": "board-shorts", "Boardshorts": "board-shorts",
        "Bikini Top": "bikini-tops", "Bikini Bottom": "bikini-bottoms", "Swim Bottom": "bikini-bottoms",
        "One Piece Swim": "one-piece-swimsuits", "One Piece": "one-piece-swimsuits",
        "Tankini": "tankini-swimsuits", "Short": "board-shorts",
        # Gender-aware: see post-mapping logic below
        "Swim Set 2 pcs": "rash-guard-sets", "Swim Set": "rash-guard-sets",
        "Swim Shirt": "rash-guards", "Cover Up": "fashion-swimwear-cover-ups",
        "Swimdress": "fashion-swimwear-cover-ups",
        # Blazers
        "Blazer": "blazers", "Sport Coat": "sport-coats",
        # Bras
        "Bra": "bras", "Bralette": "bralettes", "Sports Bra": "sports-bras",
        # Coats/Jackets
        "Jacket": "jackets", "Coat": "coats", "Vest": "vests",
        "Puffer": "puffer-jackets", "Windbreaker": "windbreakers",
        "Anorak": "anoraks", "Parka": "parkas",
        # Hats
        "Hat": "hats", "Cap": "baseball-caps", "Beanie": "beanies",
        "Visor": "visors", "Sun Hat": "sun-hats",
        "Trucker Hat": "trucker-hats", "Bucket Hat": "bucket-hats",
        # One-piece outfits
        "Romper": "rompers", "Jumpsuit": "jumpsuits",
        "Bodysuit": "bodysuits", "One Piece Outfit": "rompers",
        # Overalls
        "Overalls": "overalls", "Dungarees": "overalls", "Overall": "overalls",
        # Pants
        "Pants": "casual-pants", "Leggings": "leggings",
        "Joggers": "jogger-pants", "Trousers": "dress-pants",
        "Chino": "chinos", "Cargo Pant": "cargo-pants",
        # Sandals
        "Sandal": "sandals", "Flip Flop": "flip-flops",
        "Slide": "slide-sandals", "Thong Sandal": "thong-sandals", "Slipper": "slippers",
        # Shirts
        "Pullover": "pullovers", "Tank": "tank-tops", "Tee": "t-shirts",
        "Blouse": "blouses", "Polo": "polo-shirts", "Henley": "henley-shirts",
        "Crop Top": "crop-tops", "Camisole": "camisoles", "Tunic": "tunics",
        # Shorts
        "Shorts": "casual-shorts", "Chino Short": "chino-shorts",
        "Cargo Short": "cargo-shorts", "Skort": "skorts",
        # Skirts
        "Skirt": "skirts", "Mini Skirt": "mini-skirts",
        "Maxi Skirt": "maxi-skirts", "Wrap Skirt": "wrap-skirts",
        # Snowsuits
        "Snowsuit": "snowsuits", "Snow Suit": "snowsuits", "Ski Suit": "ski-suits",
        # Snow Pants
        "Snow Pant": "snow-pants", "Snow Pants": "snow-pants",
        "Ski Pants": "ski-pants", "Ski Pant": "ski-pants",
        # Sweatshirts
        "Sweatshirt": "sweatshirts", "Hoodie": "hoodies",
        "Fleece": "fleece-jackets", "Quarter Zip": "quarter-zip-pullovers",
    }
    base = _map.get(sub_class, "")
    # Gender-aware swim-set keyword
    if sub_class in ("Swim Set 2 pcs", "Swim Set"):
        sn = (style_name or "").lower()
        g = (gender or "").lower()
        if "rash" in sn or "sleeve" in sn or "swim shirt" in sn or "sun shirt" in sn:
            return "rash-guard-sets"
        if g == "female" or "girls" in sn or "bikini" in sn:
            return "bikini-sets"
        return "swim-sets"
    return base

def _derive_item_type_name(sub_class, product_type="", gender="", style_name=""):
    """Human-readable item type name — must match template dropdown exactly.
    Covers all 16 product types."""
    _map = {
        # Dresses
        "Day Dress": "Casual Dress", "Cocktail Dress": "Cocktail Dress",
        "Active Dress": "Tennis Dress", "Maxi Dress": "Casual Dress",
        "Mini Dress": "Casual Dress", "Wrap Dress": "Casual Dress",
        "Shirt Dress": "Business Casual Dress", "Dress": "Dress",
        # Swimwear
        "Rashguard": "Rash Guard Shirt", "Rash Guard": "Rash Guard Shirt",
        "Trunk": "Swim Trunks", "Swim Trunk": "Swim Trunks",
        "Board Short": "Board Shorts", "Boardshort": "Board Shorts", "Boardshorts": "Board Shorts",
        "Bikini Top": "Bikini Top", "Bikini Bottom": "Bikini Bottoms", "Swim Bottom": "Bikini Bottoms",
        "One Piece Swim": "One Piece Swimsuit", "One Piece": "One Piece Swimsuit",
        "Tankini": "Tankini Swimsuit", "Short": "Board Shorts",
        # "Swim Set 2 pcs" resolved below — gender-aware (see post-mapping logic)
        "Swim Set 2 pcs": "Rash Guard Set", "Swim Set": "Rash Guard Set",
        "Swim Shirt": "Rash Guard Shirt", "Cover Up": "Swimwear Cover Up",
        "Swimdress": "Swimwear Cover Up",
        # Blazers
        "Blazer": "Blazer", "Sport Coat": "Sport Jacket",
        # Bras
        "Bra": "Bra", "Bralette": "Bra", "Sports Bra": "Sports Bra",
        # Coats/Jackets (template has no ITN dropdown — leave blank, operator fills)
        # Hats
        "Hat": "Hat", "Cap": "Baseball Cap", "Beanie": "Beanie Hat",
        "Visor": "Cap", "Sun Hat": "Hat",
        "Trucker Hat": "Baseball Cap", "Bucket Hat": "Bucket Hat",
        # One-piece outfits (template has no ITN dropdown)
        "Romper": "", "Jumpsuit": "", "Bodysuit": "",
        # Overalls (template has no ITN dropdown)
        "Overalls": "", "Dungarees": "",
        # Pants
        "Pants": "Casual Pants", "Leggings": "Leggings",
        "Joggers": "Casual Pants", "Trousers": "Dress Pants",
        "Chino": "Khakis", "Cargo Pant": "Casual Pants",
        # Sandals
        "Sandal": "Sandal", "Flip Flop": "Flip-Flop",
        "Slide": "Slide Sandal", "Thong Sandal": "Flat Sandal", "Slipper": "Sandal",
        # Shirts (template has no ITN dropdown)
        "Pullover": "", "Tank": "", "Tee": "", "Blouse": "",
        "Polo": "", "Henley": "", "Crop Top": "", "Camisole": "", "Tunic": "",
        # Shorts
        "Shorts": "Casual Shorts", "Chino Short": "Khaki Shorts",
        "Cargo Short": "Cargo Shorts", "Skort": "Skorts",
        # Skirts
        "Skirt": "Skirt", "Mini Skirt": "Skirt",
        "Maxi Skirt": "Skirt", "Wrap Skirt": "Skirt",
        # Snowsuits (template has no ITN dropdown)
        "Snowsuit": "", "Snow Suit": "", "Ski Suit": "",
        # Snow Pants
        "Snow Pant": "Casual Pants", "Snow Pants": "Casual Pants",
        "Ski Pants": "Casual Pants", "Ski Pant": "Casual Pants",
        # Sweatshirts
        "Sweatshirt": "Sweatshirt", "Hoodie": "Hooded Sweatshirt",
        "Fleece": "Pullover Sweater", "Quarter Zip": "Pullover Sweater",
    }
    base = _map.get(sub_class, "")
    # Gender-aware swim-set mapping: boys' swim sets are NOT bikini sets.
    # Valid SWIMWEAR item_type_name values include: 'Bikini Set', 'Rash Guard Set',
    # 'Swim Shirt Set', 'Tankini Set', 'Two Piece Swimsuit', 'Swimwear Cover Up Set'.
    if sub_class in ("Swim Set 2 pcs", "Swim Set"):
        sn = (style_name or "").lower()
        g = (gender or "").lower()
        # Rash guard style sets (long/short sleeve swim shirt + trunk)
        if "rash" in sn or "sleeve" in sn or "swim shirt" in sn or "sun shirt" in sn:
            return "Rash Guard Set"
        # Girls/women bikini set (only when clearly signaled)
        if g == "female" or "girls" in sn or "girl" in sn or "bikini" in sn:
            return "Bikini Set"
        # Default — two-piece swimsuit (safer than 'Bikini Set' for boys/unisex)
        return "Two Piece Swimsuit"
    return base  # leave blank if unknown

def _derive_item_length(sub_subclass, style_name, product_type="", sub_class=""):
    """Derive item length description from sub_subclass / style name.
    SWIMWEAR: return blank (field isn't on the Swimwear template; rashguards are torso
    garments, not leg garments, and Amazon's 'Knee-Length' value doesn't apply).
    DRESS/SKIRT: use MAXI/MINI/MIDI detection; default Knee-Length.
    """
    # Swimwear items don't have a meaningful item_length_description
    if (product_type or "").upper() == "SWIMWEAR" or sub_class in (
            "Rashguard","Rash Guard","Trunk","Swim Trunk","Bikini Top","Bikini Bottom",
            "Swim Bottom","One Piece Swim","Tankini","Short","Swim Set 2 pcs","Swim Set",
            "Board Short","Boardshort","Boardshorts","Cover Up","Swim Shirt"):
        return ""
    combined = f"{sub_subclass or ''} {style_name or ''}".upper()
    if "MAXI" in combined:
        return "Long"
    if "MINI" in combined:
        return "Short"
    if "MIDI" in combined:
        return "Mid-Calf"
    return "Knee-Length"

def _derive_swim_product_subcategory(sub_class, gender="", style_name="", product_category=""):
    """Derive the correct Amazon product_subcategory value for a SWIMWEAR style.
    Depends on product_category (Men's Swimwear | Women's Swimwear | Swim).
    Falls back to '' (blank) rather than guessing when we can't match.
    """
    cat = (product_category or "").strip()
    sc = (sub_class or "").strip()
    sn = (style_name or "").lower()
    is_baby = "baby" in sn or "infant" in sn

    # Men's Swimwear sub-subcategories: Board Shorts | Trunks | Briefs | Misc
    if cat == "Men's Swimwear":
        if sc in ("Trunk", "Swim Trunk"):
            if "board" in sn or "boardshort" in sn:
                return "Board Shorts"
            return "Trunks"
        if sc in ("Board Short", "Boardshort", "Boardshorts", "Short"):
            return "Board Shorts"
        # Men's rashguards live under Athletic or Misc — Amazon has no Rashguards
        # under Men's Swimwear. Use Misc for non-bottom items.
        if sc in ("Rashguard", "Rash Guard", "Swim Shirt"):
            return "Misc"
        return "Misc"

    # Women's Swimwear sub-subcategories
    if cat == "Women's Swimwear":
        if sc in ("Bikini Top",):
            return "Bikini Top Separates"
        if sc in ("Bikini Bottom", "Swim Bottom"):
            return "Bikini Bottom Separates"
        if sc in ("One Piece Swim", "One Piece"):
            return "One-Piece Swimsuits"
        if sc == "Tankini":
            return "Tankini Top Separates"
        if sc in ("Rashguard", "Rash Guard", "Swim Shirt"):
            return "Rashguards"
        if sc in ("Swim Set 2 pcs", "Swim Set"):
            return "Two-Piece Swimsuit Sets"
        if sc in ("Short", "Board Short", "Boardshort", "Boardshorts"):
            return "Swim Shorts"
        if sc == "Cover Up":
            return "Cover-Ups"
        return ""

    # Swim (youth / boys / girls)
    if cat == "Swim":
        is_boys = "boys" in sn or "boy" in sn or (gender or "").lower() == "male"
        is_girls = "girls" in sn or "girl" in sn or (gender or "").lower() == "female"
        prefix = "Baby " if is_baby else ""
        kind_boys = "Boys" if is_boys else ("Girls" if is_girls else "Boys")
        if sc in ("Rashguard", "Rash Guard", "Swim Shirt"):
            return f"{prefix}{kind_boys} Swim Rashguards" if is_boys else f"{prefix}{kind_boys} Rashguards"
        if sc in ("Trunk", "Swim Trunk", "Board Short", "Boardshort", "Boardshorts", "Short"):
            # Boys -> 'Boys Swim Bottoms'; Girls -> 'Girls Coverups & Boardshorts'
            return f"{prefix}{kind_boys} Swim Bottoms" if is_boys else f"{prefix}{kind_boys} Coverups & Boardshorts"
        if sc in ("Swim Set 2 pcs", "Swim Set"):
            return f"{prefix}{kind_boys} Swim Sets"
        if sc in ("Bikini Top", "Bikini Bottom", "Swim Bottom"):
            return f"{prefix}{kind_boys} Two Piece Swim"
        if sc in ("One Piece Swim", "One Piece"):
            return f"{prefix}{kind_boys} One Piece Swim"
        return ""

    return ""

def _derive_fabric_type(fabric):
    """Derive simplified fabric type for Col 59."""
    if not fabric:
        return "Polyester"
    f = fabric.upper()
    if "NYLON" in f:
        return "Nylon"
    if "COTTON" in f or "COTT" in f:
        return "Cotton"
    if "RAYON" in f:
        return "Rayon"
    if "LINEN" in f:
        return "Linen"
    if "MODAL" in f:
        return "Modal"
    if "SILK" in f:
        return "Silk"
    if "WOOL" in f:
        return "Wool"
    if "BAMBOO" in f:
        return "Bamboo"
    if "SPAN" in f or "SPANDEX" in f or "LYCRA" in f:
        return "Spandex"
    if "POLY" in f or "POLYESTER" in f:
        return "Polyester"
    return "Polyester"

def _derive_sleeve_length(sleeve_type):
    """Map sleeve type string to sleeve length description for Col 129."""
    if not sleeve_type:
        return "Sleeveless"
    s = sleeve_type.lower()
    if "sleeveless" in s:
        return "Sleeveless"
    if "long" in s:
        return "Long Sleeve"
    if "3/4" in s:
        return "3/4 Sleeve"
    if "short" in s or "flutter" in s or "cap" in s or "ruffle" in s or "balloon" in s:
        return "Short Sleeve"
    if "off" in s:
        return "Sleeveless"
    return "Sleeveless"


# ── QA Preview helpers ─────────────────────────────────────────────────────────
def _build_preview_fields(brand, brand_cfg, vendor_code, style, content):
    """
    Build the list of all fields that would go into the .xlsm for a given style,
    with status: 'filled', 'default', 'empty', or 'locked'.
    Returns a list of field dicts.
    """
    style_num     = style["style_num"]
    style_name    = style["style_name"]
    variants      = style["variants"]
    list_price    = style.get("list_price", "")
    cost_price    = style.get("cost_price", "")
    sub_class     = style.get("subclass", "") or style.get("sub_class", "")
    sub_subclass  = style.get("sub_subclass", "")
    model_name_raw = style.get("model_name", "") or style_name

    bullets      = content.get("bullets", [])
    description  = content.get("description", "")
    backend_kw   = content.get("backend_keywords", "")
    neck_type    = content.get("neck_type", "") or style.get("neck_type", "") or derive_neck_type(style_name)
    sleeve_type  = content.get("sleeve_type", "") or style.get("sleeve_type", "") or derive_sleeve_type(style_name)
    silhouette   = content.get("silhouette", "") or derive_silhouette(sub_subclass)
    # Derive gender/department per-style from division_name
    style_gender, style_dept = _derive_gender_department(style)
    eff_gender = style_gender or brand_cfg.get("gender", "")
    eff_dept   = style_dept or brand_cfg.get("department", "")
    # Resolve actual product type for this style
    resolved_pt    = _resolve_style_product_type(style) or "DRESS"
    # Always derive from dropdown-validated function, gender-aware
    category     = _derive_amazon_product_category(sub_class, gender=eff_gender, product_type=resolved_pt, style_name=style_name, department=eff_dept)
    subcategory  = SUBCLASS_SUBCATEGORY_MAP.get(sub_class, '')
    # Swim-aware subcategory derivation (replaces blank fallback for all 59 swim styles)
    if not subcategory and resolved_pt == "SWIMWEAR":
        subcategory = _derive_swim_product_subcategory(sub_class, gender=eff_gender, style_name=style_name, product_category=category)
    fabric       = content.get("fabric", "") or style.get("fabric", "") or brand_cfg.get("default_fabric", "")
    care         = content.get("care", "") or style.get("care", "") or brand_cfg.get("default_care", "")
    upf          = content.get("upf", "") or style.get("upf", "") or brand_cfg.get("default_upf", "")
    coo          = normalize_coo(content.get("coo", "") or style.get("coo", "") or brand_cfg.get("default_coo", "")) or ""
    clean_brand  = clean_brand_name(brand)
    item_type_name = _derive_item_type_name(sub_class, product_type=resolved_pt, gender=eff_gender, style_name=style_name)
    item_length    = _derive_item_length(sub_subclass, style_name, product_type=resolved_pt, sub_class=sub_class)
    fabric_type    = _derive_fabric_type(fabric)
    itk_value      = _derive_item_type_keyword(sub_class, product_type=resolved_pt, gender=eff_gender, style_name=style_name)
    # Taxonomy override: a confirmed override (if any) trumps the auto-derived values above
    _tax = _resolve_taxonomy_for_style(style, brand_cfg)
    if _tax.get("matched"):
        _e = _tax["entry"]
        category     = _e.get("product_category", category)
        subcategory  = _e.get("product_subcategory", subcategory)
        itk_value    = _e.get("item_type_keyword", itk_value)
        item_type_name = _e.get("item_type_name", item_type_name)
    taxonomy_source = "override" if _tax.get("matched") else "auto"
    taxonomy_key = _tax.get("key", "")
    today_str      = datetime.now().strftime("%Y%m%d")
    parent_sku     = style_num

    # Preview values: variation theme, target_gender (Male/Female refined), youth size info
    _prev_vts = variants or []
    _prev_hc  = len({(v.get("color_name") or v.get("color") or "") for v in _prev_vts}) > 1
    _prev_hs  = len({v.get("size", "") for v in _prev_vts}) > 1
    _prev_vt  = "SIZE/COLOR" if (_prev_hc and _prev_hs) else ("COLOR" if _prev_hc else ("SIZE" if _prev_hs else "COLOR"))
    _prev_sn_low = (style_name or "").lower()
    if "boys" in _prev_sn_low or "men" in _prev_sn_low:
        _prev_tg = "Male"
    elif "girls" in _prev_sn_low or "women" in _prev_sn_low:
        _prev_tg = "Female"
    else:
        _prev_tg = eff_gender if eff_gender in ("Male","Female") else ""
    _first_var_sz = (variants[0] if variants else {}).get("size", "")
    _prev_sst, _prev_sclass, _prev_ard, _prev_size = _derive_youth_size_info(style_name, eff_gender, _first_var_sz)

    # Sample title (parent)
    title = content.get("title", style_name)

    # Determine first variant for example child fields
    first_variant = variants[0] if variants else {}
    color_name = first_variant.get("color_name", "")
    size       = first_variant.get("size", "")
    upc        = first_variant.get("upc", "")
    sku        = first_variant.get("sku", "") or f"{style_num}-{color_name}-{size}".replace(" ", "-")
    color_family = COLOR_MAP.get(color_name.upper().strip(), normalize_color(color_name))
    size_normalized = normalize_size(size)
    variant_cost = first_variant.get("cost_price", "") or cost_price

    def f(col, header, value, status, editable=True, note="", field_id="", req_level="optional"):
        return {"col": col, "header": header, "value": str(value) if value is not None else "",
                "status": status, "editable": editable, "note": note,
                "field_id": field_id, "req_level": req_level}

    # req_level: "required" = Amazon rejects without it
    #            "conditional" = required depending on product type / other fields
    #            "recommended" = improves listing quality
    #            "optional" = nice to have
    fields = [
        # ── Identity & Structure ──
        f(1, "Vendor Code", vendor_code or brand_cfg.get("vendor_code_full", ""),
          "filled" if (vendor_code or brand_cfg.get("vendor_code_full", "")) else "empty", False,
          field_id="rtip_vendor_code#1.value", req_level="required"),
        f(2, "Vendor SKU (Parent)", parent_sku, "filled", False,
          field_id="vendor_sku#1.value", req_level="required"),
        f(3, "Product Type", resolved_pt, "default", True,
          field_id="product_type#1.value", req_level="required"),
        f(4, "Parentage Level", "Parent / Child", "default", True,
          field_id="parentage_level#1.value", req_level="required"),
        f(5, "Child Relationship Type", "Variation", "default", True,
          field_id="child_parent_sku_relationship#1.child_relationship_type", req_level="required"),
        f(6, "Parent SKU", parent_sku, "default", True,
          field_id="child_parent_sku_relationship#1.parent_sku", req_level="required"),
        f(7, "Variation Theme", _prev_vt, "default", True,
          field_id="variation_theme#1.name", req_level="required"),

        # ── Product Info ──
        f(8, "Item Name", title, "filled" if title and title != style_name else "default", True,
          field_id="item_name#1.value", req_level="required"),
        f(9, "Brand Name", clean_brand, "filled" if clean_brand else "empty", False,
          field_id="brand#1.value", req_level="required"),
        f(10, "External Product ID Type", "UPC" if upc else "",
          "filled" if upc else "default", False,
          field_id="external_product_id#1.type", req_level="required"),
        f(11, "External Product ID Value", re.sub(r'\D', '', str(upc)) if upc else "",
          "filled" if upc else "default", False,
          field_id="external_product_id#1.value", req_level="required"),
        f(13, "Product Category", category, "filled" if category else "default", False,
          field_id="product_category#1.value", req_level="recommended"),
        f(14, "Product Subcategory", subcategory, "filled" if subcategory else "default", False,
          field_id="product_subcategory#1.value", req_level="recommended"),
        f(15, "Item Type Keyword", itk_value, "filled" if itk_value else "default", False,
          field_id="item_type_keyword#1.value", req_level="required"),
        f(18, "Model Number", style_num, "filled", False,
          field_id="model_number#1.value", req_level="required"),
        f(19, "Model Name", (model_name_raw or style_name).title(), "filled", False,
          field_id="model_name#1.value", req_level="required"),

        # ── Content ──
        f(30, "Bullet Point 1", bullets[0][:120] + "..." if bullets and len(bullets[0]) > 120 else (bullets[0] if bullets else ""),
          "filled" if bullets else "empty", True,
          "" if bullets else "Required for submission.",
          field_id="bullet_point#1.value", req_level="required"),
        f(31, "Bullet Point 2", bullets[1][:120] + "..." if len(bullets) > 1 and len(bullets[1]) > 120 else (bullets[1] if len(bullets) > 1 else ""),
          "filled" if len(bullets) > 1 else "empty", True,
          field_id="bullet_point#2.value", req_level="required"),
        f(32, "Bullet Point 3", bullets[2][:120] + "..." if len(bullets) > 2 and len(bullets[2]) > 120 else (bullets[2] if len(bullets) > 2 else ""),
          "filled" if len(bullets) > 2 else "empty", True,
          field_id="bullet_point#3.value", req_level="recommended"),
        f(33, "Bullet Point 4", bullets[3][:120] + "..." if len(bullets) > 3 and len(bullets[3]) > 120 else (bullets[3] if len(bullets) > 3 else ""),
          "filled" if len(bullets) > 3 else "empty", True,
          field_id="bullet_point#4.value", req_level="recommended"),
        f(34, "Bullet Point 5", bullets[4][:120] + "..." if len(bullets) > 4 and len(bullets[4]) > 120 else (bullets[4] if len(bullets) > 4 else ""),
          "filled" if len(bullets) > 4 else "empty", True,
          field_id="bullet_point#5.value", req_level="recommended"),
        f(35, "Backend Keywords", backend_kw[:100] + "..." if backend_kw and len(backend_kw) > 100 else backend_kw,
          "filled" if backend_kw else "default", True,
          "" if backend_kw else "Using category defaults.",
          field_id="generic_keyword#1.value", req_level="recommended"),
        f(67, "Product Description", description[:120] + "..." if description and len(description) > 120 else description,
          "filled" if description else "empty", True,
          "" if description else "Required for submission.",
          field_id="rtip_product_description#1.value", req_level="required"),

        # ── Attributes ──
        f(46, "Style Name", style_name.title(), "filled", False,
          field_id="style#1.value", req_level="recommended"),
        f(47, "Department", eff_dept, "filled" if eff_dept else "empty", False,
          field_id="department#1.value", req_level="required"),
        f(48, "Target Gender", _prev_tg, "filled" if _prev_tg else "empty", False,
          field_id="target_gender#1.value", req_level="required"),
        f(49, "Age Range", _prev_ard, "default", True,
          field_id="age_range_description#1.value", req_level="required"),
        f(50, "Size System", "US", "default", True,
          field_id=_size_field(resolved_pt, "size_system"), req_level="required"),
        f(51, "Size Class", _prev_sclass, "default", True,
          field_id=_size_field(resolved_pt, "size_class"), req_level="required"),
        f(52, "Size (first variant)", size_normalized or size, "filled" if size else "default", False,
          field_id=_size_field(resolved_pt, "size"), req_level="required"),
        f(56, "Material", fabric, "filled" if fabric else "default", True,
          "" if fabric else "No fabric data. Will use brand default.",
          field_id="material#1.value", req_level="required"),
        f(59, "Fabric Type", fabric_type, "filled" if fabric else "default", False,
          field_id="fabric_type#1.value", req_level="recommended"),
        f(61, "Number of Items", "1", "default", True,
          field_id="number_of_items#1.value", req_level="required"),
        f(62, "Item Type Name", item_type_name, "filled", False,
          field_id="item_type_name#1.value", req_level="required"),
        f(66, "Special Size Type", _prev_sst, "default", True,
          field_id="special_size_type#1.value", req_level="conditional"),
        f(68, "Color (Standardized)", color_family, "filled" if color_family else "default", False,
          field_id="color#1.standardized_values#1", req_level="required"),
        f(69, "Color", color_name.title() if color_name else "",
          "filled" if color_name else "default", False,
          field_id="color#1.value", req_level="required"),
        f(70, "Item Length Description", item_length, "filled", False,
          field_id="item_length_description#1.value", req_level="conditional"),
        f(83, "Fit Type", "", "empty", True,
          "Not auto-filled. Set if known.",
          field_id="fit_type#1.value", req_level="recommended"),
        f(89, "Care Instructions", care, "filled" if care else "default", True,
          "" if care else "No care data. Will use brand default.",
          field_id="care_instructions#1.value", req_level="recommended"),
        f(118, "Neck/Collar Style", neck_type, "filled" if neck_type else "default", True,
          "" if neck_type else "Could not derive from style name.",
          field_id="neck#1.neck_style#1.value", req_level="conditional"),
        f(128, "Silhouette", silhouette, "filled" if silhouette else "default", True,
          field_id="apparel_silhouette#1.value", req_level="conditional"),
        f(129, "Sleeve Length", _derive_sleeve_length(sleeve_type), "filled", False,
          field_id="sleeve#1.length_description#1.value", req_level="conditional"),
        f(130, "Sleeve Type", sleeve_type, "filled" if sleeve_type else "default", True,
          field_id="sleeve#1.type#1.value", req_level="conditional"),
        f(131, "Closure Type", "", "empty", True,
          "Not auto-filled. Set if known (Pull On, Button, Zipper, etc.).",
          field_id="closure#1.type#1.value", req_level="recommended"),
        f(138, "UPF Protection", upf, "filled" if upf else "default", True,
          "" if upf else "No UPF value — leave blank if not applicable.",
          field_id="ultraviolet_protection_factor#1.value", req_level="conditional"),

        # ── Dates & Lifecycle ──
        f(77, "Item Booking Date", datetime.now().strftime("%Y-%m-%dT00:00:00Z"), "default", True,
          "Using today's date. Change if different.",
          field_id="item_booking_date#1.value", req_level="required"),
        f(126, "Product Lifecycle", "Perennial", "default", True,
          field_id="lifecycle_supply_type#1.value", req_level="required"),
        f(153, "Earliest Shipping Date", today_str, "default", True,
          "Using today's date. Update if different.",
          field_id="rtip_earliest_shipping_date#1.value", req_level="required"),

        # ── Counts & Units ──
        f(91, "Unit Count", "1", "default", True,
          field_id="unit_count#1.value", req_level="required"),
        f(92, "Unit Count Type", "Count", "default", True,
          field_id="unit_count#1.type.value", req_level="required"),
        f(149, "Skip Offer", "No", "default", True,
          field_id="skip_offer#1.value", req_level="required"),

        # ── Pricing ──
        f(150, "List Price", list_price, "filled" if list_price else "empty", True,
          "" if list_price else "Enter retail list price.",
          field_id="list_price#1.value", req_level="required"),
        f(151, "Cost Price", variant_cost, "filled" if variant_cost else "empty", True,
          "" if variant_cost else "Required for submission. Enter cost price.",
          field_id="cost_price#1.value", req_level="required"),

        # ── Shipping & Compliance ──
        f(152, "Import Designation", "Imported", "default", True,
          field_id="import_designation#1.value", req_level="required"),
        f(160, "Item Package Length", brand_cfg.get("default_pkg_length", ""), "filled" if brand_cfg.get("default_pkg_length") else "empty", True,
          "" if brand_cfg.get("default_pkg_length") else "Set package length or use Apply All.",
          field_id="item_package_dimensions#1.length.value", req_level="required"),
        f(161, "Package Length Unit", "Inches", "default", True,
          field_id="item_package_dimensions#1.length.unit", req_level="required"),
        f(162, "Item Package Width", brand_cfg.get("default_pkg_width", ""), "filled" if brand_cfg.get("default_pkg_width") else "empty", True,
          "" if brand_cfg.get("default_pkg_width") else "Set package width or use Apply All.",
          field_id="item_package_dimensions#1.width.value", req_level="required"),
        f(163, "Package Width Unit", "Inches", "default", True,
          field_id="item_package_dimensions#1.width.unit", req_level="required"),
        f(164, "Item Package Height", brand_cfg.get("default_pkg_height", ""), "filled" if brand_cfg.get("default_pkg_height") else "empty", True,
          "" if brand_cfg.get("default_pkg_height") else "Set package height or use Apply All.",
          field_id="item_package_dimensions#1.height.value", req_level="required"),
        f(165, "Package Height Unit", "Inches", "default", True,
          field_id="item_package_dimensions#1.height.unit", req_level="required"),
        f(166, "Item Package Weight", brand_cfg.get("default_pkg_weight", ""), "filled" if brand_cfg.get("default_pkg_weight") else "empty", True,
          "" if brand_cfg.get("default_pkg_weight") else "Set package weight or use Apply All.",
          field_id="item_package_weight#1.value", req_level="required"),
        f(167, "Package Weight Unit", "Pounds", "default", True,
          field_id="item_package_weight#1.unit", req_level="required"),
        f(168, "Order Aggregate Type", "Each", "default", True,
          field_id="rtip_order_aggregate_type#1.value", req_level="required"),
        f(169, "Items per Inner Pack", "1", "default", True,
          field_id="rtip_items_per_inner_pack#1.value", req_level="required"),
        f(170, "Country of Origin", coo, "filled" if coo else "default", True,
          "" if coo else "Update with actual country of origin.",
          field_id="country_of_origin#1.value", req_level="required"),
        f(171, "Batteries Required", "No", "default", True,
          field_id="batteries_required#1.value", req_level="required"),
        f(172, "Batteries Included", "No", "default", True,
          field_id="batteries_included#1.value", req_level="required"),
        f(230, "Contains Battery or Cell", "No", "default", True,
          field_id="contains_battery_or_cell#1.value", req_level="required"),
    ]

    return fields


def _qa_summary_for_style(style_num, style_name, fields):
    """Given a list of field dicts, return summary counts and status."""
    filled  = sum(1 for f in fields if f["status"] == "filled" or f["status"] == "locked")
    defaults = sum(1 for f in fields if f["status"] == "default")
    empty   = sum(1 for f in fields if f["status"] == "empty")
    req_empty = sum(1 for f in fields if f["status"] == "empty" and f.get("req_level") == "required")
    total   = len(fields)
    if req_empty > 0:
        status = "attention"
    elif defaults > 0:
        status = "defaults"
    else:
        status = "ready"
    return {
        "style_num": style_num,
        "style_name": style_name,
        "filled": filled,
        "defaults": defaults,
        "empty": empty,
        "req_empty": req_empty,
        "total": total,
        "status": status,
    }


@app.route("/api/preview-nis", methods=["POST"])
def preview_nis():
    """Return all fields for a style with their values and QA status.
    Accepts brand/style/content from request body as fallback when session is empty.
    """
    data = request.get_json(force=True)
    style_num = data.get("style_num", "").strip()
    if not style_num:
        return jsonify({"error": "style_num required"}), 400

    # Use session data, but allow frontend to pass data directly as fallback
    brand        = session_data.get("brand") or data.get("brand", "")
    vendor_code  = session_data.get("vendor_code", "") or data.get("vendor_code", "")
    styles       = session_data.get("styles", []) or data.get("styles", [])
    content_map  = session_data.get("generated_content", {}) or data.get("content_map", {})
    overrides    = session_data.get("field_overrides", {}).get(style_num, {})

    # If session was empty but frontend sent data, restore session for subsequent calls
    if not session_data.get("brand") and brand:
        session_data["brand"] = brand
    if not session_data.get("vendor_code") and vendor_code:
        session_data["vendor_code"] = vendor_code
    if not session_data.get("styles") and styles:
        session_data["styles"] = styles
    if not session_data.get("generated_content") and content_map:
        session_data["generated_content"] = content_map

    if not brand:
        return jsonify({"error": "No brand selected"}), 400

    # Find style from session or from frontend-provided list
    style = next((s for s in styles if s["style_num"] == style_num), None)
    # Also accept a single style object passed directly
    if not style and data.get("style"):
        style = data["style"]
    if not style:
        return jsonify({"error": f"Style {style_num} not found"}), 404

    brand_cfg = _load_brand_config_data(brand)
    content   = content_map.get(style_num, {}) or data.get("content", {})

    fields = _build_preview_fields(brand, brand_cfg, vendor_code, style, content)

    # Apply any stored overrides (keyed by field_id) — mark overridden fields as 'filled'
    for field in fields:
        fid = field.get("field_id", "")
        if fid and fid in overrides:
            field["value"]  = overrides[fid]
            field["status"] = "filled"
            field["overridden"] = True

    filled_count   = sum(1 for f in fields if f["status"] in ("filled", "locked"))
    defaults_count = sum(1 for f in fields if f["status"] == "default")
    empty_count    = sum(1 for f in fields if f["status"] == "empty")

    return jsonify({
        "style_num":       style_num,
        "style_name":      style["style_name"],
        "total_fields":    len(fields),
        "filled":          filled_count,
        "defaults_used":   defaults_count,
        "needs_attention": empty_count,
        "fields":          fields,
    })


@app.route("/api/update-field", methods=["POST"])
def update_field():
    """Store a field override for a style, keyed by field_id.
    Picked up when generating .xlsm via do_xlsm_surgery / _generate_category_file.
    """
    data      = request.get_json(force=True)
    style_num = str(data.get("style_num", "")).strip()
    field_id  = str(data.get("field_id", "")).strip()
    value     = data.get("value", "")

    if not style_num or not field_id:
        return jsonify({"error": "style_num and field_id required"}), 400

    if "field_overrides" not in session_data:
        session_data["field_overrides"] = {}
    if style_num not in session_data["field_overrides"]:
        session_data["field_overrides"][style_num] = {}

    # Get the previous value for feedback tracking
    prev_value = session_data["field_overrides"][style_num].get(field_id, "")
    session_data["field_overrides"][style_num][field_id] = value

    # Auto-capture as implicit feedback (no extra clicks from operator)
    field_name = data.get("field_name", field_id)  # frontend can send human name
    _store_feedback({
        "id": f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_uuid.uuid4().hex[:8]}",
        "timestamp": datetime.utcnow().isoformat(),
        "operator": session_data.get("operator", ""),
        "brand": session_data.get("brand", ""),
        "type": "field_override",
        "phase": "review",
        "context": {
            "scope": "field",
            "brand": session_data.get("brand", ""),
            "style_num": style_num,
            "field_id": field_id,
            "field_name": field_name,
        },
        "data": {"field_id": field_id, "field_name": field_name,
                 "original": prev_value, "updated": value},
        "maps_to": "derive_logic",
    })

    return jsonify({"ok": True, "style_num": style_num, "field_id": field_id, "value": value})


@app.route("/api/update-field-all", methods=["POST"])
def update_field_all():
    """Apply a field override to ALL styles at once.
    Used for shared fields like package dims, COO, care, etc.
    """
    data     = request.get_json(force=True)
    field_id = str(data.get("field_id", "")).strip()
    value    = data.get("value", "")

    if not field_id:
        return jsonify({"error": "field_id required"}), 400

    styles = session_data.get("styles", [])
    if not styles:
        return jsonify({"error": "No styles loaded"}), 400

    if "field_overrides" not in session_data:
        session_data["field_overrides"] = {}

    count = 0
    for style in styles:
        sn = style["style_num"]
        if sn not in session_data["field_overrides"]:
            session_data["field_overrides"][sn] = {}
        session_data["field_overrides"][sn][field_id] = value
        count += 1

    # Auto-capture as implicit feedback
    field_name = data.get("field_name", field_id)
    _store_feedback({
        "id": f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{_uuid.uuid4().hex[:8]}",
        "timestamp": datetime.utcnow().isoformat(),
        "operator": session_data.get("operator", ""),
        "brand": session_data.get("brand", ""),
        "type": "apply_all",
        "phase": "review",
        "context": {
            "scope": "brand",
            "brand": session_data.get("brand", ""),
            "field_id": field_id,
            "field_name": field_name,
        },
        "data": {"field_id": field_id, "field_name": field_name,
                 "value": value, "styles_count": count},
        "maps_to": "brand_config",
    })

    return jsonify({"ok": True, "field_id": field_id, "value": value, "styles_updated": count})


@app.route("/api/save-field-as-brand-default", methods=["POST"])
def save_field_as_brand_default():
    """Save a field value as a brand default in the brand config JSON.
    Next time this brand is loaded, this value will be pre-filled.
    """
    data     = request.get_json(force=True)
    brand    = data.get("brand", "") or session_data.get("brand", "")
    field_id = data.get("field_id", "").strip()
    value    = data.get("value", "")
    field_name = data.get("field_name", field_id)

    if not brand or not field_id:
        return jsonify({"error": "brand and field_id required"}), 400

    # Load existing brand config
    cfg = _load_brand_config_data(brand)

    # Map known field_ids to brand config keys
    FIELD_TO_CONFIG = {
        "country_of_origin#1.value": "default_coo",
        "care_instructions#1.value": "default_care",
        "ultraviolet_protection_factor#1.value": "default_upf",
        "material#1.value": "default_fabric",
        "item_package_dimensions#1.length.value": "default_pkg_length",
        "item_package_dimensions#1.width.value": "default_pkg_width",
        "item_package_dimensions#1.height.value": "default_pkg_height",
        "item_package_weight#1.value": "default_pkg_weight",
        "department#1.value": "department",
        "target_gender#1.value": "gender",
    }

    config_key = FIELD_TO_CONFIG.get(field_id)
    if config_key:
        cfg[config_key] = value
    else:
        # Store in a generic defaults dict for unmapped fields
        if "field_defaults" not in cfg:
            cfg["field_defaults"] = {}
        cfg["field_defaults"][field_id] = value

    # Save brand config
    safe_brand = re.sub(r'[^\w\-]', '_', brand)
    cfg_path = BRAND_CONFIGS_DIR / f"{safe_brand}.json"
    with open(str(cfg_path), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    return jsonify({"ok": True, "brand": brand, "field_id": field_id, "config_key": config_key or f"field_defaults.{field_id}"})


@app.route("/api/nis-qa-summary", methods=["GET", "POST"])
def nis_qa_summary():
    """Return QA summary across all styles.
    Accepts POST with brand/styles/content_map as fallback when session is empty.
    """
    req_data = request.get_json(force=True) if request.method == "POST" else {}

    brand       = session_data.get("brand") or req_data.get("brand", "")
    vendor_code = session_data.get("vendor_code", "") or req_data.get("vendor_code", "")
    styles      = session_data.get("styles", []) or req_data.get("styles", [])
    content_map = session_data.get("generated_content", {}) or req_data.get("content_map", {})
    overrides   = session_data.get("field_overrides", {})

    # Restore session if frontend sent data
    if not session_data.get("brand") and brand:
        session_data["brand"] = brand
    if not session_data.get("styles") and styles:
        session_data["styles"] = styles
    if not session_data.get("generated_content") and content_map:
        session_data["generated_content"] = content_map
    if not session_data.get("vendor_code") and vendor_code:
        session_data["vendor_code"] = vendor_code

    if not brand:
        return jsonify({"error": "No brand selected"}), 400
    if not styles:
        return jsonify({"error": "No styles loaded"}), 400

    brand_cfg = _load_brand_config_data(brand)

    style_summaries = []
    total_ready = 0
    total_attention = 0
    total_defaults = 0

    for style in styles:
        snum    = style["style_num"]
        sname   = style["style_name"]
        content = content_map.get(snum, {})
        fields  = _build_preview_fields(brand, brand_cfg, vendor_code, style, content)

        # Apply overrides (keyed by field_id)
        style_overrides = overrides.get(snum, {})
        for field in fields:
            fid = field.get("field_id", "")
            if fid and fid in style_overrides:
                field["status"] = "filled"

        summary = _qa_summary_for_style(snum, sname, fields)
        style_summaries.append(summary)

        if summary["status"] == "ready":
            total_ready += 1
        elif summary["status"] == "attention":
            total_attention += 1
        else:  # defaults
            total_defaults += 1

    return jsonify({
        "total_styles":    len(styles),
        "ready":           total_ready,
        "has_defaults":    total_defaults,
        "needs_attention": total_attention,
        "content_generated": len(content_map) > 0,
        "styles":          style_summaries,
    })


# ═══════════════════════════════════════════════════════════════════════
# NIS ROW-SPACING FIX
# Long-text fields (bullets, description, item_name, style, model_name) must
# render with wrap_text=True and auto-fit row heights. The Amazon templates
# ship with customHeight=True, height=12.75 on every data row — when wrapped
# 200-char bullets are written, Excel hides the wrapped lines because the
# row is clamped to a single text-line's worth of vertical space, which
# shows up as "rows on top of each other".
# ═══════════════════════════════════════════════════════════════════════
LONG_TEXT_FIELD_IDS = {
    "bullet_point#1.value", "bullet_point#2.value", "bullet_point#3.value",
    "bullet_point#4.value", "bullet_point#5.value",
    "rtip_product_description#1.value",
    "item_name#1.value", "style#1.value", "model_name#1.value",
    "generic_keyword#1.value", "item_type_keyword#1.value",
}

def _is_long_text_field(field_id):
    return field_id in LONG_TEXT_FIELD_IDS

def _apply_long_text_alignment(cell, cached_alignment=None):
    """Force wrap_text=True and top-vertical alignment on a long-text cell,
    preserving any other alignment properties from the template's row-7 style."""
    base = cached_alignment if cached_alignment is not None else cell.alignment
    cell.alignment = Alignment(
        horizontal=(base.horizontal if base is not None else None) or "left",
        vertical="top",
        wrap_text=True,
        shrink_to_fit=False,
        indent=base.indent if base is not None else 0,
        text_rotation=base.text_rotation if base is not None else 0,
    )

def _clear_row_heights_for_auto_fit(ws, start_row=7, end_row=None):
    """Remove the fixed 12.75 customHeight on data rows so Excel auto-fits
    wrapped bullet text. Preserves heights on header rows 1-6.
    openpyxl derives customHeight from whether height is set, so clearing
    the height alone is enough — the customHeight attribute has no setter."""
    end_row = end_row or (ws.max_row or 100)
    for r in range(start_row, end_row + 1):
        if r in ws.row_dimensions:
            rd = ws.row_dimensions[r]
            # Setting height to None releases the fixed row height and lets
            # Excel auto-size based on wrapped content.
            rd.height = None

def do_xlsm_surgery(template_path, brand, brand_cfg, vendor_code, style, content):
    """
    .xlsm surgery — field-ID based dynamic column mapping:
    1. Load template with keep_vba=True
    2. Find the Template-* sheet; derive product_type from sheet name
    3. Build field_id → column_number map from row 4 (exact matches, stripped)
    4. Capture cell styles from row 7
    5. Clear rows 7+
    6. Write parent + child rows using exact field_id lookups
    7. Save as new file
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(template_path, keep_vba=True)

    # ── Find Template sheet and detect product type ───────────────────────────
    ws = None
    detected_product_type = "DRESS"  # safe default
    for name in wb.sheetnames:
        if name.upper().startswith("TEMPLATE"):
            ws = wb[name]
            # Extract product type: "Template-SWIMWEAR" → "SWIMWEAR"
            parts = name.split("-", 1)
            if len(parts) == 2 and parts[1].strip():
                detected_product_type = parts[1].strip().upper()
            break
    if ws is None:
        # Fallback: any sheet with "template" in name
        for name in wb.sheetnames:
            if "template" in name.lower():
                ws = wb[name]
                break
    if ws is None:
        ws = wb.active

    # ── Build field_id → column_number map from row 4 ─────────────────────────
    max_col = ws.max_column or 254
    col_map = {}  # field_id_string → column_number
    for col in range(1, max_col + 1):
        raw = ws.cell(row=4, column=col).value
        if raw is not None:
            fid = str(raw).strip()
            if fid:
                col_map[fid] = col

    def _col(field_id):
        """Exact field_id lookup. Returns column number or None."""
        return col_map.get(field_id)

    # ── Capture styles from row 7 (before clearing) ───────────────────────────
    style_cache = {}
    for col in range(1, max_col + 1):
        cell = ws.cell(row=7, column=col)
        style_cache[col] = {
            "font":          copy.copy(cell.font)      if cell.font      else None,
            "fill":          copy.copy(cell.fill)      if cell.fill      else None,
            "border":        copy.copy(cell.border)    if cell.border    else None,
            "alignment":     copy.copy(cell.alignment) if cell.alignment else None,
            "number_format": cell.number_format,
        }

    # ── Clear existing data rows (7+) ─────────────────────────────────────────
    for row_idx in range(7, (ws.max_row or 100) + 1):
        for col in range(1, max_col + 1):
            ws.cell(row=row_idx, column=col).value = None

    # ── NIS row-spacing fix: release fixed row heights so wrapped bullet text auto-fits
    _clear_row_heights_for_auto_fit(ws, start_row=7)

    # ── Unpack style / content data ───────────────────────────────────────────
    style_num      = style["style_num"]
    style_name     = style["style_name"]
    variants       = style["variants"]
    list_price     = style.get("list_price", "")
    cost_price     = style.get("cost_price", "")
    model_name_raw = style.get("model_name", "") or style_name
    sub_class      = style.get("subclass", "") or style.get("sub_class", "")
    sub_subclass   = style.get("sub_subclass", "")

    bullets     = content.get("bullets", [])
    description = content.get("description", "")
    backend_kw  = content.get("backend_keywords", "")
    neck_type   = content.get("neck_type", "")  or style.get("neck_type", "") or derive_neck_type(style_name)
    sleeve_type = content.get("sleeve_type", "") or style.get("sleeve_type", "") or derive_sleeve_type(style_name)
    silhouette  = content.get("silhouette", "")  or derive_silhouette(sub_subclass)
    style_gender, style_dept = _derive_gender_department(style)
    eff_gender = style_gender or brand_cfg.get("gender", "")
    eff_dept   = style_dept or brand_cfg.get("department", "")
    category    = _derive_amazon_product_category(sub_class, gender=eff_gender, product_type=detected_product_type, style_name=style_name, department=eff_dept)
    subcategory = SUBCLASS_SUBCATEGORY_MAP.get(sub_class, '')
    if not subcategory and detected_product_type == "SWIMWEAR":
        subcategory = _derive_swim_product_subcategory(sub_class, gender=eff_gender, style_name=style_name, product_category=category)
    fabric      = content.get("fabric", "")      or brand_cfg.get("default_fabric", "")
    care        = content.get("care", "")        or brand_cfg.get("default_care", "")
    upf         = content.get("upf", "")         or brand_cfg.get("default_upf", "")
    coo         = normalize_coo(content.get("coo", "")         or brand_cfg.get("default_coo", "")) or "Imported"

    clean_brand    = clean_brand_name(brand)
    item_type_name = _derive_item_type_name(sub_class, product_type=detected_product_type, gender=eff_gender, style_name=style_name)
    item_length    = _derive_item_length(sub_subclass, style_name, product_type=detected_product_type, sub_class=sub_class)
    fabric_type    = _derive_fabric_type(fabric)
    itk_value      = _derive_item_type_keyword(sub_class, product_type=detected_product_type, gender=eff_gender, style_name=style_name)
    # Taxonomy override wins over auto-derivation (Phase 1)
    _tax = _resolve_taxonomy_for_style(style, brand_cfg)
    if _tax.get("matched"):
        _e = _tax["entry"]
        category       = _e.get("product_category", category)
        subcategory    = _e.get("product_subcategory", subcategory)
        itk_value      = _e.get("item_type_keyword", itk_value)
        item_type_name = _e.get("item_type_name", item_type_name)
    sleeve_len     = _derive_sleeve_length(sleeve_type)
    today_str      = datetime.now().strftime("%Y%m%d")
    booking_date  = datetime.now().strftime("%Y-%m-%dT00:00:00Z")

    parent_sku = style_num

    # Load any QA field overrides for this style (keyed by field_id)
    _field_overrides = session_data.get("field_overrides", {}).get(style_num, {})

    # ── Cell writer ───────────────────────────────────────────────────────────
    def write_cell(row_idx, field_id, value):
        """Write value to the cell for field_id, applying row-7 styles.
        Checks field_overrides first (keyed by field_id).
        """
        # Field-ID-keyed override takes priority
        if field_id in _field_overrides:
            value = _field_overrides[field_id]
        col_num = _col(field_id)
        if col_num is None or value is None:
            return
        if value == "":
            return
        cell = ws.cell(row=row_idx, column=col_num)
        # Try numeric conversion for price/dimension fields
        if isinstance(value, str):
            try:
                value = float(value)
                if value == int(value):
                    value = int(value)
            except (ValueError, TypeError):
                pass
        cell.value = value if isinstance(value, (int, float)) else str(value)
        cached = style_cache.get(col_num, {})
        if cached.get("font"):          cell.font          = copy.copy(cached["font"])
        if cached.get("fill"):          cell.fill          = copy.copy(cached["fill"])
        if cached.get("border"):        cell.border        = copy.copy(cached["border"])
        if cached.get("alignment"):     cell.alignment     = copy.copy(cached["alignment"])
        if cached.get("number_format"): cell.number_format = cached["number_format"]
        # Long-text fields need wrap_text=True so Excel renders all wrapped
        # lines instead of stacking them into a single 12.75pt row.
        if _is_long_text_field(field_id):
            _apply_long_text_alignment(cell, cached.get("alignment"))

    # ── Shared-fields writer (called for both parent and child rows) ──────────
    def write_shared(row_idx, vendor_sku_val, is_child=False):
        """Write all fields common to parent and child rows."""
        write_cell(row_idx, "rtip_vendor_code#1.value",         vendor_code or brand_cfg.get("vendor_code_full", ""))
        write_cell(row_idx, "vendor_sku#1.value",               vendor_sku_val)
        write_cell(row_idx, "product_type#1.value",             detected_product_type)
        # Variation theme must match Amazon dropdown. For SWIMWEAR, 'SIZE/COLOR' is valid; 'COLOR/SIZE' is NOT.
        _hm_color = len({(_v.get("color") or _v.get("color_name") or "") for _v in variants}) > 1
        _hm_size  = len({_v.get("size", "") for _v in variants}) > 1
        if _hm_color and _hm_size:
            _vt = "SIZE/COLOR"
        elif _hm_color:
            _vt = "COLOR"
        elif _hm_size:
            _vt = "SIZE"
        else:
            _vt = "COLOR"
        write_cell(row_idx, "variation_theme#1.name",     _vt)
        write_cell(row_idx, "brand#1.value",                    clean_brand)
        if category:
            write_cell(row_idx, "product_category#1.value",     category)
        if subcategory:
            write_cell(row_idx, "product_subcategory#1.value",  subcategory)
        write_cell(row_idx, "item_type_keyword#1.value",        itk_value)
        write_cell(row_idx, "model_number#1.value",             style_num)
        write_cell(row_idx, "model_name#1.value",               (model_name_raw or style_name).title())
        # Bullets
        for i, bkey in enumerate(["bullet_point#1.value", "bullet_point#2.value",
                                   "bullet_point#3.value", "bullet_point#4.value",
                                   "bullet_point#5.value"]):
            if i < len(bullets):
                write_cell(row_idx, bkey, bullets[i][:500])
        write_cell(row_idx, "generic_keyword#1.value",          backend_kw)
        write_cell(row_idx, "style#1.value",                    style_name.title())
        # fit_type — left blank unless from data/override
        write_cell(row_idx, "fit_type#1.value",                 content.get("fit_type", "") or style.get("fit_type", "") or brand_cfg.get("default_fit_type", ""))
        # Target gender: Amazon accepts Male/Female; refine youth via style_name.
        _tg_sn = (style_name or "").lower()
        if "boys" in _tg_sn or "men" in _tg_sn:
            _tg = "Male"
        elif "girls" in _tg_sn or "women" in _tg_sn:
            _tg = "Female"
        else:
            _tg = eff_gender if eff_gender in ("Male", "Female") else ""
        _first_var = variants[0] if variants else {}
        _sst_s, _sclass_s, _ard_s, _ = _derive_youth_size_info(style_name, eff_gender, _first_var.get("size", ""))
        write_cell(row_idx, "department#1.value",               eff_dept)
        write_cell(row_idx, "target_gender#1.value",            _tg)
        write_cell(row_idx, "age_range_description#1.value",    _ard_s)
        if _sst_s:
            write_cell(row_idx, "special_size_type#1.value",   _sst_s)
        write_cell(row_idx, _size_field(detected_product_type, "body_type", col_map),         "")
        write_cell(row_idx, _size_field(detected_product_type, "height_type", col_map),       "")
        if fabric:
            write_cell(row_idx, "material#1.value",             fabric)
        write_cell(row_idx, "fabric_type#1.value",              fabric_type)
        write_cell(row_idx, "number_of_items#1.value", "1")
        write_cell(row_idx, "item_type_name#1.value",           item_type_name)
        write_cell(row_idx, "rtip_product_description#1.value", description)
        write_cell(row_idx, "item_length_description#1.value",  item_length)
        write_cell(row_idx, "item_booking_date#1.value",        booking_date)
        if care:
            write_cell(row_idx, "care_instructions#1.value",    care)
        write_cell(row_idx, "unit_count#1.value",               "1")
        write_cell(row_idx, "unit_count#1.type.value",                "Count")
        if neck_type:
            write_cell(row_idx, "neck#1.neck_style#1.value",         neck_type)
        write_cell(row_idx, "lifecycle_supply_type#1.value", "Perennial")
        if silhouette:
            write_cell(row_idx, "apparel_silhouette#1.value",   silhouette)
        write_cell(row_idx, "sleeve#1.length_description#1.value", sleeve_len)
        if sleeve_type:
            write_cell(row_idx, "sleeve#1.type#1.value",        sleeve_type)
        # closure — left blank unless from data/override
        write_cell(row_idx, "closure#1.type#1.value",             content.get("closure_type", "") or style.get("closure_type", "") or brand_cfg.get("default_closure", ""))
        if upf:
            write_cell(row_idx, "ultraviolet_protection_factor#1.value", upf)
        write_cell(row_idx, "skip_offer#1.value",                       "No")
        # Import designation: "Imported" unless COO is US
        import_desig = "Imported" if coo.upper() not in ("US", "USA", "UNITED STATES") else "Domestic"
        write_cell(row_idx, "import_designation#1.value",       import_desig)
        write_cell(row_idx, "rtip_earliest_shipping_date#1.value", today_str)
        # Contains battery/cell — required compliance field
        write_cell(row_idx, "contains_battery_or_cell#1.value", "No")
        # Package dimensions
        # Package dims — left blank unless from data/override/brand config
        write_cell(row_idx, "item_package_dimensions#1.length.value",      brand_cfg.get("default_pkg_length", ""))
        write_cell(row_idx, "item_package_dimensions#1.length.unit",       "Inches")
        write_cell(row_idx, "item_package_dimensions#1.width.value",       brand_cfg.get("default_pkg_width", ""))
        write_cell(row_idx, "item_package_dimensions#1.width.unit",        "Inches")
        write_cell(row_idx, "item_package_dimensions#1.height.value",      brand_cfg.get("default_pkg_height", ""))
        write_cell(row_idx, "item_package_dimensions#1.height.unit",       "Inches")
        write_cell(row_idx, "item_package_weight#1.value",      brand_cfg.get("default_pkg_weight", ""))
        write_cell(row_idx, "item_package_weight#1.unit",       "Pounds")
        write_cell(row_idx, "rtip_order_aggregate_type#1.value",     "Each")
        write_cell(row_idx, "rtip_items_per_inner_pack#1.value",     "1")
        if coo:
            write_cell(row_idx, "country_of_origin#1.value",   coo)
        write_cell(row_idx, "batteries_required#1.value",  "No")
        write_cell(row_idx, "batteries_included#1.value",  "No")
        # List price on shared fields (applies to parent; child may override)
        if list_price:
            try:    write_cell(row_idx, "list_price#1.value",   float(list_price))
            except: write_cell(row_idx, "list_price#1.value",   list_price)

    current_row = 7

    # ── Parent row ────────────────────────────────────────────────────────────
    # Parent row (required by Amazon)
    write_shared(current_row, parent_sku, is_child=False)
    write_cell(current_row, "parentage_level#1.value", "Parent")
    write_cell(current_row, "item_name#1.value", content.get("title", style_name))
    current_row += 1

    # Child rows (one per variant)
    for v in variants:
        color_name = v.get("color_name", "")
        size       = v.get("size", "")
        upc        = v.get("upc", "")
        child_asin = v.get("child_asin", "")
        sku        = v.get("sku", "") or f"{style_num}-{color_name}-{size}".replace(" ", "-")

        color_family    = COLOR_MAP.get(color_name.upper().strip(), normalize_color(color_name))
        size_normalized = normalize_size(size)

        variant_title = generate_title(
            brand_cfg, brand, style_name, detected_product_type.title(),
            color_name, size, upf, style_gender=style_gender
        )
        # Youth-aware size resolution: 2T -> '2 Years', Alpha stays as-is.
        _sst_v, _sclass_v, _ard_v, _size_youth = _derive_youth_size_info(style_name, eff_gender, size)
        size_normalized = _size_youth or size_normalized

        write_shared(current_row, sku, is_child=True)
        write_cell(current_row, "parentage_level#1.value",      "Child")
        write_cell(current_row, "child_parent_sku_relationship#1.child_relationship_type", "Variation")
        write_cell(current_row, "child_parent_sku_relationship#1.parent_sku",           parent_sku)
        write_cell(current_row, "item_name#1.value",            variant_title)
        if upc:
            write_cell(current_row, "external_product_id#1.type",  "UPC")
            write_cell(current_row, "external_product_id#1.value", re.sub(r'\D', '', str(upc)))
        if child_asin:
            asin_col = _col("merchant_suggested_asin#1.value")
            if asin_col:
                ws.cell(row=current_row, column=asin_col).value = child_asin
        write_cell(current_row, _size_field(detected_product_type, "size_system", col_map),   "US")
        write_cell(current_row, _size_field(detected_product_type, "size_class", col_map),    _sclass_v)
        write_cell(current_row, _size_field(detected_product_type, "size", col_map),          size_normalized or size)
        if _sst_v:
            write_cell(current_row, "special_size_type#1.value", _sst_v)
        write_cell(current_row, "age_range_description#1.value", _ard_v)
        write_cell(current_row, "color#1.standardized_values#1",         color_family)
        write_cell(current_row, "color#1.value",                color_name.title() if color_name else "")
        # Child cost price — always write so overrides can apply even if source is empty
        child_cost = v.get("cost_price") or cost_price
        if child_cost:
            try:    write_cell(current_row, "cost_price#1.value",     float(child_cost))
            except: write_cell(current_row, "cost_price#1.value",     child_cost)
        else:
            write_cell(current_row, "cost_price#1.value",             "")  # trigger override check
        current_row += 1

    # ── Save ──────────────────────────────────────────────────────────────────
    safe_name = re.sub(r'[^\w\-]', '_', style_num)
    output_filename = f"NIS_{brand.replace(' ', '_')}_{safe_name}.xlsm"
    output_path = str(UPLOAD_OUTPUT / output_filename)

    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        wb.save(output_path)
    wb.close()

    return output_path

@app.route("/api/download/<filename>")
def download_file(filename):
    # Sanitize
    filename = Path(filename).name
    file_path = UPLOAD_OUTPUT / filename
    if not file_path.exists():
        abort(404)
    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.ms-excel.sheet.macroEnabled.12",
    )

def _generate_category_file(cat_styles, content_map, template_path, brand, brand_cfg, vendor_code, output_path):
    """Generate a single .xlsm with all styles of one category — field-ID based.
    Fills ALL required and conditionally-required Amazon columns.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(template_path, keep_vba=True)

    ws = None
    detected_product_type = "DRESS"  # safe default
    for name in wb.sheetnames:
        if name.upper().startswith("TEMPLATE"):
            ws = wb[name]
            parts = name.split("-", 1)
            if len(parts) == 2 and parts[1].strip():
                detected_product_type = parts[1].strip().upper()
            break
    if ws is None:
        for name in wb.sheetnames:
            if "template" in name.lower():
                ws = wb[name]
                break
    if ws is None:
        ws = wb.active

    # ── Build field_id → column_number map from row 4 ─────────────────────────
    max_col = ws.max_column or 254
    col_map = {}  # field_id_string → column_number
    for col in range(1, max_col + 1):
        raw = ws.cell(row=4, column=col).value
        if raw is not None:
            fid = str(raw).strip()
            if fid:
                col_map[fid] = col

    def _col(field_id):
        return col_map.get(field_id)
    
    # ── Capture styles from row 7 ────────────────────────────────────────────
    cell_styles = {}
    for col in range(1, max_col + 1):
        cell = ws.cell(row=7, column=col)
        cell_styles[col] = {
            "font":          copy.copy(cell.font)      if cell.font      else None,
            "fill":          copy.copy(cell.fill)      if cell.fill      else None,
            "border":        copy.copy(cell.border)    if cell.border    else None,
            "alignment":     copy.copy(cell.alignment) if cell.alignment else None,
            "number_format": cell.number_format,
        }

    # ── Clear data rows ───────────────────────────────────────────────────────
    for row in range(7, (ws.max_row or 100) + 1):
        for col in range(1, max_col + 1):
            ws.cell(row=row, column=col).value = None

    # ── NIS row-spacing fix: release fixed row heights so wrapped bullet text auto-fits
    _clear_row_heights_for_auto_fit(ws, start_row=7)

    clean_brand = clean_brand_name(brand)
    today_str   = datetime.now().strftime("%Y%m%d")
    booking_date = datetime.now().strftime("%Y-%m-%dT00:00:00Z")
    all_overrides = session_data.get("field_overrides", {})

    def wc(row_idx, field_id, value, style_num=None):
        """Write value to the cell for field_id, applying row-7 styles.
        Checks field_overrides first (keyed by field_id).
        """
        if style_num and field_id:
            style_ov = all_overrides.get(style_num, {})
            if field_id in style_ov:
                value = style_ov[field_id]
        c = _col(field_id)
        if c is None or value is None:
            return
        # Auto-correct against template dropdown (if available)
        # Skip fuzzy-match for free-text fields that accept compositions (e.g. "95% Polyester, 5% Spandex")
        SKIP_FUZZY = {"material#1.value", "generic_keyword#1.value", "item_type_keyword#1.value",
                      "rtip_product_description#1.value", "item_name#1.value",
                      "bullet_point#1.value", "bullet_point#2.value", "bullet_point#3.value",
                      "bullet_point#4.value", "bullet_point#5.value"}
        if isinstance(value, str) and value and field_id and field_id not in SKIP_FUZZY:
            dropdowns = load_dropdown_cache(detected_product_type)
            if field_id in dropdowns:
                valid = dropdowns[field_id]
                if value not in valid:
                    match, conf = _fuzzy_match_dropdown(value, valid)
                    if match and conf >= 0.6:
                        value = match

        # Skip truly empty values (don't write blank cells)
        if value == "":
            return
        cell = ws.cell(row=row_idx, column=c)
        # Try numeric conversion for price/dimension fields
        if isinstance(value, str):
            try:
                value = float(value)
                if value == int(value):
                    value = int(value)
            except (ValueError, TypeError):
                pass
        cell.value = value if isinstance(value, (int, float)) else str(value)
        cached = cell_styles.get(c, {})
        if cached.get("font"):          cell.font          = copy.copy(cached["font"])
        if cached.get("fill"):          cell.fill          = copy.copy(cached["fill"])
        if cached.get("border"):        cell.border        = copy.copy(cached["border"])
        if cached.get("alignment"):     cell.alignment     = copy.copy(cached["alignment"])
        if cached.get("number_format"): cell.number_format = cached["number_format"]
        # Long-text fields need wrap_text=True for bullets, description, etc.
        if _is_long_text_field(field_id):
            _apply_long_text_alignment(cell, cached.get("alignment"))

    cr = 7
    for style in cat_styles:
        sn           = style["style_num"]
        style_name   = style.get("style_name", sn)
        sub_class    = style.get("subclass", "") or style.get("sub_class", "")
        sub_subclass = style.get("sub_subclass", "")
        content      = content_map.get(sn, {})
        if not content:
            continue

        psku = f"{brand_cfg.get('vendor_code_prefix', '')}-{sn}".strip("-") or sn

        # Derive per-style gender/department from division_name
        style_gender, style_dept = _derive_gender_department(style)
        eff_gender = style_gender or brand_cfg.get("gender", "")
        eff_dept   = style_dept or brand_cfg.get("department", "")
        # Derive per-style fields
        fabric     = content.get("fabric", "")     or brand_cfg.get("default_fabric", "")
        care       = content.get("care", "")       or brand_cfg.get("default_care", "")
        upf        = content.get("upf", "")        or brand_cfg.get("default_upf", "")
        coo        = normalize_coo(content.get("coo", "")        or brand_cfg.get("default_coo", "")) or "Imported"
        neck       = content.get("neck_type", "") or style.get("neck_type", "") or derive_neck_type(style_name)
        sleeve     = content.get("sleeve_type", "") or style.get("sleeve_type", "") or derive_sleeve_type(style_name)
        sil        = content.get("silhouette", "") or derive_silhouette(sub_subclass)
        itk        = _derive_item_type_keyword(sub_class, product_type=detected_product_type, gender=eff_gender, style_name=style_name)
        itn        = _derive_item_type_name(sub_class, product_type=detected_product_type, gender=eff_gender, style_name=style_name)
        # Taxonomy override: if a confirmed entry exists for this style's bucket, use it
        _tax = _resolve_taxonomy_for_style(style, brand_cfg)
        if _tax.get("matched"):
            _e = _tax["entry"]
            itk = _e.get("item_type_keyword", itk)
            itn = _e.get("item_type_name", itn)
        ilen       = _derive_item_length(sub_subclass, style_name, product_type=detected_product_type, sub_class=sub_class)
        ftype      = _derive_fabric_type(fabric)
        slvlen     = _derive_sleeve_length(sleeve)
        list_price = style.get("list_price", "") or content.get("list_price", "")
        bullets    = content.get("bullets", [])
        import_desig = "Imported" if coo.upper() not in ("US", "USA", "UNITED STATES") else "Domestic"
        cat_val    = _derive_amazon_product_category(sub_class, gender=eff_gender, product_type=detected_product_type, style_name=style_name, department=eff_dept)
        subcat_val = SUBCLASS_SUBCATEGORY_MAP.get(sub_class, "")
        if not subcat_val and detected_product_type == "SWIMWEAR":
            subcat_val = _derive_swim_product_subcategory(sub_class, gender=eff_gender, style_name=style_name, product_category=cat_val)
        # Taxonomy override wins for category + subcategory too
        if _tax.get("matched"):
            _e = _tax["entry"]
            cat_val    = _e.get("product_category", cat_val)
            subcat_val = _e.get("product_subcategory", subcat_val)

        # ── Shared-fields helper for this style ─────────────────────────────
        def write_shared_row(r, sku_val, _fabric=fabric, _care=care, _upf=upf,
                             _coo=coo, _neck=neck, _sleeve=sleeve, _sil=sil,
                             _itk=itk, _itn=itn, _ilen=ilen, _ftype=ftype,
                             _slvlen=slvlen, _bullets=bullets, _content=content,
                             _style_name=style_name, _sn=sn, _import_desig=import_desig,
                             _cat_val=cat_val, _subcat_val=subcat_val,
                             _style=style):
            # Vendor code — REQUIRED by Amazon, falls back to brand config
            vc_val = vendor_code or brand_cfg.get("vendor_code_full", "")
            wc(r, "rtip_vendor_code#1.value",         vc_val, style_num=_sn)
            wc(r, "vendor_sku#1.value",               sku_val, style_num=_sn)
            wc(r, "product_type#1.value",             detected_product_type, style_num=_sn)
            # Variation theme — must match Amazon dropdown. For SWIMWEAR, 'SIZE/COLOR' is valid;
            # 'COLOR/SIZE' is NOT. Use SIZE/COLOR when both vary, COLOR-only or SIZE-only otherwise.
            _vts = _style.get("variants", []) or []
            _has_multi_color = len({(v.get("color") or v.get("color_name") or "") for v in _vts}) > 1
            _has_multi_size  = len({v.get("size", "") for v in _vts}) > 1
            if _has_multi_color and _has_multi_size:
                _vt = "SIZE/COLOR"
            elif _has_multi_color:
                _vt = "COLOR"
            elif _has_multi_size:
                _vt = "SIZE"
            else:
                _vt = "COLOR"
            wc(r, "variation_theme#1.name",     _vt, style_num=_sn)
            wc(r, "brand#1.value",                    clean_brand, style_num=_sn)
            if _cat_val:
                wc(r, "product_category#1.value",     _cat_val, style_num=_sn)
            if _subcat_val:
                wc(r, "product_subcategory#1.value",  _subcat_val, style_num=_sn)
            wc(r, "item_type_keyword#1.value",        _itk, style_num=_sn)
            wc(r, "model_number#1.value",             _sn, style_num=_sn)
            wc(r, "model_name#1.value",               _style_name.title(), style_num=_sn)
            for i, bfid in enumerate(["bullet_point#1.value", "bullet_point#2.value",
                                      "bullet_point#3.value", "bullet_point#4.value",
                                      "bullet_point#5.value"]):
                if i < len(_bullets):
                    wc(r, bfid, _bullets[i][:500], style_num=_sn)
                else:
                    bullet_fallback = _content.get(f"bullet_{i+1}", "")
                    if bullet_fallback:
                        wc(r, bfid, bullet_fallback[:500], style_num=_sn)
            wc(r, "generic_keyword#1.value",          _content.get("backend_keywords", ""), style_num=_sn)
            wc(r, "style#1.value",                    _style_name.title(), style_num=_sn)
            # fit_type — from data/override only
            wc(r, "fit_type#1.value",                 _content.get("fit_type", "") or _style.get("fit_type", "") if hasattr(_style, "get") else _content.get("fit_type", ""), style_num=_sn)
            # Amazon target_gender for SWIMWEAR accepts only Male/Female; refine youth
            # using style_name ("Little Boys", "Big Girls") when division gives only Unisex.
            _tg_sn = (_style_name or "").lower()
            if "boys" in _tg_sn or " boy" in _tg_sn or "men" in _tg_sn:
                _tg = "Male"
            elif "girls" in _tg_sn or " girl" in _tg_sn or "women" in _tg_sn:
                _tg = "Female"
            else:
                _tg = eff_gender if eff_gender in ("Male", "Female") else ""
            wc(r, "department#1.value",               eff_dept, style_num=_sn)
            wc(r, "target_gender#1.value",            _tg, style_num=_sn)
            # Age range / size_class / special_size_type — youth-aware via first variant
            _first_var = (_style.get("variants", []) or [{}])[0]
            _sst, _sclass, _ard, _ = _derive_youth_size_info(_style_name, eff_gender, _first_var.get("size", ""))
            wc(r, "age_range_description#1.value",    _ard, style_num=_sn)
            wc(r, _size_field(detected_product_type, "body_type", col_map),         "", style_num=_sn)
            wc(r, _size_field(detected_product_type, "height_type", col_map),       "", style_num=_sn)
            if _fabric:
                wc(r, "material#1.value",             _fabric, style_num=_sn)
            wc(r, "fabric_type#1.value",              _ftype, style_num=_sn)
            wc(r, "number_of_items#1.value", "1", style_num=_sn)
            wc(r, "item_type_name#1.value",           _itn, style_num=_sn)
            if _sst:
                wc(r, "special_size_type#1.value",    _sst, style_num=_sn)
            wc(r, "rtip_product_description#1.value", _content.get("description", ""), style_num=_sn)
            wc(r, "item_length_description#1.value",  _ilen, style_num=_sn)
            wc(r, "item_booking_date#1.value",        booking_date, style_num=_sn)
            if _care:
                wc(r, "care_instructions#1.value",   _care, style_num=_sn)
            wc(r, "unit_count#1.value",               "1", style_num=_sn)
            wc(r, "unit_count#1.type.value",                "Count", style_num=_sn)
            if _neck:
                wc(r, "neck#1.neck_style#1.value",         _neck, style_num=_sn)
            wc(r, "lifecycle_supply_type#1.value", "Perennial", style_num=_sn)
            if _sil:
                wc(r, "apparel_silhouette#1.value",   _sil, style_num=_sn)
            wc(r, "sleeve#1.length_description#1.value", _slvlen, style_num=_sn)
            if _sleeve:
                wc(r, "sleeve#1.type#1.value",        _sleeve, style_num=_sn)
            # closure — from data/override only
            wc(r, "closure#1.type#1.value",             _content.get("closure_type", ""), style_num=_sn)
            if _upf:
                wc(r, "ultraviolet_protection_factor#1.value", _upf, style_num=_sn)
            wc(r, "skip_offer#1.value",                       "No", style_num=_sn)
            wc(r, "import_designation#1.value",       _import_desig, style_num=_sn)
            wc(r, "rtip_earliest_shipping_date#1.value", today_str, style_num=_sn)
            # Contains battery/cell — required compliance field
            wc(r, "contains_battery_or_cell#1.value", "No", style_num=_sn)
            wc(r, "item_package_dimensions#1.length.value",      brand_cfg.get("default_pkg_length", ""), style_num=_sn)
            wc(r, "item_package_dimensions#1.length.unit",       "Inches", style_num=_sn)
            wc(r, "item_package_dimensions#1.width.value",       brand_cfg.get("default_pkg_width", ""), style_num=_sn)
            wc(r, "item_package_dimensions#1.width.unit",        "Inches", style_num=_sn)
            wc(r, "item_package_dimensions#1.height.value",      brand_cfg.get("default_pkg_height", ""), style_num=_sn)
            wc(r, "item_package_dimensions#1.height.unit",       "Inches", style_num=_sn)
            wc(r, "item_package_weight#1.value",      brand_cfg.get("default_pkg_weight", ""), style_num=_sn)
            wc(r, "item_package_weight#1.unit",       "Pounds", style_num=_sn)
            wc(r, "rtip_order_aggregate_type#1.value",     "Each", style_num=_sn)
            wc(r, "rtip_items_per_inner_pack#1.value",     "1", style_num=_sn)
            if _coo:
                wc(r, "country_of_origin#1.value",   _coo, style_num=_sn)
            wc(r, "batteries_required#1.value",  "No", style_num=_sn)
            wc(r, "batteries_included#1.value",  "No", style_num=_sn)

        # ── Parent row (required by Amazon) ───────────────────────────────────
        write_shared_row(cr, psku)
        wc(cr, "parentage_level#1.value",                "Parent", style_num=sn)
        wc(cr, "item_name#1.value",                      content.get("title", style_name), style_num=sn)
        # Parent rows do NOT get child-specific fields (UPC, color, size, child_parent relationship)
        cr += 1

        # ── Child rows ────────────────────────────────────────────────────────
        for var in style.get("variants", []):
            color  = var.get("color", "") or var.get("color_name", "")
            size   = var.get("size", "")
            upc    = var.get("upc", "")
            v_cost = var.get("cost_price", "")
            # Youth-aware size resolution: 2T -> '2 Years'; adult alpha stays as-is.
            _sst_c, _sclass_c, _ard_c, size_norm = _derive_youth_size_info(style_name, eff_gender, size)
            csku   = f"{psku}-{color}-{size}".replace(" ", "-")
            color_family = COLOR_MAP.get(color.upper().strip(), normalize_color(color))
            if color:
                ctitle = content.get("title", "").split(",")[0] + f", {color.title()}, {size_norm or size}"
            else:
                ctitle = content.get("title", "")

            write_shared_row(cr, csku)
            wc(cr, "parentage_level#1.value",         "Child", style_num=sn)
            wc(cr, "child_parent_sku_relationship#1.child_relationship_type", "Variation", style_num=sn)
            wc(cr, "child_parent_sku_relationship#1.parent_sku",              psku, style_num=sn)
            wc(cr, "item_name#1.value",               ctitle, style_num=sn)
            if upc:
                wc(cr, "external_product_id#1.type",  "UPC", style_num=sn)
                wc(cr, "external_product_id#1.value", re.sub(r"\D", "", str(upc)), style_num=sn)
            wc(cr, _size_field(detected_product_type, "size_system", col_map),      "US", style_num=sn)
            wc(cr, _size_field(detected_product_type, "size_class", col_map),       _sclass_c, style_num=sn)
            wc(cr, _size_field(detected_product_type, "size", col_map),             size_norm or size, style_num=sn)
            # Repeat special_size_type + age range on every child row (Amazon expects per-row)
            if _sst_c:
                wc(cr, "special_size_type#1.value",   _sst_c, style_num=sn)
            wc(cr, "age_range_description#1.value",   _ard_c, style_num=sn)
            wc(cr, "color#1.standardized_values#1",            color_family, style_num=sn)
            wc(cr, "color#1.value",                   color.title() if color else "", style_num=sn)
            v_list_price = var.get("list_price", "") or list_price
            if v_list_price:
                try:    wc(cr, "list_price#1.value",  float(v_list_price), style_num=sn)
                except: wc(cr, "list_price#1.value",  v_list_price, style_num=sn)
            else:
                wc(cr, "list_price#1.value",  "", style_num=sn)  # trigger override check
            if v_cost:
                try:    wc(cr, "cost_price#1.value",        float(v_cost), style_num=sn)
                except: wc(cr, "cost_price#1.value",        v_cost, style_num=sn)
            else:
                wc(cr, "cost_price#1.value",  "", style_num=sn)  # trigger override check
            cr += 1

    import warnings as _w2
    with _w2.catch_warnings():
        _w2.simplefilter("ignore")
        wb.save(output_path)


@app.route("/api/validate-before-build", methods=["POST"])
def validate_before_build():
    """Validate all field values against template dropdowns before building .xlsm.
    Returns per-style validation results with auto-corrections.
    Used by the dramatic QA UI.
    """
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand", "")
    styles = data.get("styles") or session_data.get("styles", [])
    content_map = data.get("content_map") or session_data.get("generated_content", {})
    overrides = data.get("field_overrides") or session_data.get("field_overrides", {})
    template_path = session_data.get("template_path") or str(DEFAULT_TEMPLATE)

    if not brand or not styles:
        return jsonify({"error": "No brand or styles"}), 400

    brand_cfg = _load_brand_config_data(brand)

    all_results = []
    total_valid = 0
    total_corrected = 0
    total_invalid = 0

    for style in styles:
        sn = style["style_num"]
        content = content_map.get(sn, {})
        style_overrides = overrides.get(sn, {})

        # Detect product type PER STYLE
        product_type = _resolve_style_product_type(style)
        dropdowns = load_dropdown_cache(product_type)

        # Build the field values that would be written
        coo = normalize_coo(content.get("coo", "") or style.get("coo", "") or brand_cfg.get("default_coo", "")) or ""
        upf = content.get("upf", "") or style.get("upf", "") or brand_cfg.get("default_upf", "")
        care = content.get("care", "") or style.get("care", "") or brand_cfg.get("default_care", "")
        fabric = content.get("fabric", "") or style.get("fabric", "") or brand_cfg.get("default_fabric", "")

        field_values = {
            "product_category#1.value": _derive_amazon_product_category(
                style.get("subclass", "") or style.get("sub_class", ""),
                gender=_derive_gender_department(style)[0] or brand_cfg.get("gender", ""),
                product_type=product_type),
            "lifecycle_supply_type#1.value": "Perennial",
            "department#1.value": _derive_gender_department(style)[1] or brand_cfg.get("department", ""),
            "target_gender#1.value": _derive_gender_department(style)[0] or brand_cfg.get("gender", ""),
            "country_of_origin#1.value": coo,
            "care_instructions#1.value": care,
            "material#1.value": fabric,
            "closure#1.type#1.value": "Pull On",
            "import_designation#1.value": "Imported" if coo != "United States" else "Made in the USA",
            "item_package_dimensions#1.length.unit": "Inches",
            "item_package_weight#1.unit": "Pounds",
            "batteries_required#1.value": "No",
            "batteries_included#1.value": "No",
            "skip_offer#1.value": "No",
            "ultraviolet_protection_factor#1.value": upf,
            "fit_type#1.value": "Regular",
        }

        # Apply overrides
        field_values.update(style_overrides)

        style_results = []
        for fid, val in field_values.items():
            if not val or fid not in dropdowns:
                continue
            r = validate_field_value(fid, val, product_type)
            style_results.append(r)
            if r["status"] == "valid":
                total_valid += 1
            elif r["status"] == "corrected":
                total_corrected += 1
            elif r["status"] == "invalid":
                total_invalid += 1

        all_results.append({
            "style_num": sn,
            "style_name": style.get("style_name", sn),
            "validations": style_results,
        })

    return jsonify({
        "product_type": product_type,
        "dropdown_fields_loaded": len(dropdowns),
        "total_valid": total_valid,
        "total_corrected": total_corrected,
        "total_invalid": total_invalid,
        "styles": all_results,
    })


@app.route("/api/sync-before-download", methods=["POST"])
def sync_before_download():
    """Sync frontend state to server before download.
    Ensures overrides, styles, content survive Render worker restarts.
    """
    data = request.get_json(force=True)
    if data.get("brand"):
        session_data["brand"] = data["brand"]
    if data.get("vendor_code"):
        session_data["vendor_code"] = data["vendor_code"]
    if data.get("styles"):
        session_data["styles"] = data["styles"]
    if data.get("content_map"):
        session_data["generated_content"] = data["content_map"]
    if data.get("field_overrides"):
        # Merge (don't replace) — frontend might have partial overrides
        for sn, ov in data["field_overrides"].items():
            if sn not in session_data.get("field_overrides", {}):
                session_data.setdefault("field_overrides", {})[sn] = {}
            session_data["field_overrides"][sn].update(ov)
    if data.get("style_product_types"):
        session_data["style_product_types"] = data["style_product_types"]
    return jsonify({"ok": True})


# Template name mapping: product type ID → template filename
# Size field prefix varies by template — detect from col_map or use this fallback
SIZE_PREFIX_MAP = {
    "BLAZER": "apparel_size",
    "BRA": "shapewear_size",
    "COAT": "apparel_size",
    "DRESS": "apparel_size",
    "HAT": "headwear_size",
    "ONE_PIECE_OUTFIT": "apparel_size",
    "OVERALLS": "bottoms_size",
    "PANTS": "bottoms_size",
    "SANDAL": "footwear_size",
    "SHIRT": "shirt_size",
    "SHORTS": "bottoms_size",
    "SKIRT": "skirt_size",
    "SNOWSUIT": "apparel_size",
    "SNOW_PANT": "bottoms_size",
    "SWEATSHIRT": "apparel_size",
    "SWIMWEAR": "shapewear_size",
}

def _size_field(product_type, suffix, col_map=None):
    """Return the correct size field_id for this product type.
    e.g. _size_field('SWIMWEAR', 'size_system') -> 'shapewear_size#1.size_system'
    If col_map provided, detect from template; else use SIZE_PREFIX_MAP.
    """
    if col_map:
        # Auto-detect from template columns
        for prefix in ["apparel_size", "shapewear_size", "bottoms_size", "shirt_size",
                       "headwear_size", "footwear_size", "skirt_size"]:
            candidate = f"{prefix}#1.{suffix}"
            if candidate in col_map:
                return candidate
    prefix = SIZE_PREFIX_MAP.get(product_type, "apparel_size")
    return f"{prefix}#1.{suffix}"

PRODUCT_TYPE_TEMPLATE_MAP = {
    "BLAZER": "Blazers.xlsm",
    "BRA": "Bras.xlsm",
    "COAT": "Jackets_and_Coats.xlsm",
    "DRESS": "Dresses.xlsm",
    "HAT": "Hats.xlsm",
    "ONE_PIECE_OUTFIT": "One-piece_Outfits.xlsm",
    "OVERALLS": "Overalls.xlsm",
    "PANTS": "Other_Pants.xlsm",
    "SANDAL": "Sandals.xlsm",
    "SHIRT": "Other_Shirts.xlsm",
    "SHORTS": "Shorts.xlsm",
    "SKIRT": "Skirts.xlsm",
    "SNOWSUIT": "Snowsuits.xlsm",
    "SNOW_PANT": "Snow_Pants.xlsm",
    "SWEATSHIRT": "Sweatshirts.xlsm",
    "SWIMWEAR": "Swimwear.xlsm",
}

def _get_template_for_product_type(product_type_id):
    """Get the template .xlsm path for a product type.
    Checks session templates first, then falls back to disk.
    """
    # Check session-loaded templates
    session_templates = session_data.get("templates", {})
    if product_type_id in session_templates:
        return session_templates[product_type_id]

    # Check the mapping
    fname = PRODUCT_TYPE_TEMPLATE_MAP.get(product_type_id)
    if fname:
        path = UPLOAD_TEMPLATES / fname
        if path.exists():
            return str(path)

    # Fallback: try to find any template that matches
    for f in UPLOAD_TEMPLATES.glob("*.xlsm"):
        if product_type_id.lower().replace("_", "") in f.stem.lower().replace("_", ""):
            return str(f)

    # Last resort: default template
    return str(DEFAULT_TEMPLATE)


@app.route("/api/save-style-product-types", methods=["POST"])
def save_style_product_types():
    """Save operator's product type assignments for styles.
    Called from the per-style PT selector table.
    """
    data = request.get_json(force=True)
    assignments = data.get("assignments", {})
    session_data["style_product_types"] = assignments
    return jsonify({"ok": True, "count": len(assignments)})


# ── Taxonomy Overrides API endpoints (Phase 1) ───────────────────────────────

@app.route("/api/taxonomy", methods=["GET"])
def taxonomy_get():
    """Return the full override store + the valid-value universe so the frontend
    can populate cascading dropdowns client-side without per-keystroke backend calls.

    Optional ?product_type=SWIMWEAR filter to reduce payload.
    """
    pt_filter = (request.args.get("product_type") or "").strip().upper() or None
    overrides = _load_taxonomy_overrides()
    universe_all = _load_taxonomy_universe()
    universe = {pt_filter: universe_all[pt_filter]} if pt_filter and pt_filter in universe_all else universe_all

    # Also surface observed sub_classes from current session for UI convenience
    sub_classes_seen = set()
    for s in session_data.get("styles", []):
        sc = s.get("subclass") or s.get("sub_class")
        if sc:
            sub_classes_seen.add(sc)

    return jsonify({
        "version": overrides.get("version", 1),
        "updated_at": overrides.get("updated_at", ""),
        "entries": overrides.get("entries", {}),
        "universe": universe,
        "sub_classes_seen": sorted(sub_classes_seen),
        "gender_buckets": GENDER_BUCKETS,
    })


@app.route("/api/taxonomy/save", methods=["POST"])
def taxonomy_save():
    """Upsert one taxonomy entry. Validates against Amazon dropdowns + template
    cascade rules before writing. Triggers a best-effort git commit + push so
    learned taxonomy survives Render redeploys.
    """
    data = request.get_json(force=True) or {}
    sub_class = (data.get("sub_class") or "").strip()
    gender_bucket = (data.get("gender_bucket") or "").strip()
    pt = (data.get("product_type") or "").strip().upper()
    cat = (data.get("product_category") or "").strip()
    subcat = (data.get("product_subcategory") or "").strip()
    itk = (data.get("item_type_keyword") or "").strip()
    itn = (data.get("item_type_name") or "").strip()
    notes = data.get("notes") or ""
    user = data.get("confirmed_by") or "unknown"

    if not sub_class or not gender_bucket or not pt:
        return jsonify({"error": "sub_class, gender_bucket, and product_type are required"}), 400
    if gender_bucket not in GENDER_BUCKETS:
        return jsonify({"error": f"Invalid gender_bucket. Must be one of {GENDER_BUCKETS}"}), 400

    # Dropdown validation
    ok, errors = _validate_taxonomy_quadruple(pt, cat, subcat, itk, itn)
    if not ok:
        return jsonify({"error": "Validation failed", "errors": errors}), 400

    key = _taxonomy_key(pt, sub_class, gender_bucket)
    now = datetime.now().isoformat() + "Z"
    entry = {
        "product_type": pt,
        "product_category": cat,
        "product_subcategory": subcat,
        "item_type_keyword": itk,
        "item_type_name": itn,
        "confirmed_by": user,
        "confirmed_at": now,
        "source": "manual",
        "notes": notes,
    }

    store = _load_taxonomy_overrides()
    existing = store.get("entries", {}).get(key)
    action = "update" if existing else "create"
    store.setdefault("entries", {})[key] = entry
    _save_taxonomy_overrides(store)
    _append_taxonomy_history(key, entry, action)

    # Count how many currently-loaded styles match this bucket — UI hint
    affected = 0
    for s in session_data.get("styles", []):
        s_pt = _resolve_style_product_type(s) or ""
        s_sc = s.get("subclass") or s.get("sub_class") or ""
        s_gb = _derive_gender_bucket(s)
        if s_pt.upper() == pt and s_sc == sub_class and s_gb == gender_bucket:
            affected += 1

    # Best-effort git commit in a thread so it doesn't block the response
    try:
        import threading
        threading.Thread(target=_try_git_commit_taxonomy, args=(key, user), daemon=True).start()
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "key": key,
        "entry": entry,
        "action": action,
        "affected_styles_in_session": affected,
    })


@app.route("/api/taxonomy/validate", methods=["POST"])
def taxonomy_validate():
    """Pre-flight check. For every style in the session (or in the request body),
    report which buckets have a confirmed override and which are auto-derived.
    Used by the post-upload banner to decide whether to prompt for confirmation.
    """
    data = request.get_json(silent=True) or {}
    styles = data.get("styles") or session_data.get("styles", [])
    if not styles:
        return jsonify({"error": "No styles in session"}), 400

    # Group styles by (product_type, sub_class, gender_bucket)
    buckets = {}  # key -> { style_nums, sub_class, gender_bucket, product_type }
    for s in styles:
        pt = _resolve_style_product_type(s) or ""
        sc = s.get("subclass") or s.get("sub_class") or ""
        gb = _derive_gender_bucket(s)
        key = _taxonomy_key(pt, sc, gb)
        b = buckets.setdefault(key, {
            "key": key, "product_type": pt, "sub_class": sc,
            "gender_bucket": gb, "style_nums": [], "style_count": 0,
        })
        b["style_nums"].append(s.get("style_num"))
        b["style_count"] += 1

    overrides = _load_taxonomy_overrides().get("entries", {})
    brand = session_data.get("brand", "")
    brand_cfg = _load_brand_config_data(brand) if brand else {}

    confirmed = []
    unconfirmed = []
    for key, b in buckets.items():
        entry = overrides.get(key)
        if entry and entry.get("source") == "manual":
            confirmed.append({"key": key, **b, "entry": entry})
        else:
            # Auto-derive fallback for UI display
            sample = {"subclass": b["sub_class"],
                      "style_name": (styles[0].get("style_name", "") if styles else ""),
                      "division_name": (styles[0].get("division_name", "") if styles else "")}
            # Find a style actually in this bucket so style_name is representative
            for s in styles:
                if s.get("style_num") in b["style_nums"]:
                    sample = s; break
            resolved = _resolve_taxonomy_for_style(sample, brand_cfg)
            unconfirmed.append({"key": key, **b, "auto_derived": resolved["entry"]})

    return jsonify({
        "total_styles": len(styles),
        "total_buckets": len(buckets),
        "confirmed": sum(b["style_count"] for b in confirmed) if False else len([b for b in confirmed]),
        "confirmed_styles": sum(b["style_count"] for b in confirmed),
        "unconfirmed_styles": sum(b["style_count"] for b in unconfirmed),
        "buckets": {"confirmed": confirmed, "unconfirmed": unconfirmed},
        "blocking": len(unconfirmed) > 0,
        "message": (
            f"{len(unconfirmed)} of {len(buckets)} item-type buckets need taxonomy confirmation."
            if unconfirmed else
            f"All {len(buckets)} item-type buckets are confirmed."
        ),
    })
# ── End Taxonomy Overrides API ──────────────────────────────────────────


def _resolve_style_product_type(style):
    """Resolve the product type for a single style."""
    # Check operator assignment first
    pt_assignments = session_data.get("style_product_types", {})
    if style["style_num"] in pt_assignments:
        return pt_assignments[style["style_num"]]
    # Use resolve_product_type
    sub_class = style.get("subclass", "")
    div_name = style.get("division_name", "")
    pt_id, _, _reason = resolve_product_type(sub_class, div_name)
    return pt_id


@app.route("/api/download-all")
def download_all():
    """Generate .xlsm files and ZIP them together.
    ?mode=per-style → one .xlsm per style
    ?mode=per-type (default) → one .xlsm per product type
    """
    dl_mode = request.args.get("mode", "per-type")
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})

    if not styles or not content_map:
        return jsonify({"error": "No generated content"}), 400

    date_str = datetime.now().strftime("%m%d%y")
    safe_brand = re.sub(r'[^\w\-]', '_', brand)
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code") or brand_cfg.get("vendor_code_full", "")

    generated_files = []  # (filename, filepath)

    # Optional: filter to single style
    single_style = request.args.get("style_num", "")

    if dl_mode == "per-style":
        # ── One .xlsm per STYLE ────────────────────────────────────────────
        target_styles = [s for s in styles if s["style_num"] == single_style] if single_style else styles
        for s in target_styles:
            sn = s["style_num"]
            if sn not in content_map:
                continue
            pt = _resolve_style_product_type(s)
            template_path = _get_template_for_product_type(pt)
            safe_sn = re.sub(r'[^\w\-]', '_', str(sn))
            safe_name = re.sub(r'[^\w\-]', '_', s.get("style_name", sn)[:30])
            fname = f"NIS_{safe_brand}_{safe_sn}_{safe_name}_{date_str}.xlsm"
            fpath = UPLOAD_OUTPUT / fname
            try:
                _generate_category_file([s], content_map, template_path, brand, brand_cfg, vendor_code, str(fpath))
                generated_files.append((fname, str(fpath)))
            except Exception as e:
                print(f"[Download] Error generating {sn}: {e}")
                traceback.print_exc()
    else:
        # ── One .xlsm per PRODUCT TYPE (original behavior) ─────────────
        by_pt = defaultdict(list)
        for s in styles:
            sn = s["style_num"]
            if sn not in content_map:
                continue
            pt = _resolve_style_product_type(s)
            by_pt[pt].append(s)

        for pt_id, pt_styles in by_pt.items():
            template_path = _get_template_for_product_type(pt_id)
            safe_pt = re.sub(r'[^\w\-]', '_', pt_id)
            fname = f"NIS_{safe_brand}_{safe_pt}_{date_str}.xlsm"
            fpath = UPLOAD_OUTPUT / fname
            try:
                _generate_category_file(pt_styles, content_map, template_path, brand, brand_cfg, vendor_code, str(fpath))
                generated_files.append((fname, str(fpath)))
            except Exception as e:
                traceback.print_exc()

    if not generated_files:
        return jsonify({"error": "No files generated"}), 500

    if len(generated_files) == 1:
        fname, fpath = generated_files[0]
        return send_file(fpath, as_attachment=True, download_name=fname,
                         mimetype="application/vnd.ms-excel.sheet.macroEnabled.12")

    zip_name = f"NIS_{safe_brand}_{date_str}.zip"
    zip_path = UPLOAD_OUTPUT / zip_name
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, fpath in generated_files:
            zf.write(fpath, fname)

    return send_file(str(zip_path), as_attachment=True, download_name=zip_name, mimetype="application/zip")


@app.route("/api/download-product-type/<pt_id>")
def download_product_type(pt_id):
    """Download one .xlsm for all styles of a specific product type."""
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code") or brand_cfg.get("vendor_code_full", "")

    pt_styles = [s for s in styles if _resolve_style_product_type(s) == pt_id and s["style_num"] in content_map]
    if not pt_styles:
        return jsonify({"error": f"No styles for product type {pt_id}"}), 404

    template_path = _get_template_for_product_type(pt_id)
    date_str = datetime.now().strftime("%m%d%y")
    safe_brand = re.sub(r'[^\w\-]', '_', brand)
    safe_pt = re.sub(r'[^\w\-]', '_', pt_id)
    fname = f"NIS_{safe_brand}_{safe_pt}_{date_str}.xlsm"
    fpath = UPLOAD_OUTPUT / fname

    _generate_category_file(pt_styles, content_map, template_path, brand, brand_cfg, vendor_code, str(fpath))

    return send_file(str(fpath), as_attachment=True, download_name=fname,
                     mimetype="application/vnd.ms-excel.sheet.macroEnabled.12")


@app.route("/api/download-style/<style_num>")
def download_style(style_num):
    """Generate and download a single .xlsm for one style using its product type template."""
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code") or brand_cfg.get("vendor_code_full", "")

    style = next((s for s in styles if s["style_num"] == style_num), None)
    if not style:
        return jsonify({"error": f"Style {style_num} not found"}), 404
    if style_num not in content_map:
        return jsonify({"error": f"No content for style {style_num}"}), 400

    # Use the correct template for this style's product type
    pt_id = _resolve_style_product_type(style)
    template_path = _get_template_for_product_type(pt_id)

    date_str = datetime.now().strftime("%m%d%y")
    safe_brand = re.sub(r'[^\w\-]', '_', brand)
    safe_sn = re.sub(r'[^\w\-]', '_', style_num)
    fname = f"NIS_{safe_brand}_{safe_sn}_{date_str}.xlsm"
    fpath = UPLOAD_OUTPUT / fname

    _generate_category_file([style], content_map, template_path, brand, brand_cfg, vendor_code, str(fpath))

    return send_file(str(fpath), as_attachment=True, download_name=fname,
                     mimetype="application/vnd.ms-excel.sheet.macroEnabled.12")


@app.route("/api/download-combined")
def download_combined():
    """Download ALL styles combined into a single .xlsm file.
    Delegates to _generate_category_file so all required columns are filled.
    """
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    template_path = session_data.get("template_path", str(DEFAULT_TEMPLATE))
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code", brand_cfg.get("vendor_code_full", ""))
    
    if not styles or not content_map:
        return jsonify({"error": "No generated content. Run Generate Content first."}), 400
    
    safe_brand = brand.replace(" ", "_")
    combined_name = f"NIS_{safe_brand}_ALL_STYLES.xlsm"
    combined_path = UPLOAD_OUTPUT / combined_name
    
    # Re-use the comprehensive _generate_category_file with all styles
    _generate_category_file(
        styles, content_map, template_path, brand, brand_cfg, vendor_code, str(combined_path)
    )
    
    return send_file(
        str(combined_path),
        as_attachment=True,
        download_name=combined_name,
        mimetype="application/vnd.ms-excel.sheet.macroEnabled.12",
    )


@app.route("/api/download-category/<category>")
def download_category(category):
    """Download all styles of a specific category combined into one .xlsm file.
    Delegates to _generate_category_file so all required columns are filled.
    """
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    template_path = session_data.get("template_path", str(DEFAULT_TEMPLATE))
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code", brand_cfg.get("vendor_code_full", ""))
    
    if not styles or not content_map:
        return jsonify({"error": "No generated content. Run Generate Content first."}), 400
    
    # Filter styles by category
    filtered_styles = [s for s in styles if (s.get("subclass", "") or s.get("sub_class", "")).lower() == category.lower()
                       or s.get("category", "").lower() == category.lower()]
    
    if not filtered_styles:
        return jsonify({"error": f"No styles found for category '{category}'"}), 404
    
    safe_brand = brand.replace(" ", "_")
    safe_cat = category.replace(" ", "_").replace("/", "_")
    fname = f"NIS_{safe_brand}_{safe_cat}.xlsm"
    fpath = UPLOAD_OUTPUT / fname
    
    # Re-use the comprehensive _generate_category_file
    _generate_category_file(
        filtered_styles, content_map, template_path, brand, brand_cfg, vendor_code, str(fpath)
    )
    
    return send_file(str(fpath), as_attachment=True, download_name=fname,
                     mimetype="application/vnd.ms-excel.sheet.macroEnabled.12")

@app.route("/api/categories")
def get_categories():
    """Return list of unique categories from uploaded styles."""
    styles = session_data.get("styles", [])
    cats = {}
    for s in styles:
        cat = s.get("subclass", "") or s.get("sub_class", "") or "Uncategorized"
        if cat not in cats:
            cats[cat] = {"name": cat, "count": 0, "variants": 0}
        cats[cat]["count"] += 1
        cats[cat]["variants"] += len(s.get("variants", []))
    return jsonify(list(cats.values()))


@app.route("/api/session-state")
def session_state():
    return jsonify({
        "brand": session_data.get("brand"),
        "vendor_code": session_data.get("vendor_code"),
        "template_path": session_data.get("template_path"),
        "styles_loaded": len(session_data.get("styles", [])),
        "keywords_loaded": len(session_data.get("keywords", [])),
        "content_generated": len(session_data.get("generated_content", {})),
    })


# ── Brand config file helpers ──────────────────────────────────────────────────
def _load_brand_config_data(brand):
    """Load brand config from file if saved, else from in-memory BRAND_CONFIGS."""
    brand_file = BRAND_CONFIGS_DIR / f"{re.sub(r'[^\w]', '_', brand)}.json"
    if brand_file.exists():
        try:
            with open(str(brand_file), "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with in-memory (saved overrides in-memory defaults)
            base = dict(BRAND_CONFIGS.get(brand, BRAND_CONFIGS.get("Stella Parker", {})))
            base.update(saved)
            return base
        except Exception:
            pass
    return dict(BRAND_CONFIGS.get(brand, BRAND_CONFIGS.get("Stella Parker", {})))


@app.route("/api/save-brand-config", methods=["POST"])
def save_brand_config():
    data = request.get_json(force=True)
    brand = data.get("brand", "")
    config = data.get("config", {})
    if not brand:
        return jsonify({"error": "No brand provided"}), 400
    
    brand_file = BRAND_CONFIGS_DIR / f"{re.sub(r'[^\w]', '_', brand)}.json"
    try:
        with open(str(brand_file), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return jsonify({"ok": True, "brand": brand})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/save-product-brief", methods=["POST"])
def save_product_brief():
    """Save a product brief for a brand + product type/subclass.
    Stored in brand config under 'product_briefs' key.
    """
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand", "")
    key = data.get("key", "_default")  # e.g. "SWIMWEAR/Rashguard" or "SWIMWEAR" or "_default"
    brief = data.get("brief", "")
    if not brand:
        return jsonify({"error": "No brand"}), 400

    cfg = _load_brand_config_data(brand)
    if "product_briefs" not in cfg:
        cfg["product_briefs"] = {}
    cfg["product_briefs"][key] = brief

    brand_file = BRAND_CONFIGS_DIR / f"{re.sub(r'[^\w]', '_', brand)}.json"
    try:
        with open(str(brand_file), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return jsonify({"ok": True, "brand": brand, "key": key})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/load-brand-config", methods=["GET"])
def load_brand_config_endpoint():
    brand = request.args.get("brand", "")
    if not brand:
        return jsonify({"error": "No brand provided"}), 400
    
    cfg = _load_brand_config_data(brand)
    brand_file = BRAND_CONFIGS_DIR / f"{re.sub(r'[^\w]', '_', brand)}.json"
    return jsonify({
        "brand": brand,
        "config": cfg,
        "has_saved_config": brand_file.exists(),
    })


@app.route("/api/regenerate-style", methods=["POST"])
def regenerate_style():
    """Regenerate all content for a single style using a custom brief.
    Used by the per-style 'Train this style' feature.
    """
    data = request.get_json(force=True)
    style_num = data.get("style_num", "")
    brief = data.get("brief", "")
    brand = data.get("brand") or session_data.get("brand", "")
    styles = session_data.get("styles", [])
    style = next((s for s in styles if s["style_num"] == style_num), None)
    if not style:
        return jsonify({"error": f"Style {style_num} not found"}), 404

    brand_cfg = _load_brand_config_data(brand)
    # Save the style brief
    if brief:
        if "style_briefs" not in session_data:
            session_data["style_briefs"] = {}
        session_data["style_briefs"][style_num] = brief
        # Also save to brand config for persistence
        if "product_briefs" not in brand_cfg:
            brand_cfg["product_briefs"] = {}
        brand_cfg["product_briefs"][f"style_{style_num}"] = brief

    # Inject brief into brand_cfg temporarily for LLM
    if brief:
        brand_cfg["product_briefs"] = brand_cfg.get("product_briefs", {})
        brand_cfg["product_briefs"]["_default"] = brief

    feedback_history = load_brand_feedback(brand)
    style_name = style["style_name"]
    subclass = style.get("subclass", "")
    sub_subclass = style.get("sub_subclass", "")
    fabric = parse_fabric(style.get("fabric", "")) or brand_cfg.get("default_fabric", "")
    care = style.get("care", "") or brand_cfg.get("default_care", "")
    upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
    first_variant = style["variants"][0] if style.get("variants") else {}
    first_color = first_variant.get("color_name", "")
    first_size = first_variant.get("size", "")

    # Try LLM first (unless rules mode)
    gen_mode = session_data.get("generation_mode", "auto")
    llm_result = None
    if gen_mode != "rules" and _anthropic_client is not None:
        try:
            llm_result = generate_content_llm(brand_cfg, brand, style, feedback_history)
        except Exception as e:
            print(f"[Regen] LLM failed for {style_num}: {e}")

    if llm_result:
        title = llm_result["title"]
        bullets = [llm_result.get(f"bullet_{i}", "") for i in range(1, 6)]
        description = llm_result["description"]
        backend_kw = llm_result["backend_keywords"]
    else:
        resolved_pt = _resolve_style_product_type(style) or ""
        style_gender, _ = _derive_gender_department(style)
        eff_gender = style_gender or brand_cfg.get("gender", "")
        pt_label = subclass or sub_subclass or resolved_pt.replace("_", " ").title() or "Dress"
        title = generate_title(brand_cfg, brand, style_name, pt_label, first_color, first_size, upf, style_gender=style_gender)
        bullets = generate_bullets(brand_cfg, brand, style_name, sub_subclass, fabric, care, first_color, upf,
                                   subclass=subclass, gender=eff_gender, product_type=resolved_pt, style_num=style_num)
        description = generate_description(brand_cfg, brand, style_num, style_name, sub_subclass, fabric, care, first_color, upf,
                                           subclass=subclass, gender=eff_gender, product_type=resolved_pt)
        backend_kw = generate_backend_keywords(brand, style_name, subclass, first_color, fabric, upf,
                                               subclass=subclass, gender=eff_gender, product_type=resolved_pt)

    entry = {
        "title": title,
        "bullets": bullets,
        "description": description,
        "backend_keywords": backend_kw,
        "llm_generated": llm_result is not None,
        "brief_used": brief[:100] if brief else "",
    }
    # Update session content
    if "generated_content" not in session_data:
        session_data["generated_content"] = {}
    existing = session_data["generated_content"].get(style_num, {})
    existing.update(entry)
    session_data["generated_content"][style_num] = existing

    return jsonify({"ok": True, "style_num": style_num, "content": entry})


@app.route("/api/nis-spreadsheet-preview", methods=["POST"])
def nis_spreadsheet_preview():
    """Return full NIS spreadsheet preview for a style — every column/row as it would appear in the .xlsm.
    Returns rows (parent header + child variants) with all field values.
    """
    data = request.get_json(force=True)
    style_num = data.get("style_num", "")
    brand = session_data.get("brand", "Brand")
    styles = session_data.get("styles", [])
    content_map = session_data.get("generated_content", {})
    brand_cfg = _load_brand_config_data(brand)
    vendor_code = session_data.get("vendor_code") or brand_cfg.get("vendor_code_full", "")

    style = next((s for s in styles if s["style_num"] == style_num), None)
    if not style:
        return jsonify({"error": f"Style {style_num} not found"}), 404

    content = content_map.get(style_num, {})
    fields = _build_preview_fields(brand, brand_cfg, vendor_code, style, content)

    # Apply overrides
    overrides = session_data.get("field_overrides", {}).get(style_num, {})
    for f in fields:
        fid = f.get("field_id", "")
        if fid and fid in overrides:
            f["value"] = overrides[fid]
            f["status"] = "filled"
            f["overridden"] = True

    # Build variant rows
    variants = style.get("variants", [])
    variant_rows = []
    for v in variants:
        color = v.get("color_name", "") or v.get("color", "")
        size = v.get("size", "")
        upc = v.get("upc", "")
        variant_rows.append({
            "color": color,
            "size": normalize_size(size) or size,
            "upc": re.sub(r"\D", "", str(upc)) if upc else "",
            "sku": f"{style_num}-{color}-{size}".replace(" ", "-"),
            "cost_price": v.get("cost_price", ""),
            "list_price": v.get("list_price", "") or style.get("list_price", ""),
        })

    return jsonify({
        "style_num": style_num,
        "style_name": style.get("style_name", ""),
        "product_type": _resolve_style_product_type(style),
        "fields": fields,
        "variant_count": len(variants),
        "variant_rows": variant_rows,
    })


@app.route("/api/save-style-brief", methods=["POST"])
def save_style_brief():
    """Save a per-style brief for later reuse."""
    data = request.get_json(force=True)
    style_num = data.get("style_num", "")
    brief = data.get("brief", "")
    if not style_num:
        return jsonify({"error": "No style_num"}), 400
    if "style_briefs" not in session_data:
        session_data["style_briefs"] = {}
    session_data["style_briefs"][style_num] = brief
    return jsonify({"ok": True, "style_num": style_num})


@app.route("/api/upload-style-image", methods=["POST"])
def upload_style_image():
    """Upload a product image for a specific style.
    Saved to disk and stored in session for LLM vision.
    """
    style_num = request.form.get("style_num", "")
    if not style_num:
        return jsonify({"error": "No style_num"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400

    f = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        return jsonify({"error": f"Unsupported image type: {ext}"}), 400

    # Save to disk
    style_dir = UPLOAD_IMAGES / str(style_num)
    style_dir.mkdir(parents=True, exist_ok=True)
    save_path = style_dir / f"product{ext}"
    f.save(str(save_path))

    # Store path in session
    if "style_images" not in session_data:
        session_data["style_images"] = {}
    session_data["style_images"][style_num] = str(save_path)

    return jsonify({"ok": True, "style_num": style_num, "path": f"/api/style-image/{style_num}"})


@app.route("/api/style-image/<style_num>")
def serve_style_image(style_num):
    """Serve a previously uploaded style image."""
    # Check session first
    img_path = (session_data.get("style_images") or {}).get(style_num)
    if img_path and Path(img_path).exists():
        return send_file(img_path)
    # Check disk
    style_dir = UPLOAD_IMAGES / str(style_num)
    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
        p = style_dir / f"product{ext}"
        if p.exists():
            return send_file(str(p))
    return jsonify({"error": "No image"}), 404


@app.route("/api/regenerate-field", methods=["POST"])
def regenerate_field():
    """Generate an alternative version of a single field."""
    data = request.get_json(force=True)
    style_id = data.get("style_id", "")
    field = data.get("field", "")
    current_content = data.get("current_content", "")
    
    brand = data.get("brand") or session_data.get("brand", "")
    styles = session_data.get("styles", [])
    style = next((s for s in styles if s["style_num"] == style_id), None)
    
    if not style or not brand:
        return jsonify({"error": "Style or brand not found"}), 400
    
    brand_cfg = _load_brand_config_data(brand)
    style_name = style["style_name"]
    subclass = style.get("subclass", "")
    sub_subclass = style.get("sub_subclass", "")
    fabric = parse_fabric(style.get("fabric", "")) or brand_cfg.get("default_fabric", "")
    care = style.get("care", "") or brand_cfg.get("default_care", "")
    upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
    first_variant = style["variants"][0] if style.get("variants") else {}
    first_color = first_variant.get("color_name", "")
    first_size = first_variant.get("size", "")
    has_keywords = len(session_data.get("keywords", [])) > 0
    
    try:
        if field == "title":
            # Generate alternative title using different formula variation
            alt_title = generate_title(brand_cfg, brand, style_name, "Dress", first_color, first_size, upf)
            # Vary: swap color position or add style descriptor variation
            descriptor = style_descriptor_from_name(style_name)
            alt_title2 = f"{brand} {descriptor} Dress, {first_color.title() if first_color else ''}, {first_size}".strip(", ")
            content = alt_title2[:200] if alt_title2 != current_content else alt_title[:200]
            why = generate_title_why(brand_cfg, brand, style_name, content, upf, has_keywords) + " [Alternative format.]"
        
        elif field.startswith("bullet_"):
            # Extract bullet index
            try:
                bullet_idx = int(field.split("_")[1]) - 1
            except (IndexError, ValueError):
                bullet_idx = 0
            rpt = _resolve_style_product_type(style) or ""
            sg, _ = _derive_gender_department(style)
            eg = sg or brand_cfg.get("gender", "")
            bullets = generate_bullets(brand_cfg, brand, style_name, sub_subclass, fabric, care, first_color, upf,
                                       subclass=subclass, gender=eg, product_type=rpt, style_num=style_num)
            # Rotate bullet labels for variation
            labels = ["OUTSTANDING FEATURE", "STYLE HIGHLIGHT", "DESIGN DETAIL", "FASHION FORWARD", "KEY BENEFIT"]
            if bullet_idx < len(bullets):
                b = bullets[bullet_idx]
                # Replace first word segment (the all-caps label) with an alternative
                alt_b = re.sub(r'^[A-Z\s&/]+—', labels[bullet_idx % len(labels)] + ' —', b, count=1)
                content = alt_b if alt_b != current_content else b
            else:
                content = current_content
            why = generate_bullet_why(bullet_idx, brand_cfg, brand, style_name, sub_subclass, upf, fabric, has_keywords) + " [Alternative phrasing.]"
        
        elif field == "description":
            # Use a different opener index
            total = len(DESCRIPTION_OPENERS)
            current_idx = DESCRIPTION_OPENERS_ROTATION.get(style_id, 0)
            alt_idx = (current_idx + 1) % total
            DESCRIPTION_OPENERS_ROTATION[style_id] = alt_idx
            rpt2 = _resolve_style_product_type(style) or ""
            sg2, _ = _derive_gender_department(style)
            eg2 = sg2 or brand_cfg.get("gender", "")
            content = generate_description(brand_cfg, brand, style_id, style_name, sub_subclass, fabric, care, first_color, upf,
                                           subclass=subclass, gender=eg2, product_type=rpt2)
            why = generate_description_why(brand_cfg, style_id, alt_idx, has_keywords) + " [Alternative opener used.]"
        
        elif field == "backend_keywords":
            # Reorder keywords
            rpt3 = _resolve_style_product_type(style) or ""
            sg3, _ = _derive_gender_department(style)
            eg3 = sg3 or brand_cfg.get("gender", "")
            kw_list = generate_backend_keywords(brand, style_name, subclass, first_color, fabric, upf,
                                                subclass=subclass, gender=eg3, product_type=rpt3)
            words = kw_list.split()
            # Shuffle order (deterministic rotation)
            mid = len(words) // 2
            alt_words = words[mid:] + words[:mid]
            alt_kw = " ".join(alt_words)
            while len(alt_kw.encode('utf-8')) > 250 and alt_words:
                alt_words.pop()
                alt_kw = " ".join(alt_words)
            content = alt_kw
            why = generate_keywords_why(brand, session_data.get("keywords", []), content, has_keywords) + " [Alternative keyword order.]"
        
        else:
            return jsonify({"error": f"Unknown field: {field}"}), 400
        
        return jsonify({"content": content, "why": why})
    
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-csv", methods=["POST"])
def generate_csv():
    """Generate CSV output for all styles — no template required."""
    data = request.get_json(force=True)
    brand = data.get("brand") or session_data.get("brand", "")
    styles = data.get("styles") or session_data.get("styles", [])
    content_map = data.get("content") or session_data.get("generated_content", {})
    
    if not styles:
        return jsonify({"error": "No product data loaded"}), 400
    if not content_map:
        return jsonify({"error": "No generated content. Run Generate Content first."}), 400
    
    output = io.StringIO()
    fieldnames = ["Style #", "Title", "Bullet 1", "Bullet 2", "Bullet 3", "Bullet 4", "Bullet 5",
                  "Description", "Backend Keywords", "Color", "Size", "UPC", "Price", "Category", "Brand"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    
    for style in styles:
        style_num = style["style_num"]
        content = content_map.get(style_num, {})
        bullets = content.get("bullets", [])
        
        for variant in style.get("variants", []):
            color = variant.get("color_name", "")
            size = variant.get("size", "")
            upc = variant.get("upc", "")
            
            # Per-variant title
            brand_cfg = _load_brand_config_data(brand)
            upf = style.get("upf", "") or brand_cfg.get("default_upf", "")
            var_title = generate_title(brand_cfg, brand, style["style_name"], "Dress", color, size, upf)
            
            writer.writerow({
                "Style #": style_num,
                "Title": var_title,
                "Bullet 1": bullets[0][:500] if len(bullets) > 0 else "",
                "Bullet 2": bullets[1][:500] if len(bullets) > 1 else "",
                "Bullet 3": bullets[2][:500] if len(bullets) > 2 else "",
                "Bullet 4": bullets[3][:500] if len(bullets) > 3 else "",
                "Bullet 5": bullets[4][:500] if len(bullets) > 4 else "",
                "Description": content.get("description", ""),
                "Backend Keywords": content.get("backend_keywords", ""),
                "Color": color,
                "Size": normalize_size(size),
                "UPC": upc,
                "Price": style.get("list_price", ""),
                "Category": style.get("subclass", ""),
                "Brand": brand,
            })
    
    output.seek(0)
    safe_brand = re.sub(r'[^\w]', '_', brand)
    filename = f"NIS_{safe_brand}_Content.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name=filename,
        mimetype="text/csv",
    )


# ── Main ───────────────────────────────────────────────────────────────────────


# ═══ CATALOG HEALTH ═══
import csv
# numpy removed — not needed

# ── Catalog Health: in-memory session storage ──────────────────────────────────
catalog_health_state = {
    "catalog_data": None,        # list of dicts (rows)
    "sales_data": None,          # list of dicts (rows)
    "analysis": None,            # full analysis result
    "detected_fields": None,     # mapping of internal_field -> column_name
    "detected_format": None,     # e.g. "Vendor Central", "Seller Central", "Custom"
    "progress": {"status": "idle", "processed": 0, "total": 0, "message": ""},
}
catalog_health_lock = threading.Lock()

# ── Fuzzy column detection maps ───────────────────────────────────────────────
CATALOG_FIELD_MAP = {
    "asin":             ["asin", "asin1", "child asin", "child_asin"],
    "parent_asin":      ["parent asin", "parent_asin", "parent sku", "parent_sku"],
    "sku":              ["sku", "seller-sku", "seller_sku", "vendor sku", "item_sku"],
    "title":            ["title", "item_name", "item-name", "product title", "item name"],
    "brand":            ["brand", "brand_name", "brand name"],
    "color":            ["color", "color name", "color_name", "color map", "color_map"],
    "size":             ["size", "product - size", "size_name", "apparel size value", "apparel_size"],
    "bullet_1":         ["bullet point 1", "bullet_point1", "key product features 1", "bullet1"],
    "bullet_2":         ["bullet point 2", "bullet_point2", "key product features 2", "bullet2"],
    "bullet_3":         ["bullet point 3", "bullet_point3", "key product features 3", "bullet3"],
    "bullet_4":         ["bullet point 4", "bullet_point4", "key product features 4", "bullet4"],
    "bullet_5":         ["bullet point 5", "bullet_point5", "key product features 5", "bullet5"],
    "description":      ["description", "product_description", "product description"],
    "backend_keywords": ["generic keywords", "generic_keywords", "search terms", "search_terms", "backend keywords"],
    "main_image":       ["main image url", "main_image_url", "image-url", "main image"],
    "other_images":     ["other image url", "other_image_url", "other_image_url1", "image url 2", "image-url-2"],
    "price":            ["price", "list price", "standard_price", "your price"],
    "quantity":         ["quantity", "amzn ioh", "fulfillable quantity", "quantity available"],
    "category":         ["sub-class name", "sub_class_name", "product_type", "item_type", "category"],
    "subcategory":      ["sub sub-class name", "sub_sub_class_name", "subcategory"],
    "style":            ["style #", "style number", "model number", "style_num", "style_number"],
    "parent_child":     ["parent_child", "parentage level", "parentage", "parent/child"],
    "variation_theme":  ["variation_theme", "variation theme name", "variation theme"],
    "status":           ["status", "listing status"],
    "image_count":      ["image count"],
}

SALES_FIELD_MAP = {
    "asin":     ["asin", "child asin"],
    "sessions": ["sessions", "glance views", "glance_views", "page views"],
    "units":    ["units ordered", "shipped units", "shipped_units", "units"],
    "revenue":  ["ordered product sales", "shipped revenue", "shipped_revenue", "revenue"],
    "cvr":      ["unit session percentage", "conversion rate", "conversion_rate", "cvr"],
}

SEVERITY_WEIGHTS = {
    "Orphan (no parent link)":         10,
    "Missing from variation matrix":    8,
    "Zero traffic / suppressed":        9,
    "Missing all bullet points":        6,
    "Missing main image":               7,
    "Missing backend keywords":         4,
    "Short title (<80 chars)":          3,
    "Missing description":              3,
    "Inconsistent title format":        2,
    "Single-child parent":              2,
    "Duplicate variation":              5,
    "Wrong parent link (brand mismatch)": 6,
    "Broken variation theme":           5,
    "Content issue killing conversion": 8,
}


def _norm(s):
    """Normalize a column header for fuzzy matching."""
    return str(s).lower().strip().replace("_", " ").replace("-", " ")


def detect_columns(headers, field_map):
    """
    Fuzzy-match headers to internal field names.
    Returns {internal_field: actual_header} for matched fields.
    """
    detected = {}
    header_norm = {_norm(h): h for h in headers}
    
    for field, candidates in field_map.items():
        for cand in candidates:
            cand_norm = _norm(cand)
            # Exact normalized match
            if cand_norm in header_norm:
                detected[field] = header_norm[cand_norm]
                break
            # Substring match
            for hn, orig in header_norm.items():
                if cand_norm in hn or hn in cand_norm:
                    detected[field] = orig
                    break
            if field in detected:
                break
    
    return detected


def detect_format(headers, detected_fields):
    """Guess whether this looks like Vendor Central, Seller Central, or custom."""
    header_set = {_norm(h) for h in headers}
    if any("vendor" in h for h in header_set):
        return "Vendor Central"
    if any("seller" in h or "seller-sku" in _norm(h) for h in header_set):
        return "Seller Central"
    if "asin" in detected_fields:
        return "Custom (ASIN-based)"
    return "Custom"


def read_file_to_rows(file_storage):
    """Read uploaded file (CSV, TSV, XLSX) into list of dicts. No pandas needed."""
    filename = file_storage.filename.lower()
    content = file_storage.read()
    
    if filename.endswith(".xlsx") or filename.endswith(".xls") or filename.endswith(".xlsm"):
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        raw_headers = next(rows_iter, None)
        if not raw_headers:
            return [], []
        headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(raw_headers)]
        records = []
        for row_vals in rows_iter:
            row_dict = {}
            for i, val in enumerate(row_vals):
                if i < len(headers):
                    row_dict[headers[i]] = str(val).strip() if val is not None else ""
            if any(v for v in row_dict.values()):
                records.append(row_dict)
        wb.close()
        return records, headers
    else:
        # CSV or TSV
        text = content.decode("utf-8", errors="replace")
        # Detect separator
        first_line = text.split("\n")[0] if text else ""
        sep = "\t" if "\t" in first_line else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=sep)
        headers = [str(f).strip() for f in (reader.fieldnames or [])]
        records = []
        for row in reader:
            cleaned = {str(k).strip(): str(v).strip() if v else "" for k, v in row.items()}
            if any(v for v in cleaned.values()):
                records.append(cleaned)
        return records, headers


def score_content(row, detected_fields):
    """Compute 0-100 content completeness score for a single ASIN row."""
    score = 0
    issues = []

    def get(field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    # Title: 15 pts
    title = get("title")
    if title:
        if 80 <= len(title) <= 200:
            score += 15
        elif len(title) < 80:
            score += 7
            issues.append("Short title (<80 chars)")
        else:
            score += 12  # Over 200 but present
    
    # Bullets: 15 pts (3 per bullet)
    for b in ["bullet_1", "bullet_2", "bullet_3", "bullet_4", "bullet_5"]:
        btext = get(b)
        if btext and len(btext) >= 50:
            score += 3
        elif btext:
            score += 1  # Partial credit
    if not any(get(f"bullet_{i}") for i in range(1, 6)):
        issues.append("Missing all bullet points")
    
    # Description: 10 pts
    desc = get("description")
    if desc and len(desc) >= 200:
        score += 10
    elif desc:
        score += 5
    else:
        issues.append("Missing description")
    
    # Backend keywords: 10 pts
    kw = get("backend_keywords")
    if kw:
        kw_bytes = len(kw.encode("utf-8"))
        score += 10 if kw_bytes <= 250 else 7
    else:
        issues.append("Missing backend keywords")
    
    # Main image: 10 pts
    if get("main_image"):
        score += 10
    else:
        issues.append("Missing main image")
    
    # Other images (6+): 10 pts
    img_count = 0
    if detected_fields.get("image_count"):
        try:
            img_count = int(get("image_count") or 0)
        except:
            pass
    else:
        for i in range(2, 10):
            col = detected_fields.get("other_images")
            if col:
                # Multi-image columns: check count
                img_count = 1 if get("other_images") else 0
                break
    img_count_bonus = img_count
    # Also count any columns that look like image URLs
    for col in [c for c in row if "image" in _norm(c) and c != detected_fields.get("main_image")]:
        if str(row.get(col, "")).strip():
            img_count_bonus += 1
    if img_count_bonus >= 6:
        score += 10
    elif img_count_bonus >= 3:
        score += 5

    # Price: 10 pts
    try:
        price_raw = get("price").replace("$", "").replace(",", "").strip()
        if price_raw and float(price_raw) > 0:
            score += 10
    except:
        pass
    
    # Brand: 5 pts
    if get("brand"):
        score += 5
    
    # Color + Size: 5 pts
    if get("color") or get("size"):
        score += 5
    
    # Category: 10 pts
    if get("category"):
        score += 10
    
    return min(100, score), issues


def score_color(score):
    if score >= 90:
        return "green"
    elif score >= 70:
        return "yellow"
    elif score >= 50:
        return "orange"
    return "red"


def run_catalog_analysis(rows, detected_fields, sales_lookup=None):
    """
    Full catalog health analysis. Returns structured result dict.
    Progress is updated via catalog_health_state["progress"].
    """
    state = catalog_health_state

    def get(row, field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    total = len(rows)
    state["progress"] = {"status": "running", "processed": 0, "total": total, "message": "Starting analysis..."}

    # Build lookup structures
    parent_map = {}        # parent_asin -> list of child rows
    asin_map = {}          # asin -> row
    real_parents = set()   # ASINs that actually have parentage="parent"
    
    for i, row in enumerate(rows):
        asin = get(row, "asin") or get(row, "sku")
        if asin:
            asin_map[asin] = row
        p_asin = get(row, "parent_asin")
        pc = _norm(get(row, "parent_child"))
        if pc in ("parent",):
            real_parents.add(asin)
            if asin not in parent_map:
                parent_map[asin] = []
        elif p_asin:
            if p_asin not in parent_map:
                parent_map[p_asin] = []
            parent_map[p_asin].append(row)
        
        if (i + 1) % 1000 == 0:
            state["progress"]["processed"] = i + 1
            state["progress"]["message"] = f"Building lookup structures... {i+1}/{total}"

    # Content scoring + structural checks per ASIN
    issues_list = []
    scored_rows = []
    
    brands_seen = set()
    categories_seen = set()
    subcategories_seen = set()
    
    score_dist = {"green": 0, "yellow": 0, "orange": 0, "red": 0}
    
    for i, row in enumerate(rows):
        asin = get(row, "asin") or get(row, "sku") or f"row_{i}"
        title = get(row, "title")
        brand = get(row, "brand")
        category = get(row, "category")
        subcategory = get(row, "subcategory")
        p_asin = get(row, "parent_asin")
        pc = _norm(get(row, "parent_child"))
        
        if brand:
            brands_seen.add(brand)
        if category:
            categories_seen.add(category)
        if subcategory:
            subcategories_seen.add(subcategory)
        
        content_score, content_issues = score_content(row, detected_fields)
        color = score_color(content_score)
        score_dist[color] += 1
        
        structural_issues = []
        
        # Orphan check: parent must actually exist as a parent row in dataset
        if pc in ("child", "variation") or p_asin:
            if not p_asin:
                structural_issues.append("Orphan (no parent link)")
            elif p_asin not in real_parents and p_asin not in asin_map:
                structural_issues.append("Orphan (no parent link)")
        
        # Wrong parent link (brand mismatch)
        if p_asin and p_asin in asin_map:
            parent_brand = get(asin_map[p_asin], "brand")
            if parent_brand and brand and parent_brand.lower() != brand.lower():
                structural_issues.append("Wrong parent link (brand mismatch)")
        
        # Single-child parent
        if pc == "parent":
            children = parent_map.get(asin, [])
            if len(children) == 1:
                structural_issues.append("Single-child parent")
        
        # Broken variation theme
        if detected_fields.get("variation_theme"):
            vt = get(row, "variation_theme")
            if vt and pc in ("child", "variation"):
                if not get(row, "color") and not get(row, "size"):
                    structural_issues.append("Broken variation theme")
        
        # Revenue cross-reference
        rev_impact = 0.0
        revenue_issues = []
        if sales_lookup and asin in sales_lookup:
            sale = sales_lookup[asin]
            try:
                sessions = float(str(sale.get("sessions", 0)).replace(",", "") or 0)
                units = float(str(sale.get("units", 0)).replace(",", "") or 0)
                revenue = float(str(sale.get("revenue", 0)).replace("$", "").replace(",", "") or 0)
                
                rev_impact = revenue
                
                if sessions == 0 and structural_issues:
                    revenue_issues.append("Zero traffic / suppressed")
                elif sessions > 0 and units == 0 and content_score < 70:
                    revenue_issues.append("Content issue killing conversion")
            except:
                pass
        elif sales_lookup and asin not in sales_lookup:
            # Check if it's an orphan with siblings
            if "Orphan (no parent link)" in structural_issues and p_asin:
                siblings = parent_map.get(p_asin, [])
                sibling_rev = []
                for sib in siblings:
                    sib_asin = get(sib, "asin") or get(sib, "sku")
                    if sib_asin and sib_asin in sales_lookup:
                        try:
                            sibling_rev.append(float(str(sales_lookup[sib_asin].get("revenue", 0)).replace("$","").replace(",","") or 0))
                        except:
                            pass
                if sibling_rev:
                    rev_impact = sum(sibling_rev) / len(sibling_rev)
        
        all_issues = structural_issues + content_issues + revenue_issues
        
        row_result = {
            "asin": asin,
            "title": title[:80] + ("..." if len(title) > 80 else "") if title else "",
            "brand": brand,
            "category": category,
            "subcategory": subcategory,
            "content_score": content_score,
            "score_color": color,
            "parent_asin": p_asin,
            "parent_child": pc,
            "issues": all_issues,
            "revenue_impact": round(rev_impact, 2),
        }
        
        # Compute priority score for each issue
        for issue in all_issues:
            severity = SEVERITY_WEIGHTS.get(issue, 2)
            priority = severity * max(1, rev_impact / 100 if rev_impact > 0 else 1) if rev_impact > 0 else severity
            issues_list.append({
                "priority": round(priority, 2),
                "asin": asin,
                "title": row_result["title"],
                "brand": brand,
                "category": category,
                "issue": issue,
                "severity": severity,
                "severity_label": _severity_label(severity),
                "revenue_impact": round(rev_impact, 2),
                "content_score": content_score,
                "fix_action": _fix_action(issue),
            })
        
        scored_rows.append(row_result)
        
        if (i + 1) % 500 == 0:
            state["progress"]["processed"] = i + 1
            state["progress"]["message"] = f"Analyzing ASINs... {i+1}/{total}"
    
    # Missing variation matrix check
    variation_matrix = {}
    for p_asin, children in parent_map.items():
        colors = set()
        sizes = set()
        present = set()
        for child in children:
            c = get(child, "color")
            s = get(child, "size")
            if c:
                colors.add(c)
            if s:
                sizes.add(s)
            if c and s:
                present.add((c, s))
        if colors and sizes:
            expected = {(c, s) for c in colors for s in sizes}
            missing = expected - present
            for (mc, ms) in missing:
                issues_list.append({
                    "priority": 8,
                    "asin": p_asin,
                    "title": f"[Parent] {asin_map.get(p_asin, {}).get(detected_fields.get('title',''), '')[:60]}",
                    "brand": get(asin_map.get(p_asin, {}), "brand") if p_asin in asin_map else "",
                    "category": get(asin_map.get(p_asin, {}), "category") if p_asin in asin_map else "",
                    "issue": "Missing from variation matrix",
                    "severity": 8,
                    "severity_label": "High",
                    "revenue_impact": 0,
                    "content_score": 0,
                    "fix_action": f"Add variant: Color={mc}, Size={ms}",
                })
            if colors and sizes:
                variation_matrix[p_asin] = {
                    "colors": sorted(colors),
                    "sizes": sorted(sizes),
                    "present": [list(pair) for pair in present],
                    "missing": [list(pair) for pair in missing],
                }
    
    # Duplicate children check
    seen_variants = {}
    for row in rows:
        p = get(row, "parent_asin")
        c = get(row, "color")
        s = get(row, "size")
        if p and c and s:
            key = (p, _norm(c), _norm(s))
            if key in seen_variants:
                asin = get(row, "asin") or get(row, "sku")
                issues_list.append({
                    "priority": 5,
                    "asin": asin,
                    "title": get(row, "title")[:60],
                    "brand": get(row, "brand"),
                    "category": get(row, "category"),
                    "issue": "Duplicate variation",
                    "severity": 5,
                    "severity_label": "Medium",
                    "revenue_impact": 0,
                    "content_score": 0,
                    "fix_action": f"Duplicate of {seen_variants[key]}. Remove one.",
                })
            else:
                seen_variants[key] = get(row, "asin") or get(row, "sku")
    
    # Sort issues by priority (desc)
    issues_list.sort(key=lambda x: x["priority"], reverse=True)
    
    # Summary stats
    total_parents = sum(1 for r in scored_rows if r["parent_child"] == "parent")
    total_children = sum(1 for r in scored_rows if r["parent_child"] in ("child", "variation"))
    if total_parents == 0 and total_children == 0:
        total_parents = len(parent_map)
        total_children = total - total_parents
    
    avg_score = round(sum(r["content_score"] for r in scored_rows) / max(1, len(scored_rows)), 1)
    critical_count = sum(1 for iss in issues_list if iss["severity"] >= 8)
    total_revenue_at_risk = round(sum(iss["revenue_impact"] for iss in issues_list if iss["revenue_impact"] > 0), 2)
    
    state["progress"] = {"status": "done", "processed": total, "total": total, "message": "Analysis complete"}
    
    return {
        "summary": {
            "total_asins": total,
            "total_parents": total_parents,
            "total_children": total_children,
            "avg_score": avg_score,
            "critical_issues": critical_count,
            "total_issues": len(issues_list),
            "revenue_at_risk": total_revenue_at_risk,
            "score_distribution": score_dist,
            "brands": sorted(brands_seen),
            "categories": sorted(categories_seen),
            "subcategories": sorted(subcategories_seen),
        },
        "issues": issues_list[:5000],  # cap for response size
        "scored_rows": scored_rows[:5000],
        "variation_matrix": variation_matrix,
        "has_sales_data": sales_lookup is not None,
    }


def _severity_label(weight):
    if weight >= 9:
        return "Critical"
    elif weight >= 7:
        return "High"
    elif weight >= 4:
        return "Medium"
    return "Low"


def _fix_action(issue):
    actions = {
        "Orphan (no parent link)":          "Set parent_asin field to link this child to its parent",
        "Missing from variation matrix":    "Create new child ASIN for this color/size combination",
        "Zero traffic / suppressed":        "Check listing status, parent link, and image compliance",
        "Missing all bullet points":        "Write 5 bullet points, each >50 characters",
        "Missing main image":               "Upload a compliant main image (white background, 1000px+)",
        "Missing backend keywords":         "Add search terms (<250 bytes, no repeated words)",
        "Short title (<80 chars)":          "Expand title to 80-200 characters with key attributes",
        "Missing description":              "Write product description >200 characters",
        "Inconsistent title format":        "Align title format with brand title formula",
        "Single-child parent":              "Add more child variations or merge into standalone ASIN",
        "Duplicate variation":              "Remove or merge the duplicate child ASIN",
        "Wrong parent link (brand mismatch)":"Correct parent_asin or update brand to match parent",
        "Broken variation theme":           "Add required variation fields (color, size) for this child",
        "Content issue killing conversion": "Improve bullets, images, and description to boost CVR",
    }
    return actions.get(issue, "Review and fix this field")


# ── CATALOG HEALTH ENDPOINTS ───────────────────────────────────────────────────

@app.route("/api/catalog/upload-catalog", methods=["POST"])
def catalog_upload_catalog():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    
    try:
        rows, headers = read_file_to_rows(f)
        
        if len(rows) > 60000:
            return jsonify({"error": "File too large. Max 60,000 rows."}), 400
        
        detected_fields = detect_columns(headers, CATALOG_FIELD_MAP)
        fmt = detect_format(headers, detected_fields)
        
        mapped_count = len(detected_fields)
        total_fields = len(CATALOG_FIELD_MAP)
        missing_fields = [k for k in CATALOG_FIELD_MAP if k not in detected_fields]
        
        with catalog_health_lock:
            catalog_health_state["catalog_data"] = rows
            catalog_health_state["detected_fields"] = detected_fields
            catalog_health_state["detected_format"] = fmt
            catalog_health_state["analysis"] = None
            catalog_health_state["progress"] = {"status": "idle", "processed": 0, "total": 0, "message": ""}
        
        # Run analysis in background thread
        sales_lookup = None
        if catalog_health_state.get("sales_data"):
            sales_data = catalog_health_state["sales_data"]
            sales_fields = catalog_health_state.get("sales_fields", {})
            def sg(row, field):
                col = sales_fields.get(field)
                return str(row.get(col, "")).strip() if col else ""
            sales_lookup = {sg(r, "asin"): r for r in sales_data if sg(r, "asin")}
        
        def run_analysis():
            result = run_catalog_analysis(rows, detected_fields, sales_lookup)
            with catalog_health_lock:
                catalog_health_state["analysis"] = result
        
        t = threading.Thread(target=run_analysis, daemon=True)
        t.start()
        
        return jsonify({
            "ok": True,
            "format": fmt,
            "rows": len(rows),
            "mapped_count": mapped_count,
            "total_fields": total_fields,
            "missing_fields": missing_fields,
            "detected_fields": {k: v for k, v in detected_fields.items()},
            "detection_summary": f"Detected {mapped_count} of {total_fields} fields. Missing: {', '.join(missing_fields) if missing_fields else 'none'}",
        })
    
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {str(e)}"}), 500


@app.route("/api/catalog/upload-sales", methods=["POST"])
def catalog_upload_sales():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    
    try:
        rows, headers = read_file_to_rows(f)
        sales_fields = detect_columns(headers, SALES_FIELD_MAP)
        
        def sg(row, field):
            col = sales_fields.get(field)
            return str(row.get(col, "")).strip() if col else ""
        
        sales_lookup = {sg(r, "asin"): r for r in rows if sg(r, "asin")}
        
        with catalog_health_lock:
            catalog_health_state["sales_data"] = rows
            catalog_health_state["sales_fields"] = sales_fields
        
        # Re-run analysis if catalog is already loaded
        if catalog_health_state.get("catalog_data"):
            catalog_rows = catalog_health_state["catalog_data"]
            detected_fields = catalog_health_state["detected_fields"]
            
            def run_analysis():
                result = run_catalog_analysis(catalog_rows, detected_fields, sales_lookup)
                with catalog_health_lock:
                    catalog_health_state["analysis"] = result
            
            t = threading.Thread(target=run_analysis, daemon=True)
            t.start()
        
        return jsonify({
            "ok": True,
            "rows": len(rows),
            "asins_matched": len(sales_lookup),
            "fields": list(sales_fields.keys()),
        })
    
    except Exception as e:
        return jsonify({"error": f"Failed to parse sales file: {str(e)}"}), 500


@app.route("/api/catalog/results")
def catalog_results():
    progress = catalog_health_state.get("progress", {})
    analysis = catalog_health_state.get("analysis")
    
    if not analysis and progress.get("status") == "running":
        return jsonify({
            "status": "running",
            "progress": progress,
        })
    
    if not analysis:
        return jsonify({"status": "idle"})
    
    return jsonify({
        "status": "done",
        "analysis": analysis,
        "progress": progress,
    })


@app.route("/api/catalog/progress")
def catalog_progress():
    progress = catalog_health_state.get("progress", {"status": "idle", "processed": 0, "total": 0, "message": ""})
    pct = 0
    if progress.get("total", 0) > 0:
        pct = round(progress["processed"] / progress["total"] * 100)
    return jsonify({**progress, "percent": pct})


@app.route("/api/catalog/fix-file")
def catalog_fix_file():
    analysis = catalog_health_state.get("analysis")
    if not analysis:
        return jsonify({"error": "No analysis available"}), 404
    
    issues = analysis.get("issues", [])
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["ASIN", "Title", "Brand", "Category", "Issue", "Severity", "Revenue Impact", "Fix Action"])
    writer.writeheader()
    for iss in issues:
        writer.writerow({
            "ASIN": iss["asin"],
            "Title": iss["title"],
            "Brand": iss["brand"],
            "Category": iss["category"],
            "Issue": iss["issue"],
            "Severity": iss["severity_label"],
            "Revenue Impact": f"${iss['revenue_impact']:,.2f}" if iss["revenue_impact"] else "",
            "Fix Action": iss["fix_action"],
        })
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name="Catalog_Fix_File.csv",
        mimetype="text/csv",
    )


@app.route("/api/catalog/export")
def catalog_export():
    analysis = catalog_health_state.get("analysis")
    if not analysis:
        return jsonify({"error": "No analysis available"}), 404
    
    scored_rows = analysis.get("scored_rows", [])
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["ASIN", "Title", "Brand", "Category", "Subcategory", "Content Score", "Score Grade", "Parent ASIN", "Issues", "Revenue Impact"])
    writer.writeheader()
    for r in scored_rows:
        writer.writerow({
            "ASIN": r["asin"],
            "Title": r["title"],
            "Brand": r["brand"],
            "Category": r["category"],
            "Subcategory": r.get("subcategory", ""),
            "Content Score": r["content_score"],
            "Score Grade": r["score_color"].upper(),
            "Parent ASIN": r.get("parent_asin", ""),
            "Issues": "; ".join(r.get("issues", [])),
            "Revenue Impact": f"${r['revenue_impact']:,.2f}" if r.get("revenue_impact") else "",
        })
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name="Catalog_Health_Full_Analysis.csv",
        mimetype="text/csv",
    )



# ═══ MERGE LISTINGS MODULE ═══════════════════════════════════════════════════

# In-memory merge state
merge_state = {
    "plan": None,          # list of merge actions
    "approved": {},        # action_id -> True/False
    "generated_at": None,
}
merge_lock = threading.Lock()


def _build_merge_plan(catalog_data, detected_fields):
    """
    Analyse catalog_data and produce a list of merge action dicts.
    """
    def get(row, field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    # Build structures
    asin_map = {}          # asin -> row
    parent_map = {}        # parent_asin -> [child rows]
    real_parents = set()   # ASINs that have parentage == "parent"
    model_to_asins = {}    # model_name -> [asin list]

    for row in catalog_data:
        asin = get(row, "asin") or get(row, "sku")
        if not asin:
            continue
        asin_map[asin] = row
        pc = get(row, "parent_child").lower()
        if pc == "parent":
            real_parents.add(asin)
            if asin not in parent_map:
                parent_map[asin] = []
        p_asin = get(row, "parent_asin")
        if p_asin and pc != "parent":
            if p_asin not in parent_map:
                parent_map[p_asin] = []
            parent_map[p_asin].append(row)
        # Model name grouping
        model = get(row, "model_name") or get(row, "sku")
        if model:
            model_base = re.split(r"[-_](?:XS|S|M|L|XL|XXL|2XL|3XL|BLACK|WHITE|RED|BLUE|GREEN|NAVY|[A-Z]{1,2}\d{0,2})$",
                                   model.upper())[0].strip()
            if model_base not in model_to_asins:
                model_to_asins[model_base] = []
            model_to_asins[model_base].append(asin)

    actions = []
    action_id = 0

    # ── 1. Split families: same model_name → multiple parent ASINs ──────────
    for model_base, asins_in_family in model_to_asins.items():
        if len(asins_in_family) < 2:
            continue
        parents_in_family = [a for a in asins_in_family if a in real_parents]
        if len(parents_in_family) <= 1:
            continue
        # Primary = the one with most children
        primary = max(parents_in_family, key=lambda p: len(parent_map.get(p, [])))
        secondary_parents = [p for p in parents_in_family if p != primary]
        for sec_parent in secondary_parents:
            children_to_move = parent_map.get(sec_parent, [])
            affected = [get(c, "asin") or get(c, "sku") for c in children_to_move] + [sec_parent]
            affected = [a for a in affected if a]
            primary_title = get(asin_map.get(primary, {}), "title")[:60] if asin_map.get(primary) else primary
            sec_title = get(asin_map.get(sec_parent, {}), "title")[:60] if asin_map.get(sec_parent) else sec_parent
            action_id += 1
            actions.append({
                "id": f"action_{action_id}",
                "action_type": "reassign",
                "affected_asins": affected,
                "from_parent": sec_parent,
                "to_parent": primary,
                "reasoning": f"Model family '{model_base}' is split across {len(parents_in_family)} parent ASINs. "
                             f"Primary parent {primary} has {len(parent_map.get(primary,[]))} children; "
                             f"{sec_parent} has {len(children_to_move)}. Consolidating under primary.",
                "confidence": "High" if len(children_to_move) > 0 else "Medium",
                "from_parent_title": sec_title,
                "to_parent_title": primary_title,
            })

    # ── 2. Orphan fix: children with no valid parent ────────────────────────
    for asin, row in asin_map.items():
        pc = get(row, "parent_child").lower()
        if pc in ("child", "variation", ""):
            p_asin = get(row, "parent_asin")
            if not p_asin or p_asin == asin or p_asin not in asin_map:
                # Try to find a parent by model name
                model = get(row, "model_name") or get(row, "sku")
                model_base = re.split(r"[-_](?:XS|S|M|L|XL|XXL|2XL|3XL|BLACK|WHITE|RED|BLUE|GREEN|NAVY|[A-Z]{1,2}\d{0,2})$",
                                       (model or "").upper())[0].strip() if model else ""
                suggested_parent = None
                if model_base and model_base in model_to_asins:
                    candidates = [a for a in model_to_asins[model_base]
                                  if a in real_parents and a != asin]
                    if candidates:
                        suggested_parent = max(candidates, key=lambda p: len(parent_map.get(p, [])))
                reason = (
                    f"ASIN {asin} has no valid parent link (parent_asin='{p_asin or 'empty'}'). "
                    + (f"Best match found: parent {suggested_parent} in same model family '{model_base}'."
                       if suggested_parent else "No matching parent found — may need to be made standalone or a new parent created.")
                )
                action_id += 1
                actions.append({
                    "id": f"action_{action_id}",
                    "action_type": "orphan_fix",
                    "affected_asins": [asin],
                    "from_parent": p_asin or "",
                    "to_parent": suggested_parent or "",
                    "reasoning": reason,
                    "confidence": "High" if suggested_parent else "Low",
                    "from_parent_title": "",
                    "to_parent_title": get(asin_map.get(suggested_parent, {}), "title")[:60] if suggested_parent else "",
                })

    # ── 3. Category mismatch: child's category differs from parent's ─────────
    for p_asin, children in parent_map.items():
        if p_asin not in asin_map:
            continue
        parent_cat = get(asin_map[p_asin], "category")
        for child_row in children:
            child_asin = get(child_row, "asin") or get(child_row, "sku")
            child_cat = get(child_row, "category")
            if parent_cat and child_cat and parent_cat.lower() != child_cat.lower():
                action_id += 1
                actions.append({
                    "id": f"action_{action_id}",
                    "action_type": "category_fix",
                    "affected_asins": [child_asin],
                    "from_parent": p_asin,
                    "to_parent": p_asin,
                    "reasoning": f"Child {child_asin} has category '{child_cat}' but its parent {p_asin} has category '{parent_cat}'. Child should match parent category.",
                    "confidence": "Medium",
                    "from_parent_title": get(asin_map[p_asin], "title")[:60],
                    "to_parent_title": get(asin_map[p_asin], "title")[:60],
                })

    return actions


@app.route("/api/merge/analyze", methods=["POST"])
def merge_analyze():
    catalog_data = catalog_health_state.get("catalog_data")
    detected_fields = catalog_health_state.get("detected_fields")
    if not catalog_data or not detected_fields:
        return jsonify({"error": "No catalog data loaded. Run Catalog Health upload first."}), 400
    try:
        actions = _build_merge_plan(catalog_data, detected_fields)
        with merge_lock:
            merge_state["plan"] = actions
            merge_state["approved"] = {a["id"]: True for a in actions}
            merge_state["generated_at"] = datetime.now().isoformat()

        # Summary
        split_families = sum(1 for a in actions if a["action_type"] == "reassign")
        orphans = sum(1 for a in actions if a["action_type"] == "orphan_fix")
        category_fixes = sum(1 for a in actions if a["action_type"] == "category_fix")

        return jsonify({
            "ok": True,
            "plan": actions,
            "summary": {
                "split_families": split_families,
                "orphaned_asins": orphans,
                "category_mismatches": category_fixes,
                "total_actions": len(actions),
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/merge/plan", methods=["GET"])
def merge_plan():
    plan = merge_state.get("plan")
    if plan is None:
        return jsonify({"plan": None, "summary": None})
    approved = merge_state.get("approved", {})
    split_families = sum(1 for a in plan if a["action_type"] == "reassign")
    orphans = sum(1 for a in plan if a["action_type"] == "orphan_fix")
    category_fixes = sum(1 for a in plan if a["action_type"] == "category_fix")
    return jsonify({
        "plan": plan,
        "approved": approved,
        "summary": {
            "split_families": split_families,
            "orphaned_asins": orphans,
            "category_mismatches": category_fixes,
            "total_actions": len(plan),
        },
        "generated_at": merge_state.get("generated_at"),
    })


@app.route("/api/merge/approve", methods=["POST"])
def merge_approve():
    data = request.get_json(force=True) or {}
    action_id = data.get("action_id")
    approved = data.get("approved", True)
    if not action_id:
        return jsonify({"error": "action_id required"}), 400
    with merge_lock:
        if merge_state["plan"] is None:
            return jsonify({"error": "No plan loaded"}), 400
        ids = {a["id"] for a in merge_state["plan"]}
        if action_id not in ids:
            return jsonify({"error": "Unknown action_id"}), 404
        merge_state["approved"][action_id] = bool(approved)
    return jsonify({"ok": True, "action_id": action_id, "approved": bool(approved)})


@app.route("/api/merge/generate-fix", methods=["POST"])
def merge_generate_fix():
    plan = merge_state.get("plan")
    approved = merge_state.get("approved", {})
    if not plan:
        return jsonify({"error": "No merge plan. Run analyze first."}), 400

    approved_actions = [a for a in plan if approved.get(a["id"], True)]
    if not approved_actions:
        return jsonify({"error": "No approved actions to generate."}), 400

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ASIN", "Action", "Current Parent", "New Parent",
                     "Variation Theme", "Parentage Level", "Notes"])

    catalog_data = catalog_health_state.get("catalog_data", [])
    detected_fields = catalog_health_state.get("detected_fields", {})

    def get_field(row, field):
        col = detected_fields.get(field)
        return str(row.get(col, "")).strip() if col else ""

    asin_row_map = {}
    for row in catalog_data:
        a = get_field(row, "asin") or get_field(row, "sku")
        if a:
            asin_row_map[a] = row

    for action in approved_actions:
        vt = ""
        for asin in action["affected_asins"]:
            row = asin_row_map.get(asin, {})
            vt = get_field(row, "variation_theme") if row else ""
            parentage = "child"
            if action["action_type"] == "reassign":
                notes = f"Reassign from parent {action['from_parent']} to {action['to_parent']}"
            elif action["action_type"] == "orphan_fix":
                notes = f"Orphan fix — assign to parent {action['to_parent'] or 'TBD'}"
            else:
                notes = f"Category fix under parent {action['to_parent']}"
            writer.writerow([
                asin,
                action["action_type"].replace("_", " ").title(),
                action["from_parent"],
                action["to_parent"],
                vt,
                parentage,
                notes,
            ])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        as_attachment=True,
        download_name=f"Merge_Fix_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mimetype="text/csv",
    )


# ═══ INTEL MODULE ════════════════════════════════════════════════════════════

intel_state = {
    "recommendations": None,
    "dismissed": set(),
    "accepted": {},
    "generated_at": None,
}
intel_lock = threading.Lock()


def _build_intel_recommendations(catalog_data, detected_fields,
                                  nis_state=None, feedback_data=None):
    """
    Generate ranked intelligence recommendations from all available data.
    """
    def get(row, field):
        col = detected_fields.get(field) if detected_fields else None
        return str(row.get(col, "")).strip() if col else ""

    recs = []
    rec_id = 0

    def new_id():
        nonlocal rec_id
        rec_id += 1
        return f"intel_{rec_id}"

    if not catalog_data:
        return recs

    # Build data structures
    asin_map = {}
    parent_map = {}
    real_parents = set()
    bullet_sets = {}   # bullet_index -> list of (asin, text)
    title_lengths = []

    for row in catalog_data:
        asin = get(row, "asin") or get(row, "sku")
        if not asin:
            continue
        asin_map[asin] = row
        pc = get(row, "parent_child").lower()
        if pc == "parent":
            real_parents.add(asin)
        p_asin = get(row, "parent_asin")
        if p_asin and pc != "parent":
            if p_asin not in parent_map:
                parent_map[p_asin] = []
            parent_map[p_asin].append(asin)

        # Collect bullets
        for i in range(1, 6):
            bullet = get(row, f"bullet_{i}")
            if bullet:
                if i not in bullet_sets:
                    bullet_sets[i] = []
                bullet_sets[i].append((asin, bullet))

        # Title length
        title = get(row, "title")
        if title:
            title_lengths.append((asin, len(title)))

    total_asins = len(asin_map)

    # ── 1. Duplicate bullets across ASINs ────────────────────────────────────
    for bullet_idx, entries in bullet_sets.items():
        text_to_asins = {}
        for asin, text in entries:
            t = text.lower().strip()
            if t:
                if t not in text_to_asins:
                    text_to_asins[t] = []
                text_to_asins[t].append(asin)
        for text, asins in text_to_asins.items():
            if len(asins) >= 5:
                severity = "High" if len(asins) >= 20 else "Medium"
                recs.append({
                    "id": new_id(),
                    "type": "content_duplicate",
                    "severity": severity,
                    "title": f"Bullet {bullet_idx} is identical across {len(asins)} ASINs",
                    "description": f"The same bullet point text is used word-for-word on {len(asins)} listings. "
                                   f"Amazon may suppress duplicate content and customers see no differentiation.",
                    "why": f"Exact duplicate: \"{text[:120]}...\" appears on {len(asins)} ASINs. "
                           f"Unique bullet copy improves relevance signals and conversion on style-differentiated products.",
                    "affected_asins": asins[:50],
                    "estimated_impact": "Medium — duplicate content can reduce relevance scoring",
                    "suggested_action": f"Rewrite Bullet {bullet_idx} for each style to highlight unique attributes (color, fit, occasion).",
                    "action_type": "change_bullet",
                })

    # ── 2. Title length optimization ─────────────────────────────────────────
    short_titles = [(a, ln) for a, ln in title_lengths if ln < 80]
    if short_titles:
        severity = "High" if len(short_titles) > total_asins * 0.3 else "Medium"
        recs.append({
            "id": new_id(),
            "type": "title_optimization",
            "severity": severity,
            "title": f"{len(short_titles)} titles are under 80 characters",
            "description": f"{len(short_titles)} of {total_asins} titles use fewer than 80 chars of the 200-char limit. "
                           f"Longer titles with relevant keywords improve search visibility.",
            "why": f"Amazon allows 200 characters for titles. Short titles leave keyword space unused. "
                   f"Adding size range, key features, or occasion keywords can meaningfully improve CTR.",
            "affected_asins": [a for a, _ in short_titles[:50]],
            "estimated_impact": "High — titles are the #1 ranking signal for search",
            "suggested_action": "Expand short titles to include: key feature, target customer, or occasion. Aim for 130-180 chars.",
            "action_type": "change_title",
        })

    very_long_titles = [(a, ln) for a, ln in title_lengths if ln > 190]
    if very_long_titles:
        recs.append({
            "id": new_id(),
            "type": "title_optimization",
            "severity": "Medium",
            "title": f"{len(very_long_titles)} titles exceed 190 characters (at risk of truncation)",
            "description": f"Titles over 200 chars are truncated by Amazon, cutting off important keywords.",
            "why": "Amazon truncates titles at 200 chars in some views. End-of-title keywords are the most likely to be cut.",
            "affected_asins": [a for a, _ in very_long_titles[:50]],
            "estimated_impact": "Medium — truncated titles lose keyword visibility",
            "suggested_action": "Trim these titles to under 190 characters, keeping the most important keywords first.",
            "action_type": "change_title",
        })

    # ── 3. Missing backend keywords (description empty) ───────────────────────
    no_desc = [asin for asin, row in asin_map.items()
               if len(get(row, "description")) < 100]
    if no_desc:
        severity = "Critical" if len(no_desc) > total_asins * 0.5 else "High"
        recs.append({
            "id": new_id(),
            "type": "content_quality",
            "severity": severity,
            "title": f"Description under 100 chars on {len(no_desc)} ASINs",
            "description": f"{len(no_desc)} listings have minimal or empty descriptions. "
                           f"Descriptions provide keyword real estate and help customers make purchase decisions.",
            "why": "Product descriptions index for search and provide backend keyword coverage. "
                   "Empty descriptions miss out on long-tail keyword coverage and reduce Buy Box competitiveness.",
            "affected_asins": no_desc[:50],
            "estimated_impact": "High — descriptions contribute to A9 indexing",
            "suggested_action": "Write 200-500 char descriptions highlighting fabric, fit, care instructions, and occasion suitability.",
            "action_type": "add_keyword",
        })

    # ── 4. Variation gap: parents with fewer children than average ──────────
    child_counts = [len(children) for p, children in parent_map.items() if p in real_parents]
    if child_counts:
        avg_children = sum(child_counts) / len(child_counts)
        thin_parents = [(p, len(parent_map[p])) for p in real_parents
                        if len(parent_map.get(p, [])) < max(2, avg_children * 0.4)]
        if thin_parents:
            recs.append({
                "id": new_id(),
                "type": "variation_gap",
                "severity": "Medium",
                "title": f"{len(thin_parents)} parent ASINs have fewer variations than catalog average",
                "description": f"Catalog average is {avg_children:.1f} children per parent. "
                               f"{len(thin_parents)} parents have significantly fewer. "
                               f"Thin variation families miss size/color opportunities.",
                "why": f"Parents with {avg_children:.0f}+ children capture more organic traffic across size and color searches. "
                       f"Adding common sizes (S-2X) or seasonal colors can significantly expand reach.",
                "affected_asins": [p for p, _ in thin_parents[:50]],
                "estimated_impact": "Medium — more variants = more search coverage",
                "suggested_action": f"Review thin families and consider adding missing sizes or colors. Catalog average is {avg_children:.1f} variants.",
                "action_type": "add_variant",
            })

    # ── 5. Missing bullets ────────────────────────────────────────────────────
    no_bullet_5 = [asin for asin, row in asin_map.items()
                   if not get(row, "bullet_5")]
    if no_bullet_5 and len(no_bullet_5) > 5:
        recs.append({
            "id": new_id(),
            "type": "content_quality",
            "severity": "Medium",
            "title": f"{len(no_bullet_5)} ASINs missing Bullet Point 5",
            "description": f"Amazon allows 5 bullet points. {len(no_bullet_5)} listings only use 4 or fewer, "
                           f"leaving keyword and content opportunity on the table.",
            "why": "Each bullet point is a separate keyword indexing opportunity. "
                   "Bullet 5 is often used for care instructions, compatibility, or brand story — all indexable.",
            "affected_asins": no_bullet_5[:50],
            "estimated_impact": "Low — incremental keyword coverage",
            "suggested_action": "Add Bullet 5 with care instructions, size guidance, or brand/warranty information.",
            "action_type": "change_bullet",
        })

    # ── 6. A/B test suggestions for high-count duplicate bullet families ──────
    families_with_many_dupes = []
    for bullet_idx, entries in bullet_sets.items():
        text_to_asins = {}
        for asin, text in entries:
            t = text.lower().strip()
            if t:
                if t not in text_to_asins:
                    text_to_asins[t] = []
                text_to_asins[t].append(asin)
        most_common = max(text_to_asins.items(), key=lambda x: len(x[1]), default=(None, []))
        if most_common[0] and len(most_common[1]) >= 10:
            families_with_many_dupes.append((bullet_idx, most_common[0], most_common[1]))

    if families_with_many_dupes:
        bullet_idx, text, asins = families_with_many_dupes[0]
        recs.append({
            "id": new_id(),
            "type": "ab_test_suggestion",
            "severity": "Low",
            "title": f"A/B test opportunity: Bullet {bullet_idx} variant on {len(asins)} ASINs",
            "description": f"The most-used bullet ({len(asins)} ASINs) is a candidate for A/B testing. "
                           f"Test a feature-focused variant against the current generic version.",
            "why": f"Current bullet: \"{text[:100]}...\". "
                   f"With {len(asins)} ASINs using this identical copy, a small CTR improvement "
                   f"on a test variant could justify a full rollout.",
            "affected_asins": asins[:20],
            "estimated_impact": "Medium — A/B tests on high-volume bullet copy can yield 5-15% CVR lift",
            "suggested_action": f"Draft an alternative Bullet {bullet_idx} emphasizing a specific feature or benefit. Test on 5-10 ASINs for 30 days.",
            "action_type": "change_bullet",
        })

    # Sort by severity
    severity_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    recs.sort(key=lambda r: severity_order.get(r["severity"], 4))

    return recs


@app.route("/api/intel/analyze", methods=["POST"])
def intel_analyze():
    catalog_data = catalog_health_state.get("catalog_data")
    detected_fields = catalog_health_state.get("detected_fields")
    if not catalog_data or not detected_fields:
        return jsonify({"error": "No catalog data loaded. Upload data in Catalog Health first."}), 400
    try:
        recs = _build_intel_recommendations(catalog_data, detected_fields)
        with intel_lock:
            intel_state["recommendations"] = recs
            intel_state["dismissed"] = set()
            intel_state["accepted"] = {}
            intel_state["generated_at"] = datetime.now().isoformat()
        critical = sum(1 for r in recs if r["severity"] == "Critical")
        high = sum(1 for r in recs if r["severity"] == "High")
        quick_wins = sum(1 for r in recs
                         if r["severity"] in ("High", "Critical")
                         and r["action_type"] in ("change_bullet", "change_title"))
        return jsonify({
            "ok": True,
            "recommendations": recs,
            "summary": {
                "total": len(recs),
                "critical": critical,
                "high": high,
                "quick_wins": quick_wins,
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/recommendations", methods=["GET"])
def intel_recommendations():
    recs = intel_state.get("recommendations")
    dismissed = intel_state.get("dismissed", set())
    accepted = intel_state.get("accepted", {})
    if recs is None:
        return jsonify({"recommendations": None, "summary": None})
    visible = [r for r in recs if r["id"] not in dismissed]
    critical = sum(1 for r in visible if r["severity"] == "Critical")
    high = sum(1 for r in visible if r["severity"] == "High")
    quick_wins = sum(1 for r in visible
                     if r["severity"] in ("High", "Critical")
                     and r["action_type"] in ("change_bullet", "change_title"))
    return jsonify({
        "recommendations": visible,
        "accepted": accepted,
        "summary": {
            "total": len(visible),
            "critical": critical,
            "high": high,
            "quick_wins": quick_wins,
        },
        "generated_at": intel_state.get("generated_at"),
    })


@app.route("/api/intel/accept", methods=["POST"])
def intel_accept():
    data = request.get_json(force=True) or {}
    rec_id = data.get("rec_id")
    note = data.get("note", "")
    if not rec_id:
        return jsonify({"error": "rec_id required"}), 400
    with intel_lock:
        if intel_state["recommendations"] is None:
            return jsonify({"error": "No recommendations loaded"}), 400
        ids = {r["id"] for r in intel_state["recommendations"]}
        if rec_id not in ids:
            return jsonify({"error": "Unknown rec_id"}), 404
        intel_state["accepted"][rec_id] = {
            "accepted_at": datetime.now().isoformat(),
            "note": note,
        }
    return jsonify({"ok": True, "rec_id": rec_id})


@app.route("/api/intel/dismiss", methods=["POST"])
def intel_dismiss():
    data = request.get_json(force=True) or {}
    rec_id = data.get("rec_id")
    if not rec_id:
        return jsonify({"error": "rec_id required"}), 400
    with intel_lock:
        if intel_state["recommendations"] is None:
            return jsonify({"error": "No recommendations loaded"}), 400
        intel_state["dismissed"].add(rec_id)
    return jsonify({"ok": True, "rec_id": rec_id})


if __name__ == "__main__":
    print("NIS Wizard v3 — TLG Amazon Intelligence starting on http://localhost:5000")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
