"""Build a rich taxonomy universe from the NIS rule-engine bundles.

The static taxonomy_universe.json historically shipped incomplete data
(notably: COAT had zero item_type_names and no cascade item_type_keyword lists).
The rule-engine bundles however have every named range Amazon defined, so we
can extract a complete universe from them.

Output schema (matches what the dashboard modal expects, plus new cascade maps):
{
  "COAT": {
    "product_categories": ["Men's Outerwear", ...],
    "subcategories_by_category": {
        "Men's Outerwear": ["Down", "Wool Coats", ...],
        ...
    },
    "item_type_keywords_by_cat_sub": {
        "Men's Outerwear": {
            "Wool Coats": ["wool-outerwear-coats", ...],
            "Down":       ["down-outerwear-coats", ...],
        },
        ...
    },
    "item_type_names": ["Puffer Coat", ...],  # from COATitem_type_name1.value if it exists
    "source": "engine"   # or "static_file" or "merged"
  },
  ...
}
"""

import json
import os
import re
from typing import Dict, List, Optional

try:
    from .nis_rule_engine import set_bundle_dir, load_bundle, list_product_types
except ImportError:
    from nis_rule_engine import set_bundle_dir, load_bundle, list_product_types


# Pattern for cascade names. Amazon encodes values as PascalCaseNoSpaces.
# e.g. COATproduct_category.value.WomensOuterwear.product_subcategory.value.Wool.item_type_keyword1.value
_SUB_PAT = re.compile(
    r"^([A-Z_0-9]+)product_category\.value\.(.+?)\.product_subcategory1\.value$"
)
_ITK_PAT = re.compile(
    r"^([A-Z_0-9]+)product_category\.value\.(.+?)"
    r"\.product_subcategory\.value\.(.+?)"
    r"\.item_type_keyword1\.value$"
)


def _encode(label: str) -> str:
    """Amazon's encoding: strip spaces + punctuation, keep alphanumerics, preserve case.
    Used to map a human-readable category label back to the encoded form in named ranges.
    """
    if not label:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", label)


def _decode_best_effort(encoded: str, candidates: List[str]) -> Optional[str]:
    """Map an encoded PascalCase label back to the original from a list of human-readable
    candidates. Returns None if no match."""
    if not encoded or not candidates:
        return None
    lookup = {_encode(c): c for c in candidates}
    return lookup.get(encoded)


def build_universe_from_engine(bundle_dir: str) -> Dict[str, dict]:
    """Walk every bundle in `bundle_dir` and return the full taxonomy universe."""
    set_bundle_dir(bundle_dir)
    universe: Dict[str, dict] = {}

    for pt in list_product_types():
        bundle = load_bundle(pt)
        if not bundle:
            continue

        named = bundle.get("named_ranges") or {}

        # 1) Top-level categories
        cat_name = f"{pt}product_category1.value"
        categories = named.get(cat_name, []) or []
        # Normalize to strings, strip blanks
        categories = [str(c).strip() for c in categories if c is not None and str(c).strip()]

        # 2) Subcategories per category
        subs_by_cat: Dict[str, List[str]] = {}
        for name, values in named.items():
            m = _SUB_PAT.match(name)
            if not m:
                continue
            if m.group(1) != pt:
                continue
            encoded_cat = m.group(2)
            # Decode the category
            decoded = _decode_best_effort(encoded_cat, categories)
            key = decoded or encoded_cat
            subs = [str(v).strip() for v in values if v is not None and str(v).strip()]
            if subs:
                subs_by_cat[key] = subs

        # 3) Item type keywords per (category, subcategory)
        itk_by_cat_sub: Dict[str, Dict[str, List[str]]] = {}
        for name, values in named.items():
            m = _ITK_PAT.match(name)
            if not m:
                continue
            if m.group(1) != pt:
                continue
            encoded_cat = m.group(2)
            encoded_sub = m.group(3)
            decoded_cat = _decode_best_effort(encoded_cat, categories) or encoded_cat
            sub_candidates = subs_by_cat.get(decoded_cat, [])
            decoded_sub = _decode_best_effort(encoded_sub, sub_candidates) or encoded_sub
            keywords = [str(v).strip() for v in values if v is not None and str(v).strip()]
            if keywords:
                itk_by_cat_sub.setdefault(decoded_cat, {})[decoded_sub] = keywords

        # 4) Item type names — may exist as a flat list or a per-cascade list. For COAT
        #    this is typically empty in Amazon's template. We'll surface whatever exists.
        itn_flat = named.get(f"{pt}item_type_name1.value", []) or []

        universe[pt] = {
            "product_categories": sorted(set(categories)),
            "subcategories_by_category": {
                k: sorted(set(v)) for k, v in subs_by_cat.items()
            },
            "item_type_keywords_by_cat_sub": itk_by_cat_sub,
            "item_type_names": sorted(set(str(x).strip() for x in itn_flat if x)),
            "source": "engine",
        }

    return universe


def merge_universes(static: dict, engine: dict) -> dict:
    """Merge the hand-curated static universe with the engine-derived one.

    Engine data wins for cascade maps (item_type_keywords_by_cat_sub) since it's
    the ground truth from Amazon's XLSM. Static data wins for handmade fixups
    (item_type_names that operators added even when the template doesn't define them).
    """
    out = {}
    all_pts = set(static.keys()) | set(engine.keys())
    for pt in sorted(all_pts):
        s = static.get(pt, {}) or {}
        e = engine.get(pt, {}) or {}

        # Categories: union, prefer engine ordering but add any static-only entries
        cats = list(dict.fromkeys(
            (e.get("product_categories") or []) + (s.get("product_categories") or [])
        ))

        # Subcategories: engine's map wins; add any subcategories the static file had
        #                that the engine missed (e.g. legacy categories).
        engine_subs = e.get("subcategories_by_category") or {}
        static_subs = s.get("subcategories_by_category") or {}
        merged_subs = dict(engine_subs)
        for k, v in static_subs.items():
            if k not in merged_subs:
                merged_subs[k] = v
            else:
                # union subcategory lists
                merged_subs[k] = list(dict.fromkeys(merged_subs[k] + v))

        # Item type names: keep static list if any, otherwise engine's
        itns = s.get("item_type_names") or e.get("item_type_names") or []

        out[pt] = {
            "product_categories": cats,
            "subcategories_by_category": merged_subs,
            "item_type_keywords_by_cat_sub": e.get("item_type_keywords_by_cat_sub", {}),
            "item_type_names": itns,
            "source": "merged",
        }
    return out


def rebuild_and_save(
    bundle_dir: str,
    static_file: str,
    out_file: Optional[str] = None,
) -> dict:
    """Rebuild the universe and write it to `out_file` (defaults to overwriting static_file).
    Returns the final merged universe.
    """
    engine_universe = build_universe_from_engine(bundle_dir)
    static_universe: dict = {}
    if os.path.exists(static_file):
        try:
            with open(static_file, "r", encoding="utf-8") as f:
                static_universe = json.load(f)
        except Exception:
            static_universe = {}

    merged = merge_universes(static_universe, engine_universe)

    if out_file is None:
        out_file = static_file
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python taxonomy_builder.py <bundle_dir> <static_file> [<out_file>]")
        sys.exit(1)
    bundle_dir = sys.argv[1]
    static_file = sys.argv[2]
    out_file = sys.argv[3] if len(sys.argv) > 3 else None
    u = rebuild_and_save(bundle_dir, static_file, out_file)
    print(f"Wrote {out_file or static_file}")
    for pt, data in sorted(u.items()):
        n_itk = sum(len(v) for v in data.get("item_type_keywords_by_cat_sub", {}).values())
        print(f"  {pt:<20} {len(data.get('product_categories',[])):>3} cats, "
              f"{sum(len(v) for v in data.get('subcategories_by_category',{}).values()):>4} subs, "
              f"{n_itk:>4} cascade ITKs, "
              f"{len(data.get('item_type_names',[])):>3} ITN names")
