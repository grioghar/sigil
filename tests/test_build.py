"""Differential tests for the native backend.

The interpreter is the reference semantics. For every program here, the
native binary's stdout must be byte-identical to the interpreter's, and
faults/violations must exit nonzero with the same diagnostic class.

These tests invoke rustc, so they take seconds, not milliseconds; the
programs are feature-dense on purpose to keep the build count low.
"""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.checker import check
from sigil.interp import Interpreter
from sigil.parser import parse

HAVE_RUSTC = shutil.which("rustc") is not None

# One program exercising: effects, capability threading, contracts that pass,
# recursion, while/var, lists (literal, push, index, len), text ops, str(),
# if/else-if chains, comparison and logic operators, truncating division,
# records (construction, field access, equality, recursion through List).
KITCHEN_SINK = """
record Point {
    x: Int,
    y: Int,
}

record Tree {
    value: Int,
    children: List[Tree],
}

fn norm2(p: Point) -> Int {
    return p.x * p.x + p.y * p.y;
}

fn tree_total(t: Tree) -> Int {
    var sum: Int = t.value;
    var i: Int = 0;
    while i < len(t.children) {
        sum = sum + tree_total(t.children[i]);
        i = i + 1;
    }
    return sum;
}

fn fib(n: Int) -> Int
    requires n >= 0
    ensures result >= 0
{
    if n < 2 {
        return n;
    }
    return fib(n - 1) + fib(n - 2);
}

fn classify(n: Int) -> Text {
    if n < 0 {
        return "neg";
    } else if n == 0 {
        return "zero";
    } else {
        return "pos";
    }
}

fn total(xs: List[Int]) -> Int {
    var sum: Int = 0;
    var i: Int = 0;
    while i < len(xs) {
        sum = sum + xs[i];
        i = i + 1;
    }
    return sum;
}

fn shout(c: Console, msg: Text) -> Unit ! {io.write}
    requires len(msg) > 0
{
    print(c, msg + "!");
}

fn main(console: Console) -> Unit ! {io.write} {
    shout(console, "fib(15) = " + str(fib(15)));
    let xs: List[Int] = push([3, 1, 4, 1, 5], 9);
    print(console, str(total(xs)) + " over " + str(len(xs)));
    print(console, classify(0 - 7) + " " + classify(0) + " " + classify(7));
    print(console, str((0 - 7) / 2) + " " + str((0 - 7) % 2));
    print(console, str(true and not false) + " " + str(1 < 2 or 2 < 1));
    print(console, str(len("héllo")));
    let p: Point = Point { x: 3, y: 4 };
    print(console, str(norm2(p)) + " " + str(p == Point { x: 3, y: 4 }));
    let t: Tree = Tree {
        value: 1,
        children: [Tree { value: 2, children: [] }, Tree { value: 3, children: [] }],
    };
    print(console, str(tree_total(t)));
}
"""

VIOLATION = """
fn safe_div(a: Int, b: Int) -> Int
    requires b != 0
{
    return a / b;
}

fn main(console: Console) -> Unit ! {io.write} {
    print(console, str(safe_div(1, 0)));
}
"""

# Attenuation happy path: write through a jailed Fs, read back through the
# parent, prove the jail rebased the path. Then read-only denial.
ATTENUATION_OK = """
fn main(console: Console, fs: Fs) -> Unit ! {io.write, fs.read, fs.write} {
    let jail: Fs = subdir(fs, "inner");
    write_file(jail, "x.txt", "jailed");
    print(console, read_file(fs, "inner/x.txt"));
    let ro: Fs = read_only(fs);
    print(console, read_file(ro, "inner/x.txt"));
}
"""

ATTENUATION_DENIED = """
fn main(fs: Fs) -> Unit ! {fs.write} {
    let ro: Fs = read_only(fs);
    write_file(ro, "x.txt", "data");
}
"""


def interpret(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


def build_to_tmp(source: str, tmp: str, name: str) -> Path:
    src = Path(tmp) / f"{name}.sg"
    src.write_text(source, encoding="utf-8")
    return build(str(src), output=str(Path(tmp) / f"{name}.exe"),
                 optimize=False)


@unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
class TestNativeBackend(unittest.TestCase):
    def test_native_matches_interpreter(self):
        expected = interpret(KITCHEN_SINK)
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_to_tmp(KITCHEN_SINK, tmp, "sink")
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)

    def test_native_contract_violation_exits_nonzero_with_blame(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_to_tmp(VIOLATION, tmp, "violation")
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contract violation", result.stderr)
        self.assertIn("b != 0", result.stderr)
        self.assertIn("CALLER", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_native_attenuation_matches_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_to_tmp(ATTENUATION_OK, tmp, "atten")
            result = subprocess.run([str(exe)], capture_output=True, text=True,
                                    cwd=tmp)
            written = (Path(tmp) / "inner" / "x.txt").read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), "jailed\njailed\n")
        self.assertEqual(written, "jailed")

    def test_native_read_only_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = build_to_tmp(ATTENUATION_DENIED, tmp, "denied")
            result = subprocess.run([str(exe)], capture_output=True, text=True,
                                    cwd=tmp)
            leaked = (Path(tmp) / "x.txt").exists()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("capability fault", result.stderr)
        self.assertIn("read-only", result.stderr)
        self.assertFalse(leaked, "read-only Fs must not produce a file")


if __name__ == "__main__":
    unittest.main()
