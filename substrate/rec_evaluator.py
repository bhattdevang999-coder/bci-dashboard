"""Atlas substrate — recommendation evaluator.

Two LLM passes, both best-effort and degrade-gracefully when the
client is unavailable:

  parse_raw_text(raw_text, hint)
    Extracts a structured {field_name: submitted_value} dict from a
    raw paste/PDF-extracted recommendation. Returns {} on any failure.

  evaluate_recommendation(rec, ctx)
    Runs the field-ownership + verdict pass per RECOMMENDATION_INGEST.md.
    Returns a list of evaluation dicts ready to feed into
    atlas_evaluation.create_evaluation().

Heuristic fallbacks are explicit and labeled so the operator can see
when Atlas was running degraded.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("atlas.substrate.rec_evaluator")


# Heuristic field-owner map for common Amazon backend / agency fields.
# Used when the LLM is unavailable or returns an invalid owner.
HEURISTIC_OWNERS: dict[str, str] = {
    # manufacturer-owned
    "material": "manufacturer",
    "fabric_type": "manufacturer",
    "fabric_gsm": "manufacturer",
    "weave_type": "manufacturer",
    "rise_height_inches": "manufacturer",
    "lining_description": "manufacturer",
    "country_of_origin": "manufacturer",
    "care_instructions": "manufacturer",
    "care_temp": "manufacturer",
    "care_dry_method": "manufacturer",
    "upf": "manufacturer",
    "pocket_description": "manufacturer",
    "pocket_count": "manufacturer",
    "pocket_type": "manufacturer",
    # amazon taxonomy
    "sport_type": "amazon_taxonomy",
    "theme": "amazon_taxonomy",
    "pattern": "amazon_taxonomy",
    "color_map": "amazon_taxonomy",
    "fashion_decade": "amazon_taxonomy",
    "lifestyle": "amazon_taxonomy",
    # agency-owned by convention
    "part_number": "agency",
    "specific_uses": "agency",
    "special_features": "agency",
    "embellishment_feature": "agency",
    "league_name": "agency",
    "team_name": "agency",
    # operator strategic
    "price": "operator_strategic",
    "map": "operator_strategic",
    "msrp": "operator_strategic",
    "brand": "operator_strategic",
}


def _claude_client():
    """Resolve the existing Anthropic client lazily from app.py."""
    try:
        import app  # type: ignore
        return getattr(app, "_anthropic_client", None)
    except Exception:
        return None


def parse_raw_text(
    raw_text: str,
    *,
    hint_fields: Optional[list[str]] = None,
    max_tokens: int = 1500,
) -> dict[str, Any]:
    """Best-effort field extraction from a raw recommendation paste.

    `hint_fields` (when provided) biases the LLM toward extracting those
    specific names — useful when the operator pre-tagged the rec_type
    and we know which fields matter.

    Returns {field_name: submitted_value}. {} on any failure.
    """
    if not raw_text or not raw_text.strip():
        return {}
    client = _claude_client()
    if client is None:
        # Heuristic fallback: scan for "key: value" lines.
        return _heuristic_parse(raw_text)

    hint_text = ""
    if hint_fields:
        hint_text = (
            "Bias extraction toward these field names if present: "
            + ", ".join(hint_fields[:30]) + ".\n\n"
        )

    prompt = (
        "You are extracting structured fields from an external "
        "recommendation (agency document, vendor tool output, or "
        "operator note).\n\n"
        + hint_text
        + "Return a JSON object mapping snake_case field_name to the "
        "submitted value as a plain string. Skip narrative or "
        "boilerplate. If the document submits multiple values for the "
        "same field, pick the most explicit. No commentary; JSON only.\n\n"
        "Document:\n\n"
        + raw_text[:8000]
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (message.content[0].text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}
        return {
            str(k).strip(): str(v).strip() if v is not None else None
            for k, v in parsed.items()
            if k
        }
    except Exception as exc:
        logger.warning("parse_raw_text LLM failed: %s; falling back", exc)
        return _heuristic_parse(raw_text)


def _heuristic_parse(raw_text: str) -> dict[str, Any]:
    """Fallback: scan for 'Key: value' lines."""
    out: dict[str, Any] = {}
    for line in raw_text.splitlines():
        m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 _\-/]{1,40}):\s*(.+?)\s*$", line)
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        val = m.group(2).strip().strip(",;.")
        if val:
            out[key] = val
    return out


def _heuristic_owner(field_name: str) -> str:
    """Map a parsed field name to an owner. Default 'ambiguous'."""
    key = field_name.strip().lower()
    if key in HEURISTIC_OWNERS:
        return HEURISTIC_OWNERS[key]
    # rough substring fallback
    for known, owner in HEURISTIC_OWNERS.items():
        if known in key:
            return owner
    return "ambiguous"


def evaluate_recommendation(
    parsed_fields: dict[str, Any],
    *,
    workspace_id: str,
    source: str,
    source_tier: Optional[str] = None,
    scope_asins: Optional[list[str]] = None,
    brand_position: Optional[dict[str, Any]] = None,
    max_tokens: int = 3000,
) -> list[dict[str, Any]]:
    """Run the verdict pass per RECOMMENDATION_INGEST.md.

    Returns a list of dicts with keys:
      field_name, submitted_value, field_owner, verdict, reasoning,
      citations, proposed_alternative, test_design, evidence_path,
      confidence, criticality.

    On LLM failure, returns heuristic verdicts with field_owner from
    HEURISTIC_OWNERS, verdict='unknown', criticality='normal'. The
    operator still sees the ingest, just without an Atlas verdict
    chain. They can re-run later.
    """
    if not parsed_fields:
        return []

    client = _claude_client()
    if client is None:
        return _heuristic_evaluate(parsed_fields)

    # Compose a tight context block. We deliberately do NOT dump the
    # full L0 bundle here — verdicts on agency PDFs don't need every
    # voice descriptor. Brand position is the strategic anchor.
    ctx_lines = [
        f"Workspace: {workspace_id}",
        f"Source: {source}"
        + (f" (tier: {source_tier})" if source_tier else ""),
    ]
    if scope_asins:
        ctx_lines.append(
            "Scope ASINs: " + ", ".join(scope_asins[:10])
        )
    if brand_position:
        ctx_lines.append(
            "Brand position: "
            + str(brand_position.get("position_statement") or "?")
        )
        comp_role = brand_position.get("competitor_role") or {}
        if comp_role:
            ctx_lines.append(
                "Competitor frame: "
                + ", ".join(f"{k}={v}" for k, v in comp_role.items())
            )

    field_lines = "\n".join(
        f"  - {k}: {v}" for k, v in parsed_fields.items()
    )

    prompt = f"""You are evaluating an external recommendation against
Novelle's substrate.

CONTEXT
{chr(10).join(ctx_lines)}

INCOMING RECOMMENDATION ({len(parsed_fields)} fields)
{field_lines}

For EACH field above, emit one JSON object with these keys:
  field_name           verbatim from the list above
  submitted_value      verbatim from the list above
  field_owner          one of: manufacturer | agency | amazon_taxonomy
                       | operator_strategic | atlas_calibrated | ambiguous
  verdict              one of: agree | partial | disagree | unknown
  reasoning            1-3 sentences citing the substrate primitive that
                       grounds your call (e.g., 'brand_position competitor
                       frame includes CRZ as direct_competitor; agency's
                       Lifestyle=Casual conflicts with operator_position
                       Athletic-only.')
  proposed_alternative when verdict is 'disagree' or 'partial', the
                       value Atlas would prefer; else null
  test_design          when verdict is 'partial' and the gap is testable,
                       a one-line A/B test design; else null
  evidence_path        when verdict is 'unknown', one of:
                       factory_spec_sheet | agency_response |
                       helium10_weekly | a_b_test | outcome_measurement |
                       operator_decision | declared_unknowable
  confidence           0.0-1.0
  criticality          launch_blocking | high | normal | low

Be honest about uncertainty. If you don't have substrate to ground a
verdict, return 'unknown' and route via evidence_path; do not invent
confidence.

Return a JSON array, no prose, no markdown fences."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (message.content[0].text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return _heuristic_evaluate(parsed_fields)
        out: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            fname = item.get("field_name")
            if not fname or fname not in parsed_fields:
                continue
            out.append({
                "field_name": fname,
                "submitted_value":
                    item.get("submitted_value")
                    or parsed_fields.get(fname),
                "field_owner":
                    item.get("field_owner") or _heuristic_owner(fname),
                "verdict": item.get("verdict") or "unknown",
                "reasoning":
                    item.get("reasoning") or "no reasoning supplied",
                "citations": item.get("citations") or [],
                "proposed_alternative": item.get("proposed_alternative"),
                "test_design": item.get("test_design"),
                "evidence_path": item.get("evidence_path"),
                "confidence":
                    _as_float(item.get("confidence")),
                "criticality":
                    item.get("criticality") or "normal",
            })
        return out
    except Exception as exc:
        logger.warning(
            "evaluate_recommendation LLM failed: %s; falling back", exc
        )
        return _heuristic_evaluate(parsed_fields)


def _heuristic_evaluate(
    parsed_fields: dict[str, Any],
) -> list[dict[str, Any]]:
    """Degraded path: emit 'unknown' verdicts with heuristic ownership."""
    out: list[dict[str, Any]] = []
    for fname, value in parsed_fields.items():
        owner = _heuristic_owner(fname)
        path = {
            "manufacturer": "factory_spec_sheet",
            "agency": "agency_response",
            "amazon_taxonomy": "operator_decision",
            "operator_strategic": "operator_decision",
            "atlas_calibrated": "outcome_measurement",
            "ambiguous": "operator_decision",
        }.get(owner, "operator_decision")
        out.append({
            "field_name": fname,
            "submitted_value":
                str(value) if value is not None else None,
            "field_owner": owner,
            "verdict": "unknown",
            "reasoning":
                "Atlas LLM unavailable; routed by heuristic owner. "
                "Operator should re-run evaluation or answer directly.",
            "citations": [],
            "proposed_alternative": None,
            "test_design": None,
            "evidence_path": path,
            "confidence": None,
            "criticality": "normal",
        })
    return out


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return None


__all__ = [
    "parse_raw_text",
    "evaluate_recommendation",
    "HEURISTIC_OWNERS",
]
