"""Tests for length modeling in the verifier (dogfood friction item #1).

Two kinds of tests matter here: proofs that SHOULD now succeed (lengths of
literals, slice, push, concatenation flow through the solver), and proofs
that MUST NOT succeed — the soundness guards for short-circuit operands and
partial operations inside clauses.
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
    return [c for c in fn.contracts if c.kind == kind][index]


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestLengthProofs(unittest.TestCase):
    def test_slice_length_proves_ensures(self):
        program, _ = verified("""
            fn char_at(s: Text, i: Int) -> Text
                requires i >= 0 and i < len(s)
                ensures len(result) == 1
            {
                return slice(s, i, i + 1);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, char_at("sigil", 2));
            }
        """)
        self.assertTrue(clause(program, "char_at", "ensures").proven)
        self.assertTrue(clause(program, "char_at", "requires").proven)

    def test_push_and_concat_lengths(self):
        program, _ = verified("""
            fn extend(xs: List[Int], x: Int) -> List[Int]
                ensures len(result) == len(xs) + 1
            {
                return push(xs, x);
            }
            fn shout(s: Text) -> Text
                ensures len(result) == len(s) + 1
            {
                return s + "!";
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, shout("hi" + str(len(extend([1], 2)))));
            }
        """)
        self.assertTrue(clause(program, "extend", "ensures").proven)
        self.assertTrue(clause(program, "shout", "ensures").proven)

    def test_literal_length_discharges_requires(self):
        program, _ = verified("""
            fn split(s: Text, sep: Text) -> Text
                requires len(sep) == 1
            {
                return s;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, split("a,b", ","));
            }
        """)
        self.assertTrue(clause(program, "split", "requires").proven)

    def test_loop_invariant_over_lengths(self):
        # The set_done pattern from programs/tasks: rebuild a list, prove
        # the result has the same length.
        program, report = verified("""
            fn copy(xs: List[Int]) -> List[Int]
                ensures len(result) == len(xs)
            {
                var out: List[Int] = [];
                var i: Int = 0;
                while i < len(xs)
                    invariant i >= 0
                    invariant i <= len(xs)
                    invariant len(out) == i
                {
                    out = push(out, xs[i]);
                    i = i + 1;
                }
                return out;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(len(copy([3, 1, 4]))));
            }
        """)
        self.assertTrue(clause(program, "copy", "ensures").proven)
        invariants = [f for f in report.findings
                      if f.fn == "copy" and f.kind == "invariant"]
        self.assertEqual(len(invariants), 3)
        self.assertTrue(all(f.proven for f in invariants))

    def test_equality_implies_length_equality(self):
        program, _ = verified("""
            fn two(s: Text) -> Int
                requires s == "ab"
                ensures result == 2
            {
                return len(s);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(two("ab")));
            }
        """)
        self.assertTrue(clause(program, "two", "ensures").proven)

    def test_invariants_available_to_condition_obligations(self):
        # The condition calls a contracted function inside a short-circuit
        # guard; its requires needs the loop invariants.
        program, _ = verified("""
            fn head(s: Text, i: Int) -> Text
                requires i >= 0 and i < len(s)
            {
                return slice(s, i, i + 1);
            }
            fn skip_dots(s: Text) -> Int {
                var i: Int = 0;
                while i < len(s) and head(s, i) == "."
                    invariant i >= 0
                {
                    i = i + 1;
                }
                return i;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(skip_dots("..x")));
            }
        """)
        self.assertTrue(clause(program, "head", "requires").proven)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestSoundnessGuards(unittest.TestCase):
    def test_unknown_text_length_not_overclaimed(self):
        # read_line's result has len >= 0 and nothing more.
        program, _ = verified("""
            fn main(console: Console) -> Unit ! {io.read, io.write} {
                print(console, nonempty(read_line(console)));
            }
            fn nonempty(s: Text) -> Text
                requires len(s) >= 1
            {
                return s;
            }
        """)
        self.assertFalse(clause(program, "nonempty", "requires").proven)

    def test_short_circuit_guards_callee_ensures(self):
        # lie's ensures clauses are contradictory; if the right operand of
        # `and` were assumed unguarded, the path would become UNSAT and the
        # caller's false ensures would "prove". It must not.
        program, _ = verified("""
            fn lie(n: Int) -> Int
                ensures result < 0
                ensures result > 0
            {
                return 0;
            }
            fn g(b: Bool) -> Int
                ensures result == 1
            {
                let x: Bool = b and lie(1) > 0;
                return 2;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(g(false)));
            }
        """)
        self.assertFalse(clause(program, "g", "ensures").proven)

    def test_partial_op_inside_clause_blocks_proof(self):
        # The ensures itself contains a slice that can fault; erasing the
        # check would mask the fault, so it must never be marked proven.
        program, _ = verified("""
            fn f() -> Text
                ensures len(slice(result, 0, 5)) == 5
            {
                return "ab";
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, f());
            }
        """)
        self.assertFalse(clause(program, "f", "ensures").proven)

    def test_user_call_inside_clause_blocks_proof(self):
        # Pure helper calls in clauses can fault too (their own requires);
        # conservative until callee-fault-freedom is provable.
        program, _ = verified("""
            fn helper(n: Int) -> Int
                requires n > 0
            {
                return n;
            }
            fn f(n: Int) -> Int
                requires helper(1) == 1
            {
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(3)));
            }
        """)
        self.assertFalse(clause(program, "f", "requires").proven)


if __name__ == "__main__":
    unittest.main()
