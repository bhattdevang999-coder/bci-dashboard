"""Atlas substrate — catalog_snapshots (Catalog Intel v0.2).

One row per uploaded catalog file. Immutable. Snapshots are what makes
trend analysis possible later — every upload is preserved with its raw
source file so we can re-run against old data if the parser evolves.

Contract:
    create_snapshot(...)   -> snapshot_id | None
    get_snapshot(...)      -> dict | None
    list_snapshots(...)    -> list[dict]
    update_row_counts(...) -> bool

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.catalog_snapshots")


def create_snapshot(
    workspace_id: str,
    *,
    uploaded_by: str = "devang",
    file_name: Optional[str] = None,
    file_path: Optional[str] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    notes: Optional[str] = None,
) -> Optional[str]:
    """Create a new snapshot row. Returns the snapshot_id (UUID string) or None."""
    pool = get_pool()
    if pool is None:
        logger.warning("create_snapshot: no db pool")
        return None
    snapshot_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO catalog_snapshots
                        (snapshot_id, workspace_id, uploaded_by,
                         file_name, file_path,
                         period_start, period_end, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (snapshot_id, workspace_id, uploaded_by,
                     file_name, file_path,
                     period_start, period_end, notes),
                )
            conn.commit()
        return snapshot_id
    except Exception as exc:
        logger.warning("create_snapshot failed: %s", exc)
        return None


def update_row_counts(
    snapshot_id: str,
    *,
    row_count_catalog: Optional[int] = None,
    row_count_sales: Optional[int] = None,
    parse_warnings: Optional[list] = None,
) -> bool:
    """Patch row-count fields on an existing snapshot."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                sets, args = [], []
                if row_count_catalog is not None:
                    sets.append("row_count_catalog = %s"); args.append(row_count_catalog)
                if row_count_sales is not None:
                    sets.append("row_count_sales = %s");   args.append(row_count_sales)
                if parse_warnings is not None:
                    sets.append("parse_warnings = %s::jsonb")
                    args.append(json.dumps(parse_warnings))
                if not sets:
                    return True
                args.append(snapshot_id)
                cur.execute(
                    f"UPDATE catalog_snapshots SET {', '.join(sets)} "
                    f"WHERE snapshot_id = %s",
                    tuple(args),
                )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("update_row_counts failed: %s", exc)
        return False


def get_snapshot(snapshot_id: str) -> Optional[dict]:
    """Fetch one snapshot by id."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT snapshot_id, workspace_id, uploaded_at, uploaded_by,
                           file_name, file_path,
                           row_count_catalog, row_count_sales,
                           period_start, period_end, parse_warnings, notes
                    FROM catalog_snapshots
                    WHERE snapshot_id = %s
                    """,
                    (snapshot_id,),
                )
                r = cur.fetchone()
        if not r:
            return None
        return _row_to_dict(r)
    except Exception as exc:
        logger.warning("get_snapshot failed: %s", exc)
        return None


def list_snapshots(
    workspace_id: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return snapshots for a workspace, newest first."""
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT snapshot_id, workspace_id, uploaded_at, uploaded_by,
                           file_name, file_path,
                           row_count_catalog, row_count_sales,
                           period_start, period_end, parse_warnings, notes
                    FROM catalog_snapshots
                    WHERE workspace_id = %s
                    ORDER BY uploaded_at DESC
                    LIMIT %s
                    """,
                    (workspace_id, int(limit)),
                )
                rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        logger.warning("list_snapshots failed: %s", exc)
        return []


def _row_to_dict(r: tuple) -> dict:
    return {
        "snapshot_id":       str(r[0]),
        "workspace_id":      r[1],
        "uploaded_at":       r[2].isoformat() if isinstance(r[2], datetime) else r[2],
        "uploaded_by":       r[3],
        "file_name":         r[4],
        "file_path":         r[5],
        "row_count_catalog": r[6],
        "row_count_sales":   r[7],
        "period_start":      r[8].isoformat() if isinstance(r[8], date) else r[8],
        "period_end":        r[9].isoformat() if isinstance(r[9], date) else r[9],
        "parse_warnings":    r[10] or [],
        "notes":             r[11],
    }
