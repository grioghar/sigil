"""Tests for the static contract verifier (v0.3).

The cardinal rule under test: the verifier may only ever ERASE checks it has
proven; anything it cannot model must keep its runtime check (conservatism).
"""

import unittest

from sigil.checker import check
from sigil.parser import parse
from sigil.verify import HAVE_Z3, verify


def verified(source: str):
    program = parse(source)
    check(program)
    report = verify(program)
    return program, report


def clause(program, fn_name: str, kind: str, index: int = 0):
    fn = next(f for f in program.functions if f.name == fn_name)
    matching = [c for c in fn.contracts if c.kind == kind]
    return matching[index]


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestProvable(unittest.TestCase):
    def test_recursive_fib_fully_proven(self):
        program, _ = verified("""
            fn fib(n: Int) -> Int
                requires n >= 0
                ensures result >= 0
            {
                if n < 2 {
                    return n;
                }
                return fib(n - 1) + fib(n - 2);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(fib(20)));
            }
        """)
        self.assertTrue(clause(program, "fib", "requires").proven,
                        "inductive call sites n-1, n-2 under n>=2 must prove")
        self.assertTrue(clause(program, "fib", "ensures").proven,
                        "both return sites must prove result >= 0")

    def test_abs_ensures_proven_through_branches(self):
        program, _ = verified("""
            fn abs(n: Int) -> Int
                ensures result >= 0
            {
                if n < 0 {
                    return 0 - n;
                }
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(abs(0 - 5)));
            }
        """)
        self.assertTrue(clause(program, "abs", "ensures").proven)

    def test_division_proven_safe_by_requires(self):
        program, report = verified("""
            fn safe_div(a: Int, b: Int) -> Int
                requires b != 0
            {
                return a / b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(safe_div(10, 2)));
            }
        """)
        self.assertTrue(clause(program, "safe_div", "requires").proven)
        self.assertEqual(report.divisions_proven, 1)

    def test_callee_ensures_propagates_to_caller_obligation(self):
        # g guarantees result != 0, so f may divide by it.
        program, report = verified("""
            fn g(n: Int) -> Int
                ensures result >= 1
            {
                if n < 1 {
                    return 1;
                }
                return n;
            }
            fn f(n: Int) -> Int {
                return 100 / g(n);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(0)));
            }
        """)
        self.assertTrue(clause(program, "g", "ensures").proven)
        self.assertEqual(report.divisions_proven, 1)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestConservative(unittest.TestCase):
    def test_broken_ensures_not_proven(self):
        program, _ = verified("""
            fn bad_abs(n: Int) -> Int
                ensures result >= 0
            {
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(bad_abs(5)));
            }
        """)
        self.assertFalse(clause(program, "bad_abs", "ensures").proven)

    def test_unprovable_call_site_keeps_requires(self):
        # main passes data the verifier cannot bound (len of a Text).
        program, _ = verified("""
            fn safe_div(a: Int, b: Int) -> Int
                requires b != 0
            {
                return a / b;
            }
            fn main(console: Console) -> Unit ! {io.read, io.write} {
                let s: Text = read_line(console);
                print(console, str(safe_div(10, len(s))));
            }
        """)
        self.assertFalse(clause(program, "safe_div", "requires").proven)

    def test_one_bad_call_site_poisons_elision(self):
        # First call site proves, second does not: the check must stay.
        program, _ = verified("""
            fn safe_div(a: Int, b: Int) -> Int
                requires b != 0
            {
                return a / b;
            }
            fn helper(x: Int) -> Int {
                return safe_div(10, x);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(safe_div(10, 2)));
                print(console, str(helper(0)));
            }
        """)
        self.assertFalse(clause(program, "safe_div", "requires").proven)

    def test_loop_carried_ensures_not_proven_without_invariants(self):
        program, _ = verified("""
            fn total(n: Int) -> Int
                requires n >= 0
                ensures result >= 0
            {
                var sum: Int = 0;
                var i: Int = 0;
                while i < n {
                    sum = sum + i;
                    i = i + 1;
                }
                return sum;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(total(5)));
            }
        """)
        # Havoc without invariants: sum is unknown after the loop.
        self.assertFalse(clause(program, "total", "ensures").proven)

    def test_plain_division_not_proven(self):
        _, report = verified("""
            fn f(b: Int) -> Int {
                return 10 / b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(3)));
            }
        """)
        self.assertEqual(report.divisions_proven, 0)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestNativeElision(unittest.TestCase):
    """Proven checks must disappear from the emitted Rust; unproven must stay."""

    def test_emitted_rust_reflects_proofs(self):
        from sigil.emit_rust import emit_rust
        program, _ = verified("""
            fn fib(n: Int) -> Int
                requires n >= 0
                ensures result >= 0
            {
                if n < 2 {
                    return n;
                }
                return fib(n - 1) + fib(n - 2);
            }
            fn bad_abs(n: Int) -> Int
                ensures result >= 0
            {
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(fib(10)) + str(bad_abs(1)));
            }
        """)
        rust = emit_rust(program)
        self.assertNotIn("requires clause of 'fib'", rust)
        self.assertNotIn("ensures clause of 'fib'", rust)
        self.assertIn("ensures clause of 'bad_abs'", rust)

    def test_emitted_rust_uses_raw_division_when_proven(self):
        from sigil.emit_rust import emit_rust
        program, _ = verified("""
            fn safe_div(a: Int, b: Int) -> Int
                requires b != 0
            {
                return a / b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(safe_div(10, 2)));
            }
        """)
        rust = emit_rust(program)
        self.assertNotIn("rt_div", rust.split("fn s_safe_div")[1].split("fn ")[0])


if __name__ == "__main__":
    unittest.main()
