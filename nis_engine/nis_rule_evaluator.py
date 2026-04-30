"""Amazon NIS rule evaluator — walks an AST built by nis_formula_parser against
form state and returns the formula's value + a verdict the dashboard can act on.

How the rule engine uses this:
    parser  -> AST
    AST + form_state -> evaluate -> truthy/falsy/error
    truthy  -> field is REQUIRED (or HIDDEN, depending on rule kind)
    falsy   -> rule does not fire (field is OPTIONAL)
    error   -> NEEDS REVIEW (logged, dashboard surfaces a yellow flag)

Excel semantics that matter here (verified across the 26,371 real NIS formulas):
- Empty cell == "" (Amazon writes `D7=""` to mean "D7 is blank")
- AND/OR/NOT take logicals; numbers/strings only ever feed them via comparisons
- IF(cond, true_branch, false_branch) returns one branch
- VLOOKUP returns #N/A on no-match; INDIRECT returns #REF! on missing name
- ISERROR catches both — used heavily for cascade dropdowns
- COUNTIF(named_range, value) counts matches; >0 means "value is in this set"
- LEN("") == 0; LEN("abc") == 3
- Errors propagate through expressions UNLESS caught by ISERROR

Anything we don't recognise returns an `EvalError` sentinel. The rule_verdict()
wrapper translates that into `verdict='error'` so the operator can override.
"""

from typing import Any, Dict, List, Optional, Set, Tuple, Union


# ============================================================================
# Excel-style error sentinels
# ============================================================================

class EvalError:
    """Excel error value. Propagates through expressions unless caught by ISERROR."""
    __slots__ = ("kind", "detail")
    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind     # 'NA', 'REF', 'VALUE', 'NAME', 'DIV0', 'UNKNOWN'
        self.detail = detail
    def __repr__(self):
        return f"#{self.kind}!" + (f"({self.detail})" if self.detail else "")
    def __bool__(self):
        # Outside ISERROR, errors are falsy AND propagate. We model propagation
        # explicitly in the evaluator; this dunder is a safety net.
        return False
    def __eq__(self, other):
        return isinstance(other, EvalError) and other.kind == self.kind

NA_ERROR    = EvalError("NA")
REF_ERROR   = EvalError("REF")
VALUE_ERROR = EvalError("VALUE")
NAME_ERROR  = EvalError("NAME")


def is_error(v: Any) -> bool:
    return isinstance(v, EvalError)


# ============================================================================
# Coercion helpers
# ============================================================================

def coerce_bool(v: Any) -> Union[bool, EvalError]:
    """Convert a value to boolean using Excel's rules. Errors propagate."""
    if is_error(v):
        return v
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        # Excel: "TRUE"/"FALSE" -> bool; other text -> #VALUE!.
        # Empty string is falsy in our world (Amazon writes `=""` for blank).
        upper = v.strip().upper()
        if upper == "TRUE":  return True
        if upper == "FALSE": return False
        if v == "":          return False
        # Anything else feeding a logical context: treat as VALUE_ERROR. In
        # practice this never happens in real NIS formulas (always wrapped
        # in comparisons).
        return EvalError("VALUE", f"cannot coerce {v!r} to bool")
    if v is None:
        return False
    return EvalError("VALUE", f"cannot coerce {type(v).__name__} to bool")


def coerce_str(v: Any) -> Union[str, EvalError]:
    """Convert to string for & concatenation."""
    if is_error(v):
        return v
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        # Excel doesn't print trailing .0 for whole floats
        return str(int(v)) if v.is_integer() else str(v)
    if v is None:
        return ""
    return str(v)


def coerce_number(v: Any) -> Union[float, int, EvalError]:
    if is_error(v):
        return v
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        if v == "":
            return 0
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except ValueError:
            return EvalError("VALUE", f"cannot coerce {v!r} to number")
    return EvalError("VALUE", f"cannot coerce {type(v).__name__} to number")


def excel_equal(a: Any, b: Any) -> bool:
    """Excel '=' comparison. Strings are case-insensitive; "" matches None/empty."""
    if is_error(a) or is_error(b):
        return False
    # Treat None and "" as the same blank cell
    if (a is None or a == "") and (b is None or b == ""):
        return True
    if isinstance(a, str) and isinstance(b, str):
        return a.upper() == b.upper()
    # Mixed string/number — try numeric compare, fallback to string
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a == b


# ============================================================================
# Evaluation context
# ============================================================================

class EvalContext:
    """Bundle of state needed to evaluate a formula.

    state: dict of cell_ref -> user-entered value (e.g., {"D7": "Parent", "A7": ""}).
           Missing cells are treated as "" (blank).
    named_ranges: dict of name -> list of values (e.g., {"CONDITION_LIST_3": ["foo", "bar"]}).
                  Used by COUNTIF and INDIRECT.
    vlookup_tables: dict of range_ref -> list of (key, value, ...) rows.
                    Real templates put these in the 'Dropdown Lists' sheet.
    indirect_names: set of named ranges that exist. INDIRECT returns #REF! for any name not in here.
    """
    __slots__ = ("state", "named_ranges", "vlookup_tables", "indirect_names")
    def __init__(
        self,
        state: Optional[Dict[str, Any]] = None,
        named_ranges: Optional[Dict[str, List[Any]]] = None,
        vlookup_tables: Optional[Dict[str, List[List[Any]]]] = None,
        indirect_names: Optional[Set[str]] = None,
    ):
        self.state = state or {}
        self.named_ranges = named_ranges or {}
        self.vlookup_tables = vlookup_tables or {}
        self.indirect_names = indirect_names or set()

    def get_cell(self, ref: str) -> Any:
        v = self.state.get(ref, "")
        return v if v is not None else ""


# ============================================================================
# Function implementations
# ============================================================================

def fn_AND(args: List[Any]) -> Union[bool, EvalError]:
    if not args:
        return True  # Excel: AND() with no args → TRUE
    for a in args:
        b = coerce_bool(a)
        if is_error(b): return b
        if not b: return False
    return True


def fn_OR(args: List[Any]) -> Union[bool, EvalError]:
    if not args:
        return False
    seen_error = None
    for a in args:
        b = coerce_bool(a)
        if is_error(b):
            seen_error = b
            continue
        if b: return True
    return seen_error if seen_error else False


def fn_NOT(args: List[Any]) -> Union[bool, EvalError]:
    if len(args) != 1:
        return EvalError("VALUE", f"NOT expects 1 arg, got {len(args)}")
    b = coerce_bool(args[0])
    if is_error(b): return b
    return not b


def fn_IF(args: List[Any]) -> Any:
    if len(args) < 2 or len(args) > 3:
        return EvalError("VALUE", f"IF expects 2 or 3 args, got {len(args)}")
    cond = coerce_bool(args[0])
    if is_error(cond): return cond
    if cond:
        return args[1]
    return args[2] if len(args) == 3 else False


def fn_LEN(args: List[Any]) -> Union[int, EvalError]:
    if len(args) != 1:
        return EvalError("VALUE", f"LEN expects 1 arg, got {len(args)}")
    s = coerce_str(args[0])
    if is_error(s): return s
    return len(s)


def fn_ISERROR(args: List[Any]) -> bool:
    if len(args) != 1:
        return False
    return is_error(args[0])


def fn_ISBLANK(args: List[Any]) -> bool:
    if len(args) != 1:
        return False
    v = args[0]
    return v is None or v == ""


def fn_ISTEXT(args: List[Any]) -> bool:
    return len(args) == 1 and isinstance(args[0], str) and not is_error(args[0])


def fn_ISNUMBER(args: List[Any]) -> bool:
    return len(args) == 1 and isinstance(args[0], (int, float)) and not isinstance(args[0], bool)


def fn_EXACT(args: List[Any]) -> Union[bool, EvalError]:
    if len(args) != 2:
        return EvalError("VALUE", "EXACT expects 2 args")
    a, b = coerce_str(args[0]), coerce_str(args[1])
    if is_error(a): return a
    if is_error(b): return b
    return a == b  # case-sensitive (unlike =)


def fn_TRIM(args: List[Any]) -> Union[str, EvalError]:
    if len(args) != 1:
        return EvalError("VALUE", "TRIM expects 1 arg")
    s = coerce_str(args[0])
    if is_error(s): return s
    return " ".join(s.split())


def fn_TRUE(args: List[Any]) -> bool: return True
def fn_FALSE(args: List[Any]) -> bool: return False


# COUNTIF, VLOOKUP, INDIRECT need the eval context — handled inline in evaluator
# rather than via the simple pure-function dispatch below.


PURE_FUNCTIONS = {
    "AND": fn_AND, "OR": fn_OR, "NOT": fn_NOT, "IF": fn_IF,
    "LEN": fn_LEN, "ISERROR": fn_ISERROR, "ISBLANK": fn_ISBLANK,
    "ISTEXT": fn_ISTEXT, "ISNUMBER": fn_ISNUMBER,
    "EXACT": fn_EXACT, "TRIM": fn_TRIM,
    "TRUE": fn_TRUE, "FALSE": fn_FALSE,
}


# ============================================================================
# Core evaluator
# ============================================================================

def evaluate(ast: dict, ctx: EvalContext) -> Any:
    """Walk an AST and return its value. Returns an EvalError if anything goes wrong."""
    if not isinstance(ast, dict):
        return EvalError("VALUE", f"non-dict AST node: {ast!r}")

    t = ast.get("type")

    # ---- literals
    if t == "literal":
        return ast.get("value")

    # ---- cell reference
    if t == "cell":
        return ctx.get_cell(ast["ref"])

    # ---- range reference (used as VLOOKUP/COUNTIF arg) — pass ref through
    if t == "range":
        return {"__range__": ast["ref"]}

    # ---- named range
    if t == "named":
        name = ast["name"]
        if name in ctx.named_ranges:
            return {"__named__": name, "values": ctx.named_ranges[name]}
        # No data loaded — treat as a still-valid named range marker (so that
        # INDIRECT or COUNTIF can decide what to do). For COUNTIF without data
        # we conservatively return 0 matches; for INDIRECT we look at indirect_names.
        return {"__named__": name, "values": None}

    # ---- compare
    if t == "compare":
        left  = evaluate(ast["left"], ctx)
        right = evaluate(ast["right"], ctx)
        if is_error(left):  return left
        if is_error(right): return right
        op = ast["op"]
        if op == "=":   return excel_equal(left, right)
        if op == "<>":  return not excel_equal(left, right)
        # Order comparisons: coerce to numbers when both are numeric, else strings
        if op in (">", "<", ">=", "<="):
            ln = coerce_number(left) if not isinstance(left, str) or _looks_numeric(left) else None
            rn = coerce_number(right) if not isinstance(right, str) or _looks_numeric(right) else None
            if ln is not None and rn is not None and not is_error(ln) and not is_error(rn):
                a, b = ln, rn
            else:
                a, b = coerce_str(left), coerce_str(right)
                if is_error(a): return a
                if is_error(b): return b
            if op == ">":  return a > b
            if op == "<":  return a < b
            if op == ">=": return a >= b
            if op == "<=": return a <= b
        return EvalError("VALUE", f"unknown comparison op: {op}")

    # ---- string concatenation (a & b & c)
    if t == "concat":
        out = []
        for p in ast["parts"]:
            v = evaluate(p, ctx)
            if is_error(v): return v
            s = coerce_str(v)
            if is_error(s): return s
            out.append(s)
        return "".join(out)

    # ---- function call
    if t == "func":
        name = ast["name"]

        # ISERROR is special: must NOT propagate the error of its argument
        if name == "ISERROR":
            if len(ast["args"]) != 1:
                return False
            v = evaluate(ast["args"][0], ctx)
            return is_error(v)

        # IF is special: only evaluate the chosen branch (Excel short-circuits)
        if name == "IF":
            args = ast["args"]
            if len(args) < 2 or len(args) > 3:
                return EvalError("VALUE", f"IF expects 2 or 3 args, got {len(args)}")
            cond = evaluate(args[0], ctx)
            if is_error(cond): return cond
            cb = coerce_bool(cond)
            if is_error(cb): return cb
            if cb:
                return evaluate(args[1], ctx)
            return evaluate(args[2], ctx) if len(args) == 3 else False

        # AND/OR also short-circuit
        if name == "AND":
            for a in ast["args"]:
                v = evaluate(a, ctx)
                b = coerce_bool(v)
                if is_error(b): return b
                if not b: return False
            return True
        if name == "OR":
            seen_err = None
            for a in ast["args"]:
                v = evaluate(a, ctx)
                b = coerce_bool(v)
                if is_error(b):
                    seen_err = b
                    continue
                if b: return True
            return seen_err if seen_err else False

        # COUNTIF(range_or_named, criterion) — count how many values match criterion
        if name == "COUNTIF":
            args = ast["args"]
            if len(args) != 2:
                return EvalError("VALUE", "COUNTIF expects 2 args")
            range_node = evaluate(args[0], ctx)
            criterion = evaluate(args[1], ctx)
            if is_error(criterion): return criterion
            values = _resolve_range_values(range_node, ctx)
            if values is None:
                # Range data not loaded — return 0 (conservative; we'll flag this separately)
                return 0
            return sum(1 for v in values if excel_equal(v, criterion))

        # VLOOKUP(lookup, table, col_idx, exact) — returns #N/A on miss
        if name == "VLOOKUP":
            args = ast["args"]
            if len(args) < 3:
                return EvalError("VALUE", f"VLOOKUP expects 3-4 args, got {len(args)}")
            lookup = evaluate(args[0], ctx)
            if is_error(lookup): return lookup
            table_node = evaluate(args[1], ctx)
            col_v = evaluate(args[2], ctx)
            col_n = coerce_number(col_v)
            if is_error(col_n): return col_n
            col_idx = int(col_n)
            rows = _resolve_table_rows(table_node, ctx)
            if rows is None:
                # Table not loaded — return NA so ISERROR catches it
                return NA_ERROR
            for row in rows:
                if len(row) >= col_idx and excel_equal(row[0], lookup):
                    return row[col_idx - 1]
            return NA_ERROR

        # INDIRECT(name_string) — returns #REF! if the name doesn't exist
        if name == "INDIRECT":
            args = ast["args"]
            if len(args) < 1:
                return EvalError("VALUE", "INDIRECT expects 1 arg")
            target = evaluate(args[0], ctx)
            if is_error(target): return target
            target_str = coerce_str(target)
            if is_error(target_str): return target_str
            # Direct cell ref string?
            if target_str in ctx.state:
                return ctx.get_cell(target_str)
            # Named range?
            if target_str in ctx.named_ranges:
                return {"__named__": target_str, "values": ctx.named_ranges[target_str]}
            if target_str in ctx.indirect_names:
                # Name exists in registry but data not loaded — treat as a usable ref
                return {"__indirect__": target_str, "values": None}
            return REF_ERROR

        # Pure functions (no context needed)
        if name in PURE_FUNCTIONS:
            arg_values = [evaluate(a, ctx) for a in ast["args"]]
            return PURE_FUNCTIONS[name](arg_values)

        return EvalError("NAME", f"unknown function: {name}")

    # ---- unknown
    if t == "unknown":
        return EvalError("UNKNOWN", ast.get("reason", "parser flagged unknown node"))

    return EvalError("VALUE", f"unhandled AST node type: {t}")


def _looks_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _resolve_range_values(node: Any, ctx: EvalContext) -> Optional[List[Any]]:
    """Return a flat list of values from a range or named-range node, or None if data missing."""
    if isinstance(node, dict):
        if "__named__" in node:
            return node.get("values")
        if "__range__" in node:
            ref = node["__range__"]
            tbl = ctx.vlookup_tables.get(ref)
            if tbl is None: return None
            # Flatten first column for COUNTIF
            return [r[0] for r in tbl if r]
        if "__indirect__" in node:
            return node.get("values")
    return None


def _resolve_table_rows(node: Any, ctx: EvalContext) -> Optional[List[List[Any]]]:
    """Return rows of a table from a range node, or None if not loaded."""
    if isinstance(node, dict):
        if "__range__" in node:
            return ctx.vlookup_tables.get(node["__range__"])
        if "__named__" in node:
            vals = node.get("values")
            if vals is None: return None
            # Treat 1-D list as single-column rows
            return [[v] for v in vals]
    return None


# ============================================================================
# Public rule-verdict API
# ============================================================================

def rule_verdict(
    ast: dict,
    state: Dict[str, Any],
    named_ranges: Optional[Dict[str, List[Any]]] = None,
    vlookup_tables: Optional[Dict[str, List[List[Any]]]] = None,
    indirect_names: Optional[Set[str]] = None,
    rule_kind: str = "required",
) -> dict:
    """Evaluate a parsed rule and return a structured verdict.

    rule_kind:
        'required' -> truthy means the field is REQUIRED
        'hidden'   -> truthy means the field is HIDDEN
        'visible'  -> truthy means the field is VISIBLE (inverse of hidden)
        'valid'    -> truthy means the value is VALID
        'invalid'  -> truthy means there is an ERROR
        'raw'      -> return the raw value, no verdict mapping

    Returns:
        {
            'verdict': 'required' | 'optional' | 'hidden' | 'visible' | 'error' | 'review',
            'value':   <raw evaluation result>,
            'cells_used': [list of cell refs the formula depends on],
            'named_used': [list of named ranges the formula depends on],
            'error':   None or {'kind': 'NA'|..., 'detail': str},
        }
    """
    try:
        from .nis_formula_parser import collect_cell_refs, collect_named_refs, has_unknowns
    except ImportError:
        from nis_formula_parser import collect_cell_refs, collect_named_refs, has_unknowns

    cells_used = collect_cell_refs(ast)
    named_used = collect_named_refs(ast)

    if has_unknowns(ast):
        return {
            "verdict": "review",
            "value": None,
            "cells_used": cells_used,
            "named_used": named_used,
            "error": {"kind": "UNKNOWN", "detail": "formula contains constructs the parser flagged for review"},
        }

    ctx = EvalContext(
        state=state,
        named_ranges=named_ranges,
        vlookup_tables=vlookup_tables,
        indirect_names=indirect_names,
    )
    value = evaluate(ast, ctx)

    if is_error(value):
        return {
            "verdict": "error",
            "value": None,
            "cells_used": cells_used,
            "named_used": named_used,
            "error": {"kind": value.kind, "detail": value.detail},
        }

    if rule_kind == "raw":
        return {
            "verdict": "raw", "value": value,
            "cells_used": cells_used, "named_used": named_used, "error": None,
        }

    truthy = coerce_bool(value)
    if is_error(truthy):
        return {
            "verdict": "error", "value": value,
            "cells_used": cells_used, "named_used": named_used,
            "error": {"kind": truthy.kind, "detail": truthy.detail},
        }

    verdict_map = {
        "required": ("required", "optional"),
        "hidden":   ("hidden",   "visible"),
        "visible":  ("visible",  "hidden"),
        "valid":    ("valid",    "invalid"),
        "invalid":  ("invalid",  "valid"),
    }
    yes, no = verdict_map.get(rule_kind, ("required", "optional"))
    return {
        "verdict": yes if truthy else no,
        "value": value,
        "cells_used": cells_used,
        "named_used": named_used,
        "error": None,
    }
