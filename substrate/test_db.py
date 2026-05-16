"""Tests specific to the Postgres backend.

These tests only run when DATABASE_URL points at a reachable Postgres.
When unset, every test no-ops (returning early). That means CI can run
the full substrate test suite in pure-JSONL mode without standing up
a database, while still letting us verify Postgres semantics locally
and on Render.
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
from functools import wraps

from substrate.db import (
    apply_schema,
    get_pool,
    is_postgres_active,
    reset_pool_for_tests,
    wipe_substrate_for_tests,
)
from substrate.logger import (
    log_field_decision,
    log_judgment_moment,
    open_session,
    stream_decisions,
    update_field_decision_with_operator_response,
)
from substrate.schema import Module, OperatorAction, OperatorScope, TriggerType


def _requires_postgres(fn):
    """Skip the test if no DATABASE_URL is set, but never fail the suite."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not os.environ.get("DATABASE_URL") and not os.environ.get("ATLAS_DATABASE_URL"):
            print(f"  skip {fn.__name__} (no DATABASE_URL)")
            return
        wipe_substrate_for_tests()
        return fn(*args, **kwargs)
    return wrapper


@_requires_postgres
def test_pool_init_and_select() -> None:
    """Pool comes up and a trivial SELECT works."""
    pool = get_pool()
    assert pool is not None, "pool should init when DATABASE_URL is set"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


@_requires_postgres
def test_schema_apply_is_idempotent() -> None:
    """Calling apply_schema twice does not raise."""
    pool = get_pool()
    with pool.connection() as conn:
        apply_schema(conn)
        apply_schema(conn)
    # The version marker should still have exactly the v1 row, not duplicates
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM substrate_schema_version WHERE version='v1'")
            assert cur.fetchone()[0] == 1


@_requires_postgres
def test_decision_event_roundtrips_through_pg() -> None:
    """Write a decision, read it back, verify every locked field is preserved."""
    s = open_session(workspace_id="tlg_pg", operator_id="d")
    eid = log_field_decision(
        workspace_id="tlg_pg",
        session_id=s.session_id,
        module=Module.NIS,
        field_name="item_name",
        atlas_output="Roundtrip title",
        overall_confidence=0.85,
        rules_injected=[{"rule_id": "nis.test.alpha"}, {"rule_id": "nis.test.beta"}],
        brand_profile_version="tlg_v1",
        style_id="STY-RT-1",
    )
    assert eid

    rows = list(stream_decisions("tlg_pg"))
    decisions = [r for r in rows if r.get("event_kind") == "decision_event"]
    assert len(decisions) == 1
    d = decisions[0]
    assert d["event_id"] == eid
    assert d["module"] == "nis"
    assert d["field_name"] == "item_name"
    assert d["atlas_output"] == "Roundtrip title"
    assert d["overall_confidence"] == 0.85
    # rules_injected comes back as parsed JSONB
    assert any(r["rule_id"] == "nis.test.alpha" for r in d["rules_injected"])
    # _meta was promoted from the meta column
    assert d.get("_meta", {}).get("style_id") == "STY-RT-1"


@_requires_postgres
def test_operator_response_links_to_decision() -> None:
    """An operator_response row references the decision_event_id correctly."""
    s = open_session(workspace_id="tlg_pg", operator_id="d")
    eid = log_field_decision(
        workspace_id="tlg_pg",
        session_id=s.session_id,
        module=Module.NIS,
        field_name="bullet_1",
        atlas_output="Original bullet",
        overall_confidence=0.55,
        rules_injected=[{"rule_id": "nis.test"}],
        brand_profile_version="tlg_v1",
    )
    update_field_decision_with_operator_response(
        workspace_id="tlg_pg",
        event_id=eid,
        operator_action=OperatorAction.EDIT,
        operator_value="Edited bullet",
        operator_scope=OperatorScope.BRAND_ALWAYS,
        operator_time_to_decision_ms=7300,
        operator_viewed_case=True,
    )
    rows = list(stream_decisions("tlg_pg"))
    responses = [r for r in rows if r.get("event_kind") == "operator_response"]
    assert len(responses) == 1
    r = responses[0]
    assert r["links_to_event_id"] == eid
    assert r["operator_action"] == "edit"
    assert r["operator_value"] == "Edited bullet"
    assert r["operator_scope"] == "brand_always"
    assert r["operator_viewed_case"] is True


@_requires_postgres
def test_judgment_moment_uses_event_id_column() -> None:
    """judgment_moment rows persist trigger_type and link back to the decision."""
    s = open_session(workspace_id="tlg_pg", operator_id="d")
    eid = log_field_decision(
        workspace_id="tlg_pg",
        session_id=s.session_id,
        module=Module.NIS,
        field_name="item_name",
        atlas_output="t",
        overall_confidence=0.55,
        rules_injected=[{"rule_id": "x"}],
        brand_profile_version="tlg_v1",
    )
    mid = log_judgment_moment(
        workspace_id="tlg_pg",
        decision_event_id=eid,
        trigger_type=TriggerType.LOW_CONFIDENCE,
        session_id=s.session_id,
    )
    rows = list(stream_decisions("tlg_pg"))
    moments = [r for r in rows if r.get("event_kind") == "judgment_moment_event"]
    assert len(moments) == 1
    m = moments[0]
    assert m["trigger_type"] == "low_confidence"
    assert m["decision_event_id"] == eid
    assert m["moment_id"] == mid  # alias surfaced for legacy consumers


@_requires_postgres
def test_workspace_isolation_at_query_layer() -> None:
    """Writes to workspace A must not surface when reading workspace B."""
    open_session(workspace_id="ws_a", operator_id="d")
    open_session(workspace_id="ws_b", operator_id="d")

    log_field_decision(
        workspace_id="ws_a", session_id=None, module=Module.NIS,
        field_name="item_name", atlas_output="A-only",
        overall_confidence=0.55, rules_injected=[{"rule_id": "x"}],
        brand_profile_version="v1",
    )
    log_field_decision(
        workspace_id="ws_b", session_id=None, module=Module.NIS,
        field_name="item_name", atlas_output="B-only",
        overall_confidence=0.55, rules_injected=[{"rule_id": "x"}],
        brand_profile_version="v1",
    )
    a_rows = [r for r in stream_decisions("ws_a") if r.get("event_kind") == "decision_event"]
    b_rows = [r for r in stream_decisions("ws_b") if r.get("event_kind") == "decision_event"]
    assert len(a_rows) == 1 and a_rows[0]["atlas_output"] == "A-only"
    assert len(b_rows) == 1 and b_rows[0]["atlas_output"] == "B-only"


@_requires_postgres
def test_concurrent_writes_do_not_corrupt() -> None:
    """20 threads writing simultaneously land 20 rows; no event_id collisions."""
    s = open_session(workspace_id="ws_concurrent", operator_id="d")

    def _writer(i: int) -> str:
        return log_field_decision(
            workspace_id="ws_concurrent",
            session_id=s.session_id,
            module=Module.NIS,
            field_name="item_name",
            atlas_output=f"concurrent #{i}",
            overall_confidence=0.55,
            rules_injected=[{"rule_id": "c"}],
            brand_profile_version="v1",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(_writer, range(20)))
    assert len(set(ids)) == 20, f"expected 20 unique event_ids, got {len(set(ids))}"
    rows = [r for r in stream_decisions("ws_concurrent") if r.get("event_kind") == "decision_event"]
    assert len(rows) == 20


@_requires_postgres
def test_session_state_upserts_through_resubmit() -> None:
    """submit_session updates the row in place; no duplicate session entries."""
    from substrate.logger import submit_session
    s = open_session(workspace_id="ws_sess", operator_id="d")
    submit_session(s, operator_notes="first submit", exemplar=False)
    submit_session(s, operator_notes="second submit", exemplar=True)

    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT operator_notes, exemplar, state FROM substrate_sessions WHERE session_id=%s",
                (s.session_id,),
            )
            row = cur.fetchone()
    assert row is not None
    assert row[0] == "second submit"
    assert row[1] is True
    assert row[2] == "submitted"

    # Verify only one row exists in substrate_sessions for this session_id
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM substrate_sessions WHERE session_id=%s",
                (s.session_id,),
            )
            assert cur.fetchone()[0] == 1


if __name__ == "__main__":
    tests = [
        test_pool_init_and_select,
        test_schema_apply_is_idempotent,
        test_decision_event_roundtrips_through_pg,
        test_operator_response_links_to_decision,
        test_judgment_moment_uses_event_id_column,
        test_workspace_isolation_at_query_layer,
        test_concurrent_writes_do_not_corrupt,
        test_session_state_upserts_through_resubmit,
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"all {len(tests)} Postgres-specific tests passed (or skipped without DATABASE_URL)")
