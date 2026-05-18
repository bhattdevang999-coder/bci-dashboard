"""qa_m5b.py — wires content_benchmarks into citation_chain.

Asserts the resolve/inject/bump-usage flow plus the benchmark-aware
verifier. Heuristic path (no LLM key) is enough to test the wiring
because generate_cited's contract returns `applicable_benchmarks` in
the response regardless of LLM outcome.

Usage:
    ATLAS_DATABASE_URL="postgresql://atlastest@/atlas_test?host=/tmp&port=55432" \\
        python qa_m5b.py
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
    citation_chain as cc,
    content_benchmarks as cb,
    asin_metadata as am,
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
    assert pool is not None
    with pool.connection() as conn:
        apply_schema(conn)
    wipe_substrate_for_tests()


WS = "novelle"


def test_decision_class_mapping() -> None:
    section("decision_class -> benchmark_type mapping")
    check(cc._benchmark_type_for("title_generation") == "title",
          "title_generation maps to title")
    check(cc._benchmark_type_for("bullet_generation") == "bullets",
          "bullet_generation maps to bullets")
    check(cc._benchmark_type_for("description_generation") == "description",
          "description_generation maps to description")
    check(cc._benchmark_type_for("backend_fields_generation") == "backend_fields",
          "backend_fields_generation maps")
    check(cc._benchmark_type_for("unmapped_xyz") is None,
          "unmapped decision_class returns None")
    check(cc._benchmark_type_for("") is None,
          "empty decision_class returns None")


def test_resolve_with_family() -> None:
    section("resolve_applicable_benchmarks: pulls family from factual layer")

    wipe_substrate_for_tests()

    # Seed parent + child ASIN in pocket family
    am.set_asin_metadata(
        WS, "B0VEL-PKT",
        variation_family="velune_pocket",
        ground_truth_fields={"brand": "Novelle"},
        set_by="devang",
    )
    am.set_asin_metadata(
        WS, "B0VEL-PKT-BLK-M",
        parent_asin="B0VEL-PKT",
        variation_family="velune_pocket",
        variation_axes={"color": "Midnight Black", "size": "M"},
        ground_truth_fields={"color_name": "Midnight Black", "size": "M"},
        set_by="devang",
    )

    # Lock family + global benchmarks for title
    bid_family = cb.lock_benchmark(
        WS, scope="family", scope_ref="velune_pocket",
        benchmark_type="title",
        approved_value="Velune pocket family title",
        source_event_id="evt_fam", approved_by="devang",
    )
    bid_global = cb.lock_benchmark(
        WS, scope="global", scope_ref=None,
        benchmark_type="title",
        approved_value="Generic global title",
        source_event_id="evt_glb", approved_by="devang",
    )
    check(bid_family and bid_global, "two benchmarks locked")

    # Simulate a bundle with the factual layer carrying variation_family
    bundle = {
        "asin": "B0VEL-PKT-BLK-M",
        "factual": {"variation_family": "velune_pocket"},
    }
    rows = cc.resolve_applicable_benchmarks(WS, bundle, "title_generation")
    check(len(rows) == 2, f"two applicable rows (got {len(rows)})")
    check(rows[0]["scope"] == "family",
          "family benchmark ranks first when no asin override")

    # ASIN-scope wins when present
    bid_asin = cb.lock_benchmark(
        WS, scope="asin", scope_ref="B0VEL-PKT-BLK-M",
        benchmark_type="title",
        approved_value="ASIN-specific title",
        source_event_id="evt_a", approved_by="devang",
    )
    rows2 = cc.resolve_applicable_benchmarks(WS, bundle, "title_generation")
    check(rows2[0]["scope"] == "asin",
          "asin benchmark ranks first when present")
    check(rows2[0]["benchmark_id"] == bid_asin, "expected asin benchmark id")

    # Unmapped decision_class returns []
    rows3 = cc.resolve_applicable_benchmarks(
        WS, bundle, "totally_unmapped_class",
    )
    check(rows3 == [], "unmapped decision_class returns []")

    # No factual.variation_family: only global + asin apply
    bundle_no_family = {
        "asin": "B0VEL-PKT-BLK-M",
        "factual": {},
    }
    rows4 = cc.resolve_applicable_benchmarks(
        WS, bundle_no_family, "title_generation",
    )
    scopes = sorted(r["scope"] for r in rows4)
    check(scopes == ["asin", "global"],
          f"without family, only asin+global apply (got {scopes})")


def test_prompt_injection() -> None:
    section("build_cited_prompt: PRIOR APPROVED PATTERNS section")

    bundle = {
        "workspace_id": WS,
        "asin": "B0VEL-PKT-BLK-M",
        "factual": {"variation_family": "velune_pocket"},
        "strategic": {}, "voice": {}, "evidence": {},
        "calibrated_external": {}, "market_state": {},
        "competitor_state": {}, "unit_economics": {}, "goals": {},
        "unknowns": [],
        "context_rows_read": [],
    }

    prompt_no_bench = cc.build_cited_prompt(bundle, "title_generation")
    check("PRIOR APPROVED PATTERNS" not in prompt_no_bench,
          "section absent when no benchmarks passed")
    check("seeded_from_benchmark" not in prompt_no_bench,
          "no seeded_from_benchmark field when no benchmarks")

    benchmarks = [
        {
            "benchmark_id": "bench-aaa-111",
            "scope": "family", "scope_ref": "velune_pocket",
            "approved_value": "Velune pocket family title",
            "used_count": 3,
        },
    ]
    prompt_bench = cc.build_cited_prompt(
        bundle, "title_generation", benchmarks=benchmarks,
    )
    check("PRIOR APPROVED PATTERNS" in prompt_bench,
          "section present when benchmarks passed")
    check("bench-aaa-111" in prompt_bench,
          "benchmark_id rendered in the prompt")
    check("velune_pocket" in prompt_bench,
          "scope_ref rendered in the prompt")
    check("seeded_from_benchmark" in prompt_bench,
          "output schema includes seeded_from_benchmark")
    check("seed_divergence_reason" in prompt_bench,
          "output schema includes seed_divergence_reason")
    check("benchmark" in prompt_bench
          and "layer" in prompt_bench,
          "benchmark layer added to citation schema")


def test_verifier_accepts_benchmark_ids() -> None:
    section("verify_citations: benchmark layer + benchmark_ids")

    citations = [
        # benchmark layer, valid id
        {"claim": "Seeded from family benchmark",
         "layer": "benchmark",
         "source_row_ids": ["bench-aaa-111"],
         "rationale": "Used as seed"},
        # benchmark layer, invalid id
        {"claim": "Made-up benchmark reference",
         "layer": "benchmark",
         "source_row_ids": ["bench-fake-999"],
         "rationale": "no good"},
        # normal layer, valid row
        {"claim": "Material from substrate",
         "layer": "factual",
         "source_row_ids": ["asin_metadata#B0VEL-PKT"],
         "rationale": "asin spec"},
        # normal layer, missing row
        {"claim": "Phantom",
         "layer": "factual",
         "source_row_ids": ["missing#row"],
         "rationale": "x"},
        # convention layer
        {"claim": "Common phrasing",
         "layer": "convention",
         "source_row_ids": [],
         "rationale": "convention"},
    ]
    rows_read = ["asin_metadata#B0VEL-PKT"]
    benchmark_ids = ["bench-aaa-111"]
    verified = cc.verify_citations(
        citations, rows_read, benchmark_ids=benchmark_ids,
    )
    by_claim = {c["claim"]: c for c in verified}
    check(by_claim["Seeded from family benchmark"]["verifier_status"] == "verified",
          "benchmark layer + valid id -> verified")
    check(by_claim["Made-up benchmark reference"]["verifier_status"] == "weak",
          "benchmark layer + invalid id -> weak")
    check(by_claim["Material from substrate"]["verifier_status"] == "verified",
          "factual layer + valid row -> verified")
    check(by_claim["Phantom"]["verifier_status"] == "weak",
          "factual layer + missing row -> weak")
    check(by_claim["Common phrasing"]["verifier_status"] == "convention",
          "convention layer -> convention")


def test_generate_cited_with_no_llm() -> None:
    section("generate_cited: applicable_benchmarks always returned")

    # Bundle is built from existing test fixtures (parent + child + family
    # benchmark already seeded by earlier sections).
    result = cc.generate_cited(
        WS, "B0VEL-PKT-BLK-M", "title_generation",
        operator_id="devang", log_decision=False,
    )
    check("applicable_benchmarks" in result,
          "response includes applicable_benchmarks key")
    check(isinstance(result["applicable_benchmarks"], list),
          "applicable_benchmarks is a list")
    check(len(result["applicable_benchmarks"]) >= 1,
          f"at least one applicable benchmark resolved (got {len(result['applicable_benchmarks'])})")
    check("seeded_from_benchmark" in result,
          "response includes seeded_from_benchmark key")
    check("seed_divergence_reason" in result,
          "response includes seed_divergence_reason key")
    # LLM-unavailable in sandbox: seeded_from_benchmark must be None
    # since no parse can happen.
    if result.get("llm_failed"):
        check(result["seeded_from_benchmark"] is None,
              "LLM failure -> seeded_from_benchmark None")


def test_bump_usage_on_seeded() -> None:
    section("generate_cited: bump_usage runs when LLM names a benchmark")

    # We can't drive a live LLM in the sandbox, but we can test the
    # downstream path by simulating a parsed response and calling
    # bump_usage directly to verify the contract used by generate_cited.
    wipe_substrate_for_tests()
    am.set_asin_metadata(
        WS, "B0SOLO",
        variation_family="solo_family",
        ground_truth_fields={"brand": "Novelle"},
        set_by="devang",
    )
    bid = cb.lock_benchmark(
        WS, scope="family", scope_ref="solo_family",
        benchmark_type="title",
        approved_value="Solo family title",
        source_event_id="evt_solo", approved_by="devang",
    )
    before = cb.get_benchmark(bid)
    check(before["used_count"] == 0, "starts at used_count=0")
    cb.bump_usage(bid)
    after = cb.get_benchmark(bid)
    check(after["used_count"] == 1, "bump_usage increments correctly")


def main() -> int:
    setup()
    test_decision_class_mapping()
    test_resolve_with_family()
    test_prompt_injection()
    test_verifier_accepts_benchmark_ids()
    test_generate_cited_with_no_llm()
    test_bump_usage_on_seeded()

    print()
    print("=" * 60)
    print(f"M5b QA: {CHECKS - len(FAILURES)} / {CHECKS} passed")
    if FAILURES:
        print(f"FAILURES: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All M5b assertions green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
