"""qa_m2.py — M2 substrate regression suite.

Asserts behavior of the six new tables added in schema v7 plus the
field_schema.yml / field_suggest contract. Runs against the local test
Postgres at postgresql://atlastest@/atlas_test?host=/tmp&port=55432.

Counts assertions explicitly so we can compare green deltas vs prior
QA suites (qa_context_layer.py = 35 assertions, qa_citation_chain.py =
41 assertions).

Usage:
    ATLAS_DATABASE_URL="postgresql://atlastest@/atlas_test?host=/tmp&port=55432" \\
        python qa_m2.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

# Ensure repo root on path
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if not os.environ.get("ATLAS_DATABASE_URL"):
    os.environ["ATLAS_DATABASE_URL"] = (
        "postgresql://atlastest@/atlas_test?host=/tmp&port=55432"
    )

from substrate import (
    asin_metadata as am,
    operator_positions as op,
    brand_position as bp,
    pricing_logic as pl,
    competitor_state as cs,
    field_suggest as fs,
)
from substrate.db import (
    apply_schema, get_pool, wipe_substrate_for_tests,
)


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


def test_asin_metadata() -> None:
    section("asin_metadata: parent/child inheritance")

    # 1. Parent insert
    ok = am.set_asin_metadata(
        WS, "B0PARENT",
        variation_family="velune_pocket",
        ground_truth_fields={
            "brand": "Novelle",
            "material": "79% Nylon, 21% Spandex",
            "pocket_description": "Hidden Waistband Pocket",
        },
        set_by="devang",
    )
    check(ok, "parent insert returns True")

    parent = am.get_asin_metadata(WS, "B0PARENT")
    check(parent is not None, "parent fetchable")
    check(parent["revision"] == 1, "parent revision=1 on insert")
    check(parent["variation_family"] == "velune_pocket",
          "parent family set")

    # 2. Child insert
    am.set_asin_metadata(
        WS, "B0CHILD-BLK-M",
        parent_asin="B0PARENT",
        variation_family="velune_pocket",
        variation_axes={"color": "Midnight Black", "size": "M"},
        ground_truth_fields={
            "color_name": "Midnight Black",
            "size": "M",
        },
        set_by="devang",
    )

    # 3. Inheritance read
    merged = am.read_asin_metadata(WS, "B0CHILD-BLK-M")
    check(merged is not None, "child read returns row")
    gtf = merged["ground_truth_fields"]
    check(gtf.get("brand") == "Novelle",
          "child inherits brand from parent")
    check(gtf.get("material") == "79% Nylon, 21% Spandex",
          "child inherits material from parent")
    check(gtf.get("color_name") == "Midnight Black",
          "child overrides own variation axis")
    check(merged.get("_inherited_from") == "B0PARENT",
          "merged result tags _inherited_from")

    # 4. Variation axis from parent must not leak when child sets its own
    am.set_asin_metadata(
        WS, "B0CHILD-NAVY-L",
        parent_asin="B0PARENT",
        variation_family="velune_pocket",
        variation_axes={"color": "Deep Navy", "size": "L"},
        ground_truth_fields={"color_name": "Deep Navy", "size": "L"},
        set_by="devang",
    )
    merged2 = am.read_asin_metadata(WS, "B0CHILD-NAVY-L")
    check(merged2["ground_truth_fields"].get("color_name") == "Deep Navy",
          "second child has own color")
    check(merged2["ground_truth_fields"].get("size") == "L",
          "second child has own size")

    # 5. confirm_field
    ok = am.confirm_field(WS, "B0PARENT", "material", "devang")
    check(ok, "confirm_field returns True")
    parent_after = am.get_asin_metadata(WS, "B0PARENT")
    check(
        parent_after["field_sources"]["material"]["confirmed_by_operator"]
        is True,
        "confirmed_by_operator flag set on material",
    )

    # 6. record_field_source updates both gtf and field_sources
    am.record_field_source(
        WS, "B0PARENT", "fabric_gsm",
        value=220, source="factory_provided",
        confirmed=True, set_by="devang",
    )
    p2 = am.get_asin_metadata(WS, "B0PARENT")
    check(p2["ground_truth_fields"]["fabric_gsm"] == 220,
          "fabric_gsm value persisted")
    check(p2["field_sources"]["fabric_gsm"]["source"] == "factory_provided",
          "fabric_gsm source labelled")

    # 7. list_family_asins
    family = am.list_family_asins(WS, "B0PARENT")
    asins = {r["asin"] for r in family}
    check("B0PARENT" in asins, "list_family includes parent")
    check("B0CHILD-BLK-M" in asins, "list_family includes child 1")
    check("B0CHILD-NAVY-L" in asins, "list_family includes child 2")
    check(len(family) == 3, f"list_family exact count = 3 (got {len(family)})")

    # 8. Revision bumps on subsequent set
    am.set_asin_metadata(
        WS, "B0PARENT",
        variation_family="velune_pocket",
        ground_truth_fields={**parent_after["ground_truth_fields"],
                             "brand": "Novelle"},
        field_sources=parent_after["field_sources"],
        set_by="devang",
    )
    p3 = am.get_asin_metadata(WS, "B0PARENT")
    check(p3["revision"] == 2, f"revision bumps on re-set (got {p3['revision']})")


def test_brand_position() -> None:
    section("brand_position")
    next_review = datetime.now(timezone.utc) + timedelta(days=90)
    ok = bp.set_brand_position(
        WS,
        position_statement="Premium-adjacent at $35-55.",
        competitor_set=["lululemon", "crz_yoga", "vuori"],
        competitor_role={
            "lululemon": "visual_anchor",
            "crz_yoga": "direct_competitor",
            "vuori": "price_ceiling",
        },
        price_band={"anchor_target": 42},
        positioning_hypothesis="Premium-curious shopper.",
        next_review_at=next_review,
        set_by="devang",
    )
    check(ok, "set_brand_position returns True")

    row = bp.get_brand_position(WS)
    check(row is not None, "row fetchable")
    check(row["revision"] == 1, "revision=1 on first set")
    check(len(row["competitor_set"]) == 3, "competitor_set length")
    check(row["competitor_role"]["lululemon"] == "visual_anchor",
          "competitor_role roundtrip")

    # Update should bump revision
    bp.set_brand_position(
        WS,
        position_statement=row["position_statement"],
        competitor_set=row["competitor_set"],
        competitor_role=row["competitor_role"],
        price_band={"anchor_target": 44},
        positioning_hypothesis=row["positioning_hypothesis"],
        next_review_at=next_review,
        set_by="devang",
    )
    row2 = bp.get_brand_position(WS)
    check(row2["revision"] == 2, "revision bumps on update")
    check(row2["price_band"]["anchor_target"] == 44, "price_band updated")

    # update_review_timestamp bumps last_reviewed_at but not revision
    bp.update_review_timestamp(
        WS,
        reaffirmed_by="devang",
        next_review_at=next_review + timedelta(days=90),
    )
    row3 = bp.get_brand_position(WS)
    check(row3["last_reviewed_at"] is not None,
          "last_reviewed_at populated after reaffirm")
    check(row3["revision"] == 2,
          "revision unchanged by review reaffirm")


def test_operator_positions() -> None:
    section("operator_positions: CRUD + scope priority")

    # Seed five
    p_strat_brand = op.create_position(
        WS, scope="brand", scope_ref=None,
        claim="Athletic only, no Casual",
        position_type="strategic",
    )
    p_hr_brand = op.create_position(
        WS, scope="brand", scope_ref=None,
        claim="No discount language",
        position_type="hard_refusal",
    )
    p_hr_fam = op.create_position(
        WS, scope="family", scope_ref="velune_pocket",
        claim="Family has hidden_waistband pocket",
        position_type="hard_refusal",
    )
    p_asin = op.create_position(
        WS, scope="asin", scope_ref="B0CHILD-BLK-M",
        claim="ASIN-specific style",
        position_type="style",
    )
    p_workflow = op.create_position(
        WS, scope="brand", scope_ref=None,
        claim="Launch velocity for 60 days",
        position_type="workflow",
    )
    check(all([p_strat_brand, p_hr_brand, p_hr_fam, p_asin, p_workflow]),
          "five positions create OK")

    # read_active_positions returns hard_refusals first regardless of scope
    rows = op.read_active_positions(
        WS, asin="B0CHILD-BLK-M", family="velune_pocket",
    )
    check(len(rows) == 5, f"read returns 5 positions (got {len(rows)})")
    check(rows[0]["position_type"] == "hard_refusal",
          "first position is hard_refusal")

    # Specificity within hard_refusal: asin > family > brand
    hr_only = [r for r in rows if r["position_type"] == "hard_refusal"]
    check(len(hr_only) == 2, "two hard_refusals")
    check(hr_only[0]["scope"] == "family",
          "family hard_refusal precedes brand hard_refusal")

    # archive_position
    ok = op.archive_position(p_workflow, "devang")
    check(ok, "archive_position returns True")
    rows2 = op.read_active_positions(
        WS, asin="B0CHILD-BLK-M", family="velune_pocket",
    )
    check(len(rows2) == 4, "archived position no longer in active set")

    # list_active_positions filter by type
    only_hr = op.list_active_positions(
        WS, position_type="hard_refusal",
    )
    check(len(only_hr) == 2, "filter by position_type=hard_refusal")

    # Invalid scope rejects
    bad = op.create_position(
        WS, scope="bogus", scope_ref=None,
        claim="bad", position_type="strategic",
    )
    check(bad is None, "invalid scope returns None")


def test_pricing_logic() -> None:
    section("pricing_logic + pricing_decisions")

    floor_rule = {
        "method": "variable_contribution_zero",
        "components": [
            "landed_cost", "fba_fee", "third_pl_fee",
            "referral_fee_15pct", "ad_spend_per_unit",
        ],
        "behavior_when_components_missing":
            "use_forecast_and_flag_in_provenance",
    }
    ceiling_rule = {
        "method": "operator_manual",
        "anchor_reference": "CRZ Yoga premium pocket legging",
        "current_value": None,
    }
    ok = pl.set_pricing_logic(
        WS, scope="global", scope_ref=None,
        floor_rule=floor_rule, ceiling_rule=ceiling_rule,
        reasoning="Locked 2026-05-18", set_by="devang",
    )
    check(ok, "set_pricing_logic returns True")

    row = pl.read_active_logic(WS, asin="B0CHILD-BLK-M")
    check(row is not None, "read_active_logic finds global fallback")
    check(row["floor_rule"]["method"] == "variable_contribution_zero",
          "floor_rule method")

    # ASIN-scoped overrides global
    pl.set_pricing_logic(
        WS, scope="asin", scope_ref="B0CHILD-BLK-M",
        floor_rule={"method": "variable_contribution_zero"},
        ceiling_rule={"method": "operator_manual", "current_value": 55.0},
        reasoning="ASIN-specific", set_by="devang",
    )
    row_asin = pl.read_active_logic(WS, asin="B0CHILD-BLK-M")
    check(row_asin["scope"] == "asin",
          "asin scope takes priority over global")
    check(row_asin["ceiling_rule"]["current_value"] == 55.0,
          "asin scope ceiling differs from global")

    # compute_floor_from_rule
    floor = pl.compute_floor_from_rule(
        floor_rule,
        landed_cost=10.0, fba_fee=4.50,
        third_pl_fee=1.20, ad_spend_per_unit=8.0,
    )
    # total = 23.70; floor = 23.70 / 0.85 = 27.882...
    check(27.0 < floor < 29.0,
          f"compute_floor in plausible range (got {floor})")

    # log_pricing_decision
    did = pl.log_pricing_decision(
        WS, asin="B0CHILD-BLK-M",
        price_set=39.0, price_set_by="devang",
        mode="manual", goal_regime="launch_velocity",
        floor_at_time=floor, reasoning="Day 0",
    )
    check(did is not None, "log_pricing_decision returns id")

    rows = pl.list_pricing_decisions(WS, asin="B0CHILD-BLK-M")
    check(len(rows) == 1, "one decision logged")
    check(rows[0]["price_set"] == 39.0, "price_set roundtrip")
    check(rows[0]["mode"] == "manual", "mode roundtrip")

    # attach_outcome
    ok = pl.attach_outcome(
        did, window="30d",
        outcome={"units": 220, "tacos": 0.31},
    )
    check(ok, "attach_outcome 30d returns True")
    rows2 = pl.list_pricing_decisions(WS, asin="B0CHILD-BLK-M")
    check(rows2[0]["outcome_at_30d"] is not None,
          "outcome_at_30d populated")

    # Invalid mode rejects
    bad = pl.log_pricing_decision(
        WS, asin="B0CHILD-BLK-M", price_set=10.0,
        price_set_by="devang", mode="bogus",
    )
    check(bad is None, "invalid mode rejected")


def test_competitor_state() -> None:
    section("competitor_state")

    oid = cs.record_observation(
        WS, competitor_id="crz_yoga", metric="price",
        value=44.99, observed_by="devang",
    )
    check(oid is not None, "record_observation returns id")

    # Later observation
    cs.record_observation(
        WS, competitor_id="crz_yoga", metric="price",
        value=46.99, observed_by="devang",
    )

    latest = cs.latest_value(WS, "crz_yoga", "price")
    check(latest is not None, "latest_value returns row")
    check(latest["value"] == 46.99, "latest_value picks newer observation")

    bsr = cs.record_observation(
        WS, competitor_id="crz_yoga", metric="bsr",
        value={"category": "Active Leggings", "rank": 142},
        observed_by="devang",
    )
    check(bsr is not None, "structured value (bsr) accepted")

    bad = cs.record_observation(
        WS, competitor_id="crz_yoga", metric="bogus_metric",
        value=1.0, observed_by="devang",
    )
    check(bad is None, "invalid metric rejected")


def test_field_suggest() -> None:
    section("field_suggest: schema modes")

    schema = fs.load_field_schema()
    check("asin_metadata" in schema, "schema includes asin_metadata")
    check("brand_voice" in schema, "schema includes brand_voice")
    check("pricing_logic" in schema, "schema includes pricing_logic")
    check("competitor_state" in schema, "schema includes competitor_state")

    spec = fs.suggest_for_field("asin_metadata", "material")
    check(spec["mode"] == "manual_only",
          "material is manual_only")
    check("consistency_check" in spec,
          "material declares consistency_check")

    spec = fs.suggest_for_field("asin_metadata", "fabric_type")
    check(spec["mode"] == "substrate_read", "fabric_type is substrate_read")
    check(spec.get("read_from") == "asin_metadata.material",
          "fabric_type read_from material")

    spec = fs.suggest_for_field(
        "asin_metadata", "upf",
    )
    check(spec["mode"] == "q_and_a", "upf is q_and_a")
    check("UPF" in spec["question"], "upf question content")

    spec = fs.suggest_for_field(
        "asin_metadata", "weave_type",
        context={"material": "Nylon Spandex", "stretch": "4-way"},
    )
    check(spec["mode"] == "llm_suggest", "weave_type is llm_suggest")
    # LLM may or may not be reachable in CI; we just confirm structure.
    check("prompt_text" in spec,
          "llm_suggest returns prompt_text in result")
    check("Nylon Spandex" in spec["prompt_text"],
          "context interpolated into prompt template")

    # Unknown field
    bad = fs.suggest_for_field("asin_metadata", "definitely_not_a_field")
    check(bad["ok"] is False, "unknown field returns ok=false")


def main() -> int:
    setup()
    test_asin_metadata()
    test_brand_position()
    test_operator_positions()
    test_pricing_logic()
    test_competitor_state()
    test_field_suggest()

    print()
    print("=" * 60)
    print(f"M2 QA: {CHECKS - len(FAILURES)} / {CHECKS} passed")
    if FAILURES:
        print(f"FAILURES: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All M2 assertions green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
