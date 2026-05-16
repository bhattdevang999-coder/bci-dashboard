"""Atlas operators \u2014 lightweight named accounts.

Real auth is deferred. For now, an operator is a (workspace_id, operator_id)
tuple plus a display name and role. The frontend identifies the current
operator via a session cookie; every decision_event the substrate logs
carries that operator_id so the agency's team is distinguishable from you.

This is NOT access control. The cookie can be set to anything; the API does
not authenticate the value. Use this only for attribution, not for security.

Phase 3 will replace the cookie with real auth (Render Auth, Clerk, or
similar). The substrate's operator_id column is forward-compatible \u2014
whatever auth ships will write the same column with a trusted identity.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("atlas.substrate.operators")

_VALID_ROLES = {"owner", "operator", "agency", "viewer"}
_OPERATOR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def slugify_operator_id(display_name: str) -> str:
    """Turn a display name into a stable operator_id slug.

    'Devang Bhatt' -> 'devang_bhatt'
    'Sarah (Agency)' -> 'sarah_agency'
    Falls back to 'operator' if input is empty or sluggable to nothing.
    """
    if not display_name:
        return "operator"
    s = display_name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s[:64] or "operator"


def upsert_operator(
    workspace_id: str,
    operator_id: str,
    display_name: str,
    role: str = "operator",
) -> dict:
    """Insert or update an operator. Returns the persisted row as a dict.

    Idempotent. If the operator already exists, only display_name/role
    are updated; created_at is preserved.

    No-op (returns a synthetic dict) when no Postgres pool is available
    \u2014 the operator stays \"virtual\" but decision_events can still
    reference the operator_id for JSONL attribution.
    """
    if role not in _VALID_ROLES:
        role = "operator"

    row = {
        "workspace_id": workspace_id,
        "operator_id": operator_id,
        "display_name": display_name,
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
    }

    from substrate.db import get_pool
    pool = get_pool()
    if pool is None:
        return row

    sql = """
        INSERT INTO operators (workspace_id, operator_id, display_name, role, last_seen)
        VALUES (%(workspace_id)s, %(operator_id)s, %(display_name)s, %(role)s, NOW())
        ON CONFLICT (workspace_id, operator_id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            role = EXCLUDED.role,
            last_seen = NOW(),
            active = TRUE
        RETURNING created_at, last_seen
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, row)
            res = cur.fetchone()
        conn.commit()
    if res:
        row["created_at"] = res[0].isoformat() if res[0] else row["created_at"]
        row["last_seen"] = res[1].isoformat() if res[1] else None
    return row


def list_operators(workspace_id: str, active_only: bool = True) -> list[dict]:
    """List operators in a workspace, sorted by last_seen DESC.

    Returns empty list when DB unavailable.
    """
    from substrate.db import get_pool
    pool = get_pool()
    if pool is None:
        return []
    sql = """
        SELECT operator_id, display_name, role, created_at, last_seen, active
        FROM operators
        WHERE workspace_id = %s
    """
    params: list = [workspace_id]
    if active_only:
        sql += " AND active = TRUE"
    sql += " ORDER BY last_seen DESC NULLS LAST, display_name ASC"
    out = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            for row in cur:
                out.append({
                    "operator_id": row[0],
                    "display_name": row[1],
                    "role": row[2],
                    "created_at": row[3].isoformat() if row[3] else None,
                    "last_seen": row[4].isoformat() if row[4] else None,
                    "active": row[5],
                })
    return out


def get_operator(workspace_id: str, operator_id: str) -> Optional[dict]:
    """Read a single operator. Returns None if not found or DB unavailable."""
    from substrate.db import get_pool
    pool = get_pool()
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT operator_id, display_name, role, created_at, last_seen, active
                FROM operators
                WHERE workspace_id = %s AND operator_id = %s
                """,
                (workspace_id, operator_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "operator_id": row[0],
        "display_name": row[1],
        "role": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
        "last_seen": row[4].isoformat() if row[4] else None,
        "active": row[5],
    }


def touch_operator(workspace_id: str, operator_id: str) -> None:
    """Update last_seen for an existing operator. Silent no-op if not found."""
    from substrate.db import get_pool
    pool = get_pool()
    if pool is None:
        return
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE operators
                SET last_seen = NOW(), active = TRUE
                WHERE workspace_id = %s AND operator_id = %s
                """,
                (workspace_id, operator_id),
            )
        conn.commit()


__all__ = [
    "slugify_operator_id",
    "upsert_operator",
    "list_operators",
    "get_operator",
    "touch_operator",
]
