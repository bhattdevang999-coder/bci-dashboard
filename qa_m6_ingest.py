"""qa_m6_ingest.py — M6 Day 1 catalog substrate + ingest regression.

Covers brand_workspace, audit_rules (15 SEED_RULES + fork), cohort_classifications,
asin_metadata, outcome_events, catalog_ingest end-to-end on the Roxy XLSX.

Usage:
    ATLAS_DATABASE_URL="postgresql://atlastest@/atlas_test?host=/tmp&port=55432" \\
        python qa_m6_ingest.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if not os.environ.get("ATLAS_DATABASE_URL"):
    os.environ["ATLAS_DATABASE_URL"] = (
        "postgresql://atlastest@/atlas_test?host=/tmp&port=55432"
    )

from substrate import audit_rules as ar
from substrate import brand_workspace as bw
from substrate import catalog_audit as ca
from substrate import catalog_ingest as ci
from substrate.db import apply_schema, get_pool, wipe_substrate_for_tests


CHECKS = 0
FAILURES: list[str] = []
WS = "novelle"
ROXY_XLSX = "/home/user/workspace/ROXY-Atlas_Catalog_Data_Template_20260519-FINAL.xlsx"


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


def test_workspace_register() -> None:
    section("brand_workspace: register + list")
    ok = bw.register_workspace(
        WS, display_name="Novelle", brand_role="operator_brand",
    )
    check(ok, "register_workspace ok")
    ws = bw.get_workspace(WS)
    check(ws is not None, "get_workspace returns row")
    check(ws["workspace_id"] == WS, "workspace_id roundtrip")
    check(ws["display_name"] == "Novelle", "display_name roundtrip")
    check(ws["is_active"] is True, "is_active true by default")

    rows = bw.list_workspaces()
    check(len(rows) >= 1, "list_workspaces returns at least one")
    check(any(r["workspace_id"] == WS for r in rows),
          "novelle present in list")


def test_seed_rules() -> None:
    section("audit_rules: seed + list")
    count = ar.seed_default_rules()
    check(count >= 0, "seed_default_rules returns count")

    rows = ar.list_active_rules(workspace_id=None, include_global=True)
    global_rules = [r for r in rows if r.get("workspace_id") is None]
    check(len(global_rules) == 15,
          f"15 global SEED_RULES present (got {len(global_rules)})")

    # Re-seeding should be idempotent (no duplicates)
    ar.seed_default_rules()
    rows2 = ar.list_active_rules(workspace_id=None, include_global=True)
    global2 = [r for r in rows2 if r.get("workspace_id") is None]
    check(len(global2) == 15, "re-seed is idempotent")


def test_rule_fork() -> None:
    section("audit_rules: fork_for_brand")
    rows = ar.list_active_rules(workspace_id=None, include_global=True)
    target = next((r for r in rows if r["name"] == "fewer_than_7_images"), None)
    check(target is not None, "fewer_than_7_images global rule exists")

    new_id = ar.fork_for_brand(
        target["rule_id"], WS,
        threshold_overrides={"min_images": 5},
        forked_by="devang",
        reasoning="Novelle launches at 5 images while live photoshoot catches up.",
    )
    check(new_id is not None, "fork_for_brand returns new rule_id")

    # Idempotent fork: second attempt should NOT create a duplicate
    new_id2 = ar.fork_for_brand(
        target["rule_id"], WS,
        threshold_overrides={"min_images": 5},
        forked_by="devang",
    )
    check(new_id2 == new_id, "fork is idempotent")

    resolved = ar.resolve_rules_for_brand(WS)
    fork_resolved = next(
        (r for r in resolved if r["name"] == "fewer_than_7_images"), None,
    )
    check(fork_resolved is not None, "resolved set contains fork")
    check(fork_resolved.get("workspace_id") == WS,
          "fork resolved with workspace override (brand override wins)")


def test_ingest_roxy() -> None:
    section("catalog_ingest: end-to-end on Roxy XLSX")
    if not os.path.exists(ROXY_XLSX):
        print(f"   skip   Roxy XLSX missing at {ROXY_XLSX} — skipping ingest test",
              flush=True)
        return

    t0 = time.time()
    result = ci.ingest_workbook(
        ROXY_XLSX, WS, write_substrate=True, ingested_by="devang",
    )
    elapsed = time.time() - t0
    print(f"        elapsed: {elapsed:.1f}s", flush=True)

    check(result.get("ok") is True, "ingest returns ok")
    check(result["rows_loaded"] > 0, "rows_loaded > 0")
    check(result["metadata_written"] > 0, "asin_metadata written")
    check(result["cohorts_classified"] > 0, "cohort_classifications written")
    check(result["outcome_events_written"] >= 0,
          "outcome_events count present")

    cohort_counts = result.get("cohort_counts") or {}
    check(cohort_counts.get("active", 0) > 0,
          f"active cohort > 0 (got {cohort_counts.get('active')})")
    check(sum(cohort_counts.values()) == result["cohorts_classified"],
          "cohort_counts sum == cohorts_classified")

    skipped = result.get("skipped_rules") or []
    check(isinstance(skipped, list), "skipped_rules is list")
    # Roxy XLSX won't have BSR/inventory connectors, so we expect at least
    # below_bsr_floor + out_of_stock_90d + missing reviews + missing care.
    check(len(skipped) >= 3,
          f"skipped_rules captures missing connectors (got {len(skipped)})")

    # Substrate counts via dedicated reader
    counts = ca.count_by_cohort(WS)
    check(counts.get("active", 0) == cohort_counts.get("active"),
          "count_by_cohort matches ingest report (active)")
    check(counts.get("unknown", 0) == cohort_counts.get("unknown", 0),
          "count_by_cohort matches ingest report (unknown)")


def test_coverage_endpoint_payload() -> None:
    section("catalog_audit: count_by_cohort shape")
    counts = ca.count_by_cohort(WS)
    check(isinstance(counts, dict), "count_by_cohort returns dict")
    for k in ("active", "dormant", "unknown"):
        check(k in counts, f"count_by_cohort has key {k}")


def main() -> int:
    setup()
    test_workspace_register()
    test_seed_rules()
    test_rule_fork()
    test_ingest_roxy()
    test_coverage_endpoint_payload()

    print()
    print("=" * 60)
    print(f"M6 Day 1 QA: {CHECKS - len(FAILURES)} / {CHECKS} passed")
    if FAILURES:
        print(f"FAILURES: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All M6 Day 1 assertions green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
