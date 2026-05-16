"""Atlas pre-change snapshots.

When a decision is logged that targets an ASIN, this module assembles a
'before' snapshot of the ASIN's current state. The snapshot lives on the
decision_event row and is the anchor for outcome attribution later.

What goes in a snapshot (when available):
  - Rolling 14-day session count / units / revenue / CVR / CTR
  - Current catalog health score (from the most recent catalog ingestion)
  - Current ACOS / ad spend
  - Current organic rank for top keywords
  - Days of inventory cover

What doesn't go in:
  - Anything we don't have data for yet. The substrate's job is to
    capture what's known, not to fabricate. Empty snapshots are honest
    snapshots.

Snapshots are computed at decision time from the most recent ingestion
records and outcome_events rows. If the catalog hasn't been uploaded in
30 days, the catalog component is stale; we record that staleness so
closed-loop can discount the comparison later.

Pure functions: no side effects, no writes, no LLM calls. If the data
isn't in Postgres, the snapshot is empty.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("atlas.substrate.snapshot")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_snapshot_for_asin(workspace_id: str, asin: str) -> dict[str, Any]:
    """Build the pre-change snapshot for one ASIN.

    Returns a dict with three top-level keys:
        freshness   - when each ingestion stream was last refreshed
        metrics     - latest observed value per metric for this ASIN
        data_quality - empty | partial | full

    Never raises. Always returns a dict (possibly empty).
    """
    snapshot: dict[str, Any] = {
        "asin": asin,
        "captured_at": _now_iso(),
        "freshness": {},
        "metrics": {},
        "data_quality": "empty",
    }

    try:
        from substrate.db import get_pool
    except Exception:
        return snapshot

    pool = get_pool()
    if pool is None:
        return snapshot

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Freshness per ingestion stream
                cur.execute(
                    """
                    SELECT file_kind, MAX(uploaded_at)
                    FROM ingestion_records
                    WHERE workspace_id = %s
                      AND file_kind IN ('catalog', 'sales', 'ppc_bulk', 'search_term')
                    GROUP BY file_kind
                    """,
                    (workspace_id,),
                )
                for kind, ts in cur:
                    snapshot["freshness"][f"{kind}_uploaded_at"] = (
                        ts.isoformat() if ts else None
                    )

                # Latest outcome value per metric for this ASIN
                cur.execute(
                    """
                    SELECT metric, value, observed_at
                    FROM outcome_events
                    WHERE workspace_id = %s AND asin = %s
                    ORDER BY observed_at DESC
                    LIMIT 200
                    """,
                    (workspace_id, asin),
                )
                seen: set[str] = set()
                for metric, value, observed_at in cur:
                    if metric in seen:
                        continue
                    seen.add(metric)
                    snapshot["metrics"][metric] = {
                        "value": float(value) if value is not None else None,
                        "observed_at": observed_at.isoformat() if observed_at else None,
                    }
    except Exception as exc:
        logger.warning("snapshot build query failed: %s", exc)
        return snapshot

    # Quality bucket
    fresh_count = sum(1 for v in snapshot["freshness"].values() if v)
    metric_count = len(snapshot["metrics"])
    if metric_count >= 3 and fresh_count >= 2:
        snapshot["data_quality"] = "full"
    elif metric_count > 0 or fresh_count > 0:
        snapshot["data_quality"] = "partial"
    else:
        snapshot["data_quality"] = "empty"

    return snapshot


__all__ = ["build_snapshot_for_asin"]
