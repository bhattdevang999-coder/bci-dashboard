"""qa_m5.py — M5 content_benchmarks regression suite.

Asserts behavior of content_benchmarks plus the unknowns.resolve_unknown
→ flag_by_unknown hook.

Usage:
    ATLAS_DATABASE_URL="postgresql://atlastest@/atlas_test?host=/tmp&port=55432" \\
        python qa_m5.py
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

from substrate import content_benchmarks as cb
from substrate import unknowns as uk
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


def test_lock_and_validate() -> None:
    section("content_benchmarks: lock + validation")

    bid = cb.lock_benchmark(
        WS,
        scope="family", scope_ref="velune_pocket",
        benchmark_type="title",
        approved_value="Novelle Velune Pocket Leggings — Athletic Fit",
        source_event_id="evt_a",
        approved_by="devang",
        citations=[
            {"layer": "factual", "source": "asin_metadata#247"},
        ],
    )
    check(bid is not None, "lock returns benchmark_id")

    row = cb.get_benchmark(bid)
    check(row is not None, "get returns row")
    check(row["status"] == "active", "initial status is active")
    check(row["used_count"] == 0, "used_count starts at 0")
    check(row["scope_ref"] == "velune_pocket", "scope_ref roundtrip")
    check(len(row["citations"]) == 1, "citations persisted")

    # Invalid scope rejects
    bad_scope = cb.lock_benchmark(
        WS, scope="bogus", scope_ref=None,
        benchmark_type="title", approved_value="x",
        source_event_id="evt_b", approved_by="devang",
    )
    check(bad_scope is None, "invalid scope rejected")

    # Invalid benchmark_type rejects
    bad_type = cb.lock_benchmark(
        WS, scope="family", scope_ref="x",
        benchmark_type="bogus", approved_value="x",
        source_event_id="evt_c", approved_by="devang",
    )
    check(bad_type is None, "invalid benchmark_type rejected")

    # Missing source_event_id rejects
    bad_evt = cb.lock_benchmark(
        WS, scope="family", scope_ref="x",
        benchmark_type="title", approved_value="x",
        source_event_id="", approved_by="devang",
    )
    check(bad_evt is None, "missing source_event_id rejected")


def test_scope_priority() -> None:
    section("content_benchmarks: scope priority")

    # Reset
    wipe_substrate_for_tests()

    # Lock at three scopes
    bid_global = cb.lock_benchmark(
        WS, scope="global", scope_ref=None,
        benchmark_type="title", approved_value="global title",
        source_event_id="evt_g", approved_by="devang",
    )
    bid_family = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="title", approved_value="family title",
        source_event_id="evt_f", approved_by="devang",
    )
    bid_asin = cb.lock_benchmark(
        WS, scope="asin", scope_ref="B0VEL-PKT-BLK-M",
        benchmark_type="title", approved_value="asin title",
        source_event_id="evt_a", approved_by="devang",
    )
    check(all([bid_global, bid_family, bid_asin]),
          "three benchmarks created at three scopes")

    # For ASIN in family: asin > family > global
    rows = cb.list_applicable(
        WS, benchmark_type="title",
        asin="B0VEL-PKT-BLK-M", family="velune_pocket",
    )
    check(len(rows) == 3, "three applicable rows")
    check(rows[0]["scope"] == "asin", "asin scope ranks first")
    check(rows[1]["scope"] == "family", "family scope ranks second")
    check(rows[2]["scope"] == "global", "global scope ranks third")

    # For ASIN NOT in family: only asin + global
    rows2 = cb.list_applicable(
        WS, benchmark_type="title",
        asin="B0OTHER", family=None,
    )
    check(len(rows2) == 1, "only global when no family/asin match")
    check(rows2[0]["scope"] == "global",
          "global is the only applicable")

    # For ASIN in family but no asin override
    rows3 = cb.list_applicable(
        WS, benchmark_type="title",
        asin="B0VEL-PKT-NAVY-L", family="velune_pocket",
    )
    check(len(rows3) == 2, "two applicable (family + global)")
    check(rows3[0]["scope"] == "family", "family ranks first")

    # Different benchmark_type — no applicable
    rows4 = cb.list_applicable(
        WS, benchmark_type="description",
        asin="B0VEL-PKT-BLK-M", family="velune_pocket",
    )
    check(len(rows4) == 0,
          "different benchmark_type yields empty")


def test_cap_enforcement() -> None:
    section("content_benchmarks: per-scope cap")

    wipe_substrate_for_tests()

    # Fill cap (3) for family:velune_pocket title
    for i in range(3):
        bid = cb.lock_benchmark(
            WS, scope="family", scope_ref="velune_pocket",
            benchmark_type="title",
            approved_value=f"title v{i+1}",
            source_event_id=f"evt_{i}", approved_by="devang",
        )
        check(bid is not None, f"benchmark {i+1}/3 locks under cap")

    # 4th should be blocked
    blocked = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="title",
        approved_value="over cap",
        source_event_id="evt_over", approved_by="devang",
    )
    check(blocked is None, "4th benchmark blocked by cap")

    # enforce_cap=False bypasses
    bypass = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="title",
        approved_value="bypass",
        source_event_id="evt_bp", approved_by="devang",
        enforce_cap=False,
    )
    check(bypass is not None, "enforce_cap=False bypasses cap")

    # Cap is per (scope, scope_ref, type) — different type unaffected
    diff_type = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="bullets",
        approved_value="bullets v1",
        source_event_id="evt_bul", approved_by="devang",
    )
    check(diff_type is not None,
          "different benchmark_type unaffected by title cap")

    # Cap is per scope_ref — different family unaffected
    diff_family = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_no_pocket",
        benchmark_type="title",
        approved_value="other family title",
        source_event_id="evt_np", approved_by="devang",
    )
    check(diff_family is not None,
          "different scope_ref unaffected by velune_pocket cap")


def test_usage_lifecycle() -> None:
    section("content_benchmarks: bump_usage + supersede + archive")

    wipe_substrate_for_tests()

    bid = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="title", approved_value="v1",
        source_event_id="evt_1", approved_by="devang",
    )
    check(cb.bump_usage(bid), "bump_usage returns True")
    check(cb.bump_usage(bid), "bump_usage idempotent (returns True again)")
    after = cb.get_benchmark(bid)
    check(after["used_count"] == 2, "used_count = 2 after two bumps")
    check(after["last_used_at"] is not None,
          "last_used_at stamped")

    # Supersede
    new_bid = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="title", approved_value="v2",
        source_event_id="evt_2", approved_by="devang",
    )
    check(cb.supersede(bid, new_bid, "devang"), "supersede returns True")
    old = cb.get_benchmark(bid)
    check(old["status"] == "superseded", "old status = superseded")
    check(old["superseded_by"] == new_bid, "superseded_by set")

    # Supersede already-superseded returns False
    check(cb.supersede(bid, new_bid, "devang") is False,
          "can't re-supersede")

    # Superseded benchmarks don't appear in list_applicable
    applicable = cb.list_applicable(
        WS, benchmark_type="title", family="velune_pocket",
    )
    check(len(applicable) == 1, "only new benchmark applicable")
    check(applicable[0]["benchmark_id"] == new_bid,
          "new benchmark is the applicable one")

    # Archive
    check(cb.archive(new_bid, "devang"), "archive returns True")
    arc = cb.get_benchmark(new_bid)
    check(arc["status"] == "archived", "status = archived")

    # Archived can't be reactivated (only review_recommended can)
    check(cb.reactivate(new_bid, "devang") is False,
          "can't reactivate archived")


def test_flag_by_unknown_hook() -> None:
    section("content_benchmarks: flag_by_unknown + reactivate")

    wipe_substrate_for_tests()

    # Two unknowns
    unk_a = uk.emit_unknown(
        WS, scope="brand", scope_ref=None,
        question="fabric GSM?",
        required_for=["title_generation"],
        evidence_path="factory_spec_sheet",
    )
    unk_b = uk.emit_unknown(
        WS, scope="brand", scope_ref=None,
        question="UPF rating?",
        required_for=["title_generation"],
        evidence_path="factory_spec_sheet",
    )

    # Two benchmarks — one with unk_a open, one with both, one with none
    b1 = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="title", approved_value="b1",
        source_event_id="evt_1", approved_by="devang",
        open_unknowns_at_approval=[unk_a],
    )
    b2 = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="bullets", approved_value="b2",
        source_event_id="evt_2", approved_by="devang",
        open_unknowns_at_approval=[unk_a, unk_b],
    )
    b3 = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="description", approved_value="b3",
        source_event_id="evt_3", approved_by="devang",
    )

    # Resolving unk_a should flag b1 and b2 but not b3
    ok = uk.resolve_unknown(
        unk_a, answer_value={"value": "160 g/m^2"},
        answer_source="factory_response_2026_05_24",
        answered_by="devang",
    )
    check(ok, "resolve_unknown succeeds")

    check(cb.get_benchmark(b1)["status"] == "review_recommended",
          "b1 flagged for review")
    check(cb.get_benchmark(b2)["status"] == "review_recommended",
          "b2 flagged for review")
    check(cb.get_benchmark(b3)["status"] == "active",
          "b3 not affected")

    # review_reason mentions the unknown
    b1_after = cb.get_benchmark(b1)
    check(
        "unknown" in (b1_after["review_reason"] or "").lower()
        and unk_a in (b1_after["review_reason"] or ""),
        "review_reason references the unknown_id",
    )

    # Resolving unk_b should flag b2 again (review_reason appended)
    ok2 = uk.resolve_unknown(
        unk_b, answer_value={"value": "50+"},
        answer_source="factory_response_2026_05_24",
        answered_by="devang",
    )
    check(ok2, "resolve_unknown for b2 succeeds")
    # b2 stays review_recommended (no change in status)
    # but b1 status unaffected since unk_b wasn't in its list
    check(cb.get_benchmark(b1)["status"] == "review_recommended",
          "b1 status unchanged by unk_b resolution")

    # Reactivate b1
    check(cb.reactivate(b1, "devang"),
          "reactivate review_recommended benchmark")
    b1_after2 = cb.get_benchmark(b1)
    check(b1_after2["status"] == "active",
          "b1 back to active")
    check(b1_after2["review_reason"] is None,
          "review_reason cleared on reactivate")

    # Reactivate active benchmark returns False
    check(cb.reactivate(b1, "devang") is False,
          "reactivate active benchmark rejected")

    # list_applicable: review_recommended excluded by default
    rows = cb.list_applicable(
        WS, benchmark_type="bullets", family="velune_pocket",
    )
    check(len(rows) == 0,
          "review_recommended excluded by default")

    # ...but included when include_review_recommended=True
    rows2 = cb.list_applicable(
        WS, benchmark_type="bullets", family="velune_pocket",
        include_review_recommended=True,
    )
    check(len(rows2) == 1,
          "review_recommended included when flag set")


def test_list_filters() -> None:
    section("content_benchmarks: list filters")

    # Use whatever's in the db at this point — don't wipe.
    all_rows = cb.list_benchmarks(WS)
    check(len(all_rows) > 0, "list returns rows")

    # Filter by status
    active = cb.list_benchmarks(WS, status="active")
    review = cb.list_benchmarks(WS, status="review_recommended")
    check(len(active) + len(review) <= len(all_rows),
          "status filters subset")

    # Filter by type
    titles = cb.list_benchmarks(WS, benchmark_type="title")
    check(all(b["benchmark_type"] == "title" for b in titles),
          "benchmark_type filter pure")


def main() -> int:
    setup()
    test_lock_and_validate()
    test_scope_priority()
    test_cap_enforcement()
    test_usage_lifecycle()
    test_flag_by_unknown_hook()
    test_list_filters()

    print()
    print("=" * 60)
    print(f"M5 QA: {CHECKS - len(FAILURES)} / {CHECKS} passed")
    if FAILURES:
        print(f"FAILURES: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All M5 assertions green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
