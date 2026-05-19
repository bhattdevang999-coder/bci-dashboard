"""Atlas substrate — brand_workspace registry.

One row per brand the operator audits. Drives the topbar workspace
switcher and the per-brand isolation of all other catalog substrate.

`brand_role` distinguishes:
  - operator_brand: Novelle. Atlas writes substrate; full module access.
  - audit_only:     Roxy/TLG diagnostic targets. Atlas can audit and
                    propose fixes; substrate writes still happen but the
                    UI treats it as read-mostly.
  - demo:           sandbox brands for screenshots/demos.

Contract:
    register_workspace(...)  -> bool
    get_workspace(...)       -> dict | None
    list_workspaces(...)     -> list[dict]
    touch_last_audit(...)    -> bool
    update_catalog_size(...) -> bool

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.brand_workspace")


VALID_ROLES = ("operator_brand", "audit_only", "demo")


def register_workspace(
    workspace_id: str,
    *,
    display_name: str,
    brand_role: str = "operator_brand",
    sales_period_start: Optional[date] = None,
    sales_period_end: Optional[date] = None,
    catalog_size_asins: Optional[int] = None,
    meta: Optional[dict[str, Any]] = None,
) -> bool:
    """Upsert a workspace row. Brand role validated."""
    if brand_role not in VALID_ROLES:
        logger.warning("register_workspace: invalid brand_role %s", brand_role)
        return False
    if not workspace_id or not display_name:
        return False
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO brand_workspace (
                        workspace_id, display_name, brand_role,
                        sales_period_start, sales_period_end,
                        catalog_size_asins, meta
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (workspace_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        brand_role = EXCLUDED.brand_role,
                        sales_period_start =
                            COALESCE(EXCLUDED.sales_period_start,
                                     brand_workspace.sales_period_start),
                        sales_period_end =
                            COALESCE(EXCLUDED.sales_period_end,
                                     brand_workspace.sales_period_end),
                        catalog_size_asins =
                            COALESCE(EXCLUDED.catalog_size_asins,
                                     brand_workspace.catalog_size_asins),
                        meta = brand_workspace.meta || EXCLUDED.meta
                    """,
                    (
                        workspace_id, display_name, brand_role,
                        sales_period_start, sales_period_end,
                        catalog_size_asins, json.dumps(meta or {}),
                    ),
                )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("register_workspace failed: %s", exc)
        return False


def get_workspace(workspace_id: str) -> Optional[dict[str, Any]]:
    """Fetch one workspace by id."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workspace_id, display_name, brand_role,
                           sales_period_start, sales_period_end,
                           catalog_size_asins, is_active, created_at,
                           last_audit_at, meta
                    FROM brand_workspace
                    WHERE workspace_id = %s
                    """,
                    (workspace_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return _row(r)
    except Exception as exc:
        logger.warning("get_workspace failed: %s", exc)
        return None


def list_workspaces(
    *,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """All workspaces, newest first."""
    pool = get_pool()
    if pool is None:
        return []
    where = []
    params: list[Any] = []
    if active_only:
        where.append("is_active = true")
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT workspace_id, display_name, brand_role,
               sales_period_start, sales_period_end,
               catalog_size_asins, is_active, created_at,
               last_audit_at, meta
        FROM brand_workspace
        {where_clause}
        ORDER BY brand_role = 'operator_brand' DESC, display_name
    """
    out: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append(_row(r))
    except Exception as exc:
        logger.warning("list_workspaces failed: %s", exc)
    return out


def touch_last_audit(workspace_id: str) -> bool:
    """Update last_audit_at to NOW(). Called by the audit engine."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE brand_workspace
                    SET last_audit_at = NOW()
                    WHERE workspace_id = %s
                    """,
                    (workspace_id,),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("touch_last_audit failed: %s", exc)
        return False


def update_catalog_size(
    workspace_id: str,
    catalog_size_asins: int,
    *,
    sales_period_start: Optional[date] = None,
    sales_period_end: Optional[date] = None,
) -> bool:
    """Update post-ingest stats."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE brand_workspace
                    SET catalog_size_asins = %s,
                        sales_period_start =
                            COALESCE(%s, sales_period_start),
                        sales_period_end =
                            COALESCE(%s, sales_period_end)
                    WHERE workspace_id = %s
                    """,
                    (catalog_size_asins, sales_period_start,
                     sales_period_end, workspace_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("update_catalog_size failed: %s", exc)
        return False


def _row(r: tuple) -> dict[str, Any]:
    return {
        "workspace_id": r[0],
        "display_name": r[1],
        "brand_role": r[2],
        "sales_period_start": r[3].isoformat() if r[3] else None,
        "sales_period_end": r[4].isoformat() if r[4] else None,
        "catalog_size_asins": r[5],
        "is_active": r[6],
        "created_at": r[7].isoformat() if r[7] else None,
        "last_audit_at": r[8].isoformat() if r[8] else None,
        "meta": r[9] or {},
    }


__all__ = [
    "register_workspace",
    "get_workspace",
    "list_workspaces",
    "touch_last_audit",
    "update_catalog_size",
    "VALID_ROLES",
]
