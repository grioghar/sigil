"""Tests for loop invariants (0.3b): `invariant` clauses on while loops.

The full vertical slice is under test: parsing (source spans), checking
(pure Bool in the enclosing scope), reference semantics (checked at every
loop head with loop blame), static verification (entry + preservation, and
the post-loop assumption that makes loop-carried ensures provable), and
native lowering (proven invariants emit nothing; unproven keep their checks).
"""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil import ast_nodes as A
from sigil.checker import check
from sigil.errors import CheckError, ContractViolation
from sigil.interp import Interpreter
from sigil.parser import parse
from sigil.verify import HAVE_Z3, verify

HAVE_RUSTC = shutil.which("rustc") is not None

# Iterative sum: the invariants carry exactly what the ensures needs across
# the havoc, so every clause must prove.
TOTAL = """
    fn total(n: Int) -> Int
        requires n >= 0
        ensures result >= 0
    {
        var sum: Int = 0;
        var i: Int = 0;
        while i < n invariant sum >= 0 invariant i >= 0 {
            sum = sum + i;
            i = i + 1;
        }
        return sum;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(total(5)));
    }
"""

FIB = """
    fn fib(n: Int) -> Int
        requires n >= 0
        ensures result >= 0
    {
        var a: Int = 0;
        var b: Int = 1;
        var i: Int = 0;
        while i < n invariant a >= 0 invariant b >= 0 {
            let next: Int = a + b;
            a = b;
            b = next;
            i = i + 1;
        }
        return a;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(fib(20)));
    }
"""

# The body drives x to 0, so `x > 0` is not preserved: never provable, and
# the interpreter must catch it at the loop head after the last iteration.
DRAIN = """
    fn drain(n: Int) -> Int
        requires n > 0
    {
        var x: Int = n;
        while x > 0 invariant x > 0 {
            x = x - 1;
        }
        return x;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(drain(3)));
    }
"""

# An invariant over Text: the verifier cannot model it, so it must stay a
# runtime check — and the interpreter must still enforce it.
TEXTY = """
    fn build(n: Int) -> Text
        requires n >= 0
    {
        var t: Text = "";
        var i: Int = 0;
        while i < n invariant len(t) < 3 {
            t = t + "x";
            i = i + 1;
        }
        return t;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, build(5));
    }
"""


def run(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


def verified(source: str):
    program = parse(source)
    check(program)
    report = verify(program)
    return program, report


def loop_of(program, fn_name: str) -> A.While:
    fn = next(f for f in program.functions if f.name == fn_name)
    return next(s for s in fn.body if isinstance(s, A.While))


def clause(program, fn_name: str, kind: str, index: int = 0):
    fn = next(f for f in program.functions if f.name == fn_name)
    matching = [c for c in fn.contracts if c.kind == kind]
    return matching[index]


class TestParsing(unittest.TestCase):
    def test_invariants_capture_exact_source_spans(self):
        program = parse(TOTAL)
        loop = loop_of(program, "total")
        self.assertEqual([inv.kind for inv in loop.invariants],
                         ["invariant", "invariant"])
        self.assertEqual([inv.source for inv in loop.invariants],
                         ["sum >= 0", "i >= 0"])

    def test_while_without_invariants_still_parses(self):
        program = parse(DRAIN.replace("invariant x > 0 ", ""))
        self.assertEqual(loop_of(program, "drain").invariants, [])


class TestChecking(unittest.TestCase):
    def test_invariant_must_be_bool(self):
        src = """
            fn f(n: Int) -> Int {
                var i: Int = 0;
                while i < n invariant i + 1 {
                    i = i + 1;
                }
                return i;
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("invariant clause must be Bool", ctx.exception.message)

    def test_invariant_must_be_pure(self):
        src = """
            fn f(c: Console, n: Int) -> Int ! {io.read} {
                var i: Int = 0;
                while i < n invariant len(read_line(c)) >= 0 {
                    i = i + 1;
                }
                return i;
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("pure", ctx.exception.message)

    def test_invariant_sees_enclosing_scope_not_body_scope(self):
        # `inner` is declared in the body, so the invariant cannot see it.
        src = """
            fn f(n: Int) -> Int {
                var i: Int = 0;
                while i < n invariant inner >= 0 {
                    let inner: Int = i;
                    i = i + 1;
                }
                return i;
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("unknown name 'inner'", ctx.exception.message)


class TestInterpreter(unittest.TestCase):
    """Reference semantics: invariants are ALWAYS checked, never elided."""

    def test_holding_invariants_pass_silently(self):
        self.assertEqual(run(TOTAL), "10\n")
        self.assertEqual(run(FIB), "6765\n")

    def test_false_invariant_blames_the_loop(self):
        with self.assertRaises(ContractViolation) as ctx:
            run(DRAIN)
        self.assertEqual(ctx.exception.blame, "loop")
        self.assertIn("invariant of while loop in 'drain' failed", ctx.exception.message)
        self.assertIn("`x > 0`", ctx.exception.message)
        self.assertIn("(line 6)", ctx.exception.message)  # the while statement

    def test_unmodeled_text_invariant_still_enforced(self):
        with self.assertRaises(ContractViolation) as ctx:
            run(TEXTY)
        self.assertEqual(ctx.exception.blame, "loop")
        self.assertIn("`len(t) < 3`", ctx.exception.message)

    def test_invariant_checked_before_first_iteration(self):
        # The loop never runs, but the invariant must hold at the loop head.
        src = """
            fn f(n: Int) -> Int {
                var i: Int = 0;
                while i < 0 invariant n > 0 {
                    i = i + 1;
                }
                return i;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(0)));
            }
        """
        with self.assertRaises(ContractViolation) as ctx:
            run(src)
        self.assertEqual(ctx.exception.blame, "loop")


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestVerifier(unittest.TestCase):
    def test_sum_invariants_make_loop_carried_ensures_provable(self):
        program, report = verified(TOTAL)
        for inv in loop_of(program, "total").invariants:
            self.assertTrue(inv.proven, f"invariant `{inv.source}` must prove")
        self.assertTrue(clause(program, "total", "ensures").proven,
                        "post-loop invariants + not-cond must carry the ensures")
        self.assertEqual(report.contracts_proven, report.contracts_total)

    def test_fib_invariants_prove_ensures(self):
        program, _ = verified(FIB)
        for inv in loop_of(program, "fib").invariants:
            self.assertTrue(inv.proven)
        self.assertTrue(clause(program, "fib", "ensures").proven)

    def test_false_invariant_not_proven(self):
        program, _ = verified(DRAIN)
        inv = loop_of(program, "drain").invariants[0]
        self.assertFalse(inv.proven, "x > 0 is not preserved by x = x - 1")

    def test_unmodeled_text_invariant_stays_runtime(self):
        program, report = verified(TEXTY)
        inv = loop_of(program, "build").invariants[0]
        self.assertFalse(inv.proven)
        invariant_findings = [f for f in report.findings if f.kind == "invariant"]
        self.assertEqual(len(invariant_findings), 1)
        self.assertFalse(invariant_findings[0].proven)

    def test_invariants_counted_in_report_totals(self):
        _, report = verified(TOTAL)
        invariant_findings = [f for f in report.findings if f.kind == "invariant"]
        self.assertEqual(len(invariant_findings), 2)
        # requires + ensures + two invariants
        self.assertEqual(report.contracts_total, 4)
        self.assertEqual(report.contracts_proven, 4)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestNativeElision(unittest.TestCase):
    """Proven invariants emit nothing; unproven ones keep both checks."""

    def test_proven_invariants_emit_no_checks(self):
        from sigil.emit_rust import emit_rust
        program, _ = verified(TOTAL)
        rust = emit_rust(program)
        self.assertNotIn("invariant of while loop", rust)

    def test_unproven_invariant_emits_check_before_loop_and_in_body(self):
        from sigil.emit_rust import emit_rust
        program, _ = verified(DRAIN)
        rust = emit_rust(program)
        self.assertEqual(rust.count("invariant of while loop in 'drain'"), 2)
        self.assertIn("`x > 0`", rust)
        self.assertIn("blame the loop", rust)


@unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
class TestNativeBackend(unittest.TestCase):
    """Differential: the native binary must agree with the interpreter."""

    def build_to_tmp(self, source: str, tmp: str, name: str):
        from sigil.build import build
        src = Path(tmp) / f"{name}.sg"
        src.write_text(source, encoding="utf-8")
        rs = Path(tmp) / f"{name}.rs"
        exe = build(str(src), output=str(Path(tmp) / f"{name}.exe"),
                    emit_rust_path=str(rs), optimize=False, quiet=True)
        return exe, rs.read_text(encoding="utf-8")

    def test_native_proven_invariants_run_checkless_and_agree(self):
        expected = run(TOTAL)
        with tempfile.TemporaryDirectory() as tmp:
            exe, rust = self.build_to_tmp(TOTAL, tmp, "total")
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertNotIn("invariant of while loop", rust)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)

    def test_native_unproven_invariant_fails_at_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe, rust = self.build_to_tmp(DRAIN, tmp, "drain")
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertIn("invariant of while loop", rust)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contract violation", result.stderr)
        self.assertIn("invariant of while loop in 'drain'", result.stderr)
        self.assertIn("x > 0", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
