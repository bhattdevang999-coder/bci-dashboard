"""Atlas substrate — audit_rules library.

Operator-editable rule library backing the catalog audit. Every rule is
substrate; brand-specific overrides are first-class rows that point at
their parent. Resolution order at audit time:

   brand-specific row (workspace_id = X) > global default (workspace_id IS NULL)

Contract:
    create_rule(...)            -> str | None
    fork_for_brand(...)         -> str | None
    edit_rule(...)              -> bool   (revision bumped + reasoning saved)
    deactivate_rule(...)        -> bool
    list_active_rules(...)      -> list[dict]
    resolve_rules_for_brand(...) -> list[dict]
                              (returns the brand-effective ruleset:
                               override row if present, else global default)
    seed_default_rules()        -> int    (one-time idempotent seed)

Best-effort writes. Never raises.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.audit_rules")


VALID_RULE_KINDS = (
    "numeric_threshold", "presence_check",
    "group_size", "compound", "cohort_aggregate",
)

VALID_SEVERITIES = ("critical", "high", "medium", "low", "strategic")

VALID_LIFT_MODELS = (
    "same_parent_aplus", "image_count_regression",
    "rule_of_thumb", "compliance_only", "unknown",
)


def create_rule(
    *,
    workspace_id: Optional[str],
    name: str,
    display_name: str,
    description: Optional[str],
    rule_kind: str,
    threshold_json: dict[str, Any],
    applies_to: dict[str, Any],
    severity: str,
    confidence_default: Optional[float] = None,
    predicted_lift_model: str = "unknown",
    parent_rule_id: Optional[str] = None,
    created_by: str = "system",
    rule_id: Optional[str] = None,
) -> Optional[str]:
    """Create a rule. workspace_id=None for global default; non-None for
    brand-specific override (must reference a parent_rule_id)."""
    if rule_kind not in VALID_RULE_KINDS:
        logger.warning("create_rule: invalid rule_kind %s", rule_kind)
        return None
    if severity not in VALID_SEVERITIES:
        logger.warning("create_rule: invalid severity %s", severity)
        return None
    if predicted_lift_model not in VALID_LIFT_MODELS:
        predicted_lift_model = "unknown"
    if not name or not display_name:
        return None

    rid = rule_id or str(uuid.uuid4())
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_rules (
                        rule_id, workspace_id, parent_rule_id,
                        name, display_name, description,
                        rule_kind, threshold_json, applies_to,
                        severity, confidence_default,
                        predicted_lift_model,
                        is_active, revision, created_by
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s::jsonb, %s::jsonb,
                        %s, %s,
                        %s,
                        true, 1, %s
                    )
                    """,
                    (
                        rid, workspace_id, parent_rule_id,
                        name, display_name, description,
                        rule_kind, json.dumps(threshold_json),
                        json.dumps(applies_to),
                        severity, confidence_default,
                        predicted_lift_model,
                        created_by,
                    ),
                )
            conn.commit()
        return rid
    except Exception as exc:
        logger.warning("create_rule failed: %s", exc)
        return None


def fork_for_brand(
    parent_rule_id: str,
    workspace_id: str,
    *,
    threshold_overrides: Optional[dict[str, Any]] = None,
    severity_override: Optional[str] = None,
    is_active: bool = True,
    forked_by: str,
    reasoning: Optional[str] = None,
) -> Optional[str]:
    """Fork a global-default rule into a brand-specific override.

    The override starts as a copy of the parent; threshold/severity can
    differ. Reasoning is persisted in last_edit_reasoning so the fork is
    auditable. Idempotent: if a fork already exists for the (parent,
    workspace) pair, returns its id without creating a duplicate.
    """
    parent = get_rule(parent_rule_id)
    if parent is None or parent["workspace_id"] is not None:
        logger.warning(
            "fork_for_brand: parent must exist and be a global default"
        )
        return None

    # Idempotency — only check for an existing BRAND-specific fork,
    # not the global default (which would always match by name).
    existing = list_active_rules(
        workspace_id=workspace_id, name=parent["name"],
        include_global=False,
    )
    if existing:
        return existing[0]["rule_id"]

    threshold_json = dict(parent["threshold_json"])
    if threshold_overrides:
        threshold_json.update(threshold_overrides)
    severity = severity_override or parent["severity"]

    rid = create_rule(
        workspace_id=workspace_id,
        name=parent["name"],
        display_name=parent["display_name"],
        description=parent["description"],
        rule_kind=parent["rule_kind"],
        threshold_json=threshold_json,
        applies_to=parent["applies_to"],
        severity=severity,
        confidence_default=parent["confidence_default"],
        predicted_lift_model=parent["predicted_lift_model"],
        parent_rule_id=parent_rule_id,
        created_by=forked_by,
    )
    if rid and reasoning:
        # Stamp reasoning into last_edit_reasoning for audit
        edit_rule(
            rid,
            edited_by=forked_by,
            reasoning=reasoning,
        )
    return rid


def edit_rule(
    rule_id: str,
    *,
    edited_by: str,
    threshold_json: Optional[dict[str, Any]] = None,
    severity: Optional[str] = None,
    is_active: Optional[bool] = None,
    applies_to: Optional[dict[str, Any]] = None,
    reasoning: Optional[str] = None,
) -> bool:
    """Edit a rule in place. Bumps revision; reasoning saved."""
    if severity and severity not in VALID_SEVERITIES:
        return False
    pool = get_pool()
    if pool is None:
        return False

    sets = []
    params: list[Any] = []
    if threshold_json is not None:
        sets.append("threshold_json = %s::jsonb")
        params.append(json.dumps(threshold_json))
    if severity is not None:
        sets.append("severity = %s")
        params.append(severity)
    if is_active is not None:
        sets.append("is_active = %s")
        params.append(is_active)
    if applies_to is not None:
        sets.append("applies_to = %s::jsonb")
        params.append(json.dumps(applies_to))
    sets.append("revision = revision + 1")
    sets.append("last_edited_at = NOW()")
    sets.append("last_edited_by = %s")
    params.append(edited_by)
    if reasoning is not None:
        sets.append("last_edit_reasoning = %s")
        params.append(reasoning)

    params.append(rule_id)
    sql = f"UPDATE audit_rules SET {', '.join(sets)} WHERE rule_id = %s"

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                affected = cur.rowcount
            conn.commit()
            return affected > 0
    except Exception as exc:
        logger.warning("edit_rule failed: %s", exc)
        return False


def deactivate_rule(rule_id: str, deactivated_by: str) -> bool:
    """Set is_active=false. Reversible via edit_rule(is_active=True)."""
    return edit_rule(
        rule_id, edited_by=deactivated_by, is_active=False,
        reasoning="deactivated",
    )


def get_rule(rule_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rule_id, workspace_id, parent_rule_id,
                           name, display_name, description,
                           rule_kind, threshold_json, applies_to,
                           severity, confidence_default,
                           predicted_lift_model, is_active,
                           revision, created_at, created_by,
                           last_edited_at, last_edited_by,
                           last_edit_reasoning, meta
                    FROM audit_rules WHERE rule_id = %s
                    """,
                    (rule_id,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                return _row(r)
    except Exception as exc:
        logger.warning("get_rule failed: %s", exc)
        return None


def list_active_rules(
    *,
    workspace_id: Optional[str] = None,
    name: Optional[str] = None,
    include_global: bool = True,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """List rules with optional workspace/name filters."""
    pool = get_pool()
    if pool is None:
        return []
    where = []
    params: list[Any] = []
    if not include_inactive:
        where.append("is_active = true")
    if workspace_id is not None and include_global:
        where.append("(workspace_id = %s OR workspace_id IS NULL)")
        params.append(workspace_id)
    elif workspace_id is not None:
        where.append("workspace_id = %s")
        params.append(workspace_id)
    elif not include_global:
        where.append("workspace_id IS NOT NULL")
    if name:
        where.append("name = %s")
        params.append(name)

    sql = f"""
        SELECT rule_id, workspace_id, parent_rule_id,
               name, display_name, description,
               rule_kind, threshold_json, applies_to,
               severity, confidence_default,
               predicted_lift_model, is_active,
               revision, created_at, created_by,
               last_edited_at, last_edited_by,
               last_edit_reasoning, meta
        FROM audit_rules
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY name, workspace_id NULLS LAST
    """
    out: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                for r in cur.fetchall():
                    out.append(_row(r))
    except Exception as exc:
        logger.warning("list_active_rules failed: %s", exc)
    return out


def resolve_rules_for_brand(workspace_id: str) -> list[dict[str, Any]]:
    """Return the brand-effective ruleset.

    For each rule name, return the brand-specific override if present
    (and active), otherwise the global default. Inactive rules in either
    layer are excluded — a brand can deactivate a rule for itself by
    forking it and toggling is_active=false.
    """
    all_rules = list_active_rules(
        workspace_id=workspace_id, include_global=True,
        include_inactive=False,
    )
    by_name: dict[str, dict[str, Any]] = {}
    # First pass: globals
    for r in all_rules:
        if r["workspace_id"] is None:
            by_name[r["name"]] = r
    # Second pass: brand overrides win
    for r in all_rules:
        if r["workspace_id"] == workspace_id:
            by_name[r["name"]] = r
    return list(by_name.values())


def _row(r: tuple) -> dict[str, Any]:
    return {
        "rule_id": r[0],
        "workspace_id": r[1],
        "parent_rule_id": r[2],
        "name": r[3],
        "display_name": r[4],
        "description": r[5],
        "rule_kind": r[6],
        "threshold_json": r[7] or {},
        "applies_to": r[8] or {},
        "severity": r[9],
        "confidence_default":
            float(r[10]) if r[10] is not None else None,
        "predicted_lift_model": r[11],
        "is_active": r[12],
        "revision": r[13],
        "created_at": r[14].isoformat() if r[14] else None,
        "created_by": r[15],
        "last_edited_at":
            r[16].isoformat() if r[16] else None,
        "last_edited_by": r[17],
        "last_edit_reasoning": r[18],
        "meta": r[19] or {},
    }


# ---------------------------------------------------------------------------
# Seed library (15 rules) — derived from the Roxy audit
# ---------------------------------------------------------------------------

SEED_RULES = [
    {
        "name": "fewer_than_7_images",
        "display_name": "Fewer than 7 product images",
        "description": (
            "Listings with fewer than 7 images consistently underperform "
            "richer listings on CVR. Same-parent comparisons show 3-5x "
            "session-to-conversion lift when image count moves from 4 to 7+."
        ),
        "rule_kind": "numeric_threshold",
        "threshold_json": {
            "column": "image_count", "op": "<", "value": 7,
        },
        "applies_to": {"cohort": "active"},
        "severity": "critical",
        "confidence_default": 0.85,
        "predicted_lift_model": "image_count_regression",
    },
    {
        "name": "fewer_than_5_images",
        "display_name": "Fewer than 5 product images",
        "description": "Threshold for the lower bound; severity high.",
        "rule_kind": "numeric_threshold",
        "threshold_json": {
            "column": "image_count", "op": "<", "value": 5,
        },
        "applies_to": {"cohort": "active"},
        "severity": "high",
        "confidence_default": 0.80,
        "predicted_lift_model": "image_count_regression",
    },
    {
        "name": "fewer_than_5_bullets",
        "display_name": "Fewer than 5 bullet points",
        "description": (
            "Amazon offers 5 bullet slots; using fewer leaves keyword "
            "and benefit coverage on the table."
        ),
        "rule_kind": "numeric_threshold",
        "threshold_json": {
            "column": "bullet_count", "op": "<", "value": 5,
        },
        "applies_to": {"cohort": "active"},
        "severity": "high",
        "confidence_default": 0.65,
        "predicted_lift_model": "rule_of_thumb",
    },
    {
        "name": "no_description",
        "display_name": "No product description",
        "description": (
            "A blank description suppresses the long-tail keyword index "
            "and reads as low-effort to shoppers."
        ),
        "rule_kind": "presence_check",
        "threshold_json": {"column": "description", "expect": "non_empty"},
        "applies_to": {"cohort": "active"},
        "severity": "high",
        "confidence_default": 0.70,
        "predicted_lift_model": "rule_of_thumb",
    },
    {
        "name": "missing_a_plus_top_decile",
        "display_name": "Missing A+ on top revenue decile",
        "description": (
            "Within-parent natural experiment shows A+ children earn "
            "3.8x more revenue than non-A+ siblings. Highest-leverage "
            "single fix on top revenue ASINs."
        ),
        "rule_kind": "compound",
        "threshold_json": {
            "all_of": [
                {"column": "a_plus_status", "op": "==", "value": "No"},
                {"column": "revenue_decile", "op": "==", "value": "top"},
            ]
        },
        "applies_to": {"cohort": "active", "decile": "top_revenue"},
        "severity": "critical",
        "confidence_default": 0.90,
        "predicted_lift_model": "same_parent_aplus",
    },
    {
        "name": "missing_a_plus",
        "display_name": "Missing A+ Content",
        "description": (
            "Same evidence as the top-decile rule, broader scope. "
            "Severity steps down because lift is smaller on tail ASINs."
        ),
        "rule_kind": "presence_check",
        "threshold_json": {"column": "a_plus_status", "expect": "Yes"},
        "applies_to": {"cohort": "active"},
        "severity": "high",
        "confidence_default": 0.75,
        "predicted_lift_model": "same_parent_aplus",
    },
    {
        "name": "title_under_80_chars",
        "display_name": "Title shorter than 80 characters",
        "description": (
            "Apparel category allows up to 200 characters. Titles below "
            "80 leave keyword indexing capacity unused."
        ),
        "rule_kind": "numeric_threshold",
        "threshold_json": {
            "column": "title_length", "op": "<", "value": 80,
        },
        "applies_to": {"cohort": "active"},
        "severity": "medium",
        "confidence_default": 0.55,
        "predicted_lift_model": "rule_of_thumb",
    },
    {
        "name": "title_over_200_chars",
        "display_name": "Title over 200 characters",
        "description": (
            "Amazon truncates titles past 200 in mobile search results. "
            "Hard ceiling violation; critical."
        ),
        "rule_kind": "numeric_threshold",
        "threshold_json": {
            "column": "title_length", "op": ">", "value": 200,
        },
        "applies_to": {"cohort": "all"},
        "severity": "critical",
        "confidence_default": 0.95,
        "predicted_lift_model": "compliance_only",
    },
    {
        "name": "orphan_variation",
        "display_name": "Color and Size set but no Variation Theme",
        "description": (
            "Orphan listings: Amazon will not render the size/color picker. "
            "Same-product comparison shows 1.9x mean revenue gap vs "
            "properly-themed siblings."
        ),
        "rule_kind": "compound",
        "threshold_json": {
            "all_of": [
                {"column": "color", "op": "is_set"},
                {"column": "size", "op": "is_set"},
                {"column": "variation_theme", "op": "is_blank"},
            ]
        },
        "applies_to": {"cohort": "active"},
        "severity": "critical",
        "confidence_default": 0.80,
        "predicted_lift_model": "rule_of_thumb",
    },
    {
        "name": "duplicate_style_group",
        "display_name": "Duplicate Style # cluster (>5 ASINs share style)",
        "description": (
            "Multiple ASINs republished from the same Style # cannibalize "
            "each other's reviews, BSR, and ad spend. Surfaces as a strategic "
            "decision, not an automatic merge."
        ),
        "rule_kind": "group_size",
        "threshold_json": {
            "group_by": "style_number", "op": ">", "value": 5,
        },
        "applies_to": {"cohort": "all"},
        "severity": "strategic",
        "confidence_default": 0.65,
        "predicted_lift_model": "unknown",
    },
    {
        "name": "title_missing_brand",
        "display_name": "Title missing brand name",
        "description": "Brand prefix is convention; absence is a small but "
                       "consistent rank signal.",
        "rule_kind": "presence_check",
        "threshold_json": {
            "column": "title", "expect": "contains_brand",
        },
        "applies_to": {"cohort": "active"},
        "severity": "low",
        "confidence_default": 0.40,
        "predicted_lift_model": "rule_of_thumb",
    },
    {
        "name": "title_mostly_uppercase",
        "display_name": "Title is mostly UPPERCASE",
        "description": "Amazon style guide requires sentence case. Compliance.",
        "rule_kind": "numeric_threshold",
        "threshold_json": {
            "column": "title_uppercase_ratio", "op": ">", "value": 0.5,
        },
        "applies_to": {"cohort": "all"},
        "severity": "medium",
        "confidence_default": 0.85,
        "predicted_lift_model": "compliance_only",
    },
    {
        "name": "missing_country_of_origin",
        "display_name": "Country of Origin not on file",
        "description": "Amazon Apparel category requires CoO. Compliance.",
        "rule_kind": "presence_check",
        "threshold_json": {
            "column": "country_of_origin", "expect": "non_empty",
        },
        "applies_to": {"cohort": "all", "category": "apparel"},
        "severity": "medium",
        "confidence_default": 0.95,
        "predicted_lift_model": "compliance_only",
    },
    {
        "name": "missing_care_instructions",
        "display_name": "Care Instructions not on file",
        "description": "Amazon Apparel category requires Care. Compliance.",
        "rule_kind": "presence_check",
        "threshold_json": {
            "column": "care_instructions", "expect": "non_empty",
        },
        "applies_to": {"cohort": "all", "category": "apparel"},
        "severity": "medium",
        "confidence_default": 0.95,
        "predicted_lift_model": "compliance_only",
    },
    {
        "name": "abandoned_subcategory",
        "display_name": "Abandoned subcategory (low A+, low revenue)",
        "description": (
            "Subcategories with <20%% A+ coverage AND mean rev/listing <$50 "
            "indicate operator/agency abandoned the segment. Strategic "
            "decision: revive, kill, or rebuild."
        ),
        "rule_kind": "cohort_aggregate",
        "threshold_json": {
            "group_by": "subcategory",
            "all_of": [
                {"metric": "a_plus_coverage_pct", "op": "<", "value": 20},
                {"metric": "mean_revenue_per_listing", "op": "<", "value": 50},
            ]
        },
        "applies_to": {"cohort": "all"},
        "severity": "strategic",
        "confidence_default": 0.70,
        "predicted_lift_model": "unknown",
    },
]


def seed_default_rules(force: bool = False) -> int:
    """One-time idempotent seed of the global rule library.

    Returns the number of rules inserted (0 if all already present and
    force=False). Idempotent on the (workspace_id IS NULL, name) unique
    index — re-running is safe.
    """
    pool = get_pool()
    if pool is None:
        return 0

    inserted = 0
    for spec in SEED_RULES:
        # Skip if a global rule with this name already exists
        existing = list_active_rules(name=spec["name"], include_global=True)
        global_existing = [r for r in existing if r["workspace_id"] is None]
        if global_existing and not force:
            continue
        rid = create_rule(
            workspace_id=None,
            name=spec["name"],
            display_name=spec["display_name"],
            description=spec["description"],
            rule_kind=spec["rule_kind"],
            threshold_json=spec["threshold_json"],
            applies_to=spec["applies_to"],
            severity=spec["severity"],
            confidence_default=spec["confidence_default"],
            predicted_lift_model=spec["predicted_lift_model"],
            created_by="system_seed",
        )
        if rid:
            inserted += 1
    return inserted


__all__ = [
    "create_rule",
    "fork_for_brand",
    "edit_rule",
    "deactivate_rule",
    "get_rule",
    "list_active_rules",
    "resolve_rules_for_brand",
    "seed_default_rules",
    "SEED_RULES",
    "VALID_RULE_KINDS",
    "VALID_SEVERITIES",
    "VALID_LIFT_MODELS",
]
