"""Atlas substrate — pricing_logic & pricing_decisions.

Implements PRICING_LOGIC.md. Operator-set floor/ceiling rules are
substrate; Atlas computes implications, not recommendations (Mode 1
LLM available Day 1; Mode 2 calibrated gated to Month 6+).

Contract:
    set_pricing_logic(...)            -> bool
    get_pricing_logic(...)            -> dict | None
    read_active_logic(...)            -> dict | None     (scope-prioritised)
    log_pricing_decision(...)         -> str | None
    attach_outcome(...)               -> bool
    list_pricing_decisions(...)       -> list[dict]
    compute_floor_from_rule(...)      -> float | None

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.pricing_logic")


VALID_LOGIC_SCOPES = ("global", "family", "asin")

VALID_DECISION_MODES = ("manual", "mode1_llm", "mode2_calibrated")

VALID_GOAL_REGIMES = ("launch_velocity", "margin", "volume")


def set_pricing_logic(
    workspace_id: str,
    *,
    scope: str,
    scope_ref: Optional[str],
    floor_rule: dict[str, Any],
    ceiling_rule: dict[str, Any],
    reasoning: Optional[str],
    set_by: str,
    ceiling_next_review_at: Optional[datetime] = None,
) -> bool:
    """Upsert a pricing_logic row at the given scope."""
    if scope not in VALID_LOGIC_SCOPES:
        logger.warning("set_pricing_logic: invalid scope %s", scope)
        return False
    ref = scope_ref if scope_ref is not None else ""
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pricing_logic (
                        workspace_id, scope, scope_ref,
                        floor_rule, ceiling_rule, reasoning,
                        revision, set_at, set_by,
                        ceiling_next_review_at
                    ) VALUES (
                        %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s,
                        1, NOW(), %s,
                        %s
                    )
                    ON CONFLICT (workspace_id, scope, scope_ref) DO UPDATE SET
                        floor_rule = EXCLUDED.floor_rule,
                        ceiling_rule = EXCLUDED.ceiling_rule,
                        reasoning = EXCLUDED.reasoning,
                        revision = pricing_logic.revision + 1,
                        set_at = NOW(),
                        set_by = EXCLUDED.set_by,
                        ceiling_next_review_at =
                            EXCLUDED.ceiling_next_review_at
                    """,
                    (
                        workspace_id, scope, ref,
                        json.dumps(floor_rule), json.dumps(ceiling_rule),
                        reasoning, set_by, ceiling_next_review_at,
                    ),
                )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("set_pricing_logic failed: %s", exc)
        return False


def get_pricing_logic(
    workspace_id: str, scope: str, scope_ref: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Fetch exact pricing_logic row for the given scope key."""
    pool = get_pool()
    if pool is None:
        return None
    ref = scope_ref if scope_ref is not None else ""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT scope, scope_ref, floor_rule, ceiling_rule,
                           reasoning, revision, set_at, set_by,
                           ceiling_next_review_at, meta
                    FROM pricing_logic
                    WHERE workspace_id = %s AND scope = %s
                          AND scope_ref = %s
                    """,
                    (workspace_id, scope, ref),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "scope": r[0],
                    "scope_ref": r[1] or None,
                    "floor_rule": r[2] or {},
                    "ceiling_rule": r[3] or {},
                    "reasoning": r[4],
                    "revision": r[5],
                    "set_at": r[6].isoformat() if r[6] else None,
                    "set_by": r[7],
                    "ceiling_next_review_at":
                        r[8].isoformat() if r[8] else None,
                    "meta": r[9] or {},
                }
    except Exception as exc:
        logger.warning("get_pricing_logic failed: %s", exc)
        return None


def read_active_logic(
    workspace_id: str,
    *,
    asin: Optional[str] = None,
    family: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Resolve pricing_logic with scope priority: asin > family > global."""
    if asin:
        row = get_pricing_logic(workspace_id, "asin", asin)
        if row:
            return row
    if family:
        row = get_pricing_logic(workspace_id, "family", family)
        if row:
            return row
    return get_pricing_logic(workspace_id, "global", "")


def compute_floor_from_rule(
    floor_rule: dict[str, Any],
    *,
    landed_cost: Optional[float] = None,
    fba_fee: Optional[float] = None,
    third_pl_fee: Optional[float] = None,
    ad_spend_per_unit: Optional[float] = None,
    referral_rate: float = 0.15,
) -> Optional[float]:
    """Evaluate variable_contribution_zero floor rule against cost inputs.

    Returns floor price (P such that P - costs - referral*P = 0,
    => P = costs / (1 - referral_rate)). Returns None if any required
    component missing AND the rule's behavior is to refuse missing.
    """
    method = floor_rule.get("method")
    if method != "variable_contribution_zero":
        return None
    components = (
        landed_cost or 0.0,
        fba_fee or 0.0,
        third_pl_fee or 0.0,
        ad_spend_per_unit or 0.0,
    )
    missing = [
        x for x in (landed_cost, fba_fee, third_pl_fee, ad_spend_per_unit)
        if x is None
    ]
    if missing and floor_rule.get(
        "behavior_when_components_missing"
    ) == "refuse":
        return None
    total_cost = sum(components)
    if referral_rate >= 1.0:
        return None
    floor = total_cost / (1.0 - referral_rate)
    return round(floor, 2)


def log_pricing_decision(
    workspace_id: str,
    *,
    asin: str,
    price_set: float,
    price_set_by: str,
    mode: str,
    goal_regime: Optional[str] = "launch_velocity",
    floor_at_time: Optional[float] = None,
    ceiling_at_time: Optional[float] = None,
    play_zone_position: Optional[str] = None,
    reasoning: Optional[str] = None,
    pattern_tags: Optional[list[str]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Append a pricing_decisions journal entry. Returns decision_id."""
    if mode not in VALID_DECISION_MODES:
        logger.warning("log_pricing_decision: invalid mode %s", mode)
        return None
    if goal_regime and goal_regime not in VALID_GOAL_REGIMES:
        logger.warning(
            "log_pricing_decision: invalid goal_regime %s", goal_regime
        )
        return None
    pool = get_pool()
    if pool is None:
        return None

    decision_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pricing_decisions (
                        decision_id, workspace_id, asin,
                        price_set, price_set_by,
                        floor_at_time, ceiling_at_time, play_zone_position,
                        goal_regime, reasoning, mode,
                        pattern_tags, meta
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s::jsonb
                    )
                    """,
                    (
                        decision_id, workspace_id, asin,
                        price_set, price_set_by,
                        floor_at_time, ceiling_at_time, play_zone_position,
                        goal_regime, reasoning, mode,
                        pattern_tags or [],
                        json.dumps(meta or {}),
                    ),
                )
            conn.commit()
        return decision_id
    except Exception as exc:
        logger.warning("log_pricing_decision failed: %s", exc)
        return None


def attach_outcome(
    decision_id: str,
    *,
    window: str,   # '30d' | '60d' | '90d'
    outcome: dict[str, Any],
) -> bool:
    """Attach outcome JSON to a pricing_decisions row at the right window."""
    col = f"outcome_at_{window}"
    if col not in ("outcome_at_30d", "outcome_at_60d", "outcome_at_90d"):
        return False
    pool = get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE pricing_decisions
                    SET {col} = %s::jsonb
                    WHERE decision_id = %s
                    """,
                    (json.dumps(outcome), decision_id),
                )
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("attach_outcome failed: %s", exc)
        return False


def list_pricing_decisions(
    workspace_id: str,
    *,
    asin: Optional[str] = None,
    goal_regime: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recent pricing_decisions, newest first."""
    pool = get_pool()
    if pool is None:
        return []
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if asin:
        where.append("asin = %s")
        params.append(asin)
    if goal_regime:
        where.append("goal_regime = %s")
        params.append(goal_regime)
    params.append(limit)
    sql = f"""
        SELECT decision_id, asin, price_set, price_set_at, price_set_by,
               floor_at_time, ceiling_at_time, play_zone_position,
               goal_regime, mode, reasoning,
               outcome_at_30d, outcome_at_60d, outcome_at_90d,
               pattern_tags
        FROM pricing_decisions
        WHERE {' AND '.join(where)}
        ORDER BY price_set_at DESC
        LIMIT %s
    """
    out = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append({
                        "decision_id": r[0],
                        "asin": r[1],
                        "price_set": float(r[2]) if r[2] is not None else None,
                        "price_set_at":
                            r[3].isoformat() if r[3] else None,
                        "price_set_by": r[4],
                        "floor_at_time":
                            float(r[5]) if r[5] is not None else None,
                        "ceiling_at_time":
                            float(r[6]) if r[6] is not None else None,
                        "play_zone_position": r[7],
                        "goal_regime": r[8],
                        "mode": r[9],
                        "reasoning": r[10],
                        "outcome_at_30d": r[11],
                        "outcome_at_60d": r[12],
                        "outcome_at_90d": r[13],
                        "pattern_tags": r[14] or [],
                    })
    except Exception as exc:
        logger.warning("list_pricing_decisions failed: %s", exc)
    return out


__all__ = [
    "set_pricing_logic",
    "get_pricing_logic",
    "read_active_logic",
    "compute_floor_from_rule",
    "log_pricing_decision",
    "attach_outcome",
    "list_pricing_decisions",
    "VALID_LOGIC_SCOPES",
    "VALID_DECISION_MODES",
    "VALID_GOAL_REGIMES",
]
