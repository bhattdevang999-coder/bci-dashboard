"""Per-product-type defaults & helpers.

Single source of truth for behavior that varies between product types — what
sleeve length to seed for a coat vs a swimsuit, whether item dimensions are
required (sandals: yes; coats: no), what title noun to use for content
generation when the source data doesn't tell us, what label to put in the UI
("Jackets and Coats template" instead of the old hardcoded "Dresses template").

Universal apparel defaults (battery=No, dg=Not Applicable, body_type=All Body
Types, etc.) live in apparel_defaults.json and are merged in by the rule
engine. This file fills the gap: per-PT presentation + write-path defaults
that the engine never had a place for.

Usage:
    from nis_engine.pt_defaults import get_pt_default, pt_label, all_pts
    sleeve = get_pt_default("COAT", "default_sleeve_length")  # "Long Sleeve"
    label  = pt_label("COAT", count=4)                         # "4 coats"
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

_CACHE: Optional[Dict[str, Any]] = None


def _engine_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _load() -> Dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = os.path.join(_engine_dir(), "pt_defaults.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"[pt_defaults] failed to load {path}: {e}")
        doc = {}
    # Strip top-level metadata keys
    _CACHE = {k: v for k, v in doc.items() if not k.startswith("_")}
    return _CACHE


def reload_pt_defaults() -> None:
    """Force re-read from disk (used by tests + admin endpoints)."""
    global _CACHE
    _CACHE = None
    _load()


def all_pts() -> list[str]:
    """All product type IDs we have defaults for."""
    return sorted(_load().keys())


def get_pt(pt_id: str) -> Dict[str, Any]:
    """Full defaults dict for a product type, or empty if unknown."""
    if not pt_id:
        return {}
    return _load().get(pt_id.upper(), {})


def get_pt_default(pt_id: str, key: str, fallback: Any = "") -> Any:
    """Single value lookup. Falls back to `fallback` (default empty string)."""
    return get_pt(pt_id).get(key, fallback)


def pt_label(pt_id: str, count: Optional[int] = None) -> str:
    """Human-readable label for UI surfaces.

    pt_label("COAT")           -> "coats"
    pt_label("COAT", count=1)  -> "1 coat"
    pt_label("COAT", count=4)  -> "4 coats"
    pt_label("UNKNOWN")        -> "unknown"
    """
    pt = get_pt(pt_id)
    if not pt:
        return (pt_id or "unknown").lower()
    if count is None:
        return pt["label_plural"]
    word = pt["label_singular"] if count == 1 else pt["label_plural"]
    return f"{count} {word}"


def pt_template_filename(pt_id: str) -> str:
    """Default Amazon template filename for a PT."""
    return get_pt_default(pt_id, "template_file", "")


def pt_title_word(pt_id: str) -> str:
    """Title-case product word to use when assembling a title."""
    return get_pt_default(pt_id, "title_product_word", "")


def pt_writes(pt_id: str, field_kind: str) -> bool:
    """Should the template-writer emit this category of field for this PT?

    field_kind values:
      - 'battery'     -> writes_battery_field
      - 'dg'          -> writes_dg_regulation
      - 'item_dims'   -> writes_item_dimensions
      - 'package_dims'-> writes_package_dimensions
    """
    key_map = {
        "battery":      "writes_battery_field",
        "dg":           "writes_dg_regulation",
        "item_dims":    "writes_item_dimensions",
        "package_dims": "writes_package_dimensions",
    }
    key = key_map.get(field_kind)
    if not key:
        return False
    return bool(get_pt_default(pt_id, key, False))


def template_label_for_session(loaded_pt_ids: list[str], counts: Optional[Dict[str, int]] = None) -> str:
    """UI-friendly label for the active-template badge.

    No PTs:          "no template loaded"
    One PT (no cnt): "Coats template"
    One PT + count:  "Coats template (4 styles)"
    Multiple:        "Coats (4) + Swimwear (1)"
    """
    if not loaded_pt_ids:
        return "no template loaded"
    if len(loaded_pt_ids) == 1:
        pt = loaded_pt_ids[0]
        word = (get_pt_default(pt, "label_plural", pt.lower()) or pt.lower()).title()
        if counts and pt in counts:
            n = counts[pt]
            return f"{word} template ({n} style{'s' if n != 1 else ''})"
        return f"{word} template"
    parts = []
    for pt in loaded_pt_ids:
        word = (get_pt_default(pt, "label_plural", pt.lower()) or pt.lower()).title()
        if counts and pt in counts:
            parts.append(f"{word} ({counts[pt]})")
        else:
            parts.append(word)
    return " + ".join(parts)
