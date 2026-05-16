"""Atlas Confound View — honest before/after juxtaposition.

The principle: do not claim causation. Atlas does NOT know whether a
title change moved CVR. PPC has multi-day attribution tails. Content
changes confound each other. Seasonality dwarfs most effects.

What this module does:
  - Given a decision_event, return:
      * the captured pre_change_snapshot ('before')
      * the latest observed values per metric for the same ASIN ('after')
      * a list of other decision_events on the same ASIN between
        decision-time and now ('confounds')
      * staleness info (when each metric was last refreshed)
  - It does NOT compute lift, attribution, deltas-as-causal, or any
    statistical inference.

The operator looks at this side-by-side and decides what's real.
That's the contract. The UI must mirror this discipline — no
"+X% attribution" labels, no celebratory deltas.

Read-only. Never raises. Empty when data is missing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from substrate.db import get_pool

logger = logging.getLogger("atlas.substrate.confound")


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def confound_view_for_decision(
    workspace_id: str,
    event_id: str,
) -> dict[str, Any]:
    """Build the confound view for one decision_event.

    Always returns a dict with this shape (fields may be None/empty when
    data is unavailable):

      {
        ok: bool,
        event_id, asin, decision_at, field_name, module,
        before: { metrics: {...}, captured_at, data_quality } | None,
        after:  { metrics: {...}, latest_observed_at } | None,
        confounds: [
          { event_id, module, field_name, timestamp,
            operator_action, summary }
        ],
        days_since_decision: int | None,
        notes: list[str],     # honest caveats the UI should render
      }
    """
    out: dict[str, Any] = {
        "ok": False,
        "event_id": event_id,
        "asin": None,
        "decision_at": None,
        "field_name": None,
        "module": None,
        "before": None,
        "after": None,
        "confounds": [],
        "days_since_decision": None,
        "notes": [],
    }

    pool = get_pool()
    if pool is None:
        out["notes"].append("substrate unavailable")
        return out

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # 1. The anchor decision
                cur.execute(
                    """
                    SELECT timestamp, module, field_name,
                           pre_change_snapshot
                    FROM substrate_events
                    WHERE workspace_id = %s
                      AND event_id = %s
                      AND event_kind = 'decision_event'
                    """,
                    (workspace_id, event_id),
                )
                row = cur.fetchone()
                if row is None:
                    out["notes"].append("decision not found")
                    return out
                decision_at, module, field_name, snapshot = row
                out["decision_at"] = _iso(decision_at)
                out["module"] = module
                out["field_name"] = field_name

                if not isinstance(snapshot, dict):
                    snapshot = {}
                asin = snapshot.get("asin")
                out["asin"] = asin

                # 'Before' block built from the snapshot. We don't reach
                # back into outcome_events here — the snapshot is the
                # frozen truth at decision time and using it directly is
                # the whole point of having it.
                if snapshot and snapshot.get("metrics"):
                    out["before"] = {
                        "metrics": snapshot.get("metrics") or {},
                        "captured_at": snapshot.get("captured_at"),
                        "data_quality": snapshot.get("data_quality") or "empty",
                        "freshness": snapshot.get("freshness") or {},
                    }
                else:
                    out["notes"].append(
                        "No pre-change snapshot was captured for this decision. "
                        "Likely a Day-1 listing or a decision logged before the "
                        "ASIN attribution wiring shipped."
                    )

                # 2. 'After' block: latest observed value per metric for
                # the same ASIN. NULL ASIN means no comparison is possible.
                if asin:
                    cur.execute(
                        """
                        SELECT metric, value, observed_at
                        FROM outcome_events
                        WHERE workspace_id = %s
                          AND asin = %s
                          AND observed_at >= %s
                        ORDER BY observed_at DESC
                        LIMIT 500
                        """,
                        (workspace_id, asin, decision_at),
                    )
                    seen: set[str] = set()
                    latest_metrics: dict[str, dict[str, Any]] = {}
                    latest_overall = None
                    for metric, value, observed_at in cur:
                        if observed_at and (latest_overall is None
                                            or observed_at > latest_overall):
                            latest_overall = observed_at
                        if metric in seen:
                            continue
                        seen.add(metric)
                        latest_metrics[metric] = {
                            "value": float(value) if value is not None else None,
                            "observed_at": _iso(observed_at),
                        }
                    if latest_metrics:
                        out["after"] = {
                            "metrics": latest_metrics,
                            "latest_observed_at": _iso(latest_overall),
                        }
                    else:
                        out["notes"].append(
                            "No outcome data has been ingested for this ASIN "
                            "since the decision was made. Upload a fresh "
                            "business report or PPC bulk to populate 'after'."
                        )

                    # 3. Confounds: other decisions on the same ASIN between
                    # this decision and now. We compare by ASIN at the
                    # snapshot level (since not all events carry meta.asin).
                    cur.execute(
                        """
                        SELECT event_id, timestamp, module, field_name,
                               atlas_output, operator_action
                        FROM substrate_events
                        WHERE workspace_id = %s
                          AND event_kind IN ('decision_event', 'operator_response')
                          AND timestamp > %s
                          AND event_id <> %s
                          AND (
                            pre_change_snapshot->>'asin' = %s
                            OR meta->>'asin' = %s
                          )
                        ORDER BY timestamp ASC
                        LIMIT 50
                        """,
                        (workspace_id, decision_at, event_id, asin, asin),
                    )
                    for cev_id, cts, cmod, cfield, cout, caction in cur:
                        # Pull a 1-line summary for display. For decision
                        # events use field_name; for responses use the
                        # action verb. We deliberately keep this short —
                        # the UI shows a count, the drawer shows detail.
                        if caction:
                            summary = f"{caction} {cfield or ''}".strip()
                        else:
                            summary = cfield or "(unnamed)"
                        out["confounds"].append({
                            "event_id": str(cev_id),
                            "module": cmod,
                            "field_name": cfield,
                            "timestamp": _iso(cts),
                            "operator_action": caction,
                            "summary": summary,
                        })

    except Exception as exc:
        logger.warning("confound view query failed: %s", exc)
        out["notes"].append(f"query failed: {str(exc)[:120]}")
        return out

    # days_since_decision: pure clock-time, no inference
    if decision_at is not None:
        now = datetime.now(timezone.utc)
        if hasattr(decision_at, "tzinfo") and decision_at.tzinfo is not None:
            out["days_since_decision"] = max(0, (now - decision_at).days)

    # Universal caveats — the UI should always show at least one of these,
    # so operators don't read causation into juxtaposition.
    if out["before"] and out["after"]:
        n_conf = len(out["confounds"])
        if n_conf > 0:
            out["notes"].append(
                f"{n_conf} other decision{'s' if n_conf != 1 else ''} on this ASIN "
                f"in the same period — any of them could be moving these numbers."
            )
        else:
            out["notes"].append(
                "No other decisions logged on this ASIN in the period, but "
                "external factors (seasonality, competitor activity, Amazon "
                "algorithm changes) are not visible to Atlas and still apply."
            )

    out["ok"] = True
    return out


__all__ = ["confound_view_for_decision"]
