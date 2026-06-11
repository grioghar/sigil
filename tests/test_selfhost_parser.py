"""Conformance test for the Sigil-written parser (selfhost/parser.sg): for a
corpus of expressions, the AST it produces — dumped as a canonical S-expr —
must byte-match the AST that the reference Python parser (sigil.parser)
produces, dumped in the same form. This is bootstrap component #2 toward
self-hosting (docs/SELFHOST.md); it currently covers the expression grammar.
"""

import io
import unittest
from pathlib import Path

from sigil import ast_nodes as A
from sigil.checker import check
from sigil.interp import Interpreter
from sigil.modules import load_program
from sigil.parser import parse

REPO = Path(__file__).resolve().parent.parent
PARSER_SG = REPO / "selfhost" / "parser.sg"

CORPUS = [
    "1 + 2 * 3",
    "(1 + 2) * 3",
    "a - b - c",
    "a + b - c + d",
    "2 * 3 + 4 * 5",
    "not a and b or c",
    "a == b and c != d",
    "x < 1 or y >= 2",
    "0 - 7",
    "0 - 7 / 2",
    "f(1, 2, 3)",
    "g()",
    "xs[0]",
    "p.x",
    "p.items[1].y",
    "len(xs) > 0 and i < n",
    "[1, 2, 3]",
    "[]",
    "[a, f(b), c[0]]",
    "not not x",
    "a * b * c % d",
    "0 - -5",
    "true and false",
    "1 + 2 + 3 + 4",
    "f(a)[0].b",
    "-x + y",
]


def reference_dump(e) -> str:
    if isinstance(e, A.IntLit):
        return f"(int {e.value})"
    if isinstance(e, A.BoolLit):
        return f"(bool {'true' if e.value else 'false'})"
    if isinstance(e, A.TextLit):
        return f"(text {e.value})"
    if isinstance(e, A.Var):
        return f"(var {e.name})"
    if isinstance(e, A.Binary):
        return f"(bin {e.op} {reference_dump(e.left)} {reference_dump(e.right)})"
    if isinstance(e, A.Unary):
        return f"(un {e.op} {reference_dump(e.operand)})"
    if isinstance(e, A.Call):
        return "(call " + e.name + "".join(" " + reference_dump(a)
                                            for a in e.args) + ")"
    if isinstance(e, A.Index):
        return f"(idx {reference_dump(e.base)} {reference_dump(e.index)})"
    if isinstance(e, A.FieldAccess):
        return f"(field {reference_dump(e.base)} {e.field_name})"
    if isinstance(e, A.ListLit):
        return "(list" + "".join(" " + reference_dump(i)
                                 for i in e.items) + ")"
    raise AssertionError(f"unhandled node {type(e).__name__}")


def reference(src: str) -> str:
    program = parse(f"fn f() -> Int {{ return {src}; }}")
    return reference_dump(program.functions[0].body[0].value)


class TestSelfhostParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.program = load_program(str(PARSER_SG))
        cls.sigs = check(cls.program)

    def sigil_parse(self, src: str) -> str:
        out = io.StringIO()
        Interpreter(self.program, self.sigs, stdin=io.StringIO(src + "\n"),
                    stdout=out).run_main()
        return out.getvalue().strip()

    def test_corpus_matches_reference(self):
        for src in CORPUS:
            with self.subTest(expr=src):
                self.assertEqual(self.sigil_parse(src), reference(src))


if __name__ == "__main__":
    unittest.main()
