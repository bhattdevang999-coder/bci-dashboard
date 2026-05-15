"""Tests for the judgment-detection module."""
from __future__ import annotations

import os
import tempfile
import shutil
from functools import wraps

from .judgment import (
    LOW_CONFIDENCE_THRESHOLD,
    IN_SESSION_PATTERN_MIN_EDITS,
    detect_for_session,
)
from .logger import (
    log_field_decision,
    open_session,
    stream_decisions,
    update_field_decision_with_operator_response,
)
from .schema import Module, OperatorAction, OperatorScope


def _with_temp_root(fn):
    """Run a test under fresh, isolated substrate + sessions roots."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        sub = tempfile.mkdtemp(prefix="atlas_jt_sub_")
        sess = tempfile.mkdtemp(prefix="atlas_jt_sess_")
        os.environ["ATLAS_SUBSTRATE_ROOT"] = sub
        os.environ["ATLAS_SESSIONS_ROOT"] = sess
        try:
            return fn(*args, **kwargs)
        finally:
            shutil.rmtree(sub, ignore_errors=True)
            shutil.rmtree(sess, ignore_errors=True)
    return wrapper


def _seed_edit(
    workspace_id: str,
    session_id: str,
    *,
    field_name: str,
    confidence: float,
    rules: list[dict],
    scope: OperatorScope = OperatorScope.JUST_THIS,
) -> str:
    """Helper: log a decision then an edit response. Returns event_id."""
    eid = log_field_decision(
        workspace_id=workspace_id,
        session_id=session_id,
        module=Module.NIS,
        field_name=field_name,
        atlas_output=f"generated {field_name}",
        overall_confidence=confidence,
        rules_injected=rules,
        brand_profile_version="tlg_v1",
    )
    assert eid is not None
    update_field_decision_with_operator_response(
        workspace_id=workspace_id,
        event_id=eid,
        operator_action=OperatorAction.EDIT,
        operator_value=f"edited {field_name}",
        operator_scope=scope,
        operator_time_to_decision_ms=5000,
        operator_viewed_case=True,
    )
    return eid


@_with_temp_root
def test_low_confidence_fires_when_edit_under_threshold() -> None:
    """An edit on a low-confidence field fires low_confidence."""
    s = open_session(workspace_id="tlg", operator_id="d")
    _seed_edit(
        "tlg", s.session_id,
        field_name="bullet_1",
        confidence=0.55,  # < 0.7
        rules=[{"rule_id": "nis.llm.claude_generation"}],
    )
    fired = detect_for_session("tlg", s.session_id)
    triggers = [f["trigger_type"] for f in fired]
    assert "low_confidence" in triggers, f"expected low_confidence in {triggers}"


@_with_temp_root
def test_low_confidence_does_NOT_fire_at_threshold() -> None:
    """A field at exactly 0.7 confidence is on the safe side (>=)."""
    s = open_session(workspace_id="tlg", operator_id="d")
    _seed_edit(
        "tlg", s.session_id,
        field_name="bullet_1",
        confidence=LOW_CONFIDENCE_THRESHOLD,  # = 0.7
        rules=[{"rule_id": "nis.llm.claude_generation"}],
    )
    fired = detect_for_session("tlg", s.session_id)
    triggers = [f["trigger_type"] for f in fired]
    assert "low_confidence" not in triggers, f"unexpected low_confidence: {triggers}"


@_with_temp_root
def test_rule_override_fires_only_on_real_rules() -> None:
    """rule_override fires when a non-LLM rule was injected; not for nis.llm.*."""
    s = open_session(workspace_id="tlg", operator_id="d")
    # Decision A: only LLM 'rule' \u2014 should NOT fire rule_override
    _seed_edit(
        "tlg", s.session_id,
        field_name="bullet_2",
        confidence=0.85,
        rules=[{"rule_id": "nis.llm.claude_generation"}],
    )
    # Decision B: a real brand rule \u2014 SHOULD fire rule_override
    eid_b = _seed_edit(
        "tlg", s.session_id,
        field_name="bullet_3",
        confidence=0.85,
        rules=[
            {"rule_id": "nis.llm.claude_generation"},
            {"rule_id": "nis.brand.keywords_active"},
        ],
    )
    fired = detect_for_session("tlg", s.session_id)
    overrides = [f for f in fired if f["trigger_type"] == "rule_override"]
    assert len(overrides) == 1, f"expected exactly 1 rule_override, got {len(overrides)}"
    assert overrides[0]["decision_event_id"] == eid_b


@_with_temp_root
def test_in_session_pattern_fires_at_three_edits_same_field() -> None:
    """3+ edits on the same field_name surfaces the candidate-rule signal."""
    s = open_session(workspace_id="tlg", operator_id="d")
    for i in range(IN_SESSION_PATTERN_MIN_EDITS):
        _seed_edit(
            "tlg", s.session_id,
            field_name="bullet_1",
            confidence=0.85,
            rules=[{"rule_id": "nis.llm.claude_generation"}],
        )
    # Also a single edit on a different field \u2014 should NOT fire the pattern.
    _seed_edit(
        "tlg", s.session_id,
        field_name="item_name",
        confidence=0.85,
        rules=[{"rule_id": "nis.llm.claude_generation"}],
    )
    fired = detect_for_session("tlg", s.session_id)
    patterns = [f for f in fired if f["trigger_type"] == "in_session_pattern"]
    assert len(patterns) == 1, f"expected 1 in_session_pattern, got {len(patterns)}"
    assert patterns[0]["field_name"] == "bullet_1"


@_with_temp_root
def test_in_session_pattern_does_NOT_fire_at_two_edits() -> None:
    """Threshold is 3; two edits is not enough."""
    s = open_session(workspace_id="tlg", operator_id="d")
    for _ in range(2):
        _seed_edit(
            "tlg", s.session_id,
            field_name="bullet_1",
            confidence=0.85,
            rules=[{"rule_id": "nis.llm.claude_generation"}],
        )
    fired = detect_for_session("tlg", s.session_id)
    patterns = [f for f in fired if f["trigger_type"] == "in_session_pattern"]
    assert not patterns, f"unexpected in_session_pattern firing: {patterns}"


@_with_temp_root
def test_detection_is_idempotent() -> None:
    """Re-running detection on the same session does not duplicate moments."""
    s = open_session(workspace_id="tlg", operator_id="d")
    _seed_edit(
        "tlg", s.session_id,
        field_name="bullet_1",
        confidence=0.55,
        rules=[{"rule_id": "nis.brand.keywords_active"}],
    )
    first = detect_for_session("tlg", s.session_id)
    second = detect_for_session("tlg", s.session_id)
    assert len(first) == 2  # low_confidence + rule_override
    assert second == [], f"second run should fire nothing, got {second}"

    # Verify only one moment of each trigger_type sits in the log.
    rows = list(stream_decisions("tlg"))
    moments = [r for r in rows if r.get("event_kind") == "judgment_moment_event"]
    triggers = sorted(r["trigger_type"] for r in moments)
    assert triggers == ["low_confidence", "rule_override"], triggers


@_with_temp_root
def test_detection_does_not_fire_when_no_edit() -> None:
    """No edit \u2192 no signal. Accept-without-view is not enough to fire a moment."""
    s = open_session(workspace_id="tlg", operator_id="d")
    eid = log_field_decision(
        workspace_id="tlg",
        session_id=s.session_id,
        module=Module.NIS,
        field_name="bullet_1",
        atlas_output="anything",
        overall_confidence=0.55,
        rules_injected=[{"rule_id": "nis.brand.keywords_active"}],
        brand_profile_version="tlg_v1",
    )
    update_field_decision_with_operator_response(
        workspace_id="tlg",
        event_id=eid,
        operator_action=OperatorAction.ACCEPT,
        operator_time_to_decision_ms=400,
        operator_viewed_case=False,
    )
    fired = detect_for_session("tlg", s.session_id)
    assert fired == [], f"accept-only should not fire, got {fired}"


@_with_temp_root
def test_detection_isolated_per_session() -> None:
    """Edits in session A must not fire moments attached to session B."""
    s_a = open_session(workspace_id="tlg", operator_id="d")
    s_b = open_session(workspace_id="tlg", operator_id="d")
    _seed_edit(
        "tlg", s_a.session_id,
        field_name="bullet_1", confidence=0.55,
        rules=[{"rule_id": "nis.brand.keywords_active"}],
    )
    fired_b = detect_for_session("tlg", s_b.session_id)
    assert fired_b == [], f"session B should fire nothing, got {fired_b}"

    fired_a = detect_for_session("tlg", s_a.session_id)
    triggers_a = sorted(f["trigger_type"] for f in fired_a)
    assert triggers_a == ["low_confidence", "rule_override"], triggers_a


if __name__ == "__main__":
    tests = [
        test_low_confidence_fires_when_edit_under_threshold,
        test_low_confidence_does_NOT_fire_at_threshold,
        test_rule_override_fires_only_on_real_rules,
        test_in_session_pattern_fires_at_three_edits_same_field,
        test_in_session_pattern_does_NOT_fire_at_two_edits,
        test_detection_is_idempotent,
        test_detection_does_not_fire_when_no_edit,
        test_detection_isolated_per_session,
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"all {len(tests)} judgment tests passed")
