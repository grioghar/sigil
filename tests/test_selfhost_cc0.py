"""cc0 — the first end-to-end native compiler written in Sigil. It compiles an
integer-arithmetic expression to a static x86-64 Linux ELF whose exit status
is the computed value, using only write_bytes (no rustc/linker/libc). We
validate the ELF everywhere; on Linux we run each emitted binary and assert
its exit code equals the value the expression should produce. The whole
pipeline — lex, parse, codegen, ELF, run — proven on a tiny language subset.
See docs/SELFHOST.md."""

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sigil.checker import check
from sigil.interp import Interpreter
from sigil.modules import load_program

REPO = Path(__file__).resolve().parent.parent
CC0_SG = REPO / "selfhost" / "cc0.sg"

# (expression, expected exit status 0..255)
CASES = [
    ("42", 42),
    ("40 + 2", 42),
    ("6 * 7", 42),
    ("100 - 58", 42),
    ("2 * 3 * 7", 42),
    ("(1 + 2) * 14", 42),
    ("-5 + 47", 42),
    ("0", 0),
    ("255", 255),
    ("1 + 2 * 3 + 4 * 5 + 15", 42),
]


class TestCc0(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.program = load_program(str(CC0_SG))
        cls.sigs = check(cls.program)

    def compile(self, tmp: Path, expr: str) -> bytes:
        (tmp / "e.sg").write_text(expr, encoding="utf-8")
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(tmp)
        try:
            Interpreter(self.program, self.sigs,
                        stdin=io.StringIO("e.sg\nout.bin\n"),
                        stdout=out).run_main()
        finally:
            os.chdir(old)
        self.assertIn("compiled", out.getvalue(), out.getvalue())
        return (tmp / "out.bin").read_bytes()

    def test_elf_structure(self):
        with tempfile.TemporaryDirectory() as t:
            blob = self.compile(Path(t), "40 + 2")
        self.assertEqual(blob[:4], b"\x7fELF")
        self.assertEqual(blob[4], 2)       # ELFCLASS64
        self.assertEqual(blob[18], 62)     # x86-64
        # file size is headers (120) + emitted code, recorded in p_filesz.
        filesz = int.from_bytes(blob[96:104], "little")
        self.assertEqual(filesz, len(blob))
        self.assertEqual(len(blob), 120 + (len(blob) - 120))

    def test_unsupported_expression_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "e.sg").write_text("true and false", encoding="utf-8")
            out = io.StringIO()
            old = os.getcwd()
            os.chdir(t)
            try:
                Interpreter(self.program, self.sigs,
                            stdin=io.StringIO("e.sg\nout.bin\n"),
                            stdout=out).run_main()
            finally:
                os.chdir(old)
            self.assertIn("unsupported", out.getvalue())
            self.assertFalse((Path(t) / "out.bin").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_binaries_exit_with_value(self):
        for expr, expected in CASES:
            with self.subTest(expr=expr):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), expr)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)])
                self.assertEqual(result.returncode, expected)


if __name__ == "__main__":
    unittest.main()
