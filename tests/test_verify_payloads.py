"""Payload-aware enum verification: the prover models an enum value as a tag
plus payload slots, so contracts can see inside variants.

Two kinds of tests, and the second kind is the important one: PROOFS that
should now succeed (precise payload reasoning, including the inductive
mutually-recursive parser pattern), and SOUNDNESS tests — claims that depend
on payloads the prover cannot actually know, which must stay unproven so their
runtime checks survive."""

import unittest

from sigil.checker import check
from sigil.parser import parse
from sigil.verify import HAVE_Z3, verify

STEP = """
enum Step {
    Done(Int, Int),
    Fail(Text, Int),
}
"""


def verified(source: str):
    program = parse(source)
    check(program)
    report = verify(program)
    return program, report


def clause(program, fn_name: str, kind: str, index: int = 0):
    fn = next(f for f in program.functions if f.name == fn_name)
    return [c for c in fn.contracts if c.kind == kind][index]


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestPayloadProofs(unittest.TestCase):
    def test_constructed_payload_visible_to_ensures(self):
        program, _ = verified(STEP + """
            fn mk(i: Int) -> Step
                ensures match result {
                    Done(v, j) => j >= i,
                    Fail(m, p) => true,
                }
            {
                return Done(0, i + 1);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let s: Step = mk(3);
            }
        """)
        self.assertTrue(clause(program, "mk", "ensures").proven)

    def test_inductive_parser_pattern_end_to_end(self):
        # step's payload-carrying ensures proves at its return sites AND is
        # assumed at run's call site, so run's recursive requires proves
        # through the shared payload slots. This is the whole point.
        program, _ = verified(STEP + """
            fn step(s: Text, i: Int) -> Step
                requires i >= 0 and i <= len(s)
                ensures match result {
                    Done(v, j) => j >= i and j <= len(s),
                    Fail(m, p) => true,
                }
            {
                if i < len(s) {
                    return Done(1, i + 1);
                }
                return Fail("end", i);
            }
            fn run(s: Text, i: Int) -> Int
                requires i >= 0 and i <= len(s)
            {
                return match step(s, i) {
                    Done(v, j) => run(s, j),
                    Fail(m, p) => 0,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(run("abc", 0)));
            }
        """)
        self.assertTrue(clause(program, "step", "ensures").proven,
                        "payload ensures must prove at the return sites")
        self.assertTrue(clause(program, "run", "requires").proven,
                        "the recursive requires must prove from the assumed "
                        "ensures via shared payload slots")

    def test_wildcard_excludes_covered_tags(self):
        program, _ = verified(STEP + """
            fn classify(s: Step) -> Int
                ensures result >= 0
            {
                return match s {
                    Done(v, j) => 1,
                    _ => 2,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let n: Int = classify(Fail("x", 0));
            }
        """)
        self.assertTrue(clause(program, "classify", "ensures").proven)

    def test_statement_match_return_site_uses_payload(self):
        program, _ = verified(STEP + """
            fn second(s: Step) -> Int
                requires match s {
                    Done(v, j) => j >= 0,
                    Fail(m, p) => true,
                }
                ensures result >= 0
            {
                match s {
                    Done(v, j) => {
                        return j;
                    }
                    Fail(m, p) => {
                        return 0;
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let n: Int = second(Done(0, 5));
            }
        """)
        self.assertTrue(clause(program, "second", "ensures").proven)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestPayloadSoundness(unittest.TestCase):
    def test_false_payload_ensures_not_proven(self):
        # Done(0, i+1) carries j = i+1; claiming j > i + 100 is simply false.
        program, _ = verified(STEP + """
            fn bad(i: Int) -> Step
                ensures match result {
                    Done(v, j) => j > i + 100,
                    Fail(m, p) => true,
                }
            {
                return Done(0, i + 1);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let s: Step = bad(3);
            }
        """)
        self.assertFalse(clause(program, "bad", "ensures").proven)

    def test_unknown_payload_claim_stays_unproven(self):
        # s is an arbitrary Step; the Int it carries is unconstrained, so the
        # result can be negative. No faulting callee defends this — it must
        # keep its runtime check.
        program, _ = verified(STEP + """
            fn extract(s: Step) -> Int
                ensures result >= 0
            {
                return match s {
                    Done(v, j) => j,
                    Fail(m, p) => p,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let n: Int = extract(Fail("x", 0));
            }
        """)
        self.assertFalse(clause(program, "extract", "ensures").proven)

    def test_one_arm_truth_does_not_leak_to_other(self):
        # Only the Done arm yields a non-negative value; the Fail arm yields p,
        # which is unconstrained. The ensures must NOT prove.
        program, _ = verified(STEP + """
            fn pick(s: Step) -> Int
                ensures result >= 0
            {
                return match s {
                    Done(v, j) => 7,
                    Fail(m, p) => p,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let n: Int = pick(Done(0, 0));
            }
        """)
        self.assertFalse(clause(program, "pick", "ensures").proven)

    def test_partial_op_in_payload_clause_blocks_proof(self):
        # The ensures' own evaluation contains a slice that can fault; erasing
        # the check would mask the fault, so it must never be proven even
        # though the arithmetic would otherwise hold.
        program, _ = verified(STEP + """
            fn g(t: Text) -> Step
                requires len(t) >= 5
                ensures match result {
                    Done(v, j) => j == len(slice(t, 0, 5)),
                    Fail(m, p) => true,
                }
            {
                return Done(0, 5);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let s: Step = g("abcde");
            }
        """)
        self.assertFalse(clause(program, "g", "ensures").proven)

    def test_enum_equality_does_not_overclaim(self):
        # `s == Done(0, 0)` only constrains the tag, not the payload, so a
        # claim about the payload value must not prove.
        program, _ = verified(STEP + """
            fn h(s: Step) -> Int
                requires s == Done(0, 0)
                ensures result >= 5
            {
                return match s {
                    Done(v, j) => j,
                    Fail(m, p) => 5,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let n: Int = h(Done(0, 9));
            }
        """)
        self.assertFalse(clause(program, "h", "ensures").proven)


if __name__ == "__main__":
    unittest.main()
