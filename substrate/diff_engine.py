"""Catalog Intel — snapshot diff engine.

Compares findings between two snapshots of the same workspace and
classifies each finding as resolved / new / unchanged / changed.

Design principles:
  1. We DO NOT recompute findings against historical data. Stored
     findings are the historical record — recomputing would be lying
     about what the check said at the time.
  2. Matching key is (rule_name, asin). asin is NULL for aggregate
     findings, so aggregate findings match at the rule level.
  3. Where a rule has a comparable metric in evidence (e.g. dead_pct,
     pct_filled), we compute the direction of change so the UI can
     label 'improved' or 'worsened' — not just 'changed'.
  4. Verifiability: every diff row carries both findings (old + new)
     so the UI can show the rule spec + math for both, and drilldowns
     work as usual against whichever snapshot the row references.

Return shape (per bucket, each row):
  {
    "key": {"rule_name": str, "asin": str|None},
    "from_finding": <full finding dict, or None if new>,
    "to_finding":   <full finding dict, or None if resolved>,
    "metric": {"key": str, "from": float|None, "to": float|None,
               "direction": "improved"|"worsened"|None,
               "delta": float|None},  # None if metric can't be extracted
  }
"""
from __future__ import annotations
from typing import Optional


# Metric extraction map. For each rule, the primary metric to compare
# across snapshots and whether "up" is improvement or regression.
#
# 'lower_is_better' means smaller values = healthier (e.g. dead_pct
# dropping is good).
# 'higher_is_better' means larger values = healthier (e.g. fill rate
# rising is good).

METRIC_MAP = {
    "dead_inventory":              {"key": "dead_pct",             "direction": "lower_is_better"},
    "description_presence":        {"key": "pct_with_description", "direction": "higher_is_better"},
    "fabric_material_coverage":    {"key": "pct_filled",           "direction": "higher_is_better"},
    "buy_box_ownership":           {"key": "likely_owner_pct",     "direction": "higher_is_better"},
    "image_count_dist":            {"key": "under_5_pct",          "direction": "lower_is_better"},
    "bullet_completeness_dist":    {"key": "under_3_pct",          "direction": "lower_is_better"},
    "title_length_dist":           {"key": "flagged_pct",          "direction": "lower_is_better"},
    "variation_theme_integrity":   {"key": "inconsistent_pct",     "direction": "lower_is_better"},
    "style_family_concentration":  {"key": "mega_family_count",    "direction": "lower_is_better"},
    "list_price_dist":             {"key": "outlier_count",        "direction": "lower_is_better"},
    "concentration_pareto":        {"key": "top_50pct_asins",      "direction": "higher_is_better"},
    "cohort_split":                {"key": "dead_pct",             "direction": "lower_is_better"},
    "a_plus_lift":                 {"key": "lift_multiplier",      "direction": "higher_is_better"},
    # Rules with no single comparable metric — presence/absence only
    "fill_rate_report":            None,
    "subcategory_rollup":          None,
}

# Materiality threshold — deltas smaller than this are treated as
# 'unchanged' even if the raw number moved slightly. Keeps the diff
# from being noisy on floating-point wobble.
MATERIALITY_PCT_POINTS = 1.0
MATERIALITY_COUNT = 5


def _extract_metric(finding: dict, rule_name: str) -> tuple[Optional[float], Optional[str]]:
    """Return (metric_value, metric_key) or (None, None) if unavailable."""
    m = METRIC_MAP.get(rule_name)
    if not m:
        return None, None
    key = m["key"]
    ev = finding.get("evidence") or {}
    val = ev.get(key)
    if val is None:
        return None, None
    try:
        return float(val), key
    except (TypeError, ValueError):
        return None, None


def _classify_change(rule_name: str, from_val: float, to_val: float) -> tuple[Optional[str], float]:
    """Return ('improved'|'worsened'|None, delta).

    None direction = change was below materiality threshold, treat as unchanged.
    """
    m = METRIC_MAP.get(rule_name)
    if not m:
        return None, 0.0
    delta = to_val - from_val
    # Materiality: pick threshold by unit hint in the key name
    key = m["key"]
    materiality = MATERIALITY_PCT_POINTS if ("pct" in key or "rate" in key) else MATERIALITY_COUNT
    if abs(delta) < materiality:
        return None, delta
    direction = m["direction"]
    if direction == "lower_is_better":
        return ("improved" if delta < 0 else "worsened"), delta
    else:  # higher_is_better
        return ("improved" if delta > 0 else "worsened"), delta


def _finding_key(f: dict) -> tuple:
    """Canonical match key: (rule_name, asin_or_empty_string)."""
    return (f.get("rule_name") or "", f.get("asin") or "")


def compute_diff(from_findings: list[dict], to_findings: list[dict],
                 from_snapshot: Optional[dict] = None,
                 to_snapshot: Optional[dict] = None) -> dict:
    """Compute the full diff between two lists of findings.

    Args:
      from_findings: findings from the earlier snapshot
      to_findings:   findings from the later snapshot
      from_snapshot: metadata dict for the earlier snapshot (id, uploaded_at, ...)
      to_snapshot:   metadata dict for the later snapshot

    Returns a dict with resolved/new/unchanged/changed buckets and a
    totals summary.
    """
    from_by_key = {_finding_key(f): f for f in from_findings}
    to_by_key = {_finding_key(f): f for f in to_findings}
    from_keys = set(from_by_key.keys())
    to_keys = set(to_by_key.keys())

    resolved_keys = from_keys - to_keys      # in old, gone in new
    new_keys = to_keys - from_keys           # in new, absent in old
    shared_keys = from_keys & to_keys        # matched — either unchanged or changed

    resolved = []
    for k in sorted(resolved_keys):
        f = from_by_key[k]
        resolved.append({
            "key": {"rule_name": k[0], "asin": k[1] or None},
            "from_finding": f,
            "to_finding": None,
            "metric": None,
        })

    new = []
    for k in sorted(new_keys):
        f = to_by_key[k]
        new.append({
            "key": {"rule_name": k[0], "asin": k[1] or None},
            "from_finding": None,
            "to_finding": f,
            "metric": None,
        })

    unchanged = []
    changed = []
    for k in sorted(shared_keys):
        f_from = from_by_key[k]
        f_to = to_by_key[k]
        rule = k[0]
        from_val, mkey = _extract_metric(f_from, rule)
        to_val, _ = _extract_metric(f_to, rule)
        if from_val is None or to_val is None:
            # Can't compare metrics — treat as unchanged
            unchanged.append({
                "key": {"rule_name": rule, "asin": k[1] or None},
                "from_finding": f_from,
                "to_finding": f_to,
                "metric": {"key": mkey, "from": from_val, "to": to_val,
                           "direction": None, "delta": None},
            })
            continue
        direction, delta = _classify_change(rule, from_val, to_val)
        metric = {"key": mkey, "from": from_val, "to": to_val,
                  "direction": direction, "delta": delta}
        if direction is None:
            unchanged.append({
                "key": {"rule_name": rule, "asin": k[1] or None},
                "from_finding": f_from, "to_finding": f_to,
                "metric": metric,
            })
        else:
            changed.append({
                "key": {"rule_name": rule, "asin": k[1] or None},
                "from_finding": f_from, "to_finding": f_to,
                "metric": metric,
            })

    # Sort changed by absolute delta magnitude descending (biggest movers first)
    changed.sort(
        key=lambda r: abs((r.get("metric") or {}).get("delta") or 0),
        reverse=True,
    )

    return {
        "ok": True,
        "from_snapshot": from_snapshot,
        "to_snapshot": to_snapshot,
        "resolved": resolved,
        "new": new,
        "unchanged": unchanged,
        "changed": changed,
        "totals": {
            "resolved": len(resolved),
            "new": len(new),
            "unchanged": len(unchanged),
            "changed": len(changed),
            "improved": sum(1 for r in changed if r["metric"]["direction"] == "improved"),
            "worsened": sum(1 for r in changed if r["metric"]["direction"] == "worsened"),
        },
    }
