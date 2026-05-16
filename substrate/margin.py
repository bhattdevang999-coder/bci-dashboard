"""Atlas Unit Economics — per-ASIN-per-month margin rollup.

Joins `cost_inputs` (operator-supplied per-unit costs) with
`outcome_events` (sales metrics written by substrate/unit_economics.py)
to produce a contribution-margin view.

Three margin columns, side by side (UNIT_ECONOMICS.md decision):

  contribution_margin_per_unit
      = avg_selling_price - landed_cost - fba_fee - third_pl_fee - referral_fee
      (Variable per-unit only. NO ad spend. NO fixed overhead. This is the
      "what's left to cover ads + fixed costs + profit" number.)

  tacos
      = ad_spend / revenue
      (Total ACOS — ad spend as a share of TOTAL revenue, including organic.
      This is the % of revenue going to ads.)

  net_after_ads_per_unit
      = contribution_margin_per_unit - (ad_spend / units_sold)
      (Per-unit profit after ads. The number that actually changes the
      bank account.)

We show ALL THREE because hiding ad spend inside margin makes ad ROI
invisible, and only showing net-after-ads makes it look like the brand is
broken when in fact PPC is the leak.

Honest about gaps:

  - If no cost row exists, contribution_margin is None. The UI must
    render "no cost on file" instead of zero.
  - If no sales rows exist for the period, revenue/units are 0. The
    row is still emitted (so the operator can see the ASIN has no
    activity).
  - revenue counted from the LATEST outcome row per (asin, metric) in
    the period — business reports overwrite the same period on each
    re-upload, and we always want the latest snapshot.

Period format: 'YYYY-MM'. Periods are derived from outcome_events.period_start.
ASINs that have cost rows OR sales rows are included; ASINs with neither
are excluded (nothing to say about them).

Never raises. Best-effort. Returns empty list on DB unavailable.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from .db import get_pool
from .cost_inputs import _DEFAULT_REFERRAL_PCT, read_overhead, list_cost_inputs

logger = logging.getLogger("atlas.substrate.margin")


# Metrics we read out of outcome_events for the rollup. Keep aligned with
# substrate/unit_economics.py:_SALES_METRIC_FIELDS.
_SALES_METRICS = (
    "revenue", "units_sold", "sessions",
    "returns", "return_rate", "buy_box_pct",
    "ad_spend", "ad_revenue", "acos",
)


def _to_period(dt: Any) -> Optional[str]:
    """Convert a timestamp (str or datetime) to a 'YYYY-MM' period."""
    if dt is None:
        return None
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m")
    s = str(dt)
    if len(s) >= 7:
        return s[:7]
    return None


def margin_rollup(
    workspace_id: str,
    *,
    period: Optional[str] = None,
    asin: Optional[str] = None,
) -> dict[str, Any]:
    """Return the per-ASIN-per-month margin rollup.

    Args:
      workspace_id   — required
      period         — 'YYYY-MM' filter; None = all periods present in outcome_events
      asin           — single-ASIN filter (still groups by period)

    Shape:
      {
        ok: True,
        workspace_id, period, asin,
        rows: [
          {
            asin, period,
            revenue, units_sold, sessions, returns, return_rate, buy_box_pct,
            ad_spend, ad_revenue, acos,
            avg_selling_price,        # revenue / units_sold or None
            landed_cost, fba_fee, third_pl_fee, referral_pct, map_price,
            referral_fee_per_unit,    # avg_selling_price * referral_pct or None
            contribution_margin_per_unit,
            contribution_margin_pct,
            tacos,
            net_after_ads_per_unit,
            net_after_ads_pct,
            costs_complete,           # True iff all 4 required cost fields exist
            missing_costs: [str],     # list of cost fields not on file
            warnings: [str],
          },
          ...
        ],
        brand_fixed_overhead_monthly: float | None,
        totals: {
          revenue, units_sold, ad_spend, ad_revenue,
          tacos, net_revenue_after_ads,
          contribution_margin_total,   # sum(margin * units) where margin known
          net_after_overhead,          # contribution_margin_total - fixed_overhead_monthly
        }
      }
    """
    out: dict[str, Any] = {
        "ok": True,
        "workspace_id": workspace_id,
        "period": period,
        "asin": asin,
        "rows": [],
        "brand_fixed_overhead_monthly": None,
        "totals": {},
    }
    pool = get_pool()
    if pool is None:
        return out

    # ---- Sales: latest value per (asin, period, metric) ----
    # Outcome_events can carry multiple writes per (asin, metric) per period
    # (re-uploads of the same business report). We want the most recent
    # write per tuple. period derives from period_start if present, else
    # falls back to observed_at.
    where = ["workspace_id = %s", "metric = ANY(%s)"]
    params: list[Any] = [workspace_id, list(_SALES_METRICS)]
    if asin:
        where.append("asin = %s")
        params.append(asin)
    if period:
        # period_start may be NULL; fall back to observed_at for those
        where.append(
            "(TO_CHAR(COALESCE(period_start, observed_at), 'YYYY-MM') = %s)"
        )
        params.append(period)

    sql = f"""
        WITH ranked AS (
            SELECT
                asin,
                TO_CHAR(COALESCE(period_start, observed_at), 'YYYY-MM') AS period,
                metric,
                value,
                observed_at,
                ROW_NUMBER() OVER (
                    PARTITION BY asin,
                                 TO_CHAR(COALESCE(period_start, observed_at), 'YYYY-MM'),
                                 metric
                    ORDER BY observed_at DESC
                ) AS rn
            FROM outcome_events
            WHERE {' AND '.join(where)}
        )
        SELECT asin, period, metric, value
        FROM ranked
        WHERE rn = 1
        ORDER BY asin, period, metric
    """

    sales: dict[tuple[str, str], dict[str, float]] = {}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    a, p, m, v = r
                    if not a or not p:
                        continue
                    sales.setdefault((a, p), {})[m] = float(v) if v is not None else 0.0
    except Exception as exc:
        logger.warning("margin rollup outcome_events read failed: %s", exc)
        return out

    # ---- Costs: load all cost rows for the workspace; map by asin ----
    costs_by_asin: dict[str, dict[str, Any]] = {
        c["asin"]: c for c in list_cost_inputs(workspace_id)
    }

    # ---- Brand-level overhead ----
    overhead = read_overhead(workspace_id) or {}
    out["brand_fixed_overhead_monthly"] = overhead.get("fixed_overhead_monthly")

    # If the operator filters by an ASIN with a cost row but no sales rows
    # this period, still surface the ASIN (so they see "no activity").
    if asin and (asin, period) not in sales and asin in costs_by_asin:
        if period:
            sales.setdefault((asin, period), {})

    # ---- Build rows ----
    rows: list[dict[str, Any]] = []
    tot_revenue = tot_units = tot_ad_spend = tot_ad_revenue = 0.0
    tot_margin_dollars = 0.0  # sum(contribution_margin_per_unit * units) where known

    for (a, p), metrics in sorted(sales.items()):
        cost = costs_by_asin.get(a, {})
        revenue   = metrics.get("revenue", 0.0)
        units     = metrics.get("units_sold", 0.0)
        sessions  = metrics.get("sessions", 0.0)
        returns_  = metrics.get("returns", 0.0)
        ret_rate  = metrics.get("return_rate")  # None if not on file
        bbox      = metrics.get("buy_box_pct")
        ad_spend  = metrics.get("ad_spend", 0.0)
        ad_rev    = metrics.get("ad_revenue", 0.0)
        acos      = metrics.get("acos")

        avg_price = (revenue / units) if units else None

        landed = cost.get("landed_cost")
        fba    = cost.get("fba_fee")
        tpl    = cost.get("third_pl_fee")
        ref    = cost.get("referral_pct")
        mp     = cost.get("map_price")

        missing: list[str] = []
        if landed is None: missing.append("landed_cost")
        if fba is None:    missing.append("fba_fee")
        if tpl is None:    missing.append("third_pl_fee")
        # referral_pct is allowed to fall back to default
        eff_ref = ref if ref is not None else _DEFAULT_REFERRAL_PCT

        referral_fee = (avg_price * eff_ref) if avg_price is not None else None

        contribution = None
        contribution_pct = None
        if avg_price is not None and not missing:
            contribution = (
                avg_price - (landed or 0) - (fba or 0) - (tpl or 0) - (referral_fee or 0)
            )
            contribution_pct = (contribution / avg_price) if avg_price else None

        tacos_val = (ad_spend / revenue) if revenue else None
        ad_per_unit = (ad_spend / units) if units else 0.0
        net_per_unit = None
        net_pct = None
        if contribution is not None:
            net_per_unit = contribution - ad_per_unit
            net_pct = (net_per_unit / avg_price) if avg_price else None

        warnings: list[str] = []
        if avg_price is not None and mp is not None and avg_price < mp:
            warnings.append(
                f"Avg selling price ${avg_price:.2f} is BELOW MAP ${mp:.2f}"
            )
        if missing and units:
            warnings.append(
                f"{len(missing)} cost field(s) not on file \u2014 contribution not computed"
            )

        rows.append({
            "asin": a,
            "period": p,
            "revenue": revenue,
            "units_sold": units,
            "sessions": sessions,
            "returns": returns_,
            "return_rate": ret_rate,
            "buy_box_pct": bbox,
            "ad_spend": ad_spend,
            "ad_revenue": ad_rev,
            "acos": acos,
            "avg_selling_price": avg_price,
            "landed_cost": landed,
            "fba_fee": fba,
            "third_pl_fee": tpl,
            "referral_pct": eff_ref,
            "referral_pct_is_default": ref is None,
            "map_price": mp,
            "referral_fee_per_unit": referral_fee,
            "contribution_margin_per_unit": contribution,
            "contribution_margin_pct": contribution_pct,
            "tacos": tacos_val,
            "ad_spend_per_unit": ad_per_unit,
            "net_after_ads_per_unit": net_per_unit,
            "net_after_ads_pct": net_pct,
            "costs_complete": not missing,
            "missing_costs": missing,
            "warnings": warnings,
        })

        tot_revenue   += revenue
        tot_units     += units
        tot_ad_spend  += ad_spend
        tot_ad_revenue += ad_rev
        if contribution is not None and units:
            tot_margin_dollars += contribution * units

    overhead_monthly = out["brand_fixed_overhead_monthly"] or 0
    out["rows"] = rows
    out["totals"] = {
        "revenue": tot_revenue,
        "units_sold": tot_units,
        "ad_spend": tot_ad_spend,
        "ad_revenue": tot_ad_revenue,
        "tacos": (tot_ad_spend / tot_revenue) if tot_revenue else None,
        "net_revenue_after_ads": tot_revenue - tot_ad_spend,
        "contribution_margin_total": tot_margin_dollars,
        "net_after_overhead": tot_margin_dollars - overhead_monthly - tot_ad_spend,
    }
    return out


def list_periods(workspace_id: str) -> list[str]:
    """Return the distinct 'YYYY-MM' periods present in outcome_events.

    Used to populate the period dropdown in the UI.
    """
    pool = get_pool()
    if pool is None:
        return []
    out: list[str] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT TO_CHAR(COALESCE(period_start, observed_at), 'YYYY-MM') AS p
                    FROM outcome_events
                    WHERE workspace_id = %s
                      AND metric = ANY(%s)
                    ORDER BY p DESC
                    """,
                    (workspace_id, list(_SALES_METRICS)),
                )
                for (p,) in cur.fetchall():
                    if p:
                        out.append(p)
    except Exception as exc:
        logger.warning("list_periods failed: %s", exc)
    return out


__all__ = ["margin_rollup", "list_periods"]
