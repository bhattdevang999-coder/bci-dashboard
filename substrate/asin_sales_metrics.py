"""Atlas substrate — asin_sales_metrics (Catalog Intel v0.2).

Per-ASIN sales metrics keyed by (workspace, asin, period_end). Uploads
accumulate (older periods stay) and dedupe on period_end (re-uploading
the same period replaces its numbers).

Contract:
    upsert_metrics_bulk(workspace_id, rows, *, snapshot_id) -> int
    get_metrics(workspace_id, asin) -> list[dict]  (all periods)
    latest_period(workspace_id) -> dict | None
    aggregate_by_asin(workspace_id) -> list[dict]  (rolled up across all periods)

Best-effort writes. Never raises.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.asin_sales_metrics")


def upsert_metrics_bulk(
    workspace_id: str,
    rows: list[dict],
    *,
    snapshot_id: Optional[str] = None,
) -> int:
    """Bulk upsert sales rows.

    Each row must have: asin, period_end (date or ISO string).
    Optional: period_start, sessions, units, revenue, cvr_pct.

    Returns count of rows written (attempted). Deduplicated on primary key.
    """
    pool = get_pool()
    if pool is None or not rows:
        return 0

    def _to_date(v):
        if v is None or v == "":
            return None
        if isinstance(v, date):
            return v
        if isinstance(v, datetime):
            return v.date()
        try:
            return datetime.fromisoformat(str(v)[:10]).date()
        except Exception:
            return None

    def _to_int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    def _to_num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    prepared = []
    for row in rows:
        asin = (row.get("asin") or "").strip()
        if not asin:
            continue
        period_end = _to_date(row.get("period_end"))
        if period_end is None:
            continue
        prepared.append((
            workspace_id, asin,
            _to_date(row.get("period_start")),
            period_end,
            _to_int(row.get("sessions")),
            _to_int(row.get("units")),
            _to_num(row.get("revenue")),
            row.get("cvr_pct") if row.get("cvr_pct") not in (None, "") else None,
            snapshot_id,
        ))

    if not prepared:
        return 0

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO asin_sales_metrics
                        (workspace_id, asin, period_start, period_end,
                         sessions, units, revenue, cvr_pct, snapshot_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (workspace_id, asin, period_end) DO UPDATE
                    SET sessions = EXCLUDED.sessions,
                        units = EXCLUDED.units,
                        revenue = EXCLUDED.revenue,
                        cvr_pct = EXCLUDED.cvr_pct,
                        period_start = COALESCE(EXCLUDED.period_start, asin_sales_metrics.period_start),
                        snapshot_id = EXCLUDED.snapshot_id,
                        inserted_at = NOW()
                    """,
                    prepared,
                )
            conn.commit()
        return len(prepared)
    except Exception as exc:
        logger.warning("upsert_metrics_bulk failed: %s", exc)
        return 0


def get_metrics(
    workspace_id: str,
    asin: str,
) -> list[dict]:
    """All time-series rows for one ASIN."""
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT period_start, period_end,
                           sessions, units, revenue, cvr_pct, snapshot_id
                    FROM asin_sales_metrics
                    WHERE workspace_id = %s AND asin = %s
                    ORDER BY period_end DESC
                    """,
                    (workspace_id, asin),
                )
                rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        logger.warning("get_metrics failed: %s", exc)
        return []


def latest_period(workspace_id: str) -> Optional[dict]:
    """Return the most recent (period_start, period_end) for a workspace."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MAX(period_end) AS pe, MIN(period_start) AS ps
                    FROM asin_sales_metrics
                    WHERE workspace_id = %s
                    """,
                    (workspace_id,),
                )
                r = cur.fetchone()
        if not r or r[0] is None:
            return None
        return {
            "period_end": r[0].isoformat() if isinstance(r[0], date) else r[0],
            "period_start": r[1].isoformat() if isinstance(r[1], date) else r[1],
        }
    except Exception as exc:
        logger.warning("latest_period failed: %s", exc)
        return None


def aggregate_by_asin(
    workspace_id: str,
    *,
    period_end: Optional[date] = None,
) -> list[dict]:
    """Roll up to one row per ASIN.

    If `period_end` is given, only that period is used. Otherwise sums
    across all periods on record (useful for a TTM-style single view).
    """
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if period_end is not None:
                    cur.execute(
                        """
                        SELECT asin,
                               COALESCE(sessions, 0),
                               COALESCE(units, 0),
                               COALESCE(revenue, 0),
                               COALESCE(cvr_pct, 0)
                        FROM asin_sales_metrics
                        WHERE workspace_id = %s AND period_end = %s
                        """,
                        (workspace_id, period_end),
                    )
                else:
                    cur.execute(
                        """
                        SELECT asin,
                               SUM(COALESCE(sessions, 0)),
                               SUM(COALESCE(units, 0)),
                               SUM(COALESCE(revenue, 0)),
                               AVG(COALESCE(cvr_pct, 0))
                        FROM asin_sales_metrics
                        WHERE workspace_id = %s
                        GROUP BY asin
                        """,
                        (workspace_id,),
                    )
                rows = cur.fetchall()
        return [{
            "asin": r[0],
            "sessions": int(r[1]) if r[1] is not None else 0,
            "units": int(r[2]) if r[2] is not None else 0,
            "revenue": float(r[3]) if r[3] is not None else 0.0,
            "cvr_pct": float(r[4]) if r[4] is not None else 0.0,
        } for r in rows]
    except Exception as exc:
        logger.warning("aggregate_by_asin failed: %s", exc)
        return []


def _row_to_dict(r: tuple) -> dict:
    return {
        "period_start": r[0].isoformat() if isinstance(r[0], date) else r[0],
        "period_end":   r[1].isoformat() if isinstance(r[1], date) else r[1],
        "sessions":     int(r[2]) if r[2] is not None else 0,
        "units":        int(r[3]) if r[3] is not None else 0,
        "revenue":      float(r[4]) if r[4] is not None else 0.0,
        "cvr_pct":      float(r[5]) if r[5] is not None else None,
        "snapshot_id":  str(r[6]) if r[6] is not None else None,
    }
