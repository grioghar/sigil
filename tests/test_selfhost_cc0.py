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
                        stdin=io.StringIO("out.bin\ne.sg\n"),
                        stdout=out).run_main()
        finally:
            os.chdir(old)
        self.assertIn("compiled", out.getvalue(), out.getvalue())
        return (tmp / "out.bin").read_bytes()

    def compile_multi(self, tmp: Path, files: dict) -> bytes:
        # files: {name: source}; compiled together (cc0 merges declarations
        # across files, skipping use/pub headers). stdin = out path then one
        # source path per line, in the given order.
        for name, text in files.items():
            (tmp / name).write_text(text, encoding="utf-8")
        stdin = "out.bin\n" + "\n".join(files.keys()) + "\n"
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(tmp)
        try:
            Interpreter(self.program, self.sigs,
                        stdin=io.StringIO(stdin), stdout=out).run_main()
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
                            stdin=io.StringIO("out.bin\ne.sg\n"),
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

    def test_record_program_compiles(self):
        with tempfile.TemporaryDirectory() as t:
            blob = self.compile(
                Path(t),
                "record Point { x: Int, y: Int }\n"
                "fn main() -> Int { let p: Point = Point { x: 40, y: 2 }; "
                "return p.x + p.y; }")
        self.assertEqual(blob[:4], b"\x7fELF")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_record_programs(self):
        # records are heap blocks of one 8-byte slot per field, in declaration
        # order; construction places each field at its slot, access loads it.
        cases = [
            ("record Point { x: Int, y: Int }\n"
             "fn main() -> Int { let p: Point = Point { x: 40, y: 2 }; "
             "return p.x + p.y; }", 42),
            # field order in the literal differs from declaration order
            ("record Point { x: Int, y: Int }\n"
             "fn main() -> Int { let p: Point = Point { y: 2, x: 40 }; "
             "return p.x + p.y; }", 42),
            # record passed to and returned from functions (pointer-sized)
            ("record Pair { a: Int, b: Int }\n"
             "fn mk(a: Int, b: Int) -> Pair { return Pair { a: a, b: b }; }\n"
             "fn diff(p: Pair) -> Int { return p.a - p.b; }\n"
             "fn main() -> Int { return diff(mk(50, 8)); }", 42),
            # three fields, nested arithmetic in a field value
            ("record V3 { x: Int, y: Int, z: Int }\n"
             "fn main() -> Int { let v: V3 = V3 { x: 1, y: 2 * 10, z: 21 }; "
             "return v.x + v.y + v.z; }", 42),
            # a record field that is itself a record (pointer in a slot)
            ("record Inner { n: Int }\nrecord Outer { i: Inner, k: Int }\n"
             "fn main() -> Int { let o: Outer = "
             "Outer { i: Inner { n: 30 }, k: 12 }; return o.i.n + o.k; }", 42),
            # a record holding a Text field; access then ord
            ("record Tok { kind: Text, n: Int }\n"
             "fn main() -> Int { let t: Tok = Tok { kind: \"A\", n: 5 }; "
             "return ord(t.kind) + t.n; }", 70),
        ]
        for prog, expected in cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)])
                self.assertEqual(result.returncode, expected)

    def test_multifile_program_compiles(self):
        with tempfile.TemporaryDirectory() as t:
            blob = self.compile_multi(Path(t), {
                "a.sg": "record Pt { x: Int, y: Int }\n",
                "b.sg": "use a { Pt }\n"
                        "fn main() -> Int { let p: Pt = Pt { x: 40, y: 2 }; "
                        "return p.x + p.y; }\n",
            })
        self.assertEqual(blob[:4], b"\x7fELF")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_multifile_programs(self):
        # declarations merge across files; use/pub headers are skipped, so a
        # type defined in one file is usable from another.
        with tempfile.TemporaryDirectory() as t:
            self.compile_multi(Path(t), {
                "a.sg": "record Pt { x: Int, y: Int }\n",
                "b.sg": "use a { Pt }\n"
                        "fn main() -> Int { let p: Pt = Pt { x: 40, y: 2 }; "
                        "return p.x + p.y; }\n",
            })
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(subprocess.run([str(exe)]).returncode, 42)
        with tempfile.TemporaryDirectory() as t:
            self.compile_multi(Path(t), {
                "ea.sg": "enum Opt { None, Some(Int) }\n",
                "eb.sg": "use ea { Opt }\n"
                         "fn unwrap(o: Opt) -> Int { match o { Some(v) => "
                         "{ return v; } None => { return 0; } } }\n",
                "ec.sg": "use ea { Opt }\nuse eb { unwrap }\n"
                         "fn main() -> Int { return unwrap(Some(42)); }\n",
            })
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(subprocess.run([str(exe)]).returncode, 42)

    def test_generic_enum_compiles(self):
        with tempfile.TemporaryDirectory() as t:
            blob = self.compile(
                Path(t),
                "record Box { v: Int }\n"
                "enum Res[T] { ROk(T, Int), RErr(Text, Int) }\n"
                "fn mk(n: Int) -> Res[Box] { return ROk(Box { v: n }, 0); }\n"
                "fn run(n: Int) -> Int { match mk(n) { ROk(b, j) => "
                "{ return b.v; } RErr(m, p) => { return 0; } } }\n"
                "fn main() -> Int { return run(42); }")
        self.assertEqual(blob[:4], b"\x7fELF")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_generic_enum_programs(self):
        # generic enums are pointer-erased, but a type-variable payload binder
        # must still resolve to its concrete record/Text so field access and
        # type-directed ops lower correctly. The scrutinee's instantiation
        # (Res[Box]) flows to the binder: ROk's T payload binds as Box.
        cases = [
            # record payload used concretely (b.v) after the match
            ("record Box { v: Int }\n"
             "enum Res[T] { ROk(T, Int), RErr(Text, Int) }\n"
             "fn mk(n: Int) -> Res[Box] { return ROk(Box { v: n }, 0); }\n"
             "fn run(n: Int) -> Int { match mk(n) { ROk(b, j) => "
             "{ return b.v; } RErr(m, p) => { return 0; } } }\n"
             "fn main() -> Int { return run(42); }", 42),
            # the Text payload of the other arm lowers len()/+ correctly
            ('record Box { v: Int }\n'
             'enum Res[T] { ROk(T, Int), RErr(Text, Int) }\n'
             'fn mk() -> Res[Box] { return RErr("err", 3); }\n'
             'fn run() -> Int { match mk() { ROk(b, j) => { return b.v; } '
             'RErr(m, p) => { return len(m) + p; } } }\n'
             'fn main() -> Int { return run(); }', 6),
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
    def test_emitted_chr(self):
        # chr(n) is the inverse of ord: a one-byte Text holding n's low byte.
        with tempfile.TemporaryDirectory() as t:
            self.compile(Path(t),
                         "fn main(c: Console) -> Int { print(c, chr(72)); "
                         "print(c, chr(105)); return ord(chr(67)); }")
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            r = subprocess.run([str(exe)], capture_output=True)
        self.assertEqual(r.stdout, b"Hi")
        self.assertEqual(r.returncode, 67)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_cat(self):
        # cat(a, b) for List[Int] is a single-allocation builtin (the efficient
        # counterpart to the source-level push loop).
        with tempfile.TemporaryDirectory() as t:
            self.compile(Path(t),
                         "fn main() -> Int { let a: List[Int] = [10, 20, 30]; "
                         "let b: List[Int] = [40, 50]; let c: List[Int] = cat(a, b); "
                         "return c[0] + c[3] + len(c); }")
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(subprocess.run([str(exe)]).returncode, 55)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_io_syscalls(self):
        # write_bytes then read_file round-trips raw bytes through the
        # filesystem via raw open/read/write/close syscalls.
        with tempfile.TemporaryDirectory() as t:
            self.compile(Path(t),
                         'fn main(c: Console, f: Fs) -> Int { '
                         'write_bytes(f, "data.bin", [72, 105, 33]); '
                         'let s: Text = read_file(f, "data.bin"); '
                         'print(c, s); return len(s); }')
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            r = subprocess.run([str(exe)], capture_output=True, cwd=t)
        self.assertEqual(r.stdout, b"Hi!")
        self.assertEqual(r.returncode, 3)
        # read_line echoes a line from stdin (no trailing newline)
        with tempfile.TemporaryDirectory() as t:
            self.compile(Path(t),
                         'fn main(c: Console) -> Int { let s: Text = read_line(c); '
                         'print(c, s); return len(s); }')
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            r = subprocess.run([str(exe)], input=b"echoed\n", capture_output=True)
        self.assertEqual(r.stdout, b"echoed")
        self.assertEqual(r.returncode, 6)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_capability_programs(self):
        # main may take Console/Fs capability params (phantom one-word values on
        # bare metal), and print accepts the real print(console, text) form as
        # well as the one-arg test form — the capability is ignored.
        with tempfile.TemporaryDirectory() as t:
            self.compile(Path(t),
                         'fn main(c: Console) -> Int { print(c, "hi\\n"); '
                         'return 0; }')
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            r = subprocess.run([str(exe)], capture_output=True)
        self.assertEqual(r.stdout, b"hi\n")
        self.assertEqual(r.returncode, 0)
        with tempfile.TemporaryDirectory() as t:
            self.compile(Path(t),
                         'fn greet(c: Console, s: Text) -> Int { print(c, s); '
                         'return 0; }\n'
                         'fn main(c: Console, f: Fs) -> Int { '
                         'let x: Int = greet(c, "yo\\n"); return 7; }')
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            r = subprocess.run([str(exe)], capture_output=True)
        self.assertEqual(r.stdout, b"yo\n")
        self.assertEqual(r.returncode, 7)

    def test_enum_program_compiles(self):
        with tempfile.TemporaryDirectory() as t:
            blob = self.compile(
                Path(t),
                "enum Opt { None, Some(Int) }\n"
                "fn unwrap(o: Opt) -> Int { match o { Some(v) => { return v; } "
                "None => { return 0; } } }\n"
                "fn main() -> Int { return unwrap(Some(42)); }")
        self.assertEqual(blob[:4], b"\x7fELF")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_enum_match_programs(self):
        # an enum value is a heap block [tag][payload...]; construction stores
        # the variant index + payloads, match dispatches on the tag and binds
        # payload slots. Covers nullary + payload variants, wildcards, Text
        # payloads, reordered/declaration-order tags, and nested/reused binders.
        cases = [
            # nullary variants, dispatch by tag
            ("enum Color { Red, Green, Blue }\n"
             "fn val(c: Color) -> Int { match c { Red => { return 1; } "
             "Green => { return 2; } Blue => { return 3; } } }\n"
             "fn main() -> Int { return val(Green) + val(Blue); }", 5),
            # payload variant, bind and return; arm order != declaration order
            ("enum Opt { None, Some(Int) }\n"
             "fn f(o: Opt) -> Int { match o { Some(v) => { return v; } "
             "None => { return 0; } } }\n"
             "fn main() -> Int { return f(Some(42)); }", 42),
            # the other arm of the same enum
            ("enum Opt { None, Some(Int) }\n"
             "fn f(o: Opt) -> Int { match o { None => { return 7; } "
             "Some(v) => { return v; } } }\n"
             "fn main() -> Int { return f(None); }", 7),
            # two payloads + a wildcard arm
            ("enum Res { Ok(Int, Int), Err(Int) }\n"
             "fn f(r: Res) -> Int { match r { Ok(a, b) => { return a + b; } "
             "_ => { return 0; } } }\n"
             "fn main() -> Int { return f(Ok(40, 2)); }", 42),
            # a Text payload, used via len in the arm
            ('enum Msg { Greet(Text), Quit }\n'
             'fn h(m: Msg) -> Int { match m { Greet(s) => { return len(s); } '
             'Quit => { return 0; } } }\n'
             'fn main() -> Int { return h(Greet("hello")); }', 5),
            # match a local; construct then bind
            ("enum E { A(Int), B(Int) }\n"
             "fn main() -> Int { let e: E = B(42); "
             "match e { A(x) => { return 0; } B(y) => { return y; } } }", 42),
            # reused binder names across two matches in one function (cc0 idiom)
            ("enum P { Pt(Int, Int) }\n"
             "fn main() -> Int { let a: P = Pt(10, 20); let b: P = Pt(5, 7); "
             "var s: Int = 0; match a { Pt(x, y) => { s = s + x + y; } } "
             "match b { Pt(x, y) => { s = s + x + y; } } return s; }", 42),
            # match in a while loop, accumulating
            ("enum Cell { Val(Int) }\n"
             "fn get(i: Int) -> Cell { return Val(i); }\n"
             "fn main() -> Int { var s: Int = 0; var i: Int = 1; "
             "while i <= 8 invariant i >= 1 "
             "{ match get(i) { Val(v) => { s = s + v; } } i = i + 1; } "
             "return s; }", 36),
            # nested match (a match inside an arm body)
            ("enum A { Wrap(Int) }\nenum B { Box(Int) }\n"
             "fn f(a: A) -> Int { match a { Wrap(n) => "
             "{ match Box(n) { Box(m) => { return m + 1; } } } } }\n"
             "fn main() -> Int { return f(Wrap(41)); }", 42),
            # function returning an enum, matched at the call site
            ("enum R { Ok(Int), Err(Int) }\n"
             "fn step(n: Int) -> R { if n <= 0 { return Ok(0); } return Ok(n); }\n"
             "fn run(n: Int) -> Int { match step(n) { Ok(v) => { return v; } "
             "Err(e) => { return 0 - e; } } }\n"
             "fn main() -> Int { return run(42); }", 42),
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
    def test_emitted_if_expressions(self):
        cases = [
            ('fn main() -> Int { let x: Int = if 1 < 2 then 10 else 20; '
             'return x; }', 10),
            ('fn main() -> Int { let x: Int = if 1 > 2 then 10 else 20; '
             'return x; }', 20),
            ('fn main() -> Int { let n: Int = 7; '
             'return if n > 5 then n * 6 else 0; }', 42),
            ('fn classify(n: Int) -> Int { '
             'return if n > 0 then 1 else if n < 0 then 2 else 0; }\n'
             'fn main() -> Int { return classify(0 - 5); }', 2),
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
    def test_emitted_slice(self):
        exit_cases = [
            ('fn main() -> Int { let s: Text = slice("hello", 1, 4); '
             'return len(s); }', 3),
            ('fn main() -> Int { let s: Text = slice("hello", 1, 4); '
             'if s == "ell" { return 1; } return 0; }', 1),
            ('fn main() -> Int { let s: Text = slice("hello", 0, 1); '
             'return ord(s); }', 104),
            ('fn char_at(s: Text, i: Int) -> Text { return slice(s, i, i + 1); }\n'
             'fn main() -> Int { return ord(char_at("ABC", 1)); }', 66),
        ]
        for prog, expected in exit_cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)])
                self.assertEqual(result.returncode, expected)
        with tempfile.TemporaryDirectory() as t:
            self.compile(Path(t), 'fn main() -> Int { '
                         'print(slice("hello world", 6, 11)); return 0; }')
            exe = Path(t) / "out.bin"
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run([str(exe)], capture_output=True)
        self.assertEqual(result.stdout, b"world")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "emitted ELF only runs on Linux")
    def test_emitted_text_concat(self):
        # type-directed +: Text concatenation. Exit-code cases first.
        exit_cases = [
            ('fn main() -> Int { let s: Text = "ab" + "cd"; return len(s); }', 4),
            ('fn main() -> Int { let s: Text = "ab" + "cd"; '
             'if s == "abcd" { return 1; } return 0; }', 1),
            ('fn main() -> Int { let s: Text = "ab" + "cd"; '
             'if s == "abXd" { return 1; } return 0; }', 0),
        ]
        for prog, expected in exit_cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)])
                self.assertEqual(result.returncode, expected)
        # stdout cases: concat with a parameter, and chained concatenation.
        out_cases = [
            ('fn greet(who: Text) -> Text { return "hi " + who; }\n'
             'fn main() -> Int { print(greet("bob")); return 0; }', b"hi bob"),
            ('fn main() -> Int { let s: Text = "a" + "b" + "c"; '
             'print(s); return 0; }', b"abc"),
        ]
        for prog, out_expected in out_cases:
            with self.subTest(prog=prog):
                with tempfile.TemporaryDirectory() as t:
                    self.compile(Path(t), prog)
                    exe = Path(t) / "out.bin"
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    result = subprocess.run([str(exe)], capture_output=True)
                self.assertEqual(result.stdout, out_expected)

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
