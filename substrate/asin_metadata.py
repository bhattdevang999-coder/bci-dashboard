"""Atlas substrate — asin_metadata (ground-truth ASIN fields).

Implements ASIN_METADATA.md. Per-ASIN physical and Amazon-backend facts,
with parent-child inheritance resolved at read time.

Contract:
    set_asin_metadata(...)        -> bool
    get_asin_metadata(...)        -> dict | None    (no inheritance)
    read_asin_metadata(...)       -> dict | None    (with parent inheritance)
    list_family_asins(...)        -> list[dict]
    confirm_field(...)            -> bool
    record_field_source(...)      -> bool

Best-effort writes. Never raises.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.asin_metadata")


# Variation-axis fields are NOT inherited from parent (child overrides only).
# Pocket fields are family-defining for Velune; not in this list because
# the family itself differs, so a child inheriting from parent gets the
# parent's pocket truth correctly.
VARIATION_AXIS_FIELDS = (
    "color_name",
    "color_map",
    "size",
    "bottoms_size_to_range",
    "pattern",
)


def set_asin_metadata(
    workspace_id: str,
    asin: str,
    *,
    parent_asin: Optional[str] = None,
    variation_family: Optional[str] = None,
    variation_axes: Optional[dict[str, Any]] = None,
    ground_truth_fields: Optional[dict[str, Any]] = None,
    field_sources: Optional[dict[str, Any]] = None,
    set_by: str,
    bump_revision: bool = True,
) -> bool:
    """Upsert one asin_metadata row. Returns True on success."""
    pool = get_pool()
    if pool is None:
        return False
    gtf = ground_truth_fields or {}
    fs = field_sources or {}
    va = variation_axes or {}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO asin_metadata (
                        workspace_id, asin, parent_asin, variation_family,
                        variation_axes, ground_truth_fields, field_sources,
                        revision, set_at, set_by
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb,
                        1, NOW(), %s
                    )
                    ON CONFLICT (workspace_id, asin) DO UPDATE SET
                        parent_asin = EXCLUDED.parent_asin,
                        variation_family = EXCLUDED.variation_family,
                        variation_axes = EXCLUDED.variation_axes,
                        ground_truth_fields = EXCLUDED.ground_truth_fields,
                        field_sources = EXCLUDED.field_sources,
                        revision = CASE WHEN %s
                                        THEN asin_metadata.revision + 1
                                        ELSE asin_metadata.revision END,
                        set_at = NOW(),
                        set_by = EXCLUDED.set_by
                    """,
                    (
                        workspace_id, asin, parent_asin, variation_family,
                        json.dumps(va), json.dumps(gtf), json.dumps(fs),
                        set_by, bump_revision,
                    ),
                )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("set_asin_metadata failed for %s: %s", asin, exc)
        return False


def get_asin_metadata(workspace_id: str, asin: str) -> Optional[dict[str, Any]]:
    """Fetch raw asin_metadata row (no inheritance). None if absent."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT asin, parent_asin, variation_family, variation_axes,
                           ground_truth_fields, field_sources,
                           revision, set_at, set_by, last_confirmed_at, meta
                    FROM asin_metadata
                    WHERE workspace_id = %s AND asin = %s
                    """,
                    (workspace_id, asin),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "asin": r[0],
                    "parent_asin": r[1],
                    "variation_family": r[2],
                    "variation_axes": r[3] or {},
                    "ground_truth_fields": r[4] or {},
                    "field_sources": r[5] or {},
                    "revision": r[6],
                    "set_at": r[7].isoformat() if r[7] else None,
                    "set_by": r[8],
                    "last_confirmed_at": r[9].isoformat() if r[9] else None,
                    "meta": r[10] or {},
                }
    except Exception as exc:
        logger.warning("get_asin_metadata failed: %s", exc)
        return None


def read_asin_metadata(
    workspace_id: str, asin: str
) -> Optional[dict[str, Any]]:
    """Read asin_metadata with parent inheritance applied.

    Resolution order:
      1. Fetch child row (if exists).
      2. If parent_asin set, fetch parent row, layer parent's
         ground_truth_fields underneath the child's.
      3. Child overrides win. Variation-axis fields use child-only values.

    Returns None if the ASIN itself is not on file. A child with a missing
    parent still returns the child's stored values (logged warning).
    """
    child = get_asin_metadata(workspace_id, asin)
    if child is None:
        return None

    parent_id = child.get("parent_asin")
    if not parent_id:
        return child  # parent listing or independent child

    parent = get_asin_metadata(workspace_id, parent_id)
    if parent is None:
        logger.warning(
            "read_asin_metadata: child %s references missing parent %s",
            asin, parent_id,
        )
        return child

    # Merge: start with parent's ground_truth_fields, layer child on top.
    merged_gtf = copy.deepcopy(parent.get("ground_truth_fields") or {})
    child_gtf = child.get("ground_truth_fields") or {}
    merged_gtf.update(child_gtf)

    # Variation-axis fields: child-only. If child didn't set them, they
    # remain absent (parent's axis values do not propagate).
    for axis in VARIATION_AXIS_FIELDS:
        if axis in child_gtf:
            merged_gtf[axis] = child_gtf[axis]
        elif axis in merged_gtf and axis not in child_gtf:
            # Inherited from parent — but variation axes shouldn't inherit
            # unless explicitly set. Drop it.
            merged_gtf.pop(axis, None)

    # Merge field_sources similarly so audit info follows the field.
    merged_fs = copy.deepcopy(parent.get("field_sources") or {})
    merged_fs.update(child.get("field_sources") or {})

    out = copy.deepcopy(child)
    out["ground_truth_fields"] = merged_gtf
    out["field_sources"] = merged_fs
    out["_inherited_from"] = parent_id
    return out


def list_family_asins(
    workspace_id: str, parent_asin: str
) -> list[dict[str, Any]]:
    """All child ASINs (and the parent itself) in a variation family."""
    pool = get_pool()
    if pool is None:
        return []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT asin, parent_asin, variation_axes,
                           ground_truth_fields, revision
                    FROM asin_metadata
                    WHERE workspace_id = %s
                      AND (asin = %s OR parent_asin = %s)
                    ORDER BY parent_asin NULLS FIRST, asin
                    """,
                    (workspace_id, parent_asin, parent_asin),
                )
                rows = []
                for r in cur.fetchall():
                    rows.append({
                        "asin": r[0],
                        "parent_asin": r[1],
                        "variation_axes": r[2] or {},
                        "ground_truth_fields": r[3] or {},
                        "revision": r[4],
                    })
                return rows
    except Exception as exc:
        logger.warning("list_family_asins failed: %s", exc)
        return []


def confirm_field(
    workspace_id: str,
    asin: str,
    field_name: str,
    confirmed_by: str,
) -> bool:
    """Mark a single field's source as confirmed_by_operator=true.

    Idempotent. If the field has no source row, creates a minimal one
    with the operator's confirmation; the value remains whatever is in
    ground_truth_fields. last_confirmed_at on the ASIN is also bumped
    if all required fields are now confirmed (caller's responsibility).
    """
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE asin_metadata
                    SET field_sources = jsonb_set(
                            COALESCE(field_sources, '{}'::jsonb),
                            %s::text[],
                            (
                              COALESCE(field_sources -> %s, '{}'::jsonb)
                              || jsonb_build_object(
                                   'confirmed_by_operator', true,
                                   'confirmed_at', NOW(),
                                   'confirmed_by', %s::text
                                 )
                            )::jsonb,
                            true
                        )
                    WHERE workspace_id = %s AND asin = %s
                    """,
                    (
                        '{' + field_name + '}', field_name,
                        confirmed_by, workspace_id, asin,
                    ),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("confirm_field failed for %s.%s: %s", asin, field_name, exc)
        return False


def record_field_source(
    workspace_id: str,
    asin: str,
    field_name: str,
    *,
    value: Any,
    source: str,
    confirmed: bool = False,
    set_by: Optional[str] = None,
) -> bool:
    """Update ground_truth_fields[field_name] and field_sources[field_name]
    in a single transaction. Source labels: 'factory_provided',
    'agency_provided', 'operator_typed', 'amazon_taxonomy', 'llm_suggested'."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE asin_metadata
                    SET ground_truth_fields = jsonb_set(
                            COALESCE(ground_truth_fields, '{}'::jsonb),
                            %s::text[],
                            %s::jsonb,
                            true
                        ),
                        field_sources = jsonb_set(
                            COALESCE(field_sources, '{}'::jsonb),
                            %s::text[],
                            jsonb_build_object(
                                'value', %s::jsonb,
                                'source', %s::text,
                                'confirmed_by_operator', %s::boolean,
                                'recorded_at', to_jsonb(NOW())
                            )::jsonb,
                            true
                        ),
                        set_at = NOW(),
                        set_by = COALESCE(%s, set_by)
                    WHERE workspace_id = %s AND asin = %s
                    """,
                    (
                        '{' + field_name + '}', json.dumps(value),
                        '{' + field_name + '}', json.dumps(value),
                        source, confirmed, set_by,
                        workspace_id, asin,
                    ),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("record_field_source failed: %s", exc)
        return False


__all__ = [
    "set_asin_metadata",
    "get_asin_metadata",
    "read_asin_metadata",
    "list_family_asins",
    "confirm_field",
    "record_field_source",
    "VARIATION_AXIS_FIELDS",
]
