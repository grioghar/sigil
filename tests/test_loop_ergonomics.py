"""Tests for the loop/list ergonomics round (dogfood friction #3 and #4):
`break` and the `set` builtin.

The design rule under test for `break`: loop invariants must hold at EVERY
loop exit, including a break — the interpreter re-checks them on the way
out, the verifier treats each break site as an additional proof obligation
(like preservation), and the native emitter re-emits unproven invariant
checks before the lowered `break;`. A loop whose body can break may NOT
assume ¬cond afterwards (a broken exit does not imply the condition turned
false).

`set(xs, i, x)` is the pure single-element replacement: a copy of xs with
element i replaced, faulting out of range. The verifier models its result
as carrying EXACTLY the input list's length, which is what makes
`ensures len(result) == len(xs)` provable.
"""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil import ast_nodes as A
from sigil.build import build
from sigil.canon import ast_equal, format_source, program_json
from sigil.checker import check
from sigil.errors import CheckError, ContractViolation, RuntimeFault
from sigil.interp import Interpreter
from sigil.parser import parse
from sigil.verify import HAVE_Z3, verify

HAVE_RUSTC = shutil.which("rustc") is not None

# A search loop: break exits early, the state at the break point survives.
FIND = """
    fn find(xs: List[Int], target: Int) -> Int {
        var i: Int = 0;
        var found: Int = 0 - 1;
        while i < len(xs) {
            if xs[i] == target {
                found = i;
                break;
            }
            i = i + 1;
        }
        return found;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(find([5, 7, 9], 7)));
        print(console, str(find([5, 7, 9], 4)));
    }
"""

# break targets the INNERMOST loop only: the outer loop runs all three
# iterations, each inner run stops at j == 2.
NESTED = """
    fn main(console: Console) -> Unit ! {io.write} {
        var i: Int = 0;
        var total: Int = 0;
        while i < 3 {
            var j: Int = 0;
            while j < 10 {
                if j == 2 {
                    break;
                }
                j = j + 1;
            }
            total = total + j;
            i = i + 1;
        }
        print(console, str(total));
    }
"""

# break inside a match arm inside a while is legal and exits the loop.
MATCH_BREAK = """
    enum Flag {
        On,
        Off,
    }
    fn run(x: Flag) -> Int {
        var i: Int = 0;
        while i < 10 {
            match x {
                On => {
                    break;
                }
                Off => {
                }
            }
            i = i + 1;
        }
        return i;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(run(On)) + " " + str(run(Off)));
    }
"""

# The invariant holds at every loop head (i reaches only 0 and 1 there) but
# is false AT the break point: only the break-exit check can catch it.
BREAK_VIOLATES = """
    fn f(n: Int) -> Int
        requires n >= 0
    {
        var i: Int = 0;
        while i < n invariant i != 2 {
            i = i + 1;
            if i == 2 {
                break;
            }
        }
        return i;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(f(5)));
    }
"""

# The invariant holds everywhere, break included: must run clean, and the
# verifier must prove it (entry + preservation + break site) AND keep the
# post-loop invariant fact so the ensures proves WITHOUT ¬cond.
BREAK_HOLDS = """
    fn cap(n: Int) -> Int
        requires n >= 0
        ensures result >= 0
    {
        var i: Int = 0;
        while i < n invariant i >= 0 {
            if i >= 2 {
                break;
            }
            i = i + 1;
        }
        return i;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(cap(5)));
    }
"""

# f's ensures would only prove via the post-loop ¬cond fact (i >= n), but
# the loop breaks — assuming ¬cond would be UNSOUND (f(5) returns 0). g is
# the break-free control: there ¬cond is sound and the same ensures proves.
NOT_COND = """
    fn f(n: Int) -> Int
        requires n >= 0
        ensures result >= n
    {
        var i: Int = 0;
        while i < n invariant i >= 0 {
            break;
        }
        return i;
    }
    fn g(n: Int) -> Int
        requires n >= 0
        ensures result >= n
    {
        var i: Int = 0;
        while i < n invariant i >= 0 {
            i = i + 1;
        }
        return i;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(g(3)));
    }
"""

SET_EXEC = """
    fn main(console: Console) -> Unit ! {io.write} {
        let xs: List[Int] = [1, 2, 3];
        let a: List[Int] = set(xs, 0, 9);
        let b: List[Int] = set(xs, 1, 9);
        let c: List[Int] = set(xs, 2, 9);
        print(console, str(a[0]) + str(a[1]) + str(a[2]));
        print(console, str(b[0]) + str(b[1]) + str(b[2]));
        print(console, str(c[0]) + str(c[1]) + str(c[2]));
        print(console, str(xs[1]));
    }
"""

# `set` inside a generic function, instantiated at Int and at Text.
SET_GENERIC = """
    fn replace_first[T](xs: List[T], x: T) -> List[T] {
        return set(xs, 0, x);
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(replace_first([1, 2], 9)[0]));
        print(console, replace_first(["a", "b"], "z")[0]);
    }
"""

SET_VERIFY = """
    fn update(xs: List[Int], i: Int, x: Int) -> List[Int]
        requires i >= 0 and i < len(xs)
        ensures len(result) == len(xs)
    {
        return set(xs, i, x);
    }
    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(len(update([1, 2, 3], 1, 9))));
    }
"""

# One feature-dense program for the rustc differential: break with and
# without invariants, set on Ints and through a generic instantiation.
BREAK_SET_SINK = """
    fn find(xs: List[Int], target: Int) -> Int {
        var i: Int = 0;
        var found: Int = 0 - 1;
        while i < len(xs) {
            if xs[i] == target {
                found = i;
                break;
            }
            i = i + 1;
        }
        return found;
    }
    fn replace_first[T](xs: List[T], x: T) -> List[T]
        ensures len(result) == len(xs)
    {
        return set(xs, 0, x);
    }
    fn drain_to(n: Int, stop: Int) -> Int
        requires n >= 0
        ensures result >= 0
    {
        var i: Int = 0;
        while i < n invariant i >= 0 {
            if i == stop {
                break;
            }
            i = i + 1;
        }
        return i;
    }
    fn main(console: Console) -> Unit ! {io.write} {
        let xs: List[Int] = [5, 7, 9, 7];
        print(console, str(find(xs, 7)) + " " + str(find(xs, 4)));
        let ys: List[Int] = set(xs, find(xs, 9), 0 - 9);
        print(console, str(ys[0]) + str(ys[1]) + str(ys[2]) + str(ys[3]));
        print(console, str(replace_first(xs, 1)[0]) + " "
            + replace_first(["a", "b"], "z")[0]);
        print(console, str(drain_to(10, 4)) + " " + str(drain_to(3, 99)));
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
    return [c for c in fn.contracts if c.kind == kind][index]


class TestBreakParsingAndCanon(unittest.TestCase):
    def test_break_parses_to_a_break_node(self):
        program = parse(FIND)
        loop = loop_of(program, "find")
        then_body = loop.body[0].then_body
        self.assertIsInstance(then_body[-1], A.Break)

    def test_canonical_rendering_and_round_trip(self):
        formatted = format_source(FIND)
        self.assertIn("            break;\n", formatted)
        self.assertEqual(formatted, format_source(formatted),
                         "fmt is not idempotent")
        self.assertTrue(ast_equal(parse(FIND), parse(formatted)),
                        "fmt does not round-trip the AST")

    def test_break_serializes_as_a_json_node(self):
        program = parse(FIND)
        check(program)
        data = program_json(program)
        find = next(f for f in data["functions"] if f["name"] == "find")
        rendered = str(find["body"])
        self.assertIn("'node': 'Break'", rendered)


class TestBreakExecution(unittest.TestCase):
    def test_search_loop_exits_early_with_state_intact(self):
        self.assertEqual(run(FIND), "1\n-1\n")

    def test_nested_loops_break_only_the_inner_one(self):
        self.assertEqual(run(NESTED), "6\n")

    def test_break_inside_match_arm_inside_while(self):
        self.assertEqual(run(MATCH_BREAK), "0 10\n")


class TestBreakInvariantsAtRuntime(unittest.TestCase):
    """Reference semantics: invariants hold at every exit, breaks included."""

    def test_invariant_violated_at_the_break_point_blames_the_loop(self):
        with self.assertRaises(ContractViolation) as ctx:
            run(BREAK_VIOLATES)
        self.assertEqual(ctx.exception.blame, "loop")
        self.assertIn("invariant of while loop in 'f' failed",
                      ctx.exception.message)
        self.assertIn("`i != 2`", ctx.exception.message)

    def test_invariant_holding_at_break_runs_clean(self):
        self.assertEqual(run(BREAK_HOLDS), "2\n")


class TestBreakChecking(unittest.TestCase):
    def test_break_outside_any_loop_is_rejected(self):
        src = """
            fn f() -> Unit {
                break;
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("break outside of a loop", ctx.exception.message)

    def test_break_in_match_arm_outside_any_loop_is_rejected(self):
        src = """
            enum Flag {
                On,
                Off,
            }
            fn f(x: Flag) -> Unit {
                match x {
                    On => {
                        break;
                    }
                    Off => {
                    }
                }
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("break outside of a loop", ctx.exception.message)

    def test_break_after_a_loop_is_rejected(self):
        # The loop has closed; depth must be back at zero.
        src = """
            fn f(n: Int) -> Unit {
                var i: Int = 0;
                while i < n {
                    i = i + 1;
                }
                break;
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("break outside of a loop", ctx.exception.message)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestBreakVerifier(unittest.TestCase):
    def test_break_site_obligation_proves_and_invariant_survives_the_loop(self):
        program, _ = verified(BREAK_HOLDS)
        inv = loop_of(program, "cap").invariants[0]
        self.assertTrue(inv.proven,
                        "i >= 0 holds on entry, per iteration, and at the break")
        self.assertTrue(clause(program, "cap", "ensures").proven,
                        "the post-loop invariant fact must carry the ensures")

    def test_not_cond_is_not_assumed_after_a_breaking_loop(self):
        program, _ = verified(NOT_COND)
        self.assertFalse(clause(program, "f", "ensures").proven,
                         "result >= n must NOT prove via ¬cond: the loop breaks")
        self.assertTrue(clause(program, "g", "ensures").proven,
                        "the break-free control proves via ¬cond as before")

    def test_invariant_violated_only_at_the_break_point_is_unproven(self):
        program, _ = verified(BREAK_VIOLATES)
        inv = loop_of(program, "f").invariants[0]
        self.assertFalse(inv.proven,
                         "i != 2 fails exactly at the break site")


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestBreakNativeEmission(unittest.TestCase):
    """Unproven invariants keep a runtime check before the emitted break;
    proven invariants emit nothing there (as everywhere else)."""

    def test_unproven_invariant_emits_a_check_before_break(self):
        from sigil.emit_rust import emit_rust
        program, _ = verified(BREAK_VIOLATES)
        rust = emit_rust(program)
        # before the loop, at the end of the body, and before the break
        self.assertEqual(rust.count("invariant of while loop in 'f'"), 3)
        lines = rust.splitlines()
        idx = next(i for i, line in enumerate(lines)
                   if line.strip() == "break;")
        self.assertIn("invariant of while loop in 'f'", lines[idx - 1])
        self.assertIn("`i != 2`", lines[idx - 1])

    def test_proven_invariant_emits_nothing_at_the_break(self):
        from sigil.emit_rust import emit_rust
        program, _ = verified(BREAK_HOLDS)
        rust = emit_rust(program)
        self.assertNotIn("invariant of while loop", rust)
        self.assertIn("break;", rust)


class TestSetExecution(unittest.TestCase):
    def test_replace_first_middle_last_leaves_original_alone(self):
        self.assertEqual(run(SET_EXEC), "923\n193\n129\n2\n")

    def test_negative_index_faults(self):
        src = """
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(set([1, 2, 3], 0 - 1, 9)[0]));
            }
        """
        with self.assertRaises(RuntimeFault) as ctx:
            run(src)
        self.assertIn("set index -1 out of range for list of length 3",
                      ctx.exception.message)

    def test_index_equal_to_length_faults(self):
        src = """
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(set([1, 2, 3], 3, 9)[0]));
            }
        """
        with self.assertRaises(RuntimeFault) as ctx:
            run(src)
        self.assertIn("set index 3 out of range for list of length 3",
                      ctx.exception.message)

    def test_set_in_a_generic_fn_instantiated_twice(self):
        self.assertEqual(run(SET_GENERIC), "9\nz\n")


class TestSetChecking(unittest.TestCase):
    def test_non_list_first_argument_rejected(self):
        src = """
            fn f() -> Unit {
                set(5, 0, 1);
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("set takes a List, an Int index, and an element",
                      ctx.exception.message)

    def test_non_int_index_rejected(self):
        src = """
            fn f() -> Unit {
                set([1, 2], "a", 3);
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("set index must be Int, got Text", ctx.exception.message)

    def test_element_type_mismatch_rejected(self):
        src = """
            fn f() -> Unit {
                set([1, 2], 0, "x");
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check(parse(src))
        self.assertIn("cannot set Text into List[Int]", ctx.exception.message)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestSetVerifier(unittest.TestCase):
    def test_set_preserves_length_and_requires_proves_at_literal_site(self):
        program, _ = verified(SET_VERIFY)
        self.assertTrue(clause(program, "update", "ensures").proven,
                        "set's result carries the input list's exact length")
        self.assertTrue(clause(program, "update", "requires").proven,
                        "set([1,2,3], 1, 9) bounds prove from the literals")


@unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
class TestNativeDifferential(unittest.TestCase):
    """The interpreter is the reference semantics: the native binary's stdout
    must be byte-identical for a program combining break and set."""

    def test_break_and_set_native_matches_interpreter(self):
        expected = run(BREAK_SET_SINK)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "sink.sg"
            src.write_text(BREAK_SET_SINK, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "sink.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)


if __name__ == "__main__":
    unittest.main()
