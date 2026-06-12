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
        self.assertEqual(int.from_bytes(blob[24:32], "little"), 0x4000B0)  # entry
        self.assertEqual(int.from_bytes(blob[56:58], "little"), 2)         # e_phnum
        # first program header's p_filesz covers the whole file (headers=176).
        filesz = int.from_bytes(blob[96:104], "little")
        self.assertEqual(filesz, len(blob))

    def test_unsupported_expression_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "e.sg").write_text('fn main() -> Int { return a.b; }',
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

    def test_list_program_compiles(self):
        with tempfile.TemporaryDirectory() as t:
            blob = self.compile(
                Path(t),
                "fn main() -> Int { let xs: List[Int] = [10, 20, 30]; "
                "return xs[1] + len(xs); }")
        self.assertEqual(blob[:4], b"\x7fELF")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_list_programs(self):
        cases = [
            ("fn main() -> Int { let xs: List[Int] = [10, 20, 30]; "
             "return xs[1] + len(xs); }", 23),
            ("fn sum3(xs: List[Int]) -> Int { return xs[0] + xs[1] + xs[2]; }\n"
             "fn main() -> Int { return sum3([10, 15, 17]); }", 42),
            ("fn main() -> Int { let xs: List[Int] = [7, 14, 21]; var s: Int = 0; "
             "var i: Int = 0; while i < len(xs) invariant i >= 0 "
             "{ s = s + xs[i]; i = i + 1; } return s; }", 42),
            ("fn main() -> Int { let xs: List[Int] = [1, 2, 3]; "
             "let ys: List[Int] = [40, 50]; return xs[2] + ys[0] - len(ys); }", 41),
        ]
        for prog, expected in cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)])
                self.assertEqual(result.returncode, expected)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_text_programs(self):
        # Text is a heap [len][bytes] value: print any Text, len, ord.
        cases = [
            ('fn main() -> Int { let s: Text = "hello\\n"; print(s); return 0; }',
             b"hello\n", 0),
            ('fn shout(s: Text) -> Int { print(s); return 0; }\n'
             'fn main() -> Int { let x: Int = shout("hi\\n"); return 9; }',
             b"hi\n", 9),
            ('fn main() -> Int { let s: Text = "hello"; return len(s); }', b"", 5),
            ('fn main() -> Int { let s: Text = "A"; return ord(s); }', b"", 65),
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
    def test_emitted_text_equality(self):
        # type-directed ==: byte-wise for Text. The classify case is exactly
        # cc0's own `op == "+"` pattern.
        cases = [
            ('fn main() -> Int { let s: Text = "abc"; '
             'if s == "abc" { return 1; } return 0; }', 1),
            ('fn main() -> Int { let s: Text = "abc"; '
             'if s == "abd" { return 1; } return 0; }', 0),
            ('fn main() -> Int { let s: Text = "ab"; '
             'if s == "abc" { return 1; } return 0; }', 0),
            ('fn classify(op: Text) -> Int { if op == "+" { return 1; } '
             'if op == "-" { return 2; } return 0; }\n'
             'fn main() -> Int { return classify("-"); }', 2),
            ('fn main() -> Int { let s: Text = "hi"; '
             'if s != "bye" { return 5; } return 0; }', 5),
            ('fn main() -> Int { let s: Text = "hi"; '
             'if s != "hi" { return 5; } return 0; }', 0),
        ]
        for prog, expected in cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)])
                self.assertEqual(result.returncode, expected)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_push_programs(self):
        cases = [
            ("fn main() -> Int { let xs: List[Int] = push([1, 2, 3], 4); "
             "return xs[3] + len(xs); }", 8),
            ("fn main() -> Int { var xs: List[Int] = []; xs = push(xs, 10); "
             "xs = push(xs, 20); xs = push(xs, 12); "
             "return xs[0] + xs[1] + xs[2] + len(xs); }", 45),
            ("fn build(n: Int) -> List[Int] { var xs: List[Int] = []; "
             "var i: Int = 0; while i < n invariant i >= 0 "
             "{ xs = push(xs, i * i); i = i + 1; } return xs; }\n"
             "fn main() -> Int { let xs: List[Int] = build(5); "
             "return xs[4] + len(xs); }", 21),
        ]
        for prog, expected in cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)])
                self.assertEqual(result.returncode, expected)

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
