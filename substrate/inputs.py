"""Atlas inputs — file ingestion records.

Every file dropped through the Inputs tab gets one row in ingestion_records.
This is the audit trail that powers:

  - Inputs tab history table ("here's every file uploaded, by whom, when")
  - Staleness bar at the top of every report ("PPC: 6 days old")
  - Pre-change snapshot pipeline (knows what fresh data exists per ASIN)

This module is intentionally thin: its job is record-keeping, not
parsing. Each upload endpoint (catalog, sales, ppc_bulk, etc.) parses
the file in its own code path and then calls record_ingestion() to
persist the audit trail.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("atlas.substrate.inputs")


# Header signature → file_kind. Used by the auto-detection pass before
# the file is handed to the right parser. Free-form strings; new file
# types add a new entry here without schema changes.
FILE_KIND_SIGNATURES: dict[str, list[str]] = {
    "catalog": ["asin", "title", "brand", "main image"],
    "sales": ["asin", "sessions", "units"],
    "ppc_bulk": ["campaign id", "ad group id", "keyword text"],
    "search_term": ["customer search term", "match type", "impressions"],
    "ad_bulksheet": ["campaign", "ad group", "keyword", "match type"],
    "h10_cerebro": ["keyword phrase", "search volume", "organic rank"],
    "h10_keyword_tracker": ["keyword", "rank", "indexed"],
    "brand_analytics_terms": ["search term", "search frequency rank", "click share"],
    "returns": ["return date", "asin", "reason"],
    "reviews": ["asin", "rating", "review", "verified"],
}


def file_hash(content: bytes) -> str:
    """SHA-256 of file bytes. Used for dedup detection."""
    return hashlib.sha256(content).hexdigest()


def detect_file_kind(headers: list[str]) -> Optional[str]:
    """Auto-detect file_kind from header row by signature matching.

    Returns the first kind whose required signature columns all appear
    in the headers (case-insensitive). Returns None if no signature
    matches.
    """
    if not headers:
        return None
    norm = [h.strip().lower() for h in headers if h]
    best_kind: Optional[str] = None
    best_score = 0
    for kind, signature in FILE_KIND_SIGNATURES.items():
        hits = sum(1 for sig in signature if any(sig in h for h in norm))
        if hits == len(signature) and hits > best_score:
            best_score = hits
            best_kind = kind
    return best_kind


def record_ingestion(
    workspace_id: str,
    file_kind: str,
    file_name: Optional[str] = None,
    file_hash_value: Optional[str] = None,
    bytes_size: Optional[int] = None,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    rows_parsed: Optional[int] = None,
    rows_rejected: Optional[int] = None,
    asins_touched: Optional[int] = None,
    detected_fields: Optional[list[str]] = None,
    missing_fields: Optional[list[str]] = None,
    summary: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Persist one ingestion record. Returns ingestion_id (UUID string).

    Best-effort: returns None when DB is unavailable. Caller must not
    treat this as a write barrier.
    """
    from substrate.db import get_pool

    pool = get_pool()
    if pool is None:
        return None

    ingestion_id = str(uuid.uuid4())
    sql = """
        INSERT INTO ingestion_records (
            ingestion_id, workspace_id, uploaded_by, file_kind,
            file_name, file_hash, bytes,
            period_start, period_end,
            rows_parsed, rows_rejected, asins_touched,
            detected_fields, missing_fields, summary, meta
        ) VALUES (
            %(ingestion_id)s, %(workspace_id)s, %(uploaded_by)s, %(file_kind)s,
            %(file_name)s, %(file_hash)s, %(bytes)s,
            %(period_start)s, %(period_end)s,
            %(rows_parsed)s, %(rows_rejected)s, %(asins_touched)s,
            %(detected_fields)s::jsonb, %(missing_fields)s::jsonb, %(summary)s,
            %(meta)s::jsonb
        )
    """
    import json
    params = {
        "ingestion_id": ingestion_id,
        "workspace_id": workspace_id,
        "uploaded_by": uploaded_by,
        "file_kind": file_kind,
        "file_name": file_name,
        "file_hash": file_hash_value,
        "bytes": bytes_size,
        "period_start": period_start,
        "period_end": period_end,
        "rows_parsed": rows_parsed,
        "rows_rejected": rows_rejected,
        "asins_touched": asins_touched,
        "detected_fields": json.dumps(detected_fields or []),
        "missing_fields": json.dumps(missing_fields or []),
        "summary": summary,
        "meta": json.dumps(meta or {}, default=str),
    }
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
    except Exception as exc:
        logger.warning("ingestion_records insert failed: %s", exc)
        return None
    return ingestion_id


def list_ingestions(
    workspace_id: str,
    file_kind: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the most recent ingestion records for a workspace.

    Optional filter by file_kind. Newest first. Empty list when DB
    unavailable.
    """
    from substrate.db import get_pool

    pool = get_pool()
    if pool is None:
        return []
    sql = """
        SELECT ingestion_id, uploaded_at, uploaded_by, file_kind,
               file_name, bytes, period_start, period_end,
               rows_parsed, rows_rejected, asins_touched,
               detected_fields, missing_fields, summary
        FROM ingestion_records
        WHERE workspace_id = %s
    """
    params: list[Any] = [workspace_id]
    if file_kind:
        sql += " AND file_kind = %s"
        params.append(file_kind)
    sql += " ORDER BY uploaded_at DESC LIMIT %s"
    params.append(limit)

    out: list[dict[str, Any]] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            for row in cur:
                out.append({
                    "ingestion_id": str(row[0]),
                    "uploaded_at": row[1].isoformat() if row[1] else None,
                    "uploaded_by": row[2],
                    "file_kind": row[3],
                    "file_name": row[4],
                    "bytes": row[5],
                    "period_start": row[6].isoformat() if row[6] else None,
                    "period_end": row[7].isoformat() if row[7] else None,
                    "rows_parsed": row[8],
                    "rows_rejected": row[9],
                    "asins_touched": row[10],
                    "detected_fields": row[11] or [],
                    "missing_fields": row[12] or [],
                    "summary": row[13],
                })
    return out


def freshness_summary(workspace_id: str) -> dict[str, Any]:
    """Return per-file_kind freshness for the staleness bar.

    Shape:
        {
            "catalog":     { "uploaded_at": "...", "age_days": 2, "status": "fresh" },
            "sales":       { "uploaded_at": null,  "age_days": null, "status": "missing" },
            "ppc_bulk":    { "uploaded_at": "...", "age_days": 9, "status": "stale" },
            ...
        }

    Status thresholds:
        fresh  - <= 3 days old
        ok     - <= 7 days old
        stale  - <= 21 days old
        old    - >  21 days old
        missing - never uploaded
    """
    from substrate.db import get_pool

    summary: dict[str, Any] = {}
    pool = get_pool()
    if pool is None:
        return summary

    sql = """
        SELECT file_kind, MAX(uploaded_at)
        FROM ingestion_records
        WHERE workspace_id = %s
        GROUP BY file_kind
    """
    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (workspace_id,))
            for kind, ts in cur:
                if ts is None:
                    summary[kind] = {
                        "uploaded_at": None,
                        "age_days": None,
                        "status": "missing",
                    }
                    continue
                age_days = (now - ts).total_seconds() / 86400
                if age_days <= 3:
                    status = "fresh"
                elif age_days <= 7:
                    status = "ok"
                elif age_days <= 21:
                    status = "stale"
                else:
                    status = "old"
                summary[kind] = {
                    "uploaded_at": ts.isoformat(),
                    "age_days": round(age_days, 1),
                    "status": status,
                }
    return summary


__all__ = [
    "FILE_KIND_SIGNATURES",
    "file_hash",
    "detect_file_kind",
    "record_ingestion",
    "list_ingestions",
    "freshness_summary",
]
