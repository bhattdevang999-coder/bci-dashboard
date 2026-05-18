"""Atlas substrate — brand_position.

Implements BRAND_POSITION.md. One row per workspace; updates bump
revision. Used by Layer 2 of every NIS / pricing / recommendation
reasoning chain.

Contract:
    set_brand_position(...)        -> bool
    get_brand_position(...)        -> dict | None
    update_review_timestamp(...)   -> bool   (call when operator reaffirms)

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.brand_position")


def set_brand_position(
    workspace_id: str,
    *,
    position_statement: str,
    competitor_set: list[str],
    competitor_role: dict[str, str],
    price_band: dict[str, Any],
    positioning_hypothesis: Optional[str],
    next_review_at: datetime,
    set_by: str,
    review_freq: str = "quarterly",
    pricing_logic_revision: Optional[int] = None,
) -> bool:
    """Upsert the workspace's brand_position row. Bumps revision on update."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO brand_position (
                        workspace_id, position_statement, competitor_set,
                        competitor_role, price_band, positioning_hypothesis,
                        pricing_logic_revision,
                        review_freq, next_review_at,
                        revision, set_at, set_by
                    ) VALUES (
                        %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s,
                        %s,
                        %s, %s,
                        1, NOW(), %s
                    )
                    ON CONFLICT (workspace_id) DO UPDATE SET
                        position_statement = EXCLUDED.position_statement,
                        competitor_set = EXCLUDED.competitor_set,
                        competitor_role = EXCLUDED.competitor_role,
                        price_band = EXCLUDED.price_band,
                        positioning_hypothesis = EXCLUDED.positioning_hypothesis,
                        pricing_logic_revision = EXCLUDED.pricing_logic_revision,
                        review_freq = EXCLUDED.review_freq,
                        next_review_at = EXCLUDED.next_review_at,
                        revision = brand_position.revision + 1,
                        set_at = NOW(),
                        set_by = EXCLUDED.set_by
                    """,
                    (
                        workspace_id, position_statement, competitor_set,
                        json.dumps(competitor_role),
                        json.dumps(price_band),
                        positioning_hypothesis,
                        pricing_logic_revision,
                        review_freq, next_review_at,
                        set_by,
                    ),
                )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("set_brand_position failed: %s", exc)
        return False


def get_brand_position(workspace_id: str) -> Optional[dict[str, Any]]:
    """Return the workspace's current brand_position row, or None."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workspace_id, position_statement, competitor_set,
                           competitor_role, price_band, positioning_hypothesis,
                           pricing_logic_revision,
                           review_freq, last_reviewed_at, next_review_at,
                           revision, set_at, set_by, meta
                    FROM brand_position
                    WHERE workspace_id = %s
                    """,
                    (workspace_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "workspace_id": r[0],
                    "position_statement": r[1],
                    "competitor_set": r[2] or [],
                    "competitor_role": r[3] or {},
                    "price_band": r[4] or {},
                    "positioning_hypothesis": r[5],
                    "pricing_logic_revision": r[6],
                    "review_freq": r[7],
                    "last_reviewed_at":
                        r[8].isoformat() if r[8] else None,
                    "next_review_at":
                        r[9].isoformat() if r[9] else None,
                    "revision": r[10],
                    "set_at": r[11].isoformat() if r[11] else None,
                    "set_by": r[12],
                    "meta": r[13] or {},
                }
    except Exception as exc:
        logger.warning("get_brand_position failed: %s", exc)
        return None


def update_review_timestamp(
    workspace_id: str,
    *,
    reaffirmed_by: str,
    next_review_at: datetime,
) -> bool:
    """Bump last_reviewed_at to now; set next_review_at; no revision bump."""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE brand_position
                    SET last_reviewed_at = NOW(),
                        next_review_at = %s,
                        meta = COALESCE(meta, '{}'::jsonb)
                               || jsonb_build_object(
                                    'last_reaffirmed_by', %s::text
                                  )
                    WHERE workspace_id = %s
                    """,
                    (next_review_at, reaffirmed_by, workspace_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("update_review_timestamp failed: %s", exc)
        return False


__all__ = [
    "set_brand_position",
    "get_brand_position",
    "update_review_timestamp",
]
