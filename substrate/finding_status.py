"""Catalog Intel — finding status workflow.

Status is keyed on the STABLE identity (workspace_id, rule_name, asin)
so it survives snapshot re-runs. Findings are wiped and rewritten on
each run; status is not.

Statuses:
  open           default. finding is unaddressed
  acknowledged   client has seen the finding and confirms it
  in_progress    fix work is underway
  fixed          fix is applied. next snapshot should show it as resolved
                 (or as an 'improved' delta on the metric)
  wontfix        client has decided not to act. suppressed from the
                 default open-work view

Every transition writes to catalog_intel_status_history for audit trail.
"""
from __future__ import annotations
from typing import Optional
import logging

from substrate.db import get_pool

logger = logging.getLogger(__name__)


VALID_STATUSES = {"open", "acknowledged", "in_progress", "fixed", "wontfix"}


def _norm_asin(asin: Optional[str]) -> str:
    """Aggregate findings use '' as the key; normalize None to ''."""
    return asin or ""


def get_status(workspace_id: str, rule_name: str, asin: Optional[str] = None) -> Optional[dict]:
    """Return the current status row for a finding, or None if never set (implicit 'open')."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, note, updated_at, updated_by
                FROM catalog_intel_finding_status
                WHERE workspace_id = %s AND rule_name = %s AND asin = %s
                """,
                (workspace_id, rule_name, _norm_asin(asin)),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "status": row[0],
                "note": row[1],
                "updated_at": row[2].isoformat() if row[2] else None,
                "updated_by": row[3],
            }
    except Exception as exc:
        logger.warning("get_status failed: %s", exc)
        return None


def list_status(workspace_id: str) -> dict:
    """Return {(rule_name, asin): {status, note, ...}} for a workspace.

    Empty-string asin means aggregate finding. Included as key '' in the
    returned dict for consistency with the PK.
    """
    out: dict = {}
    pool = get_pool()
    if pool is None:
        return out
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT rule_name, asin, status, note, updated_at, updated_by
                FROM catalog_intel_finding_status
                WHERE workspace_id = %s
                """,
                (workspace_id,),
            )
            for rule, asin, status, note, ts, by in cur.fetchall():
                out[(rule, asin)] = {
                    "status": status,
                    "note": note,
                    "updated_at": ts.isoformat() if ts else None,
                    "updated_by": by,
                }
    except Exception as exc:
        logger.warning("list_status failed: %s", exc)
    return out


def set_status(workspace_id: str, rule_name: str, status: str, *,
               asin: Optional[str] = None, note: Optional[str] = None,
               updated_by: Optional[str] = None) -> dict:
    """Upsert status and append a history row. Returns the new state."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status} (must be one of {sorted(VALID_STATUSES)})")
    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "no db pool"}
    asin_norm = _norm_asin(asin)
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Fetch prior status (for history)
                cur.execute(
                    """
                    SELECT status FROM catalog_intel_finding_status
                    WHERE workspace_id = %s AND rule_name = %s AND asin = %s
                    """,
                    (workspace_id, rule_name, asin_norm),
                )
                row = cur.fetchone()
                old_status = row[0] if row else None
                # Upsert
                cur.execute(
                    """
                    INSERT INTO catalog_intel_finding_status
                        (workspace_id, rule_name, asin, status, note, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (workspace_id, rule_name, asin)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        note = EXCLUDED.note,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by
                    """,
                    (workspace_id, rule_name, asin_norm, status, note, updated_by),
                )
                # Log history (immutable)
                cur.execute(
                    """
                    INSERT INTO catalog_intel_status_history
                        (workspace_id, rule_name, asin, old_status, new_status, note, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (workspace_id, rule_name, asin_norm, old_status, status, note, updated_by),
                )
            conn.commit()
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "rule_name": rule_name,
            "asin": asin_norm or None,
            "old_status": old_status,
            "new_status": status,
        }
    except Exception as exc:
        logger.exception("set_status failed")
        return {"ok": False, "error": str(exc)[:200]}


def get_history(workspace_id: str, *, limit: int = 100,
                rule_name: Optional[str] = None,
                asin: Optional[str] = None) -> list:
    """Return audit history rows, newest first."""
    pool = get_pool()
    if pool is None:
        return []
    where = ["workspace_id = %s"]
    params: list = [workspace_id]
    if rule_name:
        where.append("rule_name = %s")
        params.append(rule_name)
    if asin is not None:
        where.append("asin = %s")
        params.append(_norm_asin(asin))
    sql = f"""
        SELECT history_id, rule_name, asin, old_status, new_status,
               note, updated_at, updated_by
        FROM catalog_intel_status_history
        WHERE {' AND '.join(where)}
        ORDER BY updated_at DESC
        LIMIT %s
    """
    params.append(int(limit))
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [{
                "history_id": r[0],
                "rule_name": r[1],
                "asin": r[2] or None,
                "old_status": r[3],
                "new_status": r[4],
                "note": r[5],
                "updated_at": r[6].isoformat() if r[6] else None,
                "updated_by": r[7],
            } for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("get_history failed: %s", exc)
        return []


def counts_by_status(workspace_id: str) -> dict:
    """Return {status: n} for the workspace \u2014 for UI totals."""
    out = {s: 0 for s in VALID_STATUSES}
    pool = get_pool()
    if pool is None:
        return out
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) FROM catalog_intel_finding_status
                WHERE workspace_id = %s
                GROUP BY status
                """,
                (workspace_id,),
            )
            for status, n in cur.fetchall():
                out[status] = int(n)
    except Exception as exc:
        logger.warning("counts_by_status failed: %s", exc)
    return out
