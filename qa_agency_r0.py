"""qa_agency_r0.py — Tahari R0 regression harness for Sheik's agency feedback.

This is the running regression set that grows across Passes 1-6 of the
agency-feedback sprint. Every pass that claims to fix an item must add an
assertion here that proves the fix on the Tahari R0 fixture.

Fixture:
    data/fixtures/tahari_r0_preupload_template_2026_05_14.xlsx
    data/fixtures/tahari_r0_feedback_2026_05_14.xlsx

Items tracked:
    See docs/agency_r0_tracker.md for the 15 items, their root causes, and
    the pass each is fixed in.

Pass status:
    Pass 0 — fixture loads, 0 items fixed
    Pass 1 — items 5, 8, 9, 11, 13 (parser + verdict)
    Pass 2 — items 1, 2, 3, 10, 14 (taxonomy)
    Pass 3 — items 11, 15 (multi-value)
    Pass 4 — Strategic 1 (scope-aware editing)
    Pass 5 — items 6, 7, 12 (cosmetic)
    Pass 6 — Strategic 2 (template v2)

Run:
    python qa_agency_r0.py
    python qa_agency_r0.py --pass 1     # gate assertions to a single pass
    python qa_agency_r0.py --verbose
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "data" / "fixtures"
TEMPLATE_FIXTURE = FIXTURES / "tahari_r0_preupload_template_2026_05_14.xlsx"
FEEDBACK_FIXTURE = FIXTURES / "tahari_r0_feedback_2026_05_14.xlsx"

# Make repo importable
sys.path.insert(0, str(ROOT))


# ============================================================================
# Assertion framework
# ============================================================================

_results: list[tuple[str, str, str, str | None]] = []
# (pass_label, item_id, name, error_message_or_None)


def assert_eq(actual, expected, msg: str = ""):
    if actual != expected:
        raise AssertionError(f"{msg or 'expected =='} → expected={expected!r}, actual={actual!r}")


def assert_in(needle, haystack, msg: str = ""):
    if needle not in haystack:
        raise AssertionError(f"{msg or 'expected in'} → {needle!r} not in {haystack!r}")


def assert_truthy(v, msg: str = ""):
    if not v:
        raise AssertionError(f"{msg or 'expected truthy'} → got {v!r}")


def assert_not_in(needle, haystack, msg: str = ""):
    if needle in haystack:
        raise AssertionError(f"{msg or 'expected not in'} → {needle!r} present in {haystack!r}")


def case(pass_label: str, item_id: str, name: str):
    """Decorator: register a test case under a pass + item."""
    def wrap(fn):
        def runner():
            try:
                fn()
                _results.append((pass_label, item_id, name, None))
                return True
            except AssertionError as e:
                _results.append((pass_label, item_id, name, str(e)))
                return False
            except Exception as e:
                _results.append((pass_label, item_id, name, f"UNEXPECTED: {type(e).__name__}: {e}"))
                return False
        runner.pass_label = pass_label
        runner.item_id = item_id
        runner.name = name
        return runner
    return wrap


# ============================================================================
# PASS 0 — fixture sanity (the only assertions today)
# ============================================================================

@case("0", "fixture", "Tahari preupload template fixture present")
def test_fixture_template_present():
    assert_truthy(TEMPLATE_FIXTURE.exists(), f"missing fixture: {TEMPLATE_FIXTURE}")


@case("0", "fixture", "Tahari feedback fixture present")
def test_fixture_feedback_present():
    assert_truthy(FEEDBACK_FIXTURE.exists(), f"missing fixture: {FEEDBACK_FIXTURE}")


@case("0", "fixture", "Preupload template parses with openpyxl")
def test_fixture_template_loads():
    from openpyxl import load_workbook
    wb = load_workbook(str(TEMPLATE_FIXTURE), data_only=True)
    ws = wb["Pre-Upload Template"]
    # First row should be the header row
    headers = [c.value for c in ws[1]]
    assert_in("Brand Code", headers)
    assert_in("STYLE#", headers)
    assert_in("Closure Type", headers)
    assert_in("Fabric Content Percentage", headers)
    # Should have >= 20 real data rows (Tahari had 23 styles)
    nonempty = sum(1 for row in ws.iter_rows(min_row=4, values_only=True) if row and row[0])
    if nonempty < 20:
        raise AssertionError(f"expected ≥20 data rows, got {nonempty}")


@case("0", "fixture", "Feedback fixture has the 15 numbered items + final thoughts")
def test_fixture_feedback_loads():
    from openpyxl import load_workbook
    wb = load_workbook(str(FEEDBACK_FIXTURE), data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(min_row=4, values_only=True))
    # Issue # column is the first; items 1..16
    issue_nums = [r[0] for r in rows if r[0] is not None]
    nums_seen = set()
    for n in issue_nums:
        try:
            nums_seen.add(int(n))
        except (ValueError, TypeError):
            pass
    expected = set(range(1, 17))
    missing = expected - nums_seen
    if missing:
        raise AssertionError(f"feedback missing items: {sorted(missing)}")


# ============================================================================
# PASS 1 — placeholders (each becomes a real assertion when Pass 1 ships)
# ============================================================================

@case("1", "item_5", "Engine Verdict reports zero false-positive blockers on Tahari R0")
def test_pass1_item5_no_false_blockers():
    raise AssertionError("PENDING — Pass 1 not started")


@case("1", "item_8", "Department + Target Gender populated for Tahari Women's puffer rows")
def test_pass1_item8_dept_gender_flow():
    raise AssertionError("PENDING — Pass 1 not started")


@case("1", "item_9", "Closure Type from template surfaces in engine view")
def test_pass1_item9_closure_flow():
    raise AssertionError("PENDING — Pass 1 not started")


@case("1", "item_11_a", "Material field receives parsed material names (not percentage string)")
def test_pass1_item11_material_parse():
    raise AssertionError("PENDING — Pass 1 not started")


@case("1", "item_13", "Sleeve Length parses from template and is editable in engine view")
def test_pass1_item13_sleeve_length():
    raise AssertionError("PENDING — Pass 1 not started")


# ============================================================================
# PASS 2 — taxonomy unsticking (placeholders)
# ============================================================================

@case("2", "item_1", "ITK dropdown does not retain swimwear options after PT switch to COAT")
def test_pass2_item1_itk_dropdown_reset():
    raise AssertionError("PENDING — Pass 2 not started")


@case("2", "item_2", "Manual PT selection on UNKNOWN row repopulates Cat/Subcat/ITK cascade")
def test_pass2_item2_unknown_recovery():
    raise AssertionError("PENDING — Pass 2 not started")


@case("2", "item_3", "Bulk-taxonomy view: UNKNOWN PT manual choice flows taxonomy options")
def test_pass2_item3_bulk_taxonomy_unknown():
    raise AssertionError("PENDING — Pass 2 not started")


@case("2", "item_10", "Item Length Description default reflects current PT, not previous PT")
def test_pass2_item10_item_length_pt_aware():
    raise AssertionError("PENDING — Pass 2 not started")


@case("2", "item_14", "UNKNOWN taxonomy editable from style-level AND bulk views")
def test_pass2_item14_unknown_editable():
    raise AssertionError("PENDING — Pass 2 not started")


# ============================================================================
# PASS 3 — multi-value (placeholders)
# ============================================================================

@case("3", "item_11_b", "Material parses to ordered array; multi-material round-trips")
def test_pass3_item11_material_multi():
    raise AssertionError("PENDING — Pass 3 not started")


@case("3", "item_15", "Closure Type accepts and persists multiple values")
def test_pass3_item15_closure_multi():
    raise AssertionError("PENDING — Pass 3 not started")


# ============================================================================
# PASS 4 — scope-aware editing
# ============================================================================

@case("4", "strategic_1", "scope=brand_always edit on style #1 propagates to style #2")
def test_pass4_scope_brand_always_reads_through():
    raise AssertionError("PENDING — Pass 4 not started")


# ============================================================================
# PASS 5 — cosmetic
# ============================================================================

@case("5", "item_6", "Style blocks render without hover-only blackout")
def test_pass5_item6_style_block_visible():
    raise AssertionError("PENDING — Pass 5 not started")


@case("5", "item_7", "Bullet formatter does not add ALLCAPS+dash when ALLCAPS+colon present")
def test_pass5_item7_bullet_colon_separator():
    # When this lands, the assertion will look like:
    #   from nis_engine.content_rules import format_bullet
    #   out = format_bullet("ELEVATED WARMTH: The quilted puffer construction...")
    #   assert_truthy(out.startswith("ELEVATED WARMTH:"))
    #   assert_not_in("ELEVATED WARMTH: THE —", out)
    #   assert_not_in("ELEVATED WARMTH —", out)
    raise AssertionError("PENDING — Pass 5 not started")


@case("5", "item_12", "All Fields > Content reads operator-edited value, not original (or removed)")
def test_pass5_item12_content_tab_sync():
    raise AssertionError("PENDING — Pass 5 not started")


# ============================================================================
# PASS 6 — template v2
# ============================================================================

@case("6", "strategic_2", "v2 template parses cleanly with new Amazon-attribute columns")
def test_pass6_template_v2_parse():
    raise AssertionError("PENDING — Pass 6 not started")


@case("6", "strategic_2", "v1 template still parses via mapping layer with no data loss")
def test_pass6_template_v1_mapping_lossless():
    raise AssertionError("PENDING — Pass 6 not started")


# ============================================================================
# Runner
# ============================================================================

ALL_CASES = [
    test_fixture_template_present,
    test_fixture_feedback_present,
    test_fixture_template_loads,
    test_fixture_feedback_loads,
    test_pass1_item5_no_false_blockers,
    test_pass1_item8_dept_gender_flow,
    test_pass1_item9_closure_flow,
    test_pass1_item11_material_parse,
    test_pass1_item13_sleeve_length,
    test_pass2_item1_itk_dropdown_reset,
    test_pass2_item2_unknown_recovery,
    test_pass2_item3_bulk_taxonomy_unknown,
    test_pass2_item10_item_length_pt_aware,
    test_pass2_item14_unknown_editable,
    test_pass3_item11_material_multi,
    test_pass3_item15_closure_multi,
    test_pass4_scope_brand_always_reads_through,
    test_pass5_item6_style_block_visible,
    test_pass5_item7_bullet_colon_separator,
    test_pass5_item12_content_tab_sync,
    test_pass6_template_v2_parse,
    test_pass6_template_v1_mapping_lossless,
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pass", dest="pass_filter", default=None,
                   help="Only run cases for this pass label (e.g. 0, 1, 2). Default: all.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    cases = [c for c in ALL_CASES
             if args.pass_filter is None or c.pass_label == args.pass_filter]

    print(f"\nqa_agency_r0.py — Tahari R0 regression harness")
    print(f"running {len(cases)} cases" + (f" (pass={args.pass_filter})" if args.pass_filter else ""))
    print("=" * 72)

    for c in cases:
        c()

    # Group results by pass
    by_pass: dict[str, list] = {}
    for entry in _results:
        by_pass.setdefault(entry[0], []).append(entry)

    pending_count = 0
    pass_count = 0
    fail_count = 0
    for pass_label in sorted(by_pass.keys()):
        print(f"\nPass {pass_label}:")
        for _, item, name, err in by_pass[pass_label]:
            if err is None:
                status = "PASS"
                pass_count += 1
            elif err.startswith("PENDING"):
                status = "PEND"
                pending_count += 1
            else:
                status = "FAIL"
                fail_count += 1
            print(f"  [{status}] {item:<14s} {name}")
            if err and args.verbose:
                print(f"           └─ {err}")

    print("\n" + "=" * 72)
    print(f"summary: {pass_count} pass, {fail_count} fail, {pending_count} pending")
    # Pending isn't a failure — passes 1-6 will turn pending into pass
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
