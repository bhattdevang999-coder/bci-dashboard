"""Atlas substrate — competitor_state.

Manual competitor observations. Operator types CRZ Yoga's current price,
review count, BSR, or notes-a-listing-change. Stored as substrate so
pricing_logic ceiling reads can pull the freshest observed value.

Contract:
    record_observation(...)         -> str | None
    list_observations(...)          -> list[dict]
    latest_value(...)               -> dict | None

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.competitor_state")


VALID_METRICS = ("price", "review_count", "bsr", "listing_changed")

VALID_SOURCES = (
    "operator_manual", "helium10", "keepa", "jungle_scout",
)


def record_observation(
    workspace_id: str,
    *,
    competitor_id: str,
    metric: str,
    value: Any,
    observed_by: str,
    source: str = "operator_manual",
    asin: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[str]:
    """Record a competitor observation. Returns observation_id."""
    if metric not in VALID_METRICS:
        logger.warning("record_observation: invalid metric %s", metric)
        return None
    if source not in VALID_SOURCES:
        logger.warning("record_observation: invalid source %s", source)
        return None
    pool = get_pool()
    if pool is None:
        return None
    oid = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO competitor_state (
                        observation_id, workspace_id, competitor_id,
                        metric, value, observed_by, source, asin, notes
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s::jsonb, %s, %s, %s, %s
                    )
                    """,
                    (
                        oid, workspace_id, competitor_id,
                        metric, json.dumps(value),
                        observed_by, source, asin, notes,
                    ),
                )
            conn.commit()
        return oid
    except Exception as exc:
        logger.warning("record_observation failed: %s", exc)
        return None


def list_observations(
    workspace_id: str,
    *,
    competitor_id: Optional[str] = None,
    metric: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List observations newest first."""
    pool = get_pool()
    if pool is None:
        return []
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if competitor_id:
        where.append("competitor_id = %s")
        params.append(competitor_id)
    if metric:
        where.append("metric = %s")
        params.append(metric)
    params.append(limit)
    sql = f"""
        SELECT observation_id, competitor_id, metric, value, observed_at,
               observed_by, source, asin, notes
        FROM competitor_state
        WHERE {' AND '.join(where)}
        ORDER BY observed_at DESC
        LIMIT %s
    """
    out = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append({
                        "observation_id": r[0],
                        "competitor_id": r[1],
                        "metric": r[2],
                        "value": r[3],
                        "observed_at":
                            r[4].isoformat() if r[4] else None,
                        "observed_by": r[5],
                        "source": r[6],
                        "asin": r[7],
                        "notes": r[8],
                    })
    except Exception as exc:
        logger.warning("list_observations failed: %s", exc)
    return out


def latest_value(
    workspace_id: str,
    competitor_id: str,
    metric: str,
) -> Optional[dict[str, Any]]:
    """Latest observation of (competitor_id, metric). None if absent."""
    rows = list_observations(
        workspace_id,
        competitor_id=competitor_id,
        metric=metric,
        limit=1,
    )
    return rows[0] if rows else None


__all__ = [
    "record_observation",
    "list_observations",
    "latest_value",
    "VALID_METRICS",
    "VALID_SOURCES",
]
