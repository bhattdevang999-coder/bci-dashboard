"""Atlas judgment detection — Step 5, first 3 of 6 signals.

This module reads decision_event + operator_response rows from the substrate
and fires judgment_moment_events when one of the detection rules triggers.

Signals shipped here (the bottom three of the six-signal spec):

  low_confidence      — operator edited a field whose generation confidence
                        was < 0.7. Direct signal the confidence model is
                        right to be uncertain.

  rule_override       — operator edited a field where Atlas had injected a
                        non-trivial brand rule. The rule is wrong, stale, or
                        miscoded; surface for the rule library QA loop.

  in_session_pattern  — operator edited the SAME field type 3+ times in one
                        session, suggesting a brand-wide pattern Atlas
                        should learn. This is the only signal here that can
                        seed a *new* candidate rule.

Design notes:

  - Detection is best-effort and never blocks writes. A bad detector must
    not corrupt the substrate; if a signal raises we swallow and continue.
  - The detector is stateless from the caller's perspective. It reads the
    log for a workspace/session and emits judgment_moment_events; the
    review UI is responsible for surfacing them.
  - Deduplication: each (decision_event_id, trigger_type) pair fires at
    most once per session. We check the existing log before emitting.

Deferred to Step 6 (after one real batch of feedback informs the spec):
  - confidence_mismatch: 2nd-derivative signal that needs a real
    probability, not the v1.0.0 proxy.
  - brand_drift: requires brand profile diffs across sessions.
  - proposed_rule_response: requires the rule proposal surface in the UI.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .logger import log_judgment_moment, stream_decisions
from .schema import TriggerType


# Threshold for low_confidence detection. Matches the substrate spec.
LOW_CONFIDENCE_THRESHOLD = 0.7

# Threshold for in_session_pattern detection. The spec calls for 3+ edits
# on the same field type within a single session to generate a candidate
# rule. Anything lower fires too often on small batches.
IN_SESSION_PATTERN_MIN_EDITS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_session(
    workspace_id: str,
    session_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], set[tuple[str, str]]]:
    """Index a session's substrate rows into:

      decisions:        event_id -> decision_event row
      responses_by_eid: event_id -> [operator_response rows in order]
      moments:          set of (decision_event_id, trigger_type) already fired

    Anything outside the requested session is ignored.
    """
    decisions: dict[str, dict[str, Any]] = {}
    responses_by_eid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    moments: set[tuple[str, str]] = set()

    for row in stream_decisions(workspace_id):
        kind = row.get("event_kind")
        if kind == "decision_event" and row.get("session_id") == session_id:
            decisions[row["event_id"]] = row
        elif kind == "operator_response":
            link = row.get("links_to_event_id")
            if link in decisions:
                responses_by_eid[link].append(row)
        elif kind == "judgment_moment_event" and row.get("session_id") == session_id:
            t = row.get("trigger_type")
            d = row.get("decision_event_id")
            if t and d:
                moments.add((d, t))
    return decisions, responses_by_eid, moments


def _latest_edit(responses: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest 'edit' response in a list, if any.

    A field can be edited, then scope-upgraded (a second edit row with a
    different scope). We want the latest \u2014 it carries the operator's final
    intent.
    """
    edits = [r for r in responses if r.get("operator_action") == "edit"]
    if not edits:
        return None
    return edits[-1]


# ---------------------------------------------------------------------------
# Signal detectors
# ---------------------------------------------------------------------------


def _detect_low_confidence(
    decision: dict[str, Any],
    edit: dict[str, Any],
) -> bool:
    """Fire when operator edited a field generated below the confidence floor."""
    conf = decision.get("overall_confidence")
    if conf is None:
        return False
    return float(conf) < LOW_CONFIDENCE_THRESHOLD


def _detect_rule_override(
    decision: dict[str, Any],
    edit: dict[str, Any],
) -> bool:
    """Fire when operator edited a field that had >=1 non-LLM rule injected.

    'nis.llm.*' rule_ids are excluded because they mean 'the LLM produced
    this output' \u2014 they're not brand rules the operator can override. The
    signal is meant for the brand-rule QA loop.
    """
    rules = decision.get("rules_injected") or []
    for r in rules:
        rid = r.get("rule_id", "")
        if rid and not rid.startswith("nis.llm."):
            return True
    return False


def _detect_in_session_pattern(
    field_name: str,
    edited_decisions: list[dict[str, Any]],
) -> bool:
    """Fire when the same field_name has been edited IN_SESSION_PATTERN_MIN_EDITS+ times.

    edited_decisions is the list of decisions for this field that had at
    least one edit response. We pass the count rather than recomputing it
    inside so the caller can cheaply check before invoking the detector.
    """
    return len(edited_decisions) >= IN_SESSION_PATTERN_MIN_EDITS


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_for_session(workspace_id: str, session_id: str) -> list[dict[str, Any]]:
    """Run all 3 detectors over a session and write any new judgment moments.

    Returns a list of {trigger_type, decision_event_id, moment_id, field_name}
    dicts describing what was fired this run. Idempotent: re-running on the
    same session does not produce duplicate moments because we check the
    existing log first.
    """
    decisions, responses_by_eid, fired = _index_session(workspace_id, session_id)

    fired_now: list[dict[str, Any]] = []

    # Pre-bucket edited decisions by field_name so in_session_pattern is one pass.
    edited_by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edit_response_by_eid: dict[str, dict[str, Any]] = {}
    for eid, decision in decisions.items():
        edit = _latest_edit(responses_by_eid.get(eid, []))
        if not edit:
            continue
        edit_response_by_eid[eid] = edit
        edited_by_field[decision.get("field_name", "")].append(decision)

    def _emit(decision_event_id: str, trigger: TriggerType, field_name: str) -> None:
        key = (decision_event_id, trigger.value)
        if key in fired:
            return  # dedupe \u2014 already in the log
        try:
            moment_id = log_judgment_moment(
                workspace_id=workspace_id,
                decision_event_id=decision_event_id,
                trigger_type=trigger,
                session_id=session_id,
            )
        except Exception as exc:
            print(f"[judgment] emit failed for {trigger.value}: {exc}", flush=True)
            return
        fired.add(key)
        fired_now.append({
            "trigger_type": trigger.value,
            "decision_event_id": decision_event_id,
            "moment_id": moment_id,
            "field_name": field_name,
        })

    # 1) Per-decision detectors (low_confidence, rule_override)
    for eid, decision in decisions.items():
        edit = edit_response_by_eid.get(eid)
        if not edit:
            continue
        field_name = decision.get("field_name", "")

        if _detect_low_confidence(decision, edit):
            _emit(eid, TriggerType.LOW_CONFIDENCE, field_name)

        if _detect_rule_override(decision, edit):
            _emit(eid, TriggerType.RULE_OVERRIDE, field_name)

    # 2) Cross-decision detector (in_session_pattern)
    for field_name, edited in edited_by_field.items():
        if not _detect_in_session_pattern(field_name, edited):
            continue
        # Attach the moment to the MOST RECENT edited decision so the UI has
        # a natural row to surface the candidate-rule prompt against.
        latest = edited[-1]
        _emit(latest["event_id"], TriggerType.IN_SESSION_PATTERN, field_name)

    return fired_now


__all__ = [
    "LOW_CONFIDENCE_THRESHOLD",
    "IN_SESSION_PATTERN_MIN_EDITS",
    "detect_for_session",
]
