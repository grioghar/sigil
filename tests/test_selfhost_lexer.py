"""Conformance test for the self-hosting bootstrap: the Sigil-written lexer
(selfhost/lexer.sg) must tokenize every corpus file identically to the Python
lexer (sigil.lexer). This is the first proof that Sigil can reproduce a piece
of its own toolchain. See docs/SELFHOST.md."""

import io
import os
import unittest
from pathlib import Path

from sigil.checker import check
from sigil.interp import Interpreter
from sigil.lexer import lex as py_lex
from sigil.parser import parse

REPO = Path(__file__).resolve().parent.parent
LEXER = REPO / "selfhost" / "lexer.sg"

CORPUS = sorted((REPO / "examples").glob("*.sg")) \
    + sorted((REPO / "programs").glob("*/*.sg"))


def esc(s: str) -> str:
    # Must match selfhost/lexer.sg's esc exactly (backslash first).
    return (s.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\t", "\\t").replace("\r", "\\r"))


def python_dump(src: str) -> str:
    lines = [f"{t.kind} {t.line} {t.col} {esc(t.value)}" for t in py_lex(src)]
    return "\n".join(lines)


class TestSelfHostLexer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Compile the Sigil lexer once; run it per corpus file via the
        # interpreter, feeding the repo-relative path on stdin.
        cls.program = parse(LEXER.read_text(encoding="utf-8"))
        cls.sigs = check(cls.program)

    def sigil_dump(self, rel_path: str) -> str:
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(REPO)
        try:
            Interpreter(self.program, self.sigs,
                        stdin=io.StringIO(rel_path + "\n"),
                        stdout=out).run_main()
        finally:
            os.chdir(cwd)
        return out.getvalue().strip("\n")

    def test_lexer_self_checks(self):
        # The Sigil lexer must itself lex cleanly (no ERROR token).
        rel = str(LEXER.relative_to(REPO)).replace("\\", "/")
        self.assertNotIn("ERROR ", self.sigil_dump(rel))

    def test_corpus_conformance(self):
        self.assertTrue(CORPUS, "no corpus files found")
        for path in CORPUS:
            rel = str(path.relative_to(REPO)).replace("\\", "/")
            with self.subTest(file=rel):
                src = path.read_text(encoding="utf-8")
                expected = python_dump(src)
                actual = self.sigil_dump(rel)
                self.assertEqual(actual, expected,
                                 f"Sigil lexer diverged from sigil.lexer on {rel}")

    def test_lexer_lexes_itself(self):
        # The bootstrap target in miniature: the Sigil lexer tokenizes its own
        # source identically to the Python lexer.
        rel = str(LEXER.relative_to(REPO)).replace("\\", "/")
        src = LEXER.read_text(encoding="utf-8")
        self.assertEqual(self.sigil_dump(rel), python_dump(src))


if __name__ == "__main__":
    unittest.main()
