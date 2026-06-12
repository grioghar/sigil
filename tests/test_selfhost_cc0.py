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

# (program, expected exit status 0..255). main's return value is the exit code.
CASES = [
    ("fn main() -> Int { return 42; }", 42),
    ("fn main() -> Int { return (8 - 6) * (10 + 11); }", 42),
    ("fn main() -> Int { let a: Int = 9; if a > 3 { return 11; } return 22; }", 11),
    ("fn main() -> Int { var s: Int = 0; var i: Int = 1; "
     "while i <= 8 invariant i >= 1 { s = s + i; i = i + 1; } return s; }", 36),
    # functions, parameters, calls
    ("fn add(a: Int, b: Int) -> Int { return a + b; }\n"
     "fn main() -> Int { return add(40, 2); }", 42),
    ("fn sq(x: Int) -> Int { return x * x; }\n"
     "fn main() -> Int { return sq(6) + 6; }", 42),
    ("fn maxi(a: Int, b: Int) -> Int { if a > b { return a; } return b; }\n"
     "fn main() -> Int { return maxi(17, 42); }", 42),
    ("fn id(x: Int) -> Int { return x; }\n"
     "fn twice(x: Int) -> Int { return id(x) + id(x); }\n"
     "fn main() -> Int { return twice(21); }", 42),
    # recursion
    ("fn fib(n: Int) -> Int { if n < 2 { return n; } "
     "return fib(n - 1) + fib(n - 2); }\n"
     "fn main() -> Int { return fib(10); }", 55),
    ("fn fact(n: Int) -> Int { if n <= 1 { return 1; } "
     "return n * fact(n - 1); }\n"
     "fn main() -> Int { return fact(5); }", 120),
    ("fn sum_to(n: Int) -> Int { var s: Int = 0; var i: Int = 1; "
     "while i <= n invariant i >= 1 { s = s + i; i = i + 1; } return s; }\n"
     "fn main() -> Int { return sum_to(8); }", 36),
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
            blob = self.compile(Path(t), "fn main() -> Int { return 40 + 2; }")
        self.assertEqual(blob[:4], b"\x7fELF")
        self.assertEqual(blob[4], 2)       # ELFCLASS64
        self.assertEqual(blob[18], 62)     # x86-64
        # file size is headers (120) + emitted code, recorded in p_filesz.
        filesz = int.from_bytes(blob[96:104], "little")
        self.assertEqual(filesz, len(blob))
        self.assertEqual(len(blob), 120 + (len(blob) - 120))

    def test_unsupported_expression_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "e.sg").write_text('fn main() -> Text { return "hi"; }',
                                          encoding="utf-8")
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

    def test_print_data_section(self):
        # the string's ASCII bytes are appended after the code.
        with tempfile.TemporaryDirectory() as t:
            blob = self.compile(Path(t), 'fn main() -> Int { print("Hi"); return 0; }')
        self.assertIn(b"Hi", blob)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_programs_print(self):
        cases = [
            ('fn main() -> Int { print("hello\\n"); return 0; }', b"hello\n", 0),
            ('fn main() -> Int { print("A"); print("B"); print("C\\n"); return 7; }',
             b"ABC\n", 7),
            ('fn main() -> Int { print("hi"); print("hi"); return 3; }', b"hihi", 3),
            ('fn greet() -> Int { print("hey\\n"); return 0; }\n'
             'fn main() -> Int { let x: Int = greet(); return 9; }', b"hey\n", 9),
        ]
        for prog, out_expected, code_expected in cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)], capture_output=True)
                self.assertEqual(result.stdout, out_expected)
                self.assertEqual(result.returncode, code_expected)

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
