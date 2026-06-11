"""Conformance test for the Sigil-written parser (selfhost/parser.sg): the AST
it produces — dumped as a canonical S-expr — must byte-match the AST the
reference Python parser (sigil.parser) produces, dumped in the same form.
Bootstrap component #2 toward self-hosting (docs/SELFHOST.md). Covers the
expression grammar and the statement grammar (types, let/var/assign/return/
if/while+invariants/break/expression statements).

selfhost/parser.sg reads a source path on stdin; if the file begins with '{'
it parses a block, otherwise a single expression.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path

from sigil import ast_nodes as A
from sigil.checker import check
from sigil.interp import Interpreter
from sigil.modules import load_program
from sigil.parser import parse

REPO = Path(__file__).resolve().parent.parent
PARSER_SG = REPO / "selfhost" / "parser.sg"

EXPR_CORPUS = [
    "1 + 2 * 3", "(1 + 2) * 3", "a - b - c", "a + b - c + d",
    "2 * 3 + 4 * 5", "not a and b or c", "a == b and c != d",
    "x < 1 or y >= 2", "0 - 7", "0 - 7 / 2", "f(1, 2, 3)", "g()",
    "xs[0]", "p.x", "p.items[1].y", "len(xs) > 0 and i < n",
    "[1, 2, 3]", "[]", "[a, f(b), c[0]]", "not not x", "a * b * c % d",
    "0 - -5", "true and false", "1 + 2 + 3 + 4", "f(a)[0].b", "-x + y",
]

BLOCK_CORPUS = [
    "{ let x: Int = 1; return x; }",
    "{ var y: Int = 2; y = y + 1; return y; }",
    "{ let xs: List[Int] = [1, 2, 3]; return xs[0]; }",
    "{ if a > 0 { return 1; } return 0; }",
    "{ if a > 0 { return 1; } else { return 2; } }",
    "{ if a > 0 { return 1; } else if b > 0 { return 2; } else { return 3; } }",
    "{ var i: Int = 0; while i < n invariant i >= 0 { i = i + 1; } return i; }",
    "{ while i < n invariant i >= 0 invariant i <= n { i = i + 1; } return 0; }",
    "{ f(x); return; }",
    "{ break; }",
    "{ let p: Pair[Int, Text] = q; return 0; }",
    "{ let t: Text = \"hi\"; return 0; }",
    "{ let n: Int = - x + len(ys); return n; }",
]


# --- reference dumpers over the Python AST, matching parser.sg's format -------

def dump_expr(e) -> str:
    if isinstance(e, A.IntLit):
        return f"(int {e.value})"
    if isinstance(e, A.BoolLit):
        return f"(bool {'true' if e.value else 'false'})"
    if isinstance(e, A.TextLit):
        return f"(text {e.value})"
    if isinstance(e, A.Var):
        return f"(var {e.name})"
    if isinstance(e, A.Binary):
        return f"(bin {e.op} {dump_expr(e.left)} {dump_expr(e.right)})"
    if isinstance(e, A.Unary):
        return f"(un {e.op} {dump_expr(e.operand)})"
    if isinstance(e, A.Call):
        return "(call " + e.name + "".join(" " + dump_expr(a) for a in e.args) + ")"
    if isinstance(e, A.Index):
        return f"(idx {dump_expr(e.base)} {dump_expr(e.index)})"
    if isinstance(e, A.FieldAccess):
        return f"(field {dump_expr(e.base)} {e.field_name})"
    if isinstance(e, A.ListLit):
        return "(list" + "".join(" " + dump_expr(i) for i in e.items) + ")"
    raise AssertionError(f"unhandled expr {type(e).__name__}")


def dump_block(stmts) -> str:
    return "(block" + "".join(" " + dump_stmt(s) for s in stmts) + ")"


def dump_stmt(s) -> str:
    if isinstance(s, A.Let):
        kw = "var" if s.mutable else "let"
        return f"({kw} {s.name} {s.declared_type} {dump_expr(s.value)})"
    if isinstance(s, A.Assign):
        return f"(assign {s.name} {dump_expr(s.value)})"
    if isinstance(s, A.Return):
        return "(return-void)" if s.value is None else f"(return {dump_expr(s.value)})"
    if isinstance(s, A.ExprStmt):
        return f"(expr {dump_expr(s.expr)})"
    if isinstance(s, A.Break):
        return "(break)"
    if isinstance(s, A.If):
        if s.else_body is None:
            return f"(if {dump_expr(s.cond)} {dump_block(s.then_body)})"
        return (f"(if-else {dump_expr(s.cond)} {dump_block(s.then_body)} "
                f"{dump_block(s.else_body)})")
    if isinstance(s, A.While):
        invs = "".join(" " + dump_expr(c.expr) for c in s.invariants)
        return f"(while {dump_expr(s.cond)} (invs{invs}) {dump_block(s.body)})"
    raise AssertionError(f"unhandled stmt {type(s).__name__}")


def dump_str_list(label, xs) -> str:
    return "(" + label + "".join(" " + x for x in xs) + ")"


def dump_params(ps) -> str:
    return "(params" + "".join(f" (p {n} {t})" for n, t in ps) + ")"


def dump_contracts(cs) -> str:
    return "(contracts" + "".join(f" ({c.kind} {dump_expr(c.expr)})"
                                  for c in cs) + ")"


def dump_fn(f) -> str:
    return ("(fn " + f.name + " " + dump_str_list("tparams", f.type_params)
            + " " + dump_params(f.params) + f" {f.ret} "
            + dump_str_list("effects", sorted(f.effects)) + " "
            + dump_contracts(f.contracts) + " " + dump_block(f.body) + ")")


def dump_program(fns) -> str:
    return "(program" + "".join(" " + dump_fn(f) for f in fns) + ")"


def ref_expr(src: str) -> str:
    program = parse(f"fn f() -> Int {{ return {src}; }}")
    return dump_expr(program.functions[0].body[0].value)


def ref_block(src: str) -> str:
    program = parse(f"fn f() -> Int {src}")
    return dump_block(program.functions[0].body)


def ref_program(src: str) -> str:
    return dump_program(parse(src).functions)


# Programs (each function has at most one effect, since effect-set ordering is
# not yet expressible in the Sigil dumper — see selfhost/NOTES.md).
PROGRAM_CORPUS = [
    "fn add(a: Int, b: Int) -> Int { return a + b; }",
    "fn id(x: Int) -> Int { return x; }\nfn use_it(y: Int) -> Int { return id(y); }",
    "fn fib(n: Int) -> Int requires n >= 0 ensures result >= 0 "
    "{ if n < 2 { return n; } return fib(n - 1) + fib(n - 2); }",
    "fn shout(c: Console, m: Text) -> Unit ! {io.write} { print(c, m); }",
    "fn first(xs: List[Int]) -> Int { return xs[0]; }",
    "fn pick(p: Pair[Int, Text]) -> Int { return 0; }",
    "fn ident[T](x: T) -> T { return x; }",
    "fn nothing() -> Unit { return; }",
]


class TestSelfhostParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.program = load_program(str(PARSER_SG))
        cls.sigs = check(cls.program)

    def sigil_parse(self, source: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "src.sg").write_text(source, encoding="utf-8")
            out = io.StringIO()
            old = os.getcwd()
            os.chdir(tmp)
            try:
                Interpreter(self.program, self.sigs,
                            stdin=io.StringIO("src.sg\n"), stdout=out).run_main()
            finally:
                os.chdir(old)
        return out.getvalue().strip()

    def test_expression_corpus(self):
        for src in EXPR_CORPUS:
            with self.subTest(expr=src):
                self.assertEqual(self.sigil_parse(src), ref_expr(src))

    def test_block_corpus(self):
        for src in BLOCK_CORPUS:
            with self.subTest(block=src):
                self.assertEqual(self.sigil_parse(src), ref_block(src))

    def test_program_corpus(self):
        for src in PROGRAM_CORPUS:
            with self.subTest(program=src):
                self.assertEqual(self.sigil_parse(src), ref_program(src))


if __name__ == "__main__":
    unittest.main()
