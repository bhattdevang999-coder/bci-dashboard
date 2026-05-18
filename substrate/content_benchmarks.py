"""Atlas substrate — content_benchmarks.

Implements CONTENT_BENCHMARKS.md. Operator approves a piece of generated
content along with its full citation chain → it becomes a benchmark.
Future generations on similar ASINs seed from the benchmark.

Contract:
    lock_benchmark(...)              -> str | None
    get_benchmark(...)               -> dict | None
    list_benchmarks(...)             -> list[dict]
    list_applicable(...)             -> list[dict]   (scope-priority)
    bump_usage(benchmark_id)         -> bool
    flag_for_review(...)             -> int          (count affected)
    flag_by_unknown(unknown_id, ...) -> list[str]    (affected ids)
    supersede(...)                   -> bool
    archive(...)                     -> bool
    reactivate(...)                  -> bool

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.content_benchmarks")


VALID_SCOPES = (
    "global", "family", "asin", "family_decision_class",
)

VALID_BENCHMARK_TYPES = (
    "title", "bullets", "description", "a_plus",
    "image_brief", "backend_fields", "launch_brief",
)

VALID_STATUSES = (
    "active", "review_recommended", "superseded", "archived",
)

# Per CONTENT_BENCHMARKS.md §"Over-benchmark" failure mode.
# Cap active style/preference-like benchmarks per (scope, type) at 3
# unless the operator explicitly archives older ones.
DEFAULT_PER_SCOPE_CAP = 3


def lock_benchmark(
    workspace_id: str,
    *,
    scope: str,
    scope_ref: Optional[str],
    benchmark_type: str,
    approved_value: Any,
    source_event_id: str,
    approved_by: str,
    citations: Optional[list[dict[str, Any]]] = None,
    resolved_inputs: Optional[dict[str, Any]] = None,
    open_unknowns_at_approval: Optional[list[str]] = None,
    enforce_cap: bool = True,
    meta: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Lock a new benchmark. Returns benchmark_id on success.

    Validates scope + benchmark_type. Optional `enforce_cap` blocks
    creation when the (scope, scope_ref, benchmark_type) tuple already
    has DEFAULT_PER_SCOPE_CAP active rows — operator must archive one
    before adding a new one.

    `source_event_id` ties this back to the original NIS / pricing /
    listing-copy decision so the benchmark can be audited.
    """
    if scope not in VALID_SCOPES:
        logger.warning("lock_benchmark: invalid scope %s", scope)
        return None
    if benchmark_type not in VALID_BENCHMARK_TYPES:
        logger.warning(
            "lock_benchmark: invalid benchmark_type %s", benchmark_type
        )
        return None
    if not source_event_id:
        logger.warning("lock_benchmark: source_event_id required")
        return None

    pool = get_pool()
    if pool is None:
        return None

    if enforce_cap:
        active = list_benchmarks(
            workspace_id,
            scope=scope, scope_ref=scope_ref,
            benchmark_type=benchmark_type,
            status="active",
        )
        if len(active) >= DEFAULT_PER_SCOPE_CAP:
            logger.warning(
                "lock_benchmark: cap reached for (%s, %s, %s); "
                "archive one before adding more",
                scope, scope_ref, benchmark_type,
            )
            return None

    bid = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO content_benchmarks (
                        benchmark_id, workspace_id,
                        scope, scope_ref,
                        benchmark_type, approved_value,
                        resolved_inputs, source_event_id,
                        citations, open_unknowns_at_approval,
                        status, approved_by, meta
                    ) VALUES (
                        %s, %s,
                        %s, %s,
                        %s, %s::jsonb,
                        %s::jsonb, %s,
                        %s::jsonb, %s,
                        'active', %s, %s::jsonb
                    )
                    """,
                    (
                        bid, workspace_id,
                        scope, scope_ref,
                        benchmark_type, json.dumps(approved_value),
                        json.dumps(resolved_inputs or {}),
                        source_event_id,
                        json.dumps(citations or []),
                        open_unknowns_at_approval or [],
                        approved_by, json.dumps(meta or {}),
                    ),
                )
            conn.commit()
        return bid
    except Exception as exc:
        logger.warning("lock_benchmark failed: %s", exc)
        return None


def get_benchmark(benchmark_id: str) -> Optional[dict[str, Any]]:
    """Fetch one benchmark by id."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT benchmark_id, workspace_id, scope, scope_ref,
                           benchmark_type, approved_value, resolved_inputs,
                           source_event_id, citations,
                           open_unknowns_at_approval, status,
                           review_reason, superseded_by,
                           approved_at, approved_by,
                           last_used_at, used_count, meta
                    FROM content_benchmarks
                    WHERE benchmark_id = %s
                    """,
                    (benchmark_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return _row_to_dict(r)
    except Exception as exc:
        logger.warning("get_benchmark failed: %s", exc)
        return None


def _row_to_dict(r: tuple) -> dict[str, Any]:
    return {
        "benchmark_id": r[0],
        "workspace_id": r[1],
        "scope": r[2],
        "scope_ref": r[3],
        "benchmark_type": r[4],
        "approved_value": r[5],
        "resolved_inputs": r[6] or {},
        "source_event_id": r[7],
        "citations": r[8] or [],
        "open_unknowns_at_approval": r[9] or [],
        "status": r[10],
        "review_reason": r[11],
        "superseded_by": r[12],
        "approved_at": r[13].isoformat() if r[13] else None,
        "approved_by": r[14],
        "last_used_at": r[15].isoformat() if r[15] else None,
        "used_count": r[16] or 0,
        "meta": r[17] or {},
    }


def list_benchmarks(
    workspace_id: str,
    *,
    scope: Optional[str] = None,
    scope_ref: Optional[str] = None,
    benchmark_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List benchmarks newest first with optional filters."""
    pool = get_pool()
    if pool is None:
        return []
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if scope:
        where.append("scope = %s")
        params.append(scope)
    if scope_ref is not None:
        where.append("scope_ref = %s")
        params.append(scope_ref)
    if benchmark_type:
        where.append("benchmark_type = %s")
        params.append(benchmark_type)
    if status:
        where.append("status = %s")
        params.append(status)
    params.append(limit)
    sql = f"""
        SELECT benchmark_id, workspace_id, scope, scope_ref,
               benchmark_type, approved_value, resolved_inputs,
               source_event_id, citations,
               open_unknowns_at_approval, status,
               review_reason, superseded_by,
               approved_at, approved_by,
               last_used_at, used_count, meta
        FROM content_benchmarks
        WHERE {' AND '.join(where)}
        ORDER BY approved_at DESC
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
        logger.warning("list_benchmarks failed: %s", exc)
    return out


def list_applicable(
    workspace_id: str,
    *,
    benchmark_type: str,
    asin: Optional[str] = None,
    family: Optional[str] = None,
    decision_class: Optional[str] = None,
    include_review_recommended: bool = False,
) -> list[dict[str, Any]]:
    """Benchmarks that could seed a generation for this ASIN/family.

    Returns rows sorted by specificity DESC (asin > family > global,
    with family_decision_class slotted between family and asin since
    it's more specific than family-only). At each level, newest first.

    By default, returns only 'active' benchmarks. Setting
    include_review_recommended=True also returns benchmarks waiting
    for operator review — useful for showing "this benchmark needs
    review before reuse" warnings.
    """
    pool = get_pool()
    if pool is None:
        return []

    fdc_ref = (
        f"{family}|{decision_class}"
        if family and decision_class else None
    )

    statuses = ["active"]
    if include_review_recommended:
        statuses.append("review_recommended")

    or_parts = ["scope = 'global'"]
    params: list[Any] = [workspace_id, list(statuses), benchmark_type]
    if asin:
        or_parts.append("(scope = 'asin' AND scope_ref = %s)")
        params.append(asin)
    if family:
        or_parts.append("(scope = 'family' AND scope_ref = %s)")
        params.append(family)
    if fdc_ref:
        or_parts.append(
            "(scope = 'family_decision_class' AND scope_ref = %s)"
        )
        params.append(fdc_ref)

    sql = f"""
        SELECT benchmark_id, workspace_id, scope, scope_ref,
               benchmark_type, approved_value, resolved_inputs,
               source_event_id, citations,
               open_unknowns_at_approval, status,
               review_reason, superseded_by,
               approved_at, approved_by,
               last_used_at, used_count, meta
        FROM content_benchmarks
        WHERE workspace_id = %s
          AND status = ANY(%s)
          AND benchmark_type = %s
          AND ({' OR '.join(or_parts)})
        ORDER BY approved_at DESC
    """

    specificity = {
        "asin": 3,
        "family_decision_class": 2,
        "family": 1,
        "global": 0,
    }

    rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    rows.append(_row_to_dict(r))
    except Exception as exc:
        logger.warning("list_applicable failed: %s", exc)
        return []

    rows.sort(
        key=lambda b: (
            -specificity.get(b["scope"], 0),
            -(b.get("used_count") or 0),
            b["approved_at"] or "",
        ),
    )
    return rows


def bump_usage(benchmark_id: str) -> bool:
    """Increment used_count + stamp last_used_at. Called when a
    generation seeds from this benchmark."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE content_benchmarks
                    SET used_count = COALESCE(used_count, 0) + 1,
                        last_used_at = NOW()
                    WHERE benchmark_id = %s
                    """,
                    (benchmark_id,),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("bump_usage failed: %s", exc)
        return False


def supersede(
    old_benchmark_id: str,
    new_benchmark_id: str,
    superseded_by_operator: str,
) -> bool:
    """Mark old benchmark superseded by new one. Both must exist."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE content_benchmarks
                    SET status = 'superseded',
                        superseded_by = %s,
                        meta = COALESCE(meta, '{}'::jsonb)
                               || jsonb_build_object(
                                    'superseded_by_operator', %s::text,
                                    'superseded_at', to_jsonb(NOW())
                                  )
                    WHERE benchmark_id = %s
                      AND status IN ('active', 'review_recommended')
                    """,
                    (
                        new_benchmark_id, superseded_by_operator,
                        old_benchmark_id,
                    ),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("supersede failed: %s", exc)
        return False


def archive(benchmark_id: str, archived_by: str) -> bool:
    """Archive a benchmark. Won't apply to future generations."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE content_benchmarks
                    SET status = 'archived',
                        meta = COALESCE(meta, '{}'::jsonb)
                               || jsonb_build_object(
                                    'archived_by', %s::text,
                                    'archived_at', to_jsonb(NOW())
                                  )
                    WHERE benchmark_id = %s
                      AND status IN ('active', 'review_recommended')
                    """,
                    (archived_by, benchmark_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("archive failed: %s", exc)
        return False


def reactivate(benchmark_id: str, reactivated_by: str) -> bool:
    """Move a review_recommended benchmark back to active.

    Use when the operator reviews a flagged benchmark and decides the
    current value still applies (no regeneration needed). Clears
    review_reason. Archived/superseded benchmarks cannot be reactivated.
    """
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE content_benchmarks
                    SET status = 'active',
                        review_reason = NULL,
                        meta = COALESCE(meta, '{}'::jsonb)
                               || jsonb_build_object(
                                    'reaffirmed_by', %s::text,
                                    'reaffirmed_at', to_jsonb(NOW())
                                  )
                    WHERE benchmark_id = %s
                      AND status = 'review_recommended'
                    """,
                    (reactivated_by, benchmark_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("reactivate failed: %s", exc)
        return False


def flag_for_review(
    benchmark_id: str, review_reason: str,
) -> bool:
    """Move a benchmark to review_recommended with a reason."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE content_benchmarks
                    SET status = 'review_recommended',
                        review_reason = %s
                    WHERE benchmark_id = %s
                      AND status = 'active'
                    """,
                    (review_reason, benchmark_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("flag_for_review failed: %s", exc)
        return False


def flag_by_unknown(
    workspace_id: str,
    unknown_id: str,
    *,
    review_reason: Optional[str] = None,
) -> list[str]:
    """Find every active benchmark that had this unknown_id open at
    approval time and flag them for review. Returns the affected
    benchmark_ids.

    Called from substrate/unknowns.py:resolve_unknown so closing an
    unknown automatically surfaces benchmarks that may want a fresh
    look. Idempotent — already-flagged benchmarks are not re-flagged
    or duplicated, but their review_reason can be augmented.
    """
    pool = get_pool()
    if pool is None:
        return []
    reason = review_reason or f"unknown #{unknown_id} resolved"
    affected: list[str] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE content_benchmarks
                    SET status = 'review_recommended',
                        review_reason =
                            COALESCE(review_reason || ' | ', '') || %s
                    WHERE workspace_id = %s
                      AND status = 'active'
                      AND %s = ANY(open_unknowns_at_approval)
                    RETURNING benchmark_id
                    """,
                    (reason, workspace_id, unknown_id),
                )
                for row in cur.fetchall():
                    affected.append(row[0])
            conn.commit()
    except Exception as exc:
        logger.warning("flag_by_unknown failed: %s", exc)
    return affected


__all__ = [
    "lock_benchmark",
    "get_benchmark",
    "list_benchmarks",
    "list_applicable",
    "bump_usage",
    "supersede",
    "archive",
    "reactivate",
    "flag_for_review",
    "flag_by_unknown",
    "VALID_SCOPES",
    "VALID_BENCHMARK_TYPES",
    "VALID_STATUSES",
    "DEFAULT_PER_SCOPE_CAP",
]
