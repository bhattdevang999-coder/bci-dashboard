"""Atlas Budget substrate.

Captures planned PPC spend per period (YYYY-MM) per scope (theme | overall).
Strictly PPC for v1 — operational costs are out of scope.

API surface:

  set_budget(workspace_id, period, scope_type, scope_value, amount, ...)
      Upsert a budget row. Also writes a decision_event with
      module='budget', field_name='monthly_allocation' so the audit
      trail in Memory carries budget intent the same way it carries
      listing intent. Returns the (event_id, scope_key).

  list_budgets(workspace_id, period=None)
      Read all budgets for a workspace (optionally filtered to one
      period). Returns list of dicts.

  variance_for_period(workspace_id, period)
      Compute planned vs actual per scope for one period. Joins:
        - budget table (planned)
        - outcome_events with metric='spend' for that period (actual)
        - decision_events with module='nis' touching this period's
          ASINs (content-change markers, for honesty annotation)

Status bucket ladder:
  - 'no_budget' when actual exists but no planned amount set
  - 'no_spend' when planned exists but no actual yet
  - 'under' when actual <= planned * 0.95
  - 'at' when 0.95 < pct_used <= 1.05
  - 'over' when pct_used > 1.05
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from substrate.db import get_pool

logger = logging.getLogger("atlas.substrate.budget")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_VALID_SCOPE_TYPES = ("theme", "overall", "asin")
_VALID_THEMES = ("branded", "feature", "competitor")


def _validate_period(period: str) -> bool:
    if not period or not _PERIOD_RE.match(period):
        return False
    try:
        y, m = period.split("-")
        yy, mm = int(y), int(m)
        return 2020 <= yy <= 2100 and 1 <= mm <= 12
    except Exception:
        return False


def _validate_scope(scope_type: str, scope_value: str) -> Optional[str]:
    """Returns None if valid, else an error string."""
    if scope_type not in _VALID_SCOPE_TYPES:
        return f"scope_type must be one of {_VALID_SCOPE_TYPES}"
    if not scope_value:
        return "scope_value required"
    if scope_type == "theme" and scope_value not in _VALID_THEMES:
        return f"theme scope_value must be one of {_VALID_THEMES}"
    if scope_type == "overall" and scope_value != "_overall":
        return "overall scope_value must be '_overall'"
    if scope_type == "asin" and not re.match(r"^B0[A-Z0-9]{8}$", scope_value):
        return "asin scope_value must look like B0XXXXXXXX"
    return None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def set_budget(
    workspace_id: str,
    period: str,
    scope_type: str,
    scope_value: str,
    amount: float,
    *,
    currency: str = "USD",
    set_by: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Upsert a budget row + emit a decision_event for the audit trail.

    Returns {ok, error?, scope_key?, event_id?}.
    """
    if not _validate_period(period):
        return {"ok": False, "error": "period must be YYYY-MM"}
    err = _validate_scope(scope_type, scope_value)
    if err:
        return {"ok": False, "error": err}
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "amount must be a number"}
    if amount_f < 0:
        return {"ok": False, "error": "amount must be >= 0"}

    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "substrate unavailable"}

    scope_key = f"{scope_type}:{scope_value}"
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO budget (
                        workspace_id, period, scope_type, scope_value,
                        amount, currency, set_by, notes
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (workspace_id, period, scope_type, scope_value)
                    DO UPDATE SET
                        amount = EXCLUDED.amount,
                        currency = EXCLUDED.currency,
                        set_at = NOW(),
                        set_by = COALESCE(EXCLUDED.set_by, budget.set_by),
                        notes = EXCLUDED.notes
                    """,
                    (workspace_id, period, scope_type, scope_value,
                     amount_f, currency, set_by, notes),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("set_budget upsert failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}

    # Emit a decision_event so the budget revision lands in Memory.
    # Budget edits are quick standalone actions, so we don't wrap
    # them in a session — that would just be noise per click.
    event_id = None
    try:
        from substrate.logger import log_field_decision
        from substrate.schema import Module
        event_id = log_field_decision(
            workspace_id=workspace_id,
            session_id=None,
            module=Module.BUDGET,
            field_name="monthly_allocation",
            atlas_output={
                "period": period,
                "scope_type": scope_type,
                "scope_value": scope_value,
                "amount": amount_f,
                "currency": currency,
                "notes": notes,
            },
            overall_confidence=1.0,
            rules_injected=[],
            brand_profile_version=f"{workspace_id}_legacy",
            enforce_filter=False,
        )
    except Exception as exc:
        logger.warning("budget decision_event write skipped: %s", exc)

    return {"ok": True, "scope_key": scope_key, "event_id": event_id,
            "period": period, "amount": amount_f}


def delete_budget(
    workspace_id: str,
    period: str,
    scope_type: str,
    scope_value: str,
) -> dict[str, Any]:
    """Remove a budget row. Does NOT delete the original decision_events
    (those stay in the audit trail). Returns {ok, deleted: int}.
    """
    if not _validate_period(period):
        return {"ok": False, "error": "period must be YYYY-MM"}
    err = _validate_scope(scope_type, scope_value)
    if err:
        return {"ok": False, "error": err}
    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "substrate unavailable"}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM budget
                    WHERE workspace_id = %s AND period = %s
                      AND scope_type = %s AND scope_value = %s
                    """,
                    (workspace_id, period, scope_type, scope_value),
                )
                deleted = cur.rowcount
            conn.commit()
        return {"ok": True, "deleted": deleted}
    except Exception as exc:
        logger.warning("delete_budget failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_budgets(
    workspace_id: str,
    period: Optional[str] = None,
) -> list[dict[str, Any]]:
    pool = get_pool()
    if pool is None:
        return []
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if period:
        if not _validate_period(period):
            return []
        where.append("period = %s")
        params.append(period)
    sql = f"""
        SELECT period, scope_type, scope_value, amount, currency,
               set_at, set_by, notes
        FROM budget
        WHERE {' AND '.join(where)}
        ORDER BY period DESC, scope_type ASC, scope_value ASC
    """
    rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                cols = [d[0] for d in cur.description]
                for r in cur:
                    d = dict(zip(cols, r))
                    if d.get("set_at") is not None and hasattr(d["set_at"], "isoformat"):
                        d["set_at"] = d["set_at"].isoformat()
                    if d.get("amount") is not None:
                        d["amount"] = float(d["amount"])
                    rows.append(d)
    except Exception as exc:
        logger.warning("list_budgets failed: %s", exc)
    return rows


# ---------------------------------------------------------------------------
# Variance computation
# ---------------------------------------------------------------------------


def _period_to_range(period: str) -> tuple[str, str]:
    """Convert 'YYYY-MM' to (start_iso, end_iso). Start is the first
    day of the month inclusive; end is the first day of the next
    month exclusive."""
    y, m = period.split("-")
    yy, mm = int(y), int(m)
    start = datetime(yy, mm, 1, 0, 0, 0, tzinfo=timezone.utc)
    if mm == 12:
        end = datetime(yy + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    else:
        end = datetime(yy, mm + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


def _bucket_status(planned: Optional[float], actual: float) -> str:
    if planned is None or planned == 0:
        return "no_budget" if actual > 0 else "no_data"
    if actual == 0:
        return "no_spend"
    pct = actual / planned
    if pct <= 0.95:
        return "under"
    if pct <= 1.05:
        return "at"
    return "over"


def variance_for_period(
    workspace_id: str,
    period: str,
) -> dict[str, Any]:
    """Compute planned vs actual per scope for one period.

    Pulls planned amounts from `budget`, actual spend from `outcome_events`
    (metric='spend'), and content-change markers from `substrate_events`
    (module='nis' decision_events) for ASINs whose spend appears in this
    period.

    Always returns a well-formed dict, even when budget or outcomes are
    empty. Never raises.
    """
    if not _validate_period(period):
        return {"ok": False, "error": "period must be YYYY-MM"}
    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "substrate unavailable"}
    start_iso, end_iso = _period_to_range(period)

    # 1. Planned per scope_key
    planned_by_key: dict[str, dict[str, Any]] = {}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT scope_type, scope_value, amount, currency, notes
                    FROM budget
                    WHERE workspace_id = %s AND period = %s
                    """,
                    (workspace_id, period),
                )
                for st, sv, amt, ccy, notes in cur:
                    key = f"{st}:{sv}"
                    planned_by_key[key] = {
                        "scope_type": st, "scope_value": sv,
                        "planned": float(amt) if amt is not None else 0.0,
                        "currency": ccy or "USD",
                        "notes": notes,
                    }
    except Exception as exc:
        logger.warning("variance: budget read failed: %s", exc)

    # 2. Actual spend per theme + per ASIN. Theme attribution uses the
    # most-recent marketing decision_event that proposed the keyword
    # (which carries the theme in atlas_output.theme); we resolve this
    # in SQL with a LATERAL join.
    actual_by_key: dict[str, float] = {}
    asins_touched: set[str] = set()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Total PPC spend in the period (rolled to overall)
                cur.execute(
                    """
                    SELECT COALESCE(SUM(value), 0)
                    FROM outcome_events
                    WHERE workspace_id = %s
                      AND metric = 'spend'
                      AND observed_at >= %s AND observed_at < %s
                    """,
                    (workspace_id, start_iso, end_iso),
                )
                row = cur.fetchone()
                overall_spend = float(row[0]) if row and row[0] is not None else 0.0
                actual_by_key["overall:_overall"] = overall_spend

                # Per-theme spend, joined to the most-recent decision_event
                # that proposed the keyword.
                cur.execute(
                    """
                    SELECT
                        COALESCE(d.atlas_output->>'theme', 'unknown') AS theme,
                        COALESCE(SUM(o.value), 0) AS spend
                    FROM outcome_events o
                    LEFT JOIN LATERAL (
                        SELECT atlas_output
                        FROM substrate_events
                        WHERE workspace_id = o.workspace_id
                          AND event_kind = 'decision_event'
                          AND module = 'marketing'
                          AND field_name = 'keyword_candidate'
                          AND LOWER(atlas_output->>'keyword') = LOWER(o.keyword)
                        ORDER BY timestamp DESC
                        LIMIT 1
                    ) d ON TRUE
                    WHERE o.workspace_id = %s
                      AND o.metric = 'spend'
                      AND o.observed_at >= %s AND o.observed_at < %s
                      AND o.keyword IS NOT NULL
                    GROUP BY 1
                    """,
                    (workspace_id, start_iso, end_iso),
                )
                for theme, spend in cur:
                    actual_by_key[f"theme:{theme}"] = float(spend or 0)

                # ASINs that had spend in the period — used for the
                # content-change marker join below.
                cur.execute(
                    """
                    SELECT DISTINCT asin
                    FROM outcome_events
                    WHERE workspace_id = %s
                      AND metric = 'spend'
                      AND observed_at >= %s AND observed_at < %s
                      AND asin IS NOT NULL AND asin <> '_unattached'
                    """,
                    (workspace_id, start_iso, end_iso),
                )
                for (asin,) in cur:
                    asins_touched.add(asin)

                # Per-ASIN spend (substrate already supports asin-scoped
                # budgets; v1 UI doesn't expose them).
                if asins_touched:
                    cur.execute(
                        """
                        SELECT asin, COALESCE(SUM(value), 0)
                        FROM outcome_events
                        WHERE workspace_id = %s
                          AND metric = 'spend'
                          AND observed_at >= %s AND observed_at < %s
                          AND asin = ANY(%s)
                        GROUP BY asin
                        """,
                        (workspace_id, start_iso, end_iso, list(asins_touched)),
                    )
                    for asin, spend in cur:
                        actual_by_key[f"asin:{asin}"] = float(spend or 0)
    except Exception as exc:
        logger.warning("variance: outcome read failed: %s", exc)

    # 3. Content-change markers: NIS decisions in this period on ASINs
    # that had spend.
    content_changes_by_asin: dict[str, dict[str, Any]] = {}
    try:
        if asins_touched:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT meta->>'asin' AS asin,
                               COUNT(*) AS n_decisions,
                               MAX(timestamp) AS last_change_at
                        FROM substrate_events
                        WHERE workspace_id = %s
                          AND event_kind = 'decision_event'
                          AND module = 'nis'
                          AND timestamp >= %s AND timestamp < %s
                          AND meta->>'asin' = ANY(%s)
                        GROUP BY 1
                        """,
                        (workspace_id, start_iso, end_iso, list(asins_touched)),
                    )
                    for asin, n, last_at in cur:
                        if not asin:
                            continue
                        content_changes_by_asin[asin] = {
                            "asin": asin,
                            "n_decisions": int(n),
                            "last_change_at": last_at.isoformat() if last_at else None,
                        }
    except Exception as exc:
        logger.warning("variance: content-change read failed: %s", exc)

    # 4. Build the unified scopes list. Include every key that appears
    # in either planned or actual.
    all_keys: set[str] = set(planned_by_key.keys()) | set(actual_by_key.keys())
    scopes: list[dict[str, Any]] = []
    for key in sorted(all_keys):
        st, sv = key.split(":", 1)
        planned_row = planned_by_key.get(key)
        planned = planned_row["planned"] if planned_row else None
        actual = actual_by_key.get(key, 0.0)
        delta = (actual - planned) if planned is not None else None
        pct = (actual / planned) if (planned and planned > 0) else None
        # Content-change list is only meaningful for asin-scoped rows.
        # For theme/overall rows we lift all touched ASINs as context.
        ccs: list[dict[str, Any]] = []
        if st == "asin":
            cc = content_changes_by_asin.get(sv)
            if cc:
                ccs = [cc]
        else:
            ccs = list(content_changes_by_asin.values())
        scopes.append({
            "scope_type": st,
            "scope_value": sv,
            "planned": planned,
            "actual": actual,
            "delta": delta,
            "pct_used": pct,
            "status": _bucket_status(planned, actual),
            "currency": (planned_row or {}).get("currency", "USD"),
            "notes": (planned_row or {}).get("notes"),
            "content_changes": ccs,
        })

    # Totals across all theme + overall rows. Prefer the overall row to
    # avoid double-counting; fall back to summing themes.
    overall_row = next((s for s in scopes if s["scope_type"] == "overall"), None)
    if overall_row:
        totals_planned = overall_row["planned"]
        totals_actual = overall_row["actual"]
    else:
        theme_rows = [s for s in scopes if s["scope_type"] == "theme"]
        tp = sum((s["planned"] or 0) for s in theme_rows) if theme_rows else None
        totals_planned = tp if tp is not None and tp > 0 else None
        totals_actual = sum(s["actual"] for s in theme_rows)
    totals_delta = (totals_actual - totals_planned) if totals_planned is not None else None
    totals_pct = (totals_actual / totals_planned) if (totals_planned and totals_planned > 0) else None

    return {
        "ok": True,
        "period": period,
        "period_start": start_iso,
        "period_end": end_iso,
        "scopes": scopes,
        "totals": {
            "planned": totals_planned,
            "actual": totals_actual,
            "delta": totals_delta,
            "pct_used": totals_pct,
            "status": _bucket_status(totals_planned, totals_actual or 0.0),
        },
        "content_changes_summary": {
            "n_asins_touched_by_spend": len(asins_touched),
            "n_asins_with_content_changes": len(content_changes_by_asin),
        },
    }


__all__ = [
    "set_budget", "delete_budget", "list_budgets",
    "variance_for_period",
]
