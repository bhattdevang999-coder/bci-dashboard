"""Tests for nis_formula_parser.py — runs synthetic + real-world formulas through it."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nis_formula_parser import (
    parse_formula, tokenize, collect_cell_refs, collect_named_refs, has_unknowns
)


# ============================================================================
# Test runner (no pytest — just simple assertions with clear output)
# ============================================================================

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
# Section 1: Tokenizer basics
# ============================================================================

def test_tokenizer():
    print("\n--- Tokenizer ---")
    toks = tokenize("AND(D7=\"Parent\")")
    kinds = [t.kind for t in toks]
    check("tokenize AND(D7=\"Parent\")",
          kinds == ["FUNC", "LPAREN", "CELLREF", "OP", "STRING", "RPAREN", "EOF"],
          f"got {kinds}")

    toks = tokenize("COUNTIF(CONDITION_LIST_3, E7)")
    vals = [t.value for t in toks if t.kind != "EOF"]
    check("tokenize COUNTIF with named range",
          vals == ["COUNTIF", "(", "CONDITION_LIST_3", ",", "E7", ")"],
          f"got {vals}")

    toks = tokenize("LEN(A7)>0")
    kinds = [t.kind for t in toks]
    check("tokenize LEN(A7)>0",
          kinds == ["FUNC", "LPAREN", "CELLREF", "RPAREN", "OP", "NUMBER", "EOF"],
          f"got {kinds}")

    toks = tokenize('NOT(D7<>"Parent")')
    check("tokenize NOT with <> operator",
          [t.value for t in toks if t.kind == "OP"] == ["<>"],
          f"got operators: {[t.value for t in toks if t.kind == 'OP']}")

    # Boolean literals
    toks = tokenize("IF(TRUE, FALSE, A7)")
    bools = [t.value for t in toks if t.kind == "BOOL"]
    check("tokenize TRUE/FALSE", bools == ["TRUE", "FALSE"], f"got {bools}")

    # Multi-letter columns (AB7, BC42)
    toks = tokenize("AB7")
    check("multi-letter column reference",
          toks[0].kind == "CELLREF" and toks[0].value == "AB7",
          f"got {toks[0]}")


# ============================================================================
# Section 2: Parser — synthetic small cases
# ============================================================================

def test_parser_small():
    print("\n--- Parser: small synthetic ---")

    # A simple equality
    ast = parse_formula('D7="Parent"')
    check("parse D7=\"Parent\"",
          ast.get("type") == "compare" and ast.get("op") == "=" and
          ast["left"]["type"] == "cell" and ast["left"]["ref"] == "D7" and
          ast["right"]["type"] == "literal" and ast["right"]["value"] == "Parent",
          f"got {ast}")

    # NOT(...)
    ast = parse_formula('NOT(D7="Parent")')
    check("parse NOT(D7=\"Parent\")",
          ast.get("type") == "func" and ast.get("name") == "NOT" and
          len(ast["args"]) == 1 and ast["args"][0]["type"] == "compare",
          f"got {ast}")

    # AND with two args
    ast = parse_formula('AND(D7="Parent", F7="")')
    check("parse AND(D7=\"Parent\", F7=\"\")",
          ast.get("name") == "AND" and len(ast["args"]) == 2,
          f"got {ast}")

    # COUNTIF returning a number, then compared
    ast = parse_formula('COUNTIF(CONDITION_LIST_3, E7)>0')
    check("parse COUNTIF(...)>0 comparison",
          ast.get("type") == "compare" and ast.get("op") == ">" and
          ast["left"]["type"] == "func" and ast["left"]["name"] == "COUNTIF",
          f"got {ast}")

    # IF(condition, true_branch, false_branch)
    ast = parse_formula('IF(LEN(A7)>0, 1, 0)')
    check("parse IF(LEN(A7)>0, 1, 0)",
          ast.get("name") == "IF" and len(ast["args"]) == 3 and
          ast["args"][1]["value"] == 1 and ast["args"][2]["value"] == 0,
          f"got {ast}")

    # INDIRECT (used for dropdown sources)
    ast = parse_formula('INDIRECT("COATproduct_type1.value")')
    check("parse INDIRECT(\"COATproduct_type1.value\")",
          ast.get("name") == "INDIRECT" and len(ast["args"]) == 1 and
          ast["args"][0]["value"] == "COATproduct_type1.value",
          f"got {ast}")

    # Empty AND/OR (Amazon really does emit these, e.g., AND(AND((0)),1=1))
    ast = parse_formula('AND((0))')
    check("parse AND((0)) — empty/zero arg",
          ast.get("name") == "AND" and len(ast["args"]) == 1,
          f"got {ast}")

    # & string concatenation (cascade dropdown pattern)
    ast = parse_formula('"COAT"&"product_subcategory.value."&D7')
    check("parse string concat with &",
          ast.get("type") == "concat" and len(ast["parts"]) == 3,
          f"got {ast}")

    # Sheet-qualified range with $ absolute refs
    ast = parse_formula("VLOOKUP(M7,'Dropdown Lists'!$A$1:$B$16000,2,FALSE)")
    check("parse VLOOKUP with sheet-qualified range",
          ast.get("name") == "VLOOKUP" and len(ast["args"]) == 4 and
          ast["args"][1]["type"] == "range" and "Dropdown Lists" in ast["args"][1]["ref"],
          f"got {ast}")

    # $ absolute cell ref normalised to plain ref
    ast = parse_formula('$A$7="Parent"')
    check("parse $A$7 (absolute ref) -> A7",
          ast.get("type") == "compare" and ast["left"]["ref"] == "A7",
          f"got {ast}")

    # Full cascade-dropdown pattern from real COAT template
    ast = parse_formula(
        'IF(NOT(ISERROR(INDIRECT("COAT"&"league_name.value."&'
        "VLOOKUP(CR7,'Dropdown Lists'!$A$1:$B$16000,2,FALSE)"
        '&".team_name1.value"))),'
        'INDIRECT("COAT"&"league_name.value."&'
        "VLOOKUP(CR7,'Dropdown Lists'!$A$1:$B$16000,2,FALSE)"
        '&".team_name1.value"),"")'
    )
    check("parse cascade dropdown formula (real COAT)",
          ast.get("name") == "IF" and not has_unknowns(ast),
          f"unknowns? {has_unknowns(ast)}, type={ast.get('type')}")


# ============================================================================
# Section 3: Parser — real formulas from the COAT template
# ============================================================================

def test_parser_real_formulas():
    print("\n--- Parser: real COAT template formulas ---")

    # From sheet3 of Jackets_and_Coats — these are actual conditional formatting
    # formulas extracted earlier this session.

    real_formulas = [
        # A7 vendor code: required when D7 (Parentage Level) = "Parent"
        # (extracted verbatim from sheet3 of Jackets_and_Coats template)
        ('A7 vendor code rule',
         'AND(AND(AND((AND(OR(OR(AND(NOT(D7="")))),A7<>"")),(AND(OR(OR(OR(NOT(D7<>"Parent")))),A7<>"")),1=1)),1=1)'),

        # Length-greater-than-zero common pattern
        ('LEN >0 idiom',
         'IF(LEN(A7)>0,1,0)'),

        # Inverse: required and missing
        ('AND inverse missing pattern',
         'AND(NOT(LEN(A7)>0),A7<>"")'),

        # Always-true (Amazon emits literal 1=1 sometimes)
        ('1=1 literal compare',
         '1=1'),

        # Always-false placeholder pattern
        ('AND((0)) always-false placeholder',
         'AND(AND((0)),1=1)'),

        # The COUNTIF condition-list membership test
        ('COUNTIF condition list membership',
         'AND(A7<>"",OR(OR(AND(OR(OR(IF(COUNTIF(CONDITION_LIST_0, D7)>0,TRUE,FALSE)))))))'),

        # The G7 variation theme rule (multiple nested ORs)
        ('G7 variation theme rule',
         'AND(AND(AND((AND(OR(AND(AND(IF(COUNTIF(CONDITION_LIST_1, E7)>0,FALSE,TRUE))),AND(AND(NOT(D7<>"")))),A7<>"")),1=1)),1=1)'),
    ]

    for name, f in real_formulas:
        ast = parse_formula(f)
        check(f"parse: {name}",
              ast.get("type") in ("func", "compare", "literal") and not has_unknowns(ast),
              f"AST root type: {ast.get('type')}, unknown? {has_unknowns(ast)}, formula was: {f[:80]}...")


# ============================================================================
# Section 4: Parser — INDIRECT data validation pattern (dropdowns)
# ============================================================================

def test_parser_indirect_patterns():
    print("\n--- Parser: INDIRECT dropdown sources ---")

    # These are the dataValidation source formulas — one per dropdown cell
    indirect_samples = [
        ('COAT', 'INDIRECT("COATrtip_vendor_code1.value")'),
        ('BLAZER', 'INDIRECT("BLAZERparentage_level1.value")'),
        ('SWIMWEAR', 'INDIRECT("SWIMWEARproduct_type1.value")'),
        ('DRESS', 'INDIRECT("DRESSchild_parent_sku_relationship1.child_relationship_type")'),
    ]

    for label, f in indirect_samples:
        ast = parse_formula(f)
        check(f"INDIRECT pattern for {label}",
              ast.get("type") == "func" and ast.get("name") == "INDIRECT" and
              ast["args"][0]["type"] == "literal" and
              ast["args"][0]["value"].startswith(label),
              f"got {ast}")


# ============================================================================
# Section 5: Cell ref + named ref collection
# ============================================================================

def test_collectors():
    print("\n--- Cell + named ref collectors ---")

    ast = parse_formula('AND(NOT(D7="Parent"), OR(F7<>"", COUNTIF(CONDITION_LIST_3, E7)>0))')
    refs = collect_cell_refs(ast)
    check("collect_cell_refs finds D7, F7, E7",
          set(refs) == {"D7", "F7", "E7"},
          f"got {refs}")

    named = collect_named_refs(ast)
    check("collect_named_refs finds CONDITION_LIST_3",
          named == ["CONDITION_LIST_3"],
          f"got {named}")

    # Multiple cells preserved in order
    ast = parse_formula('AND(A7="", B7="", C7="")')
    refs = collect_cell_refs(ast)
    check("collect_cell_refs preserves order, dedupes",
          refs == ["A7", "B7", "C7"],
          f"got {refs}")


# ============================================================================
# Section 6: Unknown function safety net
# ============================================================================

def test_unknowns_safety():
    print("\n--- Unknown function safety net ---")

    ast = parse_formula('SUMPRODUCT(A1:A10, B1:B10)')
    check("SUMPRODUCT flagged unknown",
          has_unknowns(ast) and ast.get("type") == "unknown",
          f"got {ast}")

    # Mix: known wrapping unknown
    ast = parse_formula('AND(D7="x", SUMPRODUCT(A1:A10, B1:B10))')
    check("Mixed AND+unknown still parses, flags unknown subtree",
          ast.get("type") == "func" and has_unknowns(ast),
          f"got {ast}")

    # Empty / None / weird inputs
    check("empty string returns unknown",
          parse_formula("").get("type") == "unknown")
    check("None returns unknown",
          parse_formula(None).get("type") == "unknown")
    check("garbage characters returns unknown",
          parse_formula("@#$%^&").get("type") == "unknown")


# ============================================================================
# Section 7: Smoke test against the real .xlsm
# ============================================================================

def test_real_xlsm_smoke():
    print("\n--- Smoke test: parse all formulas in COAT template ---")

    import zipfile
    from xml.etree import ElementTree as ET
    NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'

    xlsm = '/home/user/workspace/Jackets_and_Coats_2026-04-15T21_23-2.xlsm'
    if not os.path.exists(xlsm):
        print(f"  ⚠ skipping — {xlsm} not found")
        return

    with zipfile.ZipFile(xlsm) as z:
        sheet_xml = z.read('xl/worksheets/sheet3.xml').decode('utf-8')

    root = ET.fromstring(sheet_xml)

    # Count CF + DV formulas
    cf_formulas = []
    for cf in root.iter(NS + 'conditionalFormatting'):
        for rule in cf.iter(NS + 'cfRule'):
            f = rule.find(NS + 'formula')
            if f is not None and f.text:
                cf_formulas.append(f.text)

    dv_formulas = []
    for dv in root.iter(NS + 'dataValidation'):
        f = dv.find(NS + 'formula1')
        if f is not None and f.text:
            dv_formulas.append(f.text)

    print(f"  Found {len(cf_formulas)} conditional formatting formulas")
    print(f"  Found {len(dv_formulas)} data validation formulas")

    parsed_ok, parsed_unknown, errored = 0, 0, 0
    sample_unknowns = []
    for f in cf_formulas + dv_formulas:
        ast = parse_formula(f)
        if ast.get("type") == "unknown" and ast.get("text") == f:
            errored += 1
        elif has_unknowns(ast):
            parsed_unknown += 1
            if len(sample_unknowns) < 3:
                sample_unknowns.append((f[:120], ast))
        else:
            parsed_ok += 1

    total = len(cf_formulas) + len(dv_formulas)
    print(f"  Parsed cleanly: {parsed_ok}/{total}")
    print(f"  Parsed with unknowns: {parsed_unknown}/{total}")
    print(f"  Failed to parse at all: {errored}/{total}")

    check("≥99% of real COAT formulas parse without errors",
          errored / total < 0.01 if total else True,
          f"errored={errored}, total={total}")
    check("All real COAT formulas yield a defined AST node",
          parsed_ok + parsed_unknown == total,
          f"missed {total - parsed_ok - parsed_unknown}")

    if sample_unknowns:
        print("\n  Sample formulas flagged with unknowns (for review):")
        for s, _ in sample_unknowns[:3]:
            print(f"    • {s}")


# ============================================================================
# Run all
# ============================================================================

if __name__ == '__main__':
    print("=" * 70)
    print("NIS Formula Parser — test suite")
    print("=" * 70)

    test_tokenizer()
    test_parser_small()
    test_parser_real_formulas()
    test_parser_indirect_patterns()
    test_collectors()
    test_unknowns_safety()
    test_real_xlsm_smoke()

    print("\n" + "=" * 70)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    if FAILURES:
        print("\nFAILURES:")
        for name, detail in FAILURES:
            print(f"  • {name}: {detail}")
        sys.exit(1)
    sys.exit(0)
