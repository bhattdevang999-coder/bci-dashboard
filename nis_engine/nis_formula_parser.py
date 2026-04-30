r"""Amazon NIS formula parser — tokenizer + recursive-descent parser.

Returns an AST (abstract syntax tree) as nested dicts. The dashboard's evaluator
walks the AST with a form-state dict to decide if a cell is required/optional/hidden.

Grammar (verified across 31 templates by audit_nis_rules.py):

    expr        := or_expr
    or_expr     := and_expr ('OR' '(' arg_list ')' | comparison)*    # see below
    and_expr    := unary ('AND' '(' arg_list ')' | comparison)*
    unary       := 'NOT' '(' expr ')' | primary
    primary     := function_call | cell_ref | literal | '(' expr ')'
    function    := AND | OR | NOT | IF | COUNTIF | VLOOKUP | ISERROR | LEN | INDIRECT
    comparison  := primary ('=' | '<>' | '>' | '<' | '>=' | '<=') primary
    arg_list    := expr (',' expr)*
    cell_ref    := /[A-Z]+\d+/
    literal     := /"[^"]*"/ | /\d+(\.\d+)?/ | TRUE | FALSE
    named_range := /[A-Z_]+_\d+/    # e.g., CONDITION_LIST_3

Excel-style: AND / OR / NOT etc. are written as function calls in the source
formulas (e.g., `AND(x, y)` not `x AND y`). So the parser reads them as function
invocations, not infix operators.

Anything outside this set returns a node with `unknown=True` and the original text.
"""

import re
from typing import Any, Dict, List, Optional, Tuple


# Functions we understand. Anything outside this set is flagged unknown.
KNOWN_FUNCTIONS = {
    "AND", "OR", "NOT", "IF", "COUNTIF", "VLOOKUP", "ISERROR",
    "LEN", "INDIRECT", "TRUE", "FALSE", "ISBLANK", "ISTEXT",
    "ISNUMBER", "EXACT", "TRIM",
}


# ============================================================================
# Tokenizer
# ============================================================================

class Token:
    __slots__ = ("kind", "value", "pos")
    def __init__(self, kind: str, value: str, pos: int):
        self.kind = kind     # FUNC, IDENT, NUMBER, STRING, OP, COMMA, LPAREN, RPAREN, EOF
        self.value = value
        self.pos = pos
    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, pos={self.pos})"


_TOKEN_PATTERN = re.compile(r"""
    \s+                                                            |   # whitespace -> ignored
    "(?:[^"\\]|\\.)*"                                              |   # string literal
    \d+(?:\.\d+)?                                                  |   # number literal
    <>|>=|<=|=|<|>                                                 |   # comparison operators
    &                                                              |   # string concat operator
    \(                                                             |   # left paren
    \)                                                             |   # right paren
    ,                                                              |   # comma
    '[^']+'!\$?[A-Z]+\$?\d+:\$?[A-Z]+\$?\d+                        |   # sheet-qualified range: 'Sheet'!$A$1:$B$16000
    \$?[A-Z]+\$?\d+:\$?[A-Z]+\$?\d+                                |   # range with optional $ absolute refs: $A$1:$B$10
    \$?[A-Z]+\$?\d+                                                |   # cell ref with optional $ absolute: $A$1, A1, $A1, A$1
    [A-Za-z_][A-Za-z0-9_]*                                             # identifier / function name / named range
""", re.VERBOSE)


def tokenize(formula: str) -> List[Token]:
    """Convert formula string to a list of Tokens. Raises ValueError on unknown chars."""
    tokens: List[Token] = []
    pos = 0
    while pos < len(formula):
        m = _TOKEN_PATTERN.match(formula, pos)
        if not m:
            raise ValueError(f"Unexpected character at pos {pos}: {formula[pos]!r} in {formula!r}")
        text = m.group(0)
        new_pos = m.end()
        if text.strip() == "":
            pos = new_pos
            continue
        if text == "(":
            tokens.append(Token("LPAREN", text, pos))
        elif text == ")":
            tokens.append(Token("RPAREN", text, pos))
        elif text == ",":
            tokens.append(Token("COMMA", text, pos))
        elif text == "&":
            tokens.append(Token("CONCAT", text, pos))
        elif text in ("=", "<>", ">", "<", ">=", "<="):
            tokens.append(Token("OP", text, pos))
        elif text.startswith('"'):
            # strip surrounding quotes; keep escapes raw (Amazon doesn't use escapes in these formulas)
            tokens.append(Token("STRING", text[1:-1], pos))
        elif re.match(r'^\d', text):
            tokens.append(Token("NUMBER", text, pos))
        elif text.startswith("'") and "!" in text:
            # sheet-qualified range: 'Dropdown Lists'!$A$1:$B$16000
            tokens.append(Token("RANGE", text, pos))
        elif re.match(r'^\$?[A-Z]+\$?\d+:\$?[A-Z]+\$?\d+$', text):
            # range reference (with optional $ absolute refs)
            tokens.append(Token("RANGE", text, pos))
        else:
            # identifier — could be function name, cell ref, named range, or boolean literal
            upper = text.upper()
            if upper in ("TRUE", "FALSE"):
                tokens.append(Token("BOOL", upper, pos))
            elif re.match(r'^\$?[A-Z]+\$?\d+$', text):
                # cell ref with optional $ absolute markers — strip $ for canonical form
                canonical = text.replace("$", "")
                tokens.append(Token("CELLREF", canonical, pos))
            elif upper in KNOWN_FUNCTIONS:
                tokens.append(Token("FUNC", upper, pos))
            else:
                # treat as identifier (named range, or unknown function — disambiguate during parse)
                tokens.append(Token("IDENT", text, pos))
        pos = new_pos
    tokens.append(Token("EOF", "", pos))
    return tokens


# ============================================================================
# AST node constructors
# ============================================================================

def node_func(name: str, args: List[dict]) -> dict:
    return {"type": "func", "name": name, "args": args}

def node_cell(ref: str) -> dict:
    # Split into column letters and row number for evaluator convenience
    m = re.match(r'^([A-Z]+)(\d+)$', ref)
    return {"type": "cell", "ref": ref, "col": m.group(1) if m else ref, "row": int(m.group(2)) if m else 0}

def node_literal(kind: str, value: Any) -> dict:
    return {"type": "literal", "kind": kind, "value": value}

def node_named(name: str) -> dict:
    return {"type": "named", "name": name}

def node_compare(op: str, left: dict, right: dict) -> dict:
    return {"type": "compare", "op": op, "left": left, "right": right}

def node_concat(parts: List[dict]) -> dict:
    return {"type": "concat", "parts": parts}

def node_unknown(text: str, reason: str) -> dict:
    return {"type": "unknown", "text": text, "reason": reason, "needs_review": True}


# ============================================================================
# Recursive-descent parser
# ============================================================================

class Parser:
    def __init__(self, tokens: List[Token], original_formula: str):
        self.tokens = tokens
        self.pos = 0
        self.original = original_formula
        self.errors: List[str] = []

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[self.pos + offset]

    def consume(self, expected_kind: str = None, expected_value: str = None) -> Token:
        tok = self.tokens[self.pos]
        if expected_kind and tok.kind != expected_kind:
            raise ValueError(f"Expected {expected_kind} but got {tok.kind}={tok.value!r} at pos {tok.pos}")
        if expected_value and tok.value != expected_value:
            raise ValueError(f"Expected {expected_value!r} but got {tok.value!r} at pos {tok.pos}")
        self.pos += 1
        return tok

    def parse(self) -> dict:
        try:
            ast = self.parse_expr()
            if self.peek().kind != "EOF":
                self.errors.append(f"Trailing tokens after parse: {self.peek()}")
                return node_unknown(self.original, "trailing tokens")
            return ast
        except Exception as e:
            return node_unknown(self.original, f"parse error: {e}")

    def parse_expr(self) -> dict:
        # Excel formulas are basically all function calls + comparisons.
        # No infix logical operators in this dialect.
        return self.parse_comparison()

    def parse_comparison(self) -> dict:
        left = self.parse_concat()
        if self.peek().kind == "OP":
            op = self.consume().value
            right = self.parse_concat()
            return node_compare(op, left, right)
        return left

    def parse_concat(self) -> dict:
        # Excel's & string concatenation operator. Left-associative, all binary.
        left = self.parse_primary()
        if self.peek().kind != "CONCAT":
            return left
        parts = [left]
        while self.peek().kind == "CONCAT":
            self.consume("CONCAT")
            parts.append(self.parse_primary())
        return node_concat(parts)

    def parse_primary(self) -> dict:
        tok = self.peek()

        # Parenthesized expression
        if tok.kind == "LPAREN":
            self.consume("LPAREN")
            inner = self.parse_expr()
            self.consume("RPAREN")
            return inner

        # Function call
        if tok.kind == "FUNC":
            return self.parse_function()

        # Identifier — could be unknown function (followed by paren) or named range
        if tok.kind == "IDENT":
            if self.peek(1).kind == "LPAREN":
                # unknown function — flag and skip its argument list
                fname = self.consume().value
                self.consume("LPAREN")
                # skip args until matching paren
                depth = 1
                while self.peek().kind != "EOF" and depth > 0:
                    if self.peek().kind == "LPAREN":
                        depth += 1
                    elif self.peek().kind == "RPAREN":
                        depth -= 1
                        if depth == 0:
                            break
                    self.consume()
                if self.peek().kind == "RPAREN":
                    self.consume("RPAREN")
                return node_unknown(f"{fname}(...)", f"unknown function: {fname}")
            else:
                # named range, e.g., CONDITION_LIST_3
                name = self.consume().value
                return node_named(name)

        # Cell reference
        if tok.kind == "CELLREF":
            return node_cell(self.consume().value)

        # Range reference (e.g. A1:A10) — used as VLOOKUP/COUNTIF/SUM arguments
        if tok.kind == "RANGE":
            r = self.consume().value
            return {"type": "range", "ref": r}

        # String literal
        if tok.kind == "STRING":
            return node_literal("string", self.consume().value)

        # Number literal
        if tok.kind == "NUMBER":
            v = self.consume().value
            return node_literal("number", float(v) if "." in v else int(v))

        # Boolean literal
        if tok.kind == "BOOL":
            v = self.consume().value
            return node_literal("bool", v == "TRUE")

        raise ValueError(f"Unexpected token {tok.kind}={tok.value!r} at pos {tok.pos}")

    def parse_function(self) -> dict:
        fname = self.consume("FUNC").value
        self.consume("LPAREN")
        args = []
        # Handle empty arg list
        if self.peek().kind == "RPAREN":
            self.consume("RPAREN")
            return node_func(fname, args)
        # Parse first argument
        args.append(self.parse_expr())
        while self.peek().kind == "COMMA":
            self.consume("COMMA")
            args.append(self.parse_expr())
        self.consume("RPAREN")
        return node_func(fname, args)


# ============================================================================
# Public API
# ============================================================================

def parse_formula(formula: str) -> dict:
    """Parse a NIS Excel formula string into an AST.

    Returns a dict node. If the formula uses unknown constructs, returns a node
    with type='unknown' and needs_review=True so the dashboard can flag it.

    Never raises — always returns something the evaluator can handle.
    """
    if not formula or not isinstance(formula, str):
        return node_unknown(str(formula), "empty or non-string formula")

    formula = formula.strip()
    # Some formulas come in wrapped with a leading = (Excel convention) — strip it
    if formula.startswith("="):
        formula = formula[1:]

    try:
        tokens = tokenize(formula)
    except ValueError as e:
        return node_unknown(formula, f"tokenize error: {e}")

    parser = Parser(tokens, formula)
    return parser.parse()


def collect_cell_refs(ast: dict) -> List[str]:
    """Walk an AST and return every cell reference it depends on.

    Used by the dashboard to know which fields trigger re-evaluation.
    """
    refs: List[str] = []
    def walk(n):
        if not isinstance(n, dict):
            return
        if n.get("type") == "cell":
            refs.append(n["ref"])
        elif n.get("type") in ("func", "compare", "concat"):
            for child_key in ("args", "left", "right", "parts"):
                child = n.get(child_key)
                if isinstance(child, list):
                    for c in child:
                        walk(c)
                elif isinstance(child, dict):
                    walk(child)
    walk(ast)
    # Preserve order, deduplicate
    seen = set()
    out = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def collect_named_refs(ast: dict) -> List[str]:
    """Walk an AST and return every named range reference (CONDITION_LIST_X, etc.)."""
    refs: List[str] = []
    def walk(n):
        if not isinstance(n, dict):
            return
        if n.get("type") == "named":
            refs.append(n["name"])
        elif n.get("type") in ("func", "compare", "concat"):
            for child_key in ("args", "left", "right", "parts"):
                child = n.get(child_key)
                if isinstance(child, list):
                    for c in child:
                        walk(c)
                elif isinstance(child, dict):
                    walk(child)
    walk(ast)
    seen = set()
    out = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def has_unknowns(ast: dict) -> bool:
    """True if the AST contains any unknown nodes that need human review."""
    if not isinstance(ast, dict):
        return False
    if ast.get("needs_review"):
        return True
    for child_key in ("args", "left", "right", "parts"):
        child = ast.get(child_key)
        if isinstance(child, list):
            for c in child:
                if has_unknowns(c):
                    return True
        elif isinstance(child, dict):
            if has_unknowns(child):
                return True
    return False
