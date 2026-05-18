"""Atlas substrate — L0 context injection layer.

Implementation of CONTEXT.md. The single chokepoint that every LLM
call in Atlas routes through to assemble the relevant substrate state.

Contract:
    build_context(workspace_id, asin, decision_class, ...) -> ContextBundle

ContextBundle is a structured dict with all 9 reasoning layers, the
open unknowns affecting this decision, the rows consulted (for
provenance logging), and an evidence_strength score derived from
decision_class_requirements.

Never raises. Best-effort. Returns a bundle with empty layers if
substrate is unavailable.

Scope guard:
- Pure context assembly. No LLM calls here.
- No calibration_state writes (those happen on outcome arrival).
- No requirement enforcement (downstream consumer's job).
- No citation verification (that's M3 citation_chain.py).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.context")


# ---------------------------------------------------------------------------
# Decision class requirements (loaded once, cached)
# ---------------------------------------------------------------------------

_REQUIREMENTS_CACHE: Optional[dict[str, Any]] = None
_REQUIREMENTS_PATH = Path(__file__).parent / "decision_class_requirements.yml"


def _load_requirements() -> dict[str, Any]:
    global _REQUIREMENTS_CACHE
    if _REQUIREMENTS_CACHE is not None:
        return _REQUIREMENTS_CACHE
    try:
        # Lightweight YAML parsing: only flat keys + nested lists.
        # Avoids adding pyyaml as a hard dep just for this config.
        import yaml  # type: ignore
        with open(_REQUIREMENTS_PATH, "r") as f:
            _REQUIREMENTS_CACHE = yaml.safe_load(f)
    except ImportError:
        logger.warning("PyYAML not installed; using inline fallback requirements")
        _REQUIREMENTS_CACHE = _inline_fallback_requirements()
    except Exception as exc:
        logger.warning("Failed to load requirements yml: %s; using fallback", exc)
        _REQUIREMENTS_CACHE = _inline_fallback_requirements()
    return _REQUIREMENTS_CACHE or {}


def _inline_fallback_requirements() -> dict[str, Any]:
    """Conservative fallback if YAML parsing fails. Keep aligned with
    decision_class_requirements.yml on the title_generation entry as the
    canonical example."""
    return {
        "penalty_per_missing_required": 0.20,
        "penalty_per_missing_nice": 0.05,
        "confidence_floor": 0.10,
        "confidence_ceiling": 0.95,
        "classes": {
            "title_generation": {
                "required": [
                    "asin_metadata.material",
                    "brand_position",
                    "brand_voice",
                ],
                "nice_to_have": [],
            },
        },
    }


# ---------------------------------------------------------------------------
# Layer assemblers (each returns a (data, rows_read) tuple)
# ---------------------------------------------------------------------------


def _layer_factual(workspace_id: str, asin: Optional[str]) -> tuple[dict, list[str]]:
    """Layer 1 — asin_metadata. Empty for now (table ships in M2)."""
    if not asin:
        return {}, []
    pool = get_pool()
    if pool is None:
        return {}, []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # asin_metadata table not yet created in M1 — defer to M2.
                # Check existence first to keep build_context safe before M2.
                cur.execute("SELECT to_regclass('asin_metadata')")
                if cur.fetchone()[0] != "asin_metadata":
                    return {}, []
                cur.execute(
                    """
                    SELECT asin, parent_asin, variation_family, variation_axes,
                           ground_truth_fields, field_sources, revision
                    FROM asin_metadata
                    WHERE workspace_id = %s AND asin = %s
                    """,
                    (workspace_id, asin),
                )
                row = cur.fetchone()
                if row is None:
                    return {}, []
                row_id = f"asin_metadata#{asin}"
                # If child has a parent, also pull parent for inheritance.
                parent_data = None
                if row[1]:  # parent_asin
                    cur.execute(
                        """
                        SELECT ground_truth_fields, field_sources
                        FROM asin_metadata
                        WHERE workspace_id = %s AND asin = %s
                        """,
                        (workspace_id, row[1]),
                    )
                    p = cur.fetchone()
                    if p:
                        parent_data = {
                            "ground_truth_fields": p[0] or {},
                            "field_sources": p[1] or {},
                        }
                rows_read = [row_id]
                if parent_data:
                    rows_read.append(f"asin_metadata#{row[1]}")
                return {
                    "asin": row[0],
                    "parent_asin": row[1],
                    "variation_family": row[2],
                    "variation_axes": row[3] or {},
                    "ground_truth_fields": row[4] or {},
                    "field_sources": row[5] or {},
                    "revision": row[6],
                    "parent": parent_data,
                }, rows_read
    except Exception as exc:
        logger.warning("layer_factual read failed: %s", exc)
    return {}, []


def _layer_strategic(workspace_id: str, asin: Optional[str],
                     decision_class: Optional[str]) -> tuple[dict, list[str]]:
    """Layer 2 — operator_positions + brand_position + goals.
    Tables ship in M2; safe before M2 (returns empty)."""
    pool = get_pool()
    if pool is None:
        return {}, []
    out: dict[str, Any] = {"operator_positions": [], "brand_position": None, "goals": None}
    rows_read: list[str] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # operator_positions
                cur.execute("SELECT to_regclass('operator_positions')")
                if cur.fetchone()[0]:
                    cur.execute(
                        """
                        SELECT position_id, scope, scope_ref, claim, reasoning,
                               position_type
                        FROM operator_positions
                        WHERE workspace_id = %s
                          AND status = 'active'
                          AND (
                            scope = 'global'
                            OR scope = 'brand'
                            OR (scope = 'asin' AND scope_ref = %s)
                            OR (scope = 'decision_class' AND scope_ref = %s)
                          )
                        ORDER BY
                          CASE scope
                            WHEN 'asin' THEN 0
                            WHEN 'family' THEN 1
                            WHEN 'decision_class' THEN 2
                            WHEN 'brand' THEN 3
                            ELSE 4
                          END,
                          created_at DESC
                        """,
                        (workspace_id, asin, decision_class),
                    )
                    for r in cur.fetchall():
                        out["operator_positions"].append({
                            "position_id": r[0],
                            "scope": r[1],
                            "scope_ref": r[2],
                            "claim": r[3],
                            "reasoning": r[4],
                            "position_type": r[5],
                        })
                        rows_read.append(f"operator_position#{r[0]}")

                # brand_position
                cur.execute("SELECT to_regclass('brand_position')")
                if cur.fetchone()[0]:
                    cur.execute(
                        """
                        SELECT position_statement, competitor_set,
                               competitor_role, price_band,
                               positioning_hypothesis, revision
                        FROM brand_position WHERE workspace_id = %s
                        """,
                        (workspace_id,),
                    )
                    bp = cur.fetchone()
                    if bp:
                        out["brand_position"] = {
                            "position_statement": bp[0],
                            "competitor_set": bp[1] or [],
                            "competitor_role": bp[2] or {},
                            "price_band": bp[3] or {},
                            "positioning_hypothesis": bp[4],
                            "revision": bp[5],
                        }
                        rows_read.append(f"brand_position#{workspace_id}@v{bp[5]}")
    except Exception as exc:
        logger.warning("layer_strategic read failed: %s", exc)
    return out, rows_read


def _layer_voice(workspace_id: str) -> tuple[dict, list[str]]:
    """Layer 3 — brand_profile (voice). Already shipped in BRAND_VOICE.md."""
    try:
        from .brand_voice import read_voice
        v = read_voice(workspace_id) or {}
        if not v or not v.get("ok"):
            return {}, []
        version = v.get("profile_version") or "current"
        return v, [f"brand_voice@{version}"]
    except Exception as exc:
        logger.warning("layer_voice read failed: %s", exc)
        return {}, []


def _layer_evidence(workspace_id: str, asin: Optional[str],
                    decision_class: Optional[str], lookback_days: int = 90,
                    limit: int = 50) -> tuple[dict, list[str]]:
    """Layer 4 — outcome_events filtered to cohort + decision_class metrics."""
    if not asin:
        return {"outcomes": []}, []
    pool = get_pool()
    if pool is None:
        return {"outcomes": []}, []
    rows_read: list[str] = []
    out_rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT outcome_id, asin, metric, value, observed_at,
                           period_start, period_end
                    FROM outcome_events
                    WHERE workspace_id = %s
                      AND asin = %s
                      AND observed_at > NOW() - (%s || ' days')::interval
                    ORDER BY observed_at DESC
                    LIMIT %s
                    """,
                    (workspace_id, asin, str(lookback_days), limit),
                )
                for r in cur.fetchall():
                    out_rows.append({
                        "outcome_id": r[0],
                        "asin": r[1],
                        "metric": r[2],
                        "value": float(r[3]) if r[3] is not None else None,
                        "observed_at": r[4].isoformat() if r[4] else None,
                        "period_start": r[5].isoformat() if r[5] else None,
                        "period_end": r[6].isoformat() if r[6] else None,
                    })
                    rows_read.append(f"outcome_event#{r[0]}")
    except Exception as exc:
        logger.warning("layer_evidence read failed: %s", exc)
    return {"outcomes": out_rows, "lookback_days": lookback_days}, rows_read


def _layer_calibrated_external(workspace_id: str, asin: Optional[str]
                               ) -> tuple[dict, list[str]]:
    """Layer 5 — recommendation_ingest + calibration_state.
    Tables ship in M4; safe before."""
    pool = get_pool()
    if pool is None:
        return {"recommendations": [], "source_calibrations": {}}, []
    rows_read: list[str] = []
    recs: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('recommendation_ingest')")
                if cur.fetchone()[0]:
                    where_asin = ""
                    params: list[Any] = [workspace_id]
                    if asin:
                        where_asin = "AND %s = ANY(scope_asins)"
                        params.append(asin)
                    cur.execute(
                        f"""
                        SELECT rec_id, source, source_tier, rec_type,
                               parsed_fields, status
                        FROM recommendation_ingest
                        WHERE workspace_id = %s
                          AND status NOT IN ('resolved', 'archived')
                          {where_asin}
                        ORDER BY ingested_at DESC
                        LIMIT 50
                        """,
                        tuple(params),
                    )
                    for r in cur.fetchall():
                        recs.append({
                            "rec_id": r[0],
                            "source": r[1],
                            "source_tier": r[2],
                            "rec_type": r[3],
                            "parsed_fields": r[4] or {},
                            "status": r[5],
                        })
                        rows_read.append(f"recommendation_ingest#{r[0]}")
    except Exception as exc:
        logger.warning("layer_calibrated_external read failed: %s", exc)
    return {"recommendations": recs, "source_calibrations": {}}, rows_read


def _layer_unit_economics(workspace_id: str, asin: Optional[str]
                          ) -> tuple[dict, list[str]]:
    """Layer 8 — cost_inputs + brand_overhead. Already shipped Phase D."""
    if not asin:
        return {}, []
    rows_read: list[str] = []
    try:
        from .cost_inputs import read_cost_input, read_overhead
        cost = read_cost_input(workspace_id, asin)
        overhead = read_overhead(workspace_id)
        if cost.get("revision") and cost.get("revision") > 0:
            rows_read.append(f"cost_inputs#{workspace_id}/{asin}@r{cost['revision']}")
        if overhead.get("revision") and overhead.get("revision") > 0:
            rows_read.append(f"brand_overhead#{workspace_id}@r{overhead['revision']}")
        return {"cost_inputs": cost, "brand_overhead": overhead}, rows_read
    except Exception as exc:
        logger.warning("layer_unit_economics read failed: %s", exc)
        return {}, []


def _layer_market_state(workspace_id: str, asin: Optional[str]
                        ) -> tuple[dict, list[str]]:
    """Layer 6 — placeholder until Phase 4."""
    return {"populated": False, "note": "asin_state ships Phase 4"}, []


def _layer_competitor_state(workspace_id: str, asin: Optional[str]
                            ) -> tuple[dict, list[str]]:
    """Layer 7 — placeholder; competitor_state table ships M2 with manual entry."""
    pool = get_pool()
    if pool is None:
        return {"populated": False, "snapshots": []}, []
    rows_read: list[str] = []
    snapshots: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('competitor_state')")
                if cur.fetchone()[0]:
                    # M2 ships competitor_state with `asin` column and
                    # JSONB `value` (numeric OR structured per metric).
                    cur.execute(
                        """
                        SELECT competitor_id, asin, metric, value,
                               observed_at, source
                        FROM competitor_state
                        WHERE workspace_id = %s
                        ORDER BY observed_at DESC
                        LIMIT 30
                        """,
                        (workspace_id,),
                    )
                    for r in cur.fetchall():
                        snapshots.append({
                            "competitor_id": r[0],
                            "asin": r[1],
                            "metric": r[2],
                            "value": r[3],  # JSONB — keep as-is
                            "observed_at": r[4].isoformat() if r[4] else None,
                            "source": r[5],
                        })
                        rows_read.append(
                            f"competitor_state#{r[0]}/{r[1] or '-'}/{r[2]}"
                            f"@{r[4].isoformat() if r[4] else 'na'}"
                        )
    except Exception as exc:
        logger.warning("layer_competitor_state read failed: %s", exc)
    return {"populated": bool(snapshots), "snapshots": snapshots}, rows_read


def _layer_goals(workspace_id: str) -> tuple[dict, list[str]]:
    """Layer 9 — placeholder until Phase 3."""
    return {"populated": False, "note": "goals ship Phase 3"}, []


# ---------------------------------------------------------------------------
# Unknowns + evidence-strength scoring
# ---------------------------------------------------------------------------


def _check_completeness(decision_class: str, bundle: dict) -> dict:
    """Walk required + nice_to_have for the decision class and return:
        {missing_required: [...], missing_nice: [...], evidence_strength,
         confidence_starting}
    """
    reqs = _load_requirements()
    klass = (reqs.get("classes") or {}).get(decision_class) or {}
    required = klass.get("required") or []
    nice = klass.get("nice_to_have") or []

    def _present(path: str) -> bool:
        # Path is dot-separated, e.g. asin_metadata.material
        parts = path.split(".")
        # Map top-level path to bundle layer
        head = parts[0]
        layer_map = {
            "asin_metadata": ("factual", "ground_truth_fields"),
            "brand_position": ("strategic", "brand_position"),
            "brand_voice": ("voice", None),
            "operator_positions": ("strategic", "operator_positions"),
            "outcome_events": ("evidence", "outcomes"),
            "competitor_state": ("competitor_state", "snapshots"),
            "asin_state": ("market_state", None),
            "cost_inputs": ("unit_economics", "cost_inputs"),
            "brand_overhead": ("unit_economics", "brand_overhead"),
            "calibration_state": ("calibrated_external", "source_calibrations"),
            "pricing_logic": ("strategic", "pricing_logic"),  # added when M2 ships
            "pricing_decisions": ("strategic", "pricing_decisions_history"),
            "goals": ("goals", None),
        }
        if head not in layer_map:
            return False
        layer, sub = layer_map[head]
        layer_data = bundle.get(layer) or {}
        if sub is None:
            # presence == layer non-empty
            return bool(layer_data) and layer_data.get("ok") is not False
        sub_data = layer_data.get(sub)
        if sub_data is None or sub_data == [] or sub_data == {}:
            return False
        # If a deeper field is requested, look it up under sub_data
        if len(parts) > 1 and isinstance(sub_data, dict):
            field = parts[1]
            v = sub_data.get(field)
            return v is not None and v != ""
        return True

    missing_required = [p for p in required if not _present(p)]
    missing_nice = [p for p in nice if not _present(p)]

    pen_req = float(reqs.get("penalty_per_missing_required") or 0.20)
    pen_nice = float(reqs.get("penalty_per_missing_nice") or 0.05)
    floor = float(reqs.get("confidence_floor") or 0.10)
    ceiling = float(reqs.get("confidence_ceiling") or 0.95)

    raw = ceiling - (pen_req * len(missing_required)) - (pen_nice * len(missing_nice))
    confidence = max(floor, min(ceiling, raw))

    if missing_required:
        strength = "absent" if len(missing_required) > 2 else "weak"
    elif missing_nice:
        strength = "partial"
    else:
        strength = "strong"

    return {
        "missing_required": missing_required,
        "missing_nice": missing_nice,
        "evidence_strength": strength,
        "confidence_starting": round(confidence, 3),
        "penalties_applied": {
            "per_required": pen_req,
            "per_nice": pen_nice,
            "n_required": len(missing_required),
            "n_nice": len(missing_nice),
        },
    }


def _emit_unknowns_for_missing(workspace_id: str, asin: Optional[str],
                               decision_class: str,
                               missing_required: list[str],
                               missing_nice: list[str]) -> list[str]:
    """Emit unknowns for missing required + nice fields. Returns list of
    unknown_ids created (or merged into).

    Mapping: for `asin_metadata.X`, evidence_path is 'factory_spec_sheet'
    (most factory-supplied), but common operator-typed ones (lifestyle,
    theme) route to 'operator_decision'. Conservative default:
    factory_spec_sheet for asin_metadata.*, agency_response is too narrow.
    """
    from .unknowns import emit_unknown

    AGENCY_TYPED = {"lifestyle", "theme", "sport_type", "specific_uses",
                    "fashion_decade", "part_number"}
    COST_INPUT = {"cost_inputs.landed_cost", "cost_inputs.fba_fee",
                  "cost_inputs.referral_pct", "cost_inputs.third_pl_fee"}

    out_ids: list[str] = []

    def _path_to_evidence(path: str) -> str:
        if path.startswith("asin_metadata."):
            field = path.split(".", 1)[1]
            if field in AGENCY_TYPED:
                return "agency_response"
            return "factory_spec_sheet"
        if path in COST_INPUT or path.startswith("cost_inputs"):
            return "operator_decision"
        if path.startswith("competitor_state"):
            return "operator_decision"
        if path.startswith("outcome_events"):
            return "outcome_measurement"
        if path == "brand_position" or path == "brand_voice":
            return "operator_decision"
        if path.startswith("calibration_state"):
            return "outcome_measurement"
        return "operator_decision"

    def _path_to_question(path: str) -> str:
        return f"Required input '{path}' is not on file for {decision_class}."

    # Required gets priority='high'; nice gets 'normal'
    for path in missing_required:
        uid = emit_unknown(
            workspace_id=workspace_id,
            scope="asin" if asin else "global",
            scope_ref=asin,
            question=_path_to_question(path),
            required_for=[decision_class],
            evidence_path=_path_to_evidence(path),
            priority="high",
            created_by_module="context_layer_l0",
        )
        if uid:
            out_ids.append(uid)
    for path in missing_nice:
        uid = emit_unknown(
            workspace_id=workspace_id,
            scope="asin" if asin else "global",
            scope_ref=asin,
            question=_path_to_question(path),
            required_for=[decision_class],
            evidence_path=_path_to_evidence(path),
            priority="normal",
            created_by_module="context_layer_l0",
        )
        if uid:
            out_ids.append(uid)
    return out_ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_context(
    workspace_id: str,
    asin: Optional[str],
    decision_class: str,
    *,
    operator_id: Optional[str] = None,
    include_unknowns: bool = True,
    override_layers: Optional[set[str]] = None,
    emit_unknowns_on_gaps: bool = True,
) -> dict[str, Any]:
    """Assemble the full L0 context bundle for a decision.

    Args:
        workspace_id    Brand workspace (e.g., 'novelle')
        asin            Specific ASIN, nullable for global decisions
        decision_class  e.g., 'title_generation' (must be in
                        decision_class_requirements.yml or fallback)
        operator_id     Reserved (single-operator-per-brand for now)
        include_unknowns Read open unknowns affecting this decision
        override_layers Set of layer names to skip (operator override path)
        emit_unknowns_on_gaps If True (default), missing-required fields
                              automatically emit unknowns. Set False for
                              read-only previews.

    Returns ContextBundle dict. Always returns a populated structure, even
    if substrate is unavailable (all layers empty).
    """
    override_layers = override_layers or set()
    rows_read: list[str] = []
    bundle: dict[str, Any] = {
        "workspace_id": workspace_id,
        "asin": asin,
        "decision_class": decision_class,
        "operator_id": operator_id or "devang",
    }

    # Layer 1
    if "factual" not in override_layers:
        data, rows = _layer_factual(workspace_id, asin)
        bundle["factual"] = data
        rows_read.extend(rows)
    else:
        bundle["factual"] = {"_overridden": True}

    # Layer 2
    if "strategic" not in override_layers:
        data, rows = _layer_strategic(workspace_id, asin, decision_class)
        bundle["strategic"] = data
        rows_read.extend(rows)
    else:
        bundle["strategic"] = {"_overridden": True}

    # Layer 3
    if "voice" not in override_layers:
        data, rows = _layer_voice(workspace_id)
        bundle["voice"] = data
        rows_read.extend(rows)
    else:
        bundle["voice"] = {"_overridden": True}

    # Layer 4
    if "evidence" not in override_layers:
        data, rows = _layer_evidence(workspace_id, asin, decision_class)
        bundle["evidence"] = data
        rows_read.extend(rows)
    else:
        bundle["evidence"] = {"_overridden": True}

    # Layer 5
    if "calibrated_external" not in override_layers:
        data, rows = _layer_calibrated_external(workspace_id, asin)
        bundle["calibrated_external"] = data
        rows_read.extend(rows)
    else:
        bundle["calibrated_external"] = {"_overridden": True}

    # Layer 6
    if "market_state" not in override_layers:
        data, rows = _layer_market_state(workspace_id, asin)
        bundle["market_state"] = data
        rows_read.extend(rows)
    else:
        bundle["market_state"] = {"_overridden": True}

    # Layer 7
    if "competitor_state" not in override_layers:
        data, rows = _layer_competitor_state(workspace_id, asin)
        bundle["competitor_state"] = data
        rows_read.extend(rows)
    else:
        bundle["competitor_state"] = {"_overridden": True}

    # Layer 8
    if "unit_economics" not in override_layers:
        data, rows = _layer_unit_economics(workspace_id, asin)
        bundle["unit_economics"] = data
        rows_read.extend(rows)
    else:
        bundle["unit_economics"] = {"_overridden": True}

    # Layer 9
    if "goals" not in override_layers:
        data, rows = _layer_goals(workspace_id)
        bundle["goals"] = data
        rows_read.extend(rows)
    else:
        bundle["goals"] = {"_overridden": True}

    # Completeness check + scoring
    completeness = _check_completeness(decision_class, bundle)
    bundle["completeness"] = completeness
    bundle["evidence_strength"] = completeness["evidence_strength"]
    bundle["confidence_starting"] = completeness["confidence_starting"]

    # Open unknowns affecting this decision
    if include_unknowns:
        try:
            from .unknowns import list_open_unknowns
            unk = list_open_unknowns(
                workspace_id=workspace_id,
                scope_ref=asin,
                decision_class=decision_class,
            )
            bundle["unknowns"] = unk
            rows_read.extend([f"unknown#{u['unknown_id']}" for u in unk])
        except Exception as exc:
            logger.warning("unknowns lookup failed in build_context: %s", exc)
            bundle["unknowns"] = []
    else:
        bundle["unknowns"] = []

    # Emit unknowns for missing fields (write-through)
    if emit_unknowns_on_gaps:
        try:
            new_ids = _emit_unknowns_for_missing(
                workspace_id=workspace_id,
                asin=asin,
                decision_class=decision_class,
                missing_required=completeness["missing_required"],
                missing_nice=completeness["missing_nice"],
            )
            if new_ids:
                bundle["unknowns_emitted"] = new_ids
        except Exception as exc:
            logger.warning("unknowns emission failed: %s", exc)
            bundle["unknowns_emitted"] = []
    else:
        bundle["unknowns_emitted"] = []

    bundle["context_rows_read"] = rows_read
    return bundle


__all__ = ["build_context"]
