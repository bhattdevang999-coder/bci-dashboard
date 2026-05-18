"""Atlas substrate — 5-layer cited generation.

Implementation of CITATION_CHAIN.md. Wraps an LLM call with the
context-injection prompt template. Output is structured (JSON) and
includes citations referencing substrate row IDs read from L0.

Lenient verifier mode (per operator's call 2026-05-18):
  - If a cited row_id doesn't appear in context_rows_read, the
    citation is tagged `verifier_status='weak'` and rendered yellow
    in the UI. Generation does NOT block.
  - If the row_id exists but the rationale's relationship to the
    actual content is unclear, no second LLM call is made yet —
    we'll add the verifier-LLM check in a later milestone if it
    proves necessary.

Substrate writes:
  - One substrate_events row per generation, with citations + the
    full reasoning chain in JSONB columns added in schema v6.

Never raises. Best-effort. Returns a structured response even if
the LLM call fails (returns a stub with reasoning='LLM unavailable').
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .context import build_context
from .db import get_pool

logger = logging.getLogger("atlas.substrate.citation_chain")


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


_DECISION_CLASS_INSTRUCTIONS = {
    "title_generation": (
        "Generate ONE primary Amazon listing TITLE plus TWO alternates. "
        "Title char limit 200, target 150-180. Must include facts from "
        "Layer 1. Must NOT include any banned phrase from Layer 3. "
        "Must align with positioning from Layer 2."
    ),
    "bullet_generation": (
        "Generate FIVE Amazon listing bullet points. Each bullet should "
        "lead with a benefit and be supported by a specific fact from "
        "Layer 1. Voice constraints from Layer 3 are absolute. Each "
        "bullet 150-250 chars."
    ),
    "description_generation": (
        "Generate an Amazon listing DESCRIPTION (one paragraph, "
        "200-400 chars). Must reflect positioning from Layer 2 and "
        "voice from Layer 3."
    ),
}


def _render_layer(name: str, layer_data: Any, max_chars: int = 2400) -> str:
    """Render a layer to a compact string for prompt injection."""
    if not layer_data:
        return f"{name}: (empty)"
    try:
        s = json.dumps(layer_data, default=str, indent=2, ensure_ascii=False)
    except Exception:
        s = str(layer_data)
    if len(s) > max_chars:
        s = s[:max_chars] + "\n  ...(truncated)"
    return f"{name}:\n{s}"


def _render_unknowns(unknowns: list[dict]) -> str:
    if not unknowns:
        return "(no open unknowns affecting this decision)"
    lines = []
    for u in unknowns[:30]:
        lines.append(
            f"  - [{u.get('priority', 'normal')}] "
            f"{u.get('question')} "
            f"(evidence_path: {u.get('evidence_path')}, "
            f"unknown_id: {u.get('unknown_id')})"
        )
    return "\n".join(lines)


def build_cited_prompt(bundle: dict, decision_class: str) -> str:
    """Build the citation-chain prompt from an L0 bundle."""
    instr = _DECISION_CLASS_INSTRUCTIONS.get(
        decision_class,
        f"Generate a {decision_class} output. Cite substrate rows for "
        "every choice. Output strict JSON with primary, alternates, "
        "citations, confidence_self_reported, confidence_breakdown, "
        "open_unknowns_referenced, convention_flags."
    )

    parts: list[str] = []
    parts.append(
        "You are Atlas, an Amazon listing reasoning system for the "
        f"brand. You are working on workspace='{bundle.get('workspace_id')}', "
        f"asin='{bundle.get('asin')}', decision_class='{decision_class}'."
    )
    parts.append("")
    parts.append("=" * 72)
    parts.append("CONTEXT (assembled from substrate)")
    parts.append("=" * 72)
    parts.append("")
    for layer_name in ("factual", "strategic", "voice", "evidence",
                       "calibrated_external", "market_state",
                       "competitor_state", "unit_economics", "goals"):
        parts.append(_render_layer(f"LAYER — {layer_name}",
                                    bundle.get(layer_name) or {}))
        parts.append("")

    parts.append("=" * 72)
    parts.append("OPEN UNKNOWNS BLOCKING FULL CONFIDENCE")
    parts.append("=" * 72)
    parts.append(_render_unknowns(bundle.get("unknowns") or []))
    parts.append("")

    parts.append("=" * 72)
    parts.append("YOUR JOB")
    parts.append("=" * 72)
    parts.append(instr)
    parts.append("")
    parts.append(
        "CITATION REQUIREMENT: For every word/phrase choice in your "
        "output, cite the substrate row(s) that drove it. Valid layers: "
        "factual | strategic | voice | evidence | calibrated_external | "
        "convention. If you must use a phrase that has no substrate "
        "basis (e.g., common Amazon listing convention), mark it as "
        "'convention' — that's a flag for operator review, not "
        "forbidden."
    )
    parts.append("")
    parts.append(
        "CONFIDENCE: Self-report 0.0-1.0 for each: "
        "voice_compliance, factual_accuracy, positioning_match, "
        "evidence_grounding, convention_share (lower convention_share "
        "means more substrate-grounded; treat as a positive signal "
        "when low)."
    )
    parts.append("")
    parts.append("HARD CONSTRAINTS (overriding LLM judgment):")
    parts.append(
        "  - Banned phrases from voice layer are absolute. Never use them."
    )
    parts.append(
        "  - Hard-refusal operator_positions are absolute. Never violate."
    )
    parts.append("")
    parts.append("=" * 72)
    parts.append("OUTPUT FORMAT — STRICT JSON ONLY (no surrounding prose)")
    parts.append("=" * 72)
    parts.append(json.dumps({
        "primary": "<text>",
        "alternates": ["<alt1>", "<alt2>"],
        "citations": [
            {
                "claim": "<short description of what this citation supports>",
                "layer": "factual|strategic|voice|evidence|calibrated_external|convention",
                "source_row_ids": ["<row_id from context_rows_read>"],
                "rationale": "<one sentence>"
            }
        ],
        "confidence_self_reported": 0.0,
        "confidence_breakdown": {
            "voice_compliance": 0.0,
            "factual_accuracy": 0.0,
            "positioning_match": 0.0,
            "evidence_grounding": 0.0,
            "convention_share": 0.0
        },
        "open_unknowns_referenced": ["<unknown_id>"],
        "convention_flags": [
            {"claim": "<text>", "rationale": "<why this is convention>"}
        ]
    }, indent=2))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM call + parsing
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> Optional[dict]:
    """Lenient JSON extraction. LLMs sometimes wrap in code fences."""
    if not text:
        return None
    # Strip markdown fences
    s = text.strip()
    if s.startswith("```"):
        # Strip first line and trailing fence
        lines = s.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    # Find first { and last }
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        return json.loads(s[first:last + 1])
    except Exception:
        return None


def _call_llm(prompt: str, *, model: str = "claude-sonnet-4-5",
              max_tokens: int = 2000) -> Optional[str]:
    """Call Anthropic. Best-effort. Returns response text or None."""
    try:
        from anthropic import Anthropic
        client = Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # Concat text blocks
        chunks: list[str] = []
        for block in msg.content:
            if hasattr(block, "text"):
                chunks.append(block.text)
        return "".join(chunks).strip() or None
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Citation verifier (lenient)
# ---------------------------------------------------------------------------


def verify_citations(citations: list[dict], rows_read: list[str]
                     ) -> list[dict]:
    """Mark each citation with verifier_status.

    'verified'   — every source_row_id is present in rows_read
    'weak'       — at least one source_row_id missing from rows_read
    'convention' — citation is layer='convention', no row check needed
    """
    rows_set = set(rows_read or [])
    out: list[dict] = []
    for c in citations or []:
        layer = c.get("layer")
        row_ids = c.get("source_row_ids") or []
        if layer == "convention" or not row_ids:
            c2 = dict(c)
            c2["verifier_status"] = "convention" if layer == "convention" else "weak"
            out.append(c2)
            continue
        missing = [r for r in row_ids if r not in rows_set]
        c2 = dict(c)
        if not missing:
            c2["verifier_status"] = "verified"
        else:
            c2["verifier_status"] = "weak"
            c2["verifier_missing_rows"] = missing
        out.append(c2)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_cited(
    workspace_id: str,
    asin: Optional[str],
    decision_class: str,
    *,
    operator_id: Optional[str] = None,
    log_decision: bool = True,
) -> dict[str, Any]:
    """Generate cited content for a decision class.

    Returns dict with:
      - bundle               (the L0 context used)
      - prompt               (full prompt sent to LLM)
      - llm_raw              (raw response text)
      - parsed               (parsed JSON or None)
      - citations            (verified list)
      - confidence_starting  (from L0 completeness)
      - confidence_self_reported (from LLM)
      - confidence_final     (mean of starting + self_reported, capped)
      - decision_event_id    (substrate_events row, if logged)
    """
    bundle = build_context(
        workspace_id=workspace_id,
        asin=asin,
        decision_class=decision_class,
        operator_id=operator_id,
        emit_unknowns_on_gaps=True,
    )

    prompt = build_cited_prompt(bundle, decision_class)
    raw = _call_llm(prompt)
    parsed = _extract_json(raw) if raw else None

    citations: list[dict] = []
    confidence_self = None
    confidence_breakdown: dict[str, Any] = {}
    convention_flags: list[dict] = []
    primary = None
    alternates: list[str] = []

    if parsed:
        citations = verify_citations(
            parsed.get("citations") or [],
            bundle.get("context_rows_read") or [],
        )
        confidence_self = parsed.get("confidence_self_reported")
        confidence_breakdown = parsed.get("confidence_breakdown") or {}
        convention_flags = parsed.get("convention_flags") or []
        primary = parsed.get("primary")
        alternates = parsed.get("alternates") or []

    confidence_starting = bundle.get("confidence_starting") or 0.0
    if confidence_self is None:
        confidence_final = confidence_starting
    else:
        try:
            cs = float(confidence_self)
            confidence_final = round(min(0.95, (cs + confidence_starting) / 2), 3)
        except Exception:
            confidence_final = confidence_starting

    # Compute used vs read
    rows_read = bundle.get("context_rows_read") or []
    used: set[str] = set()
    for c in citations:
        for rid in c.get("source_row_ids") or []:
            used.add(rid)
    rows_used = [r for r in rows_read if r in used]

    decision_event_id = None
    if log_decision:
        decision_event_id = _log_cited_decision(
            workspace_id=workspace_id,
            asin=asin,
            decision_class=decision_class,
            operator_id=operator_id,
            bundle_summary={
                "evidence_strength": bundle.get("evidence_strength"),
                "missing_required": bundle.get("completeness", {}).get("missing_required"),
                "missing_nice": bundle.get("completeness", {}).get("missing_nice"),
                "unknowns_at_decision": [
                    u["unknown_id"] for u in (bundle.get("unknowns") or [])
                ],
            },
            citations=citations,
            confidence_breakdown=confidence_breakdown,
            convention_flags=convention_flags,
            rows_read=rows_read,
            rows_used=rows_used,
            primary=primary,
            alternates=alternates,
            llm_raw=raw,
        )

    return {
        "bundle_summary": {
            "evidence_strength": bundle.get("evidence_strength"),
            "confidence_starting": confidence_starting,
            "missing_required": bundle.get("completeness", {}).get("missing_required") or [],
            "missing_nice": bundle.get("completeness", {}).get("missing_nice") or [],
            "unknowns_count": len(bundle.get("unknowns") or []),
            "context_rows_read": rows_read,
            "context_rows_used": rows_used,
        },
        "primary": primary,
        "alternates": alternates,
        "citations": citations,
        "confidence_starting": confidence_starting,
        "confidence_self_reported": confidence_self,
        "confidence_breakdown": confidence_breakdown,
        "confidence_final": confidence_final,
        "convention_flags": convention_flags,
        "open_unknowns_referenced": (parsed or {}).get("open_unknowns_referenced") or [],
        "llm_raw": raw,
        "llm_failed": raw is None,
        "decision_event_id": decision_event_id,
    }


def _log_cited_decision(*, workspace_id, asin, decision_class, operator_id,
                        bundle_summary, citations, confidence_breakdown,
                        convention_flags, rows_read, rows_used,
                        primary, alternates, llm_raw) -> Optional[str]:
    """Write a substrate_events row capturing the full decision provenance."""
    pool = get_pool()
    if pool is None:
        return None
    event_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO substrate_events (
                        event_id, event_kind, workspace_id, session_id,
                        operator_id, timestamp, module, field_name,
                        atlas_output, overall_confidence,
                        rules_injected, brand_profile_version,
                        context_rows_read, context_rows_used,
                        evidence_strength, calibration_class,
                        citations, citation_outcomes, confidence_breakdown,
                        convention_flags, meta, pre_change_snapshot
                    ) VALUES (
                        %s::uuid, 'decision_event', %s, %s,
                        %s, NOW(), 'nis', %s,
                        %s::jsonb, %s,
                        %s::jsonb, %s,
                        %s, %s,
                        %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb, %s::jsonb
                    )
                    """,
                    (
                        event_id, workspace_id, None,
                        operator_id or "devang", decision_class,
                        json.dumps({
                            "primary": primary,
                            "alternates": alternates,
                            "asin": asin,
                        }),
                        None,  # overall_confidence (computed consumer-side)
                        json.dumps([]), None,  # rules_injected jsonb, brand_profile_version
                        rows_read, rows_used,
                        bundle_summary.get("evidence_strength"),
                        decision_class,
                        json.dumps(citations),
                        json.dumps([]),  # citation_outcomes set on operator decision
                        json.dumps(confidence_breakdown),
                        json.dumps(convention_flags),
                        json.dumps({
                            "bundle_summary": bundle_summary,
                            "llm_raw_len": len(llm_raw) if llm_raw else 0,
                            "asin": asin,
                        }),
                        json.dumps({}),
                    ),
                )
            conn.commit()
        return event_id
    except Exception as exc:
        logger.warning("_log_cited_decision write failed: %s", exc)
        return None


__all__ = [
    "build_cited_prompt",
    "verify_citations",
    "generate_cited",
]
