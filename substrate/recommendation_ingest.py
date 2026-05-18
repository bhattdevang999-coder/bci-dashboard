"""Atlas substrate — recommendation_ingest.

Implements RECOMMENDATION_INGEST.md. Every external recommendation
(agency PDF, vendor tool output, operator note, internal SOP) lands
here as substrate. Tokenized response link lets the original source
respond inside the system without a dashboard login.

Contract:
    create_recommendation(...)       -> str | None
    get_recommendation(...)          -> dict | None
    list_recommendations(...)        -> list[dict]
    update_parse(...)                -> bool   (parsed_fields, scope)
    set_status(...)                  -> bool
    generate_response_token(...)     -> str | None  (returns full URL)
    lookup_by_token(...)             -> dict | None
    mark_response_received(...)      -> bool
    consume_token(...)               -> bool

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.recommendation_ingest")


VALID_SOURCE_TIERS = (
    "top_agency", "mid_agency", "budget_agency",
    "vendor_tool", "operator", "internal_sop",
)

VALID_REC_TYPES = (
    "backend_fields", "keyword_list", "pricing_proposal",
    "listing_copy", "image_brief", "pricing_review",
    "compliance_check", "other",
)

VALID_STATUSES = (
    "pending_evaluation", "evaluated",
    "awaiting_response", "response_received",
    "resolved", "archived",
)

DEFAULT_TOKEN_TTL_DAYS = 7


def create_recommendation(
    workspace_id: str,
    *,
    source: str,
    source_tier: Optional[str] = None,
    source_contact: Optional[str] = None,
    raw_text: Optional[str] = None,
    raw_file_path: Optional[str] = None,
    raw_file_hash: Optional[str] = None,
    rec_type: Optional[str] = None,
    scope_asins: Optional[list[str]] = None,
    scope_confidence: Optional[float] = None,
    parsed_fields: Optional[dict[str, Any]] = None,
    ingested_by: str = "devang",
    meta: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Create a recommendation_ingest row. Returns rec_id."""
    if not source or not source.strip():
        return None
    if source_tier and source_tier not in VALID_SOURCE_TIERS:
        logger.warning(
            "create_recommendation: invalid source_tier %s", source_tier
        )
        source_tier = None
    if rec_type and rec_type not in VALID_REC_TYPES:
        logger.warning(
            "create_recommendation: invalid rec_type %s", rec_type
        )
        rec_type = "other"

    pool = get_pool()
    if pool is None:
        return None

    rec_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recommendation_ingest (
                        rec_id, workspace_id, source, source_tier,
                        source_contact, raw_text, raw_file_path,
                        raw_file_hash, rec_type, scope_asins,
                        scope_confidence, parsed_fields,
                        ingested_by, status, meta
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s::jsonb,
                        %s, 'pending_evaluation', %s::jsonb
                    )
                    """,
                    (
                        rec_id, workspace_id, source.strip(), source_tier,
                        source_contact, raw_text, raw_file_path,
                        raw_file_hash, rec_type, scope_asins or [],
                        scope_confidence,
                        json.dumps(parsed_fields) if parsed_fields else None,
                        ingested_by, json.dumps(meta or {}),
                    ),
                )
            conn.commit()
        return rec_id
    except Exception as exc:
        logger.warning("create_recommendation failed: %s", exc)
        return None


def get_recommendation(rec_id: str) -> Optional[dict[str, Any]]:
    """Fetch a recommendation_ingest row by id."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rec_id, workspace_id, source, source_tier,
                           source_contact, raw_text, raw_file_path,
                           raw_file_hash, rec_type, scope_asins,
                           scope_confidence, parsed_fields,
                           ingested_at, ingested_by, status,
                           response_token, response_token_url,
                           response_expires_at, response_received_at, meta
                    FROM recommendation_ingest
                    WHERE rec_id = %s
                    """,
                    (rec_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return _row_to_dict(r)
    except Exception as exc:
        logger.warning("get_recommendation failed: %s", exc)
        return None


def _row_to_dict(r: tuple) -> dict[str, Any]:
    return {
        "rec_id": r[0],
        "workspace_id": r[1],
        "source": r[2],
        "source_tier": r[3],
        "source_contact": r[4],
        "raw_text": r[5],
        "raw_file_path": r[6],
        "raw_file_hash": r[7],
        "rec_type": r[8],
        "scope_asins": r[9] or [],
        "scope_confidence":
            float(r[10]) if r[10] is not None else None,
        "parsed_fields": r[11] or {},
        "ingested_at": r[12].isoformat() if r[12] else None,
        "ingested_by": r[13],
        "status": r[14],
        "response_token": r[15],
        "response_token_url": r[16],
        "response_expires_at":
            r[17].isoformat() if r[17] else None,
        "response_received_at":
            r[18].isoformat() if r[18] else None,
        "meta": r[19] or {},
    }


def list_recommendations(
    workspace_id: str,
    *,
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recommendations newest first. Optional status / source filter."""
    pool = get_pool()
    if pool is None:
        return []
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if status:
        where.append("status = %s")
        params.append(status)
    if source:
        where.append("source = %s")
        params.append(source)
    params.append(limit)
    sql = f"""
        SELECT rec_id, workspace_id, source, source_tier,
               source_contact, raw_text, raw_file_path,
               raw_file_hash, rec_type, scope_asins,
               scope_confidence, parsed_fields,
               ingested_at, ingested_by, status,
               response_token, response_token_url,
               response_expires_at, response_received_at, meta
        FROM recommendation_ingest
        WHERE {' AND '.join(where)}
        ORDER BY ingested_at DESC
        LIMIT %s
    """
    out: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append(_row_to_dict(r))
    except Exception as exc:
        logger.warning("list_recommendations failed: %s", exc)
    return out


def update_parse(
    rec_id: str,
    *,
    parsed_fields: dict[str, Any],
    scope_asins: Optional[list[str]] = None,
    scope_confidence: Optional[float] = None,
    rec_type: Optional[str] = None,
) -> bool:
    """Update parser output (parsed_fields, scope inference).

    Idempotent — overwrites prior parse results.
    """
    pool = get_pool()
    if pool is None:
        return False
    if rec_type and rec_type not in VALID_REC_TYPES:
        rec_type = None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recommendation_ingest
                    SET parsed_fields = %s::jsonb,
                        scope_asins = COALESCE(%s, scope_asins),
                        scope_confidence =
                            COALESCE(%s, scope_confidence),
                        rec_type = COALESCE(%s, rec_type)
                    WHERE rec_id = %s
                    """,
                    (
                        json.dumps(parsed_fields), scope_asins,
                        scope_confidence, rec_type, rec_id,
                    ),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("update_parse failed: %s", exc)
        return False


def set_status(rec_id: str, status: str) -> bool:
    """Move a recommendation to a new status. Validates against VALID_STATUSES."""
    if status not in VALID_STATUSES:
        logger.warning("set_status: invalid status %s", status)
        return False
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recommendation_ingest
                    SET status = %s
                    WHERE rec_id = %s
                    """,
                    (status, rec_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("set_status failed: %s", exc)
        return False


def generate_response_token(
    rec_id: str,
    *,
    base_url: str,
    ttl_days: int = DEFAULT_TOKEN_TTL_DAYS,
) -> Optional[dict[str, Any]]:
    """Create (or regenerate) a tokenized response link.

    Returns {token, url, expires_at} on success. Status moves to
    'awaiting_response'. Each regeneration is a new token; old token
    invalidates because UNIQUE constraint replaces it.
    """
    pool = get_pool()
    if pool is None:
        return None
    token = secrets.token_urlsafe(24)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days)
    )
    # base_url example: "https://tlg-amazon-intelligence-dashboard.onrender.com"
    url = f"{base_url.rstrip('/')}/respond/{rec_id}?token={token}"
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recommendation_ingest
                    SET response_token = %s,
                        response_token_url = %s,
                        response_expires_at = %s,
                        response_received_at = NULL,
                        status = 'awaiting_response',
                        meta = COALESCE(meta, '{}'::jsonb)
                               || jsonb_build_object(
                                    'token_history',
                                    COALESCE(meta->'token_history', '[]'::jsonb)
                                    || jsonb_build_array(
                                         jsonb_build_object(
                                           'generated_at', to_jsonb(NOW()),
                                           'expires_at', to_jsonb(%s::timestamptz)
                                         )
                                       )
                                  )
                    WHERE rec_id = %s
                    """,
                    (token, url, expires_at, expires_at, rec_id),
                )
                affected = cur.rowcount
            conn.commit()
            if affected == 0:
                return None
        return {
            "token": token,
            "url": url,
            "expires_at": expires_at.isoformat(),
        }
    except Exception as exc:
        logger.warning("generate_response_token failed: %s", exc)
        return None


def lookup_by_token(
    rec_id: str, token: str
) -> Optional[dict[str, Any]]:
    """Resolve a (rec_id, token) pair for public-facing GET /respond/<rec_id>.

    Validates token matches, expires_at is in the future, and status is
    one of (awaiting_response, response_received) — allows the agency
    to revisit their submission until token consumes.

    Returns the row or None.
    """
    if not token or not rec_id:
        return None
    row = get_recommendation(rec_id)
    if not row:
        return None
    if row.get("response_token") != token:
        return None
    expires_raw = row.get("response_expires_at")
    if expires_raw:
        try:
            expires = datetime.fromisoformat(
                expires_raw.replace("Z", "+00:00")
            )
            if datetime.now(timezone.utc) > expires:
                return None
        except ValueError:
            pass
    if row.get("status") not in (
        "awaiting_response", "response_received",
    ):
        return None
    return row


def mark_response_received(rec_id: str) -> bool:
    """Flip status to response_received and stamp the timestamp."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recommendation_ingest
                    SET status = 'response_received',
                        response_received_at = NOW()
                    WHERE rec_id = %s
                      AND status IN ('awaiting_response',
                                     'response_received')
                    """,
                    (rec_id,),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("mark_response_received failed: %s", exc)
        return False


def consume_token(rec_id: str) -> bool:
    """Invalidate the token (final-submit case). Token cleared; URL kept
    for audit. Status stays as response_received."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recommendation_ingest
                    SET response_token = NULL
                    WHERE rec_id = %s
                    """,
                    (rec_id,),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("consume_token failed: %s", exc)
        return False


__all__ = [
    "create_recommendation",
    "get_recommendation",
    "list_recommendations",
    "update_parse",
    "set_status",
    "generate_response_token",
    "lookup_by_token",
    "mark_response_received",
    "consume_token",
    "VALID_SOURCE_TIERS",
    "VALID_REC_TYPES",
    "VALID_STATUSES",
    "DEFAULT_TOKEN_TTL_DAYS",
]
