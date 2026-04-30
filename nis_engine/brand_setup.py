"""Brand-level configuration that the rule engine uses to:
  - Auto-fill values that are constant per brand (vendor code, default COO, etc.)
  - Suppress entire categories of false-positive verdicts (battery flags for
    apparel, license fields for non-licensed brands, etc.)

A brand config lives at brand_configs/<Brand_Name>.json. When a pre-upload is
imported and no config exists for the detected brand, the API returns a
`needs_setup=true` flag with the list of fields the operator must supply.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional


# ── Required-on-first-setup brand fields (the absolute minimum) ─────────────
# Each entry maps a setup_key -> { label, hint, required, default_value, options }.
# This drives the brand-setup modal in the dashboard.
BRAND_SETUP_SCHEMA: Dict[str, Dict[str, Any]] = {
    "vendor_code_prefix": {
        "label":    "Vendor Code",
        "hint":     "Amazon vendor code, e.g. 'QT5G8' for Sage Activewear",
        "required": True,
    },
    "brand_name": {
        "label":    "Brand Name (as shown on Amazon)",
        "hint":     "Exact public brand name. Example: 'Sage Collective'",
        "required": True,
    },
    "default_coo": {
        "label":    "Default Country of Origin",
        "hint":     "Most common COO. Per-style values in the pre-upload override this.",
        "required": True,
    },
    "department": {
        "label":    "Default Department",
        "hint":     "Most styles in this brand are for: ",
        "required": True,
        "options":  ["womens", "mens", "unisex", "girls", "boys"],
        "default_value": "womens",
    },
    "default_care": {
        "label":    "Default Care Instructions",
        "hint":     "e.g. 'Machine Wash Cold, Tumble Dry Low'",
        "required": False,
        "default_value": "Machine Wash Cold",
    },
    "default_size_system": {
        "label":    "Default Size System",
        "hint":     "How sizes are labelled.",
        "required": False,
        "options":  ["US", "UK", "EU", "Alpha", "Numeric"],
        "default_value": "US",
    },
    # Suppression toggles \u2014 default to "no" (suppresses fields) for typical apparel
    "sells_licensed_sports": {
        "label":    "Sells licensed sports merchandise (NFL, MLB, NBA, etc.)?",
        "hint":     "If No, we suppress Team / League / Athlete / Collection fields.",
        "required": False,
        "options":  ["No", "Yes"],
        "default_value": "No",
    },
    "products_contain_batteries": {
        "label":    "Any products contain batteries?",
        "hint":     "If No, we suppress 19 lithium / cell / hazmat fields.",
        "required": False,
        "options":  ["No", "Yes"],
        "default_value": "No",
    },
    "is_government_contractor": {
        "label":    "Federal-contract / BAA-TAA compliant goods?",
        "hint":     "If No, we suppress Government Contract / TAA fields.",
        "required": False,
        "options":  ["No", "Yes"],
        "default_value": "No",
    },
    "requires_hazmat_disclosure": {
        "label":    "Requires hazmat or SDS/MSDS disclosure?",
        "hint":     "If No, we suppress GHS / Safety Data Sheet fields.",
        "required": False,
        "options":  ["No", "Yes"],
        "default_value": "No",
    },
}


# Engine-aware suppression rules: when a brand's setting matches the trigger,
# every field whose label substring is in the patterns list is hidden from the
# verdict result entirely. This is what removes the "19 battery false alarms"
# without lying to the operator.
SUPPRESSION_RULES = [
    {
        "name":     "battery_when_no",
        "trigger":  ("products_contain_batteries", "No"),
        "label_substrings": [
            "battery", "lithium", "cell composition", "hazmat aspect",
            "non-lithium", "battery installation", "battery contains",
            "non spillable", "multiple battery",
        ],
    },
    {
        "name":     "license_when_no",
        "trigger":  ("sells_licensed_sports", "No"),
        "label_substrings": [
            "league name", "team name", "athlete", "collection item",
            "league type",
        ],
    },
    {
        "name":     "government_when_no",
        "trigger":  ("is_government_contractor", "No"),
        "label_substrings": [
            "government contract", "baa taa", "taa compliant",
        ],
    },
    {
        "name":     "hazmat_when_no",
        "trigger":  ("requires_hazmat_disclosure", "No"),
        "label_substrings": [
            "ghs", "safety data sheet", "sds", "msds",
            "compliance weave", "less than 30 percent state of charge",
        ],
    },
]


# ── File system ────────────────────────────────────────────────────────────

def _brand_configs_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    # nis_engine/ is a sibling of brand_configs/
    return os.path.normpath(os.path.join(here, "..", "brand_configs"))


def _slug(brand: str) -> str:
    """Filesystem-safe filename for a brand."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", (brand or "").strip()) or "Unknown"


def load_brand_config(brand: str) -> Optional[dict]:
    """Read brand_configs/<slug>.json, return parsed dict or None."""
    if not brand:
        return None
    path = os.path.join(_brand_configs_dir(), f"{_slug(brand)}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_brand_config(brand: str, data: dict) -> str:
    """Save brand config and return the full path written."""
    os.makedirs(_brand_configs_dir(), exist_ok=True)
    path = os.path.join(_brand_configs_dir(), f"{_slug(brand)}.json")
    # Always store brand_name canonical
    data = {**data, "brand_name": brand}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def list_brand_configs() -> List[str]:
    d = _brand_configs_dir()
    if not os.path.isdir(d):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(d)
        if f.endswith(".json")
    )


# ── Core API used by the engine ─────────────────────────────────────────────

def needs_setup(brand: str) -> Dict[str, Any]:
    """Return whether a brand has the absolute-required setup fields.

    Returns: { needs_setup: bool, missing_fields: [setup_key, ...], schema: {...} }
    """
    cfg = load_brand_config(brand) or {}
    missing = []
    for key, meta in BRAND_SETUP_SCHEMA.items():
        if not meta.get("required"):
            continue
        if not cfg.get(key) and not cfg.get(key.replace("_prefix", "")):
            missing.append(key)
    return {
        "brand":         brand,
        "needs_setup":   len(missing) > 0,
        "missing_fields": missing,
        "schema":        BRAND_SETUP_SCHEMA,
        "current_config": cfg,
    }


def brand_defaults_to_state(brand: str) -> Dict[str, Any]:
    """Return a dict of NIS field_keys -> values that come from this brand's saved config.
    Empty if the brand has no config yet.
    """
    cfg = load_brand_config(brand) or {}
    if not cfg:
        return {}
    out: Dict[str, Any] = {}
    if cfg.get("vendor_code_prefix") or cfg.get("vendor_code"):
        out["rtip_vendor_code#1.value"] = cfg.get("vendor_code_prefix") or cfg.get("vendor_code")
    if cfg.get("brand_name"):
        out["brand#1.value"] = cfg["brand_name"]
    if cfg.get("default_coo"):
        out["country_of_origin#1.value"] = cfg["default_coo"]
    if cfg.get("default_care"):
        out["care_instructions#1.value"] = cfg["default_care"]
    if cfg.get("department"):
        out["department#1.value"] = cfg["department"]
        out["target_gender#1.value"] = {
            "womens": "Female", "mens": "Male", "unisex": "Unisex",
            "girls":  "Female", "boys":  "Male",
        }.get(cfg["department"], "Female")
    if cfg.get("default_size_system"):
        out["apparel_size#1.size_system"] = cfg["default_size_system"]
    return out


def get_suppressed_field_keys(brand: str, bundle_fields: Dict[str, dict]) -> List[str]:
    """Given the bundle's fields dict (col -> { label, field_key, ... }), return the
    list of field_keys that should be hidden from the verdict for this brand.

    Suppression decisions are pure data: which trigger settings the brand has,
    which label patterns each rule covers.
    """
    cfg = load_brand_config(brand) or {}
    if not cfg:
        return []
    suppressed: List[str] = []
    for rule in SUPPRESSION_RULES:
        trigger_key, trigger_value = rule["trigger"]
        actual = cfg.get(trigger_key)
        if actual is None:
            continue
        if str(actual).strip().lower() != str(trigger_value).strip().lower():
            continue
        # Match each bundle field by label substring
        substrings = [s.lower() for s in rule["label_substrings"]]
        for col, meta in bundle_fields.items():
            label_lower = (meta.get("label") or "").lower()
            for sub in substrings:
                if sub in label_lower:
                    suppressed.append(meta.get("field_key", ""))
                    break
    return suppressed
