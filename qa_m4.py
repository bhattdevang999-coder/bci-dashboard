"""qa_m4.py — M4 substrate regression suite.

Asserts behavior of recommendation_ingest + atlas_evaluation +
rec_evaluator (parse + verdict). End-to-end test of the tokenized
response flow (generate → lookup → apply agency response →
mark_received → consume → reject reuse).

Usage:
    ATLAS_DATABASE_URL="postgresql://atlastest@/atlas_test?host=/tmp&port=55432" \\
        python qa_m4.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if not os.environ.get("ATLAS_DATABASE_URL"):
    os.environ["ATLAS_DATABASE_URL"] = (
        "postgresql://atlastest@/atlas_test?host=/tmp&port=55432"
    )

from substrate import (
    recommendation_ingest as ri,
    atlas_evaluation as ae,
    rec_evaluator as ev,
)
from substrate.db import apply_schema, get_pool, wipe_substrate_for_tests


CHECKS = 0
FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(label)
        print(f"  FAIL  {label}", flush=True)
    else:
        print(f"   ok   {label}", flush=True)


def section(name: str) -> None:
    print(f"\n--- {name} ---", flush=True)


def setup() -> None:
    pool = get_pool()
    assert pool is not None, "no Postgres pool"
    with pool.connection() as conn:
        apply_schema(conn)
    wipe_substrate_for_tests()


WS = "novelle"


def test_recommendation_crud() -> None:
    section("recommendation_ingest: CRUD")

    rec_id = ri.create_recommendation(
        WS,
        source="acme_agency",
        source_tier="mid_agency",
        source_contact="agency@acme.example",
        raw_text=(
            "Material: 79% Nylon, 21% Spandex\n"
            "Part Number: Yoga Leggings\n"
            "Lifestyle: Casual\n"
        ),
        rec_type="backend_fields",
        scope_asins=["B0VEL-PKT"],
        scope_confidence=0.85,
    )
    check(rec_id is not None, "create returns rec_id")

    rec = ri.get_recommendation(rec_id)
    check(rec is not None, "get returns row")
    check(rec["source"] == "acme_agency", "source roundtrip")
    check(rec["source_tier"] == "mid_agency", "source_tier roundtrip")
    check(rec["status"] == "pending_evaluation",
          "initial status is pending_evaluation")
    check(rec["scope_asins"] == ["B0VEL-PKT"], "scope_asins roundtrip")
    check(rec["response_token"] is None,
          "no token until explicitly generated")

    # Empty source rejects
    bad = ri.create_recommendation(WS, source="")
    check(bad is None, "empty source rejected")

    # Bad source_tier falls through to None (logged, not failed)
    rec_id2 = ri.create_recommendation(
        WS, source="x", source_tier="not_real",
    )
    check(rec_id2 is not None,
          "invalid source_tier doesn't block create")
    rec2 = ri.get_recommendation(rec_id2)
    check(rec2["source_tier"] is None,
          "invalid source_tier coerced to None")

    # list_recommendations
    rows = ri.list_recommendations(WS)
    check(len(rows) >= 2, "list returns at least 2 rows")

    rows_filtered = ri.list_recommendations(WS, source="acme_agency")
    check(len(rows_filtered) == 1, "source filter works")


def test_parse_and_evaluate() -> None:
    section("rec_evaluator: parse + heuristic evaluate")

    raw = (
        "Material: 79% Nylon, 21% Spandex\n"
        "Part Number: Yoga Leggings\n"
        "Lifestyle: Casual\n"
        "Sport Type: Yoga\n"
    )
    parsed = ev.parse_raw_text(raw)
    check(len(parsed) >= 3,
          f"heuristic parse extracted {len(parsed)} fields")
    check(
        any("material" in k.lower() for k in parsed.keys()),
        "parse captured material",
    )

    verdicts = ev.evaluate_recommendation(
        parsed,
        workspace_id=WS,
        source="acme_agency",
        source_tier="mid_agency",
    )
    check(len(verdicts) == len(parsed),
          "one verdict per parsed field")
    # Heuristic path: all unknown
    check(
        all(v["verdict"] == "unknown" for v in verdicts),
        "heuristic path returns 'unknown' verdicts",
    )
    # Heuristic owner assignment
    owners = {v["field_name"]: v["field_owner"] for v in verdicts}
    if "material" in owners:
        check(owners["material"] == "manufacturer",
              "material → manufacturer owner")
    if "part_number" in owners:
        check(owners["part_number"] == "agency",
              "part_number → agency owner")
    if "sport_type" in owners:
        check(owners["sport_type"] == "amazon_taxonomy",
              "sport_type → amazon_taxonomy owner")


def test_evaluation_writes() -> None:
    section("atlas_evaluation: writes + filters + summary")

    rec_id = ri.create_recommendation(
        WS, source="acme_agency", source_tier="mid_agency",
        raw_text="x", rec_type="backend_fields",
    )
    fields = [
        ("material", "Cotton", "manufacturer", "disagree"),
        ("part_number", "Yoga Leggings", "agency", "disagree"),
        ("lifestyle", "Casual", "amazon_taxonomy", "partial"),
        ("color_map", "Black", "amazon_taxonomy", "agree"),
    ]
    ids = []
    for fname, val, owner, verdict in fields:
        eid = ae.create_evaluation(
            rec_id, WS,
            field_name=fname, submitted_value=val,
            field_owner=owner, verdict=verdict,
            reasoning="test", confidence=0.7,
        )
        check(eid is not None, f"create_evaluation {fname}")
        ids.append(eid)

    all_evals = ae.list_evaluations(rec_id)
    check(len(all_evals) == 4, "all 4 evals fetched")

    agency_only = ae.list_evaluations(rec_id, field_owner="agency")
    check(len(agency_only) == 1, "filter by field_owner")
    check(agency_only[0]["field_name"] == "part_number",
          "agency-owned is part_number")

    s = ae.summarize_rec(rec_id)
    check(s["total"] == 4, "summary total = 4")
    check(s["agree"] == 1, "summary agree count")
    check(s["partial"] == 1, "summary partial count")
    check(s["disagree"] == 2, "summary disagree count")
    check(s["pending_operator_decision"] == 4,
          "all pending initially")
    check(s["awaiting_agency_response"] == 1,
          "one agency field awaiting response")
    check(s["manufacturer_fields"] == 1, "manufacturer fields counted")

    # Invalid verdict rejects
    bad = ae.create_evaluation(
        rec_id, WS, field_name="x", submitted_value="y",
        field_owner="agency", verdict="bogus", reasoning="x",
    )
    check(bad is None, "invalid verdict rejected")

    # Invalid owner rejects
    bad2 = ae.create_evaluation(
        rec_id, WS, field_name="x", submitted_value="y",
        field_owner="nope", verdict="agree", reasoning="x",
    )
    check(bad2 is None, "invalid owner rejected")


def test_token_flow() -> None:
    section("tokenized response: generate → lookup → submit → consume")

    rec_id = ri.create_recommendation(
        WS, source="acme_agency", source_tier="mid_agency",
        raw_text="x", rec_type="backend_fields",
    )
    eid = ae.create_evaluation(
        rec_id, WS,
        field_name="part_number", submitted_value="Yoga Leggings",
        field_owner="agency", verdict="disagree",
        reasoning="not search-indexed",
        proposed_alternative="VEL-7-8-PKT-001",
        confidence=0.92, criticality="high",
    )

    # Generate
    link = ri.generate_response_token(
        rec_id, base_url="https://example.test", ttl_days=7,
    )
    check(link is not None, "token generated")
    check(link["url"].startswith("https://example.test/respond/"),
          "URL format")
    check(link["token"] and len(link["token"]) >= 24,
          "token has reasonable length")

    rec_after = ri.get_recommendation(rec_id)
    check(rec_after["status"] == "awaiting_response",
          "status flipped to awaiting_response")

    # Lookup positive
    row = ri.lookup_by_token(rec_id, link["token"])
    check(row is not None, "lookup with valid token returns row")

    # Wrong token rejects
    check(ri.lookup_by_token(rec_id, "bogus") is None,
          "wrong token rejected")
    check(ri.lookup_by_token(rec_id, "") is None,
          "empty token rejected")
    check(ri.lookup_by_token("not_a_rec", link["token"]) is None,
          "missing rec_id rejected")

    # Apply agency response
    ok = ae.apply_agency_response(
        eid, response_text="Agreed — revise to VEL-7-8-PKT-001.",
        agency_confidence=5,
    )
    check(ok, "apply_agency_response succeeds")

    eval_after = ae.get_evaluation(eid)
    check(eval_after["agency_response"] is not None,
          "agency_response persisted")
    check(eval_after["agency_confidence"] == 5,
          "agency_confidence persisted")

    # Mark received + consume
    check(ri.mark_response_received(rec_id), "mark_response_received")
    rec_after2 = ri.get_recommendation(rec_id)
    check(rec_after2["status"] == "response_received",
          "status now response_received")
    check(rec_after2["response_received_at"] is not None,
          "response_received_at stamped")

    # Lookup still works between submit and consume
    row2 = ri.lookup_by_token(rec_id, link["token"])
    check(row2 is not None,
          "token still usable until consume (re-edit window)")

    check(ri.consume_token(rec_id), "consume_token succeeds")
    row3 = ri.lookup_by_token(rec_id, link["token"])
    check(row3 is None, "consumed token cannot resolve")

    # Regeneration produces a fresh token
    link2 = ri.generate_response_token(
        rec_id, base_url="https://example.test", ttl_days=7,
    )
    check(link2 is not None and link2["token"] != link["token"],
          "regeneration produces fresh token")


def test_operator_decision() -> None:
    section("operator decision flow")

    rec_id = ri.create_recommendation(
        WS, source="acme_agency", raw_text="x",
        rec_type="backend_fields",
    )
    eid = ae.create_evaluation(
        rec_id, WS, field_name="material",
        submitted_value="Cotton", field_owner="manufacturer",
        verdict="disagree",
        reasoning="factory spec says 79% Nylon",
        proposed_alternative="79% Nylon, 21% Spandex",
    )

    ok = ae.apply_operator_decision(
        eid, decision="override",
        final_value="79% Nylon, 21% Spandex",
        reasoning="factory-confirmed",
    )
    check(ok, "apply_operator_decision succeeds")

    eval_after = ae.get_evaluation(eid)
    check(eval_after["operator_decision"] == "override",
          "operator_decision persisted")
    check(eval_after["final_value"] == "79% Nylon, 21% Spandex",
          "final_value persisted")
    check(eval_after["operator_decided_at"] is not None,
          "operator_decided_at stamped")

    # Pending filter excludes decided rows
    pending = ae.list_evaluations(rec_id, pending_only=True)
    check(len(pending) == 0, "pending_only excludes decided rows")

    # Invalid decision rejects
    bad = ae.apply_operator_decision(eid, decision="bogus")
    check(bad is False, "invalid decision rejected")


def test_status_transitions() -> None:
    section("status transitions")

    rec_id = ri.create_recommendation(
        WS, source="acme_agency", raw_text="x",
    )
    check(ri.set_status(rec_id, "evaluated"),
          "set_status to evaluated")
    rec = ri.get_recommendation(rec_id)
    check(rec["status"] == "evaluated", "status persisted")

    check(ri.set_status(rec_id, "resolved"),
          "set_status to resolved")

    # Invalid status rejects
    check(ri.set_status(rec_id, "bogus") is False,
          "invalid status rejected")


def main() -> int:
    setup()
    test_recommendation_crud()
    test_parse_and_evaluate()
    test_evaluation_writes()
    test_token_flow()
    test_operator_decision()
    test_status_transitions()

    print()
    print("=" * 60)
    print(f"M4 QA: {CHECKS - len(FAILURES)} / {CHECKS} passed")
    if FAILURES:
        print(f"FAILURES: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All M4 assertions green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
