"""Sanity tests for the Atlas decision logger.

Verifies:
- Filter rule produces the expected log/skip decisions
- log_field_decision writes a JSONL line and returns an event_id
- Strategic fields always log, non-strategic high-confidence single-rule fields skip
- Session open/submit round-trip works end-to-end
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from substrate.logger import (
    CONFIDENCE_LOG_THRESHOLD,
    STRATEGIC_FIELDS,
    log_field_decision,
    log_judgment_moment,
    open_session,
    read_session,
    should_log_field,
    stream_decisions,
    submit_session,
    update_field_decision_with_operator_response,
)
from substrate.schema import Module, OperatorAction, OperatorScope, TriggerType


def _with_temp_root(test_fn):
    def wrapper():
        tmp = tempfile.mkdtemp(prefix="atlas_substrate_test_")
        sess_tmp = tempfile.mkdtemp(prefix="atlas_sessions_test_")
        os.environ["ATLAS_SUBSTRATE_ROOT"] = tmp
        os.environ["ATLAS_SESSIONS_ROOT"] = sess_tmp
        # When running against Postgres, wipe substrate tables between tests.
        # No-op when DATABASE_URL is unset (pure JSONL run).
        try:
            from substrate.db import wipe_substrate_for_tests
            wipe_substrate_for_tests()
        except Exception:
            pass
        try:
            test_fn()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(sess_tmp, ignore_errors=True)
            del os.environ["ATLAS_SUBSTRATE_ROOT"]
            del os.environ["ATLAS_SESSIONS_ROOT"]

    wrapper.__name__ = test_fn.__name__
    return wrapper


def test_filter_strategic_always_logs() -> None:
    assert should_log_field(
        field_name="item_name",  # in STRATEGIC_FIELDS
        overall_confidence=0.99,
        rules_injected=[{"rule_id": "x"}],
    ) is True


def test_filter_nonstrategic_high_confidence_single_rule_skips() -> None:
    assert should_log_field(
        field_name="some_internal_helper_field",
        overall_confidence=0.95,
        rules_injected=[{"rule_id": "x"}],
    ) is False


def test_filter_low_confidence_logs() -> None:
    assert should_log_field(
        field_name="some_internal_helper_field",
        overall_confidence=0.5,
        rules_injected=[{"rule_id": "x"}],
    ) is True


def test_filter_multiple_rules_logs() -> None:
    assert should_log_field(
        field_name="some_internal_helper_field",
        overall_confidence=0.99,
        rules_injected=[{"rule_id": "x"}, {"rule_id": "y"}],
    ) is True


def test_filter_operator_acted_always_logs() -> None:
    assert should_log_field(
        field_name="some_internal_helper_field",
        overall_confidence=0.99,
        rules_injected=[{"rule_id": "x"}],
        operator_acted=True,
    ) is True


@_with_temp_root
def test_log_strategic_field_writes_event() -> None:
    eid = log_field_decision(
        workspace_id="tlg",
        session_id="ses_test_1",
        module=Module.NIS,
        field_name="item_name",
        atlas_output="Modern Stretch Twill Casual Jacket",
        overall_confidence=0.82,
        rules_injected=[{"rule_id": "amazon.title.max_chars", "version": "2025.01"}],
        brand_profile_version="tlg_v1",
        style_id="TLG-J-001",
        decision_number=15,
    )
    assert eid is not None
    events = list(stream_decisions("tlg"))
    assert len(events) == 1
    assert events[0]["event_id"] == eid
    assert events[0]["field_name"] == "item_name"
    assert events[0]["_meta"]["style_id"] == "TLG-J-001"


@_with_temp_root
def test_log_nonstrategic_high_confidence_is_filtered() -> None:
    eid = log_field_decision(
        workspace_id="tlg",
        session_id="ses_test_2",
        module=Module.NIS,
        field_name="some_internal_helper_field",
        atlas_output="x",
        overall_confidence=0.95,
        rules_injected=[{"rule_id": "x"}],
        brand_profile_version="tlg_v1",
    )
    assert eid is None
    events = list(stream_decisions("tlg"))
    assert len(events) == 0


@_with_temp_root
def test_operator_response_appends_delta_event() -> None:
    eid = log_field_decision(
        workspace_id="tlg",
        session_id="ses_test_3",
        module=Module.NIS,
        field_name="bullet_1",
        atlas_output="Crafted from premium twill",
        overall_confidence=0.91,
        rules_injected=[{"rule_id": "brand.tlg.banned_word"}],
        brand_profile_version="tlg_v1",
    )
    update_field_decision_with_operator_response(
        workspace_id="tlg",
        event_id=eid,
        operator_action=OperatorAction.EDIT,
        operator_value="Crafted from twill",
        operator_scope=OperatorScope.BRAND_ALWAYS,
        operator_time_to_decision_ms=8400,
        operator_comment="Sheik wants premium out of TLG voice",
    )
    events = list(stream_decisions("tlg"))
    assert len(events) == 2
    response = events[1]
    assert response["event_kind"] == "operator_response"
    assert response["links_to_event_id"] == eid
    assert response["operator_scope"] == "brand_always"


@_with_temp_root
def test_operator_response_records_viewed_case() -> None:
    """v1.1.0: distinguish verified accept (viewed Why panel) from reflex accept."""
    eid_reflex = log_field_decision(
        workspace_id="tlg",
        session_id="ses_viewed_case",
        module=Module.NIS,
        field_name="item_name",
        atlas_output="Reflex case",
        overall_confidence=0.88,
        rules_injected=[{"rule_id": "x"}],
        brand_profile_version="tlg_v1",
    )
    update_field_decision_with_operator_response(
        workspace_id="tlg",
        event_id=eid_reflex,
        operator_action=OperatorAction.ACCEPT,
        operator_time_to_decision_ms=400,
        operator_viewed_case=False,
    )

    eid_verified = log_field_decision(
        workspace_id="tlg",
        session_id="ses_viewed_case",
        module=Module.NIS,
        field_name="item_name",
        atlas_output="Verified case",
        overall_confidence=0.88,
        rules_injected=[{"rule_id": "x"}],
        brand_profile_version="tlg_v1",
    )
    update_field_decision_with_operator_response(
        workspace_id="tlg",
        event_id=eid_verified,
        operator_action=OperatorAction.ACCEPT,
        operator_time_to_decision_ms=11200,
        operator_viewed_case=True,
    )

    events = list(stream_decisions("tlg"))
    responses = [e for e in events if e.get("event_kind") == "operator_response"]
    assert len(responses) == 2
    reflex = next(r for r in responses if r["links_to_event_id"] == eid_reflex)
    verified = next(r for r in responses if r["links_to_event_id"] == eid_verified)
    assert reflex["operator_viewed_case"] is False
    assert verified["operator_viewed_case"] is True
    # Same operator_action, structurally different training signal
    assert reflex["operator_action"] == verified["operator_action"] == "accept"


@_with_temp_root
def test_session_lifecycle_round_trip() -> None:
    s = open_session(workspace_id="tlg", operator_id="devang")
    assert s.session_id
    assert s.state == "live"

    submit_session(
        s,
        operator_notes="May launch — 50 styles. Premium banned per Sheik.",
        exemplar=True,
    )
    persisted = read_session("tlg", s.session_id)
    assert persisted is not None
    assert persisted["state"] == "submitted"
    assert persisted["exemplar"] is True
    assert "Premium banned" in persisted["operator_notes"]


@_with_temp_root
def test_judgment_moment_logs() -> None:
    eid = log_field_decision(
        workspace_id="tlg",
        session_id="ses_test_5",
        module=Module.NIS,
        field_name="item_name",
        atlas_output="x",
        overall_confidence=0.92,
        rules_injected=[{"rule_id": "x"}],
        brand_profile_version="tlg_v1",
    )
    moment_id = log_judgment_moment(
        workspace_id="tlg",
        decision_event_id=eid,
        trigger_type=TriggerType.CONFIDENCE_MISMATCH,
        session_id="ses_test_5",
    )
    assert moment_id
    events = list(stream_decisions("tlg"))
    moments = [e for e in events if e.get("trigger_type")]
    assert len(moments) == 1
    assert moments[0]["trigger_type"] == "confidence_mismatch"


if __name__ == "__main__":
    tests = [
        test_filter_strategic_always_logs,
        test_filter_nonstrategic_high_confidence_single_rule_skips,
        test_filter_low_confidence_logs,
        test_filter_multiple_rules_logs,
        test_filter_operator_acted_always_logs,
        test_log_strategic_field_writes_event,
        test_log_nonstrategic_high_confidence_is_filtered,
        test_operator_response_appends_delta_event,
        test_operator_response_records_viewed_case,
        test_session_lifecycle_round_trip,
        test_judgment_moment_logs,
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print("all substrate logger tests passed")
