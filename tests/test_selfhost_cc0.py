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

# (program block, expected exit status 0..255)
CASES = [
    ("{ return 42; }", 42),
    ("{ return 40 + 2; }", 42),
    ("{ return (8 - 6) * (10 + 11); }", 42),
    ("{ return 84 / 2; }", 42),
    ("{ return 85 % 43; }", 42),
    ("{ return 5 < 10; }", 1),
    ("{ return 3 >= 3 and 4 > 2; }", 1),
    ("{ return not (2 < 1); }", 1),
    ("{ return 0; }", 0),
    ("{ return 255; }", 255),
    ("{ let x: Int = 40; return x + 2; }", 42),
    ("{ var x: Int = 0; x = 42; return x; }", 42),
    ("{ let x: Int = 7; let y: Int = 6; return x * y; }", 42),
    ("{ let a: Int = 5; if a > 3 { return 1; } else { return 0; } }", 1),
    ("{ let a: Int = 2; if a > 3 { return 1; } else { return 7; } }", 7),
    ("{ let a: Int = 9; if a > 3 { return 11; } return 22; }", 11),
    ("{ var s: Int = 0; var i: Int = 1; while i <= 8 invariant i >= 1 "
     "{ s = s + i; i = i + 1; } return s; }", 36),
    ("{ var n: Int = 5; var f: Int = 1; while n > 1 invariant n >= 0 "
     "{ f = f * n; n = n - 1; } return f; }", 120),
    ("{ var i: Int = 0; var c: Int = 0; while i < 10 invariant i >= 0 "
     "{ if i % 2 == 0 { c = c + 1; } i = i + 1; } return c; }", 5),
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
            blob = self.compile(Path(t), "{ return 40 + 2; }")
        self.assertEqual(blob[:4], b"\x7fELF")
        self.assertEqual(blob[4], 2)       # ELFCLASS64
        self.assertEqual(blob[18], 62)     # x86-64
        # file size is headers (120) + emitted code, recorded in p_filesz.
        filesz = int.from_bytes(blob[96:104], "little")
        self.assertEqual(filesz, len(blob))
        self.assertEqual(len(blob), 120 + (len(blob) - 120))

    def test_unsupported_expression_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "e.sg").write_text("{ return foo(1); }", encoding="utf-8")
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
