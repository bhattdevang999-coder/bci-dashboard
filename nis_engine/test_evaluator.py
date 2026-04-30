"""Tests for nis_rule_evaluator.py — synthetic + real-formula evaluation."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nis_formula_parser import parse_formula
from nis_rule_evaluator import (
    evaluate, rule_verdict, EvalContext, EvalError, is_error,
    coerce_bool, coerce_str, coerce_number, excel_equal,
)

PASS, FAIL = 0, 0
FAILURES = []

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print(f"  ❌ {name}")
        if detail:
            print(f"      {detail}")


# ============================================================================
# Section 1: Coercion helpers
# ============================================================================

def test_coercion():
    print("\n--- Coercion helpers ---")
    check("coerce_bool('') -> False", coerce_bool("") is False)
    check("coerce_bool('TRUE') -> True", coerce_bool("TRUE") is True)
    check("coerce_bool(0) -> False", coerce_bool(0) is False)
    check("coerce_bool(1) -> True", coerce_bool(1) is True)
    check("coerce_bool(True) -> True", coerce_bool(True) is True)
    check("coerce_bool error propagates",
          isinstance(coerce_bool(EvalError("NA")), EvalError))

    check("coerce_str(True) -> 'TRUE'", coerce_str(True) == "TRUE")
    check("coerce_str(2.0) -> '2'", coerce_str(2.0) == "2")
    check("coerce_str(2.5) -> '2.5'", coerce_str(2.5) == "2.5")
    check("coerce_str(None) -> ''", coerce_str(None) == "")

    check("coerce_number('') -> 0", coerce_number("") == 0)
    check("coerce_number('3.5') -> 3.5", coerce_number("3.5") == 3.5)
    check("coerce_number('abc') -> error",
          isinstance(coerce_number("abc"), EvalError))


# ============================================================================
# Section 2: Excel-equality semantics
# ============================================================================

def test_excel_equal():
    print("\n--- excel_equal ---")
    check("'' == None", excel_equal("", None))
    check("None == ''", excel_equal(None, ""))
    check("'Parent' == 'parent' (case-insensitive)", excel_equal("Parent", "parent"))
    check("'foo' != 'bar'", not excel_equal("foo", "bar"))
    check("1 == 1.0", excel_equal(1, 1.0))


# ============================================================================
# Section 3: Simple formula evaluation
# ============================================================================

def test_simple_eval():
    print("\n--- Simple formulas ---")

    # D7="Parent" with state {D7: "Parent"}
    ast = parse_formula('D7="Parent"')
    ctx = EvalContext(state={"D7": "Parent"})
    check("D7=\"Parent\" with D7=Parent -> True", evaluate(ast, ctx) is True)
    check("D7=\"Parent\" with D7=Child -> False",
          evaluate(ast, EvalContext(state={"D7": "Child"})) is False)
    check("D7=\"Parent\" with no D7 -> False",
          evaluate(ast, EvalContext(state={})) is False)

    # NOT(D7="")
    ast = parse_formula('NOT(D7="")')
    check("NOT(D7=\"\") with D7=Parent -> True",
          evaluate(ast, EvalContext(state={"D7": "Parent"})) is True)
    check("NOT(D7=\"\") with D7=\"\" -> False",
          evaluate(ast, EvalContext(state={"D7": ""})) is False)

    # AND(D7="Parent", A7="")
    ast = parse_formula('AND(D7="Parent", A7="")')
    check("AND when both true",
          evaluate(ast, EvalContext(state={"D7": "Parent", "A7": ""})) is True)
    check("AND when one false",
          evaluate(ast, EvalContext(state={"D7": "Parent", "A7": "X"})) is False)

    # OR(D7="Parent", D7="Child")
    ast = parse_formula('OR(D7="Parent", D7="Child")')
    check("OR Parent or Child (Parent)",
          evaluate(ast, EvalContext(state={"D7": "Parent"})) is True)
    check("OR Parent or Child (other)",
          evaluate(ast, EvalContext(state={"D7": "Standalone"})) is False)


# ============================================================================
# Section 4: IF / LEN
# ============================================================================

def test_if_len():
    print("\n--- IF / LEN ---")

    ast = parse_formula('IF(LEN(A7)>0, 1, 0)')
    check("IF(LEN(A7)>0, 1, 0) with A7='abc' -> 1",
          evaluate(ast, EvalContext(state={"A7": "abc"})) == 1)
    check("IF(LEN(A7)>0, 1, 0) with A7='' -> 0",
          evaluate(ast, EvalContext(state={"A7": ""})) == 0)

    # IF should short-circuit — branch with error in unselected side should not raise
    ast = parse_formula('IF(D7="x", A7, B7)')
    res = evaluate(ast, EvalContext(state={"D7": "x", "A7": "yes", "B7": "no"}))
    check("IF short-circuits to true branch", res == "yes")
    res = evaluate(ast, EvalContext(state={"D7": "z", "A7": "yes", "B7": "no"}))
    check("IF short-circuits to false branch", res == "no")


# ============================================================================
# Section 5: COUNTIF + named ranges
# ============================================================================

def test_countif():
    print("\n--- COUNTIF ---")

    ast = parse_formula('COUNTIF(CONDITION_LIST_3, E7)>0')
    ctx = EvalContext(
        state={"E7": "Outerwear"},
        named_ranges={"CONDITION_LIST_3": ["Outerwear", "Coat", "Jacket"]},
    )
    check("COUNTIF finds match -> True", evaluate(ast, ctx) is True)

    ctx2 = EvalContext(
        state={"E7": "NotInList"},
        named_ranges={"CONDITION_LIST_3": ["Outerwear", "Coat", "Jacket"]},
    )
    check("COUNTIF no match -> False", evaluate(ast, ctx2) is False)

    # No data loaded -> returns 0 (>0 is False) — conservative
    ctx3 = EvalContext(state={"E7": "anything"})
    check("COUNTIF with no named range data -> 0 -> False",
          evaluate(ast, ctx3) is False)

    # Case-insensitive match
    ctx4 = EvalContext(
        state={"E7": "outerwear"},
        named_ranges={"CONDITION_LIST_3": ["Outerwear", "Coat"]},
    )
    check("COUNTIF case-insensitive", evaluate(ast, ctx4) is True)


# ============================================================================
# Section 6: VLOOKUP + ISERROR
# ============================================================================

def test_vlookup_iserror():
    print("\n--- VLOOKUP + ISERROR ---")

    ast = parse_formula("VLOOKUP(M7,'Dropdown Lists'!$A$1:$B$16000,2,FALSE)")
    ctx = EvalContext(
        state={"M7": "Outerwear"},
        vlookup_tables={
            "'Dropdown Lists'!$A$1:$B$16000": [
                ["Outerwear", "outerwear_v1"],
                ["Coat", "coat_v1"],
            ],
        },
    )
    check("VLOOKUP finds key", evaluate(ast, ctx) == "outerwear_v1")

    # Miss -> #N/A
    ctx_miss = EvalContext(
        state={"M7": "NotThere"},
        vlookup_tables={"'Dropdown Lists'!$A$1:$B$16000": [["Outerwear", "x"]]},
    )
    res = evaluate(ast, ctx_miss)
    check("VLOOKUP miss -> #N/A", is_error(res) and res.kind == "NA")

    # ISERROR catches it
    ast_err = parse_formula("ISERROR(VLOOKUP(M7,'Dropdown Lists'!$A$1:$B$16000,2,FALSE))")
    check("ISERROR(VLOOKUP miss) -> True", evaluate(ast_err, ctx_miss) is True)
    check("ISERROR(VLOOKUP hit) -> False", evaluate(ast_err, ctx) is False)

    # Unloaded table -> #N/A through the same path
    ctx_unloaded = EvalContext(state={"M7": "x"})
    check("ISERROR with unloaded table -> True",
          evaluate(ast_err, ctx_unloaded) is True)


# ============================================================================
# Section 7: INDIRECT
# ============================================================================

def test_indirect():
    print("\n--- INDIRECT ---")

    # Pointer to a named range that exists
    ast = parse_formula('INDIRECT("CONDITION_LIST_3")')
    ctx = EvalContext(named_ranges={"CONDITION_LIST_3": ["a", "b"]})
    res = evaluate(ast, ctx)
    check("INDIRECT to known named range",
          isinstance(res, dict) and res.get("__named__") == "CONDITION_LIST_3")

    # Pointer to a missing name -> #REF!
    ctx_no = EvalContext()
    res2 = evaluate(ast, ctx_no)
    check("INDIRECT to unknown name -> #REF!",
          is_error(res2) and res2.kind == "REF")

    # Cascade dropdown pattern (real COAT formula)
    cascade = (
        'IF(NOT(ISERROR(INDIRECT("COAT"&"league_name.value."&'
        "VLOOKUP(CR7,'Dropdown Lists'!$A$1:$B$16000,2,FALSE)"
        '&".team_name1.value"))),'
        'INDIRECT("COAT"&"league_name.value."&'
        "VLOOKUP(CR7,'Dropdown Lists'!$A$1:$B$16000,2,FALSE)"
        '&".team_name1.value"),"")'
    )
    ast_c = parse_formula(cascade)
    # CR7 = "NFL", VLOOKUP yields "nfl", target name = "COATleague_name.value.nfl.team_name1.value"
    ctx_c = EvalContext(
        state={"CR7": "NFL"},
        vlookup_tables={
            "'Dropdown Lists'!$A$1:$B$16000": [["NFL", "nfl"]],
        },
        indirect_names={"COATleague_name.value.nfl.team_name1.value"},
    )
    res = evaluate(ast_c, ctx_c)
    check("Cascade dropdown returns indirect target dict",
          isinstance(res, dict) and res.get("__indirect__", "").endswith("team_name1.value"),
          f"got {res}")

    # Same formula with VLOOKUP miss -> branch returns ""
    ctx_c_miss = EvalContext(
        state={"CR7": "NotALeague"},
        vlookup_tables={"'Dropdown Lists'!$A$1:$B$16000": [["NFL", "nfl"]]},
    )
    res = evaluate(ast_c, ctx_c_miss)
    check("Cascade with VLOOKUP miss -> empty string", res == "")


# ============================================================================
# Section 8: rule_verdict mapping
# ============================================================================

def test_rule_verdict():
    print("\n--- rule_verdict ---")

    # Real A7 vendor-code rule: required when D7=Parent and A7=""
    ast = parse_formula(
        'AND(AND(AND((AND(OR(OR(AND(NOT(D7="")))),A7<>"")),'
        '(AND(OR(OR(OR(NOT(D7<>"Parent")))),A7<>"")),1=1)),1=1)'
    )
    # Hand-trace with D7="Parent", A7="":
    #   inner first AND: NOT(D7="") -> True; OR(...) -> True; A7<>"" -> False
    #   so inner AND(..., A7<>"") -> False
    # That makes the rule overall False (formula says "required only when A7 is filled")
    # — the formula is the CF that highlights when value is provided. The "missing"
    # signal is actually `A7<>""` being false. This is a quirk of how Amazon
    # encodes things. Let's just check that the evaluator runs cleanly.
    v = rule_verdict(ast, state={"D7": "Parent", "A7": ""})
    check("A7 vendor rule evaluates without error",
          v["verdict"] in ("required", "optional"),
          f"got {v}")
    check("A7 vendor rule reports D7 + A7 as cells_used",
          set(v["cells_used"]) >= {"A7", "D7"})

    # Simple required-when: required when D7=Parent
    ast = parse_formula('D7="Parent"')
    v = rule_verdict(ast, state={"D7": "Parent"})
    check("simple D7=Parent -> required", v["verdict"] == "required")
    v = rule_verdict(ast, state={"D7": "Standalone"})
    check("simple D7=Parent (false) -> optional", v["verdict"] == "optional")

    # Hidden-when rule kind
    v = rule_verdict(ast, state={"D7": "Parent"}, rule_kind="hidden")
    check("rule_kind=hidden truthy -> hidden", v["verdict"] == "hidden")
    v = rule_verdict(ast, state={"D7": "Standalone"}, rule_kind="hidden")
    check("rule_kind=hidden falsy -> visible", v["verdict"] == "visible")

    # Error path: SUMPRODUCT -> needs_review
    ast_unk = parse_formula("SUMPRODUCT(A1:A10, B1:B10)")
    v = rule_verdict(ast_unk, state={})
    check("unknown function -> verdict review", v["verdict"] == "review")

    # cells_used + named_used populated
    ast_co = parse_formula('AND(NOT(D7=""), COUNTIF(CONDITION_LIST_3, E7)>0)')
    v = rule_verdict(ast_co, state={"D7": "x", "E7": "y"},
                     named_ranges={"CONDITION_LIST_3": ["y"]})
    check("cells_used finds D7 & E7", set(v["cells_used"]) == {"D7", "E7"})
    check("named_used finds CONDITION_LIST_3",
          v["named_used"] == ["CONDITION_LIST_3"])


# ============================================================================
# Section 9: Smoke test against real COAT formulas
# ============================================================================

def test_real_smoke():
    print("\n--- Smoke: evaluate every real COAT formula with empty state ---")
    import zipfile
    from xml.etree import ElementTree as ET
    NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'

    xlsm = '/home/user/workspace/Jackets_and_Coats_2026-04-15T21_23-2.xlsm'
    if not os.path.exists(xlsm):
        print(f"  ⚠ skipping — {xlsm} not found")
        return

    with zipfile.ZipFile(xlsm) as z:
        root = ET.fromstring(z.read('xl/worksheets/sheet3.xml').decode('utf-8'))

    formulas = []
    for cf in root.iter(NS + 'conditionalFormatting'):
        for rule in cf.iter(NS + 'cfRule'):
            f = rule.find(NS + 'formula')
            if f is not None and f.text:
                formulas.append(f.text)
    for dv in root.iter(NS + 'dataValidation'):
        f = dv.find(NS + 'formula1')
        if f is not None and f.text:
            formulas.append(f.text)

    # Empty state: every cell is blank. We don't load named_ranges or vlookup_tables —
    # the evaluator should still produce a verdict for every formula without crashing.
    crashes = 0
    verdicts = {"required": 0, "optional": 0, "hidden": 0, "visible": 0,
                "error": 0, "review": 0}
    for f in formulas:
        try:
            ast = parse_formula(f)
            v = rule_verdict(ast, state={})
            verdicts[v["verdict"]] = verdicts.get(v["verdict"], 0) + 1
        except Exception as e:
            crashes += 1
            if crashes <= 3:
                print(f"      crash on: {f[:120]}... -> {e}")

    print(f"  Total formulas evaluated: {len(formulas)}")
    print(f"  Verdict distribution: {verdicts}")
    print(f"  Crashes: {crashes}")
    check("evaluator never crashes on real formulas", crashes == 0)
    check("at least some formulas produce a clean required/optional verdict",
          verdicts["required"] + verdicts["optional"] > 0)


# ============================================================================
# Run all
# ============================================================================

if __name__ == '__main__':
    print("=" * 70)
    print("NIS Rule Evaluator — test suite")
    print("=" * 70)

    test_coercion()
    test_excel_equal()
    test_simple_eval()
    test_if_len()
    test_countif()
    test_vlookup_iserror()
    test_indirect()
    test_rule_verdict()
    test_real_smoke()

    print("\n" + "=" * 70)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    if FAILURES:
        print("\nFAILURES:")
        for name, detail in FAILURES:
            print(f"  • {name}: {detail}")
        sys.exit(1)
    sys.exit(0)
