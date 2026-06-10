"""Tests for parametric polymorphism on functions (v0.2c).

Generics are functions-only: records stay non-generic. There is no call-site
instantiation syntax — type parameters are inferred from arguments, so the
rejection tests (uninferable parameters, conflicting bindings, comparisons of
opaque values) carry as much weight as the happy paths.
"""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.checker import check
from sigil.errors import CheckError, ContractViolation, ParseError
from sigil.interp import Interpreter
from sigil.parser import parse
from sigil.verify import HAVE_Z3, verify

HAVE_RUSTC = shutil.which("rustc") is not None


def run(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


def check_only(source: str) -> None:
    check(parse(source))


# One program exercising: a generic instantiated at Int, Text, and List[Int]
# in a single run; a generic calling another generic (instantiation must
# propagate); let/var of type T and List[T]; push/index/len over T; and a
# generic contract checked per call.
GENERICS_SINK = """
fn first[T](xs: List[T]) -> T
    requires len(xs) > 0
{
    return xs[0];
}

fn tail[T](xs: List[T]) -> List[T] {
    var out: List[T] = [];
    var i: Int = 1;
    while i < len(xs) {
        out = push(out, xs[i]);
        i = i + 1;
    }
    return out;
}

fn second[T](xs: List[T]) -> T
    requires len(xs) > 1
{
    let rest: List[T] = tail(xs);
    let found: T = first(rest);
    return found;
}

fn main(console: Console) -> Unit ! {io.write} {
    let nums: List[Int] = [10, 20, 30];
    let words: List[Text] = ["alpha", "beta", "gamma"];
    print(console, str(first(nums)) + " " + first(words));
    print(console, str(second(nums)) + " " + second(words));
    let grid: List[List[Int]] = [nums, [7]];
    print(console, str(len(first(grid))) + " " + str(second(grid)[0]));
}
"""

GENERICS_SINK_EXPECTED = "10 alpha\n20 beta\n3 7\n"


class TestGenericExecution(unittest.TestCase):
    def test_one_generic_at_three_types(self):
        self.assertEqual(run(GENERICS_SINK), GENERICS_SINK_EXPECTED)

    def test_identity_round_trip(self):
        out = run("""
            fn identity[T](x: T) -> T {
                return x;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, identity("ok " + str(identity(42))));
            }
        """)
        self.assertEqual(out, "ok 42\n")

    def test_generic_pair_same_type(self):
        out = run("""
            fn pick[T](a: T, b: T, take_first: Bool) -> T {
                if take_first {
                    return a;
                }
                return b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, pick("x", "y", false) + str(pick(1, 2, true)));
            }
        """)
        self.assertEqual(out, "y1\n")

    def test_generic_contract_blames_caller_at_runtime(self):
        with self.assertRaises(ContractViolation) as ctx:
            run("""
                fn first[T](xs: List[T]) -> T
                    requires len(xs) > 0
                {
                    return xs[0];
                }
                fn main(console: Console) -> Unit ! {io.write} {
                    let none: List[Int] = [];
                    print(console, str(first(none)));
                }
            """)
        self.assertEqual(ctx.exception.blame, "caller")

    def test_empty_list_ok_when_another_arg_binds(self):
        # The List[None] wildcard from `[]` unifies with List[T]; T is bound
        # by the second argument.
        out = run("""
            fn prepend[T](xs: List[T], x: T) -> List[T] {
                var out: List[T] = [x];
                var i: Int = 0;
                while i < len(xs) {
                    out = push(out, xs[i]);
                    i = i + 1;
                }
                return out;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(len(prepend([], 5))) + " " + prepend([], "a")[0]);
            }
        """)
        self.assertEqual(out, "1 a\n")


class TestGenericRejection(unittest.TestCase):
    def test_uninferable_type_param_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn make[T](n: Int) -> List[T] {
                    return [];
                }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("does not appear in any parameter type",
                      ctx.exception.message)

    def test_conflicting_bindings_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn pair[T](a: T, b: T) -> T {
                    return a;
                }
                fn main(c: Console) -> Unit {
                    let x: Int = pair(1, "one");
                }
            """)
        self.assertIn("conflicting types for T", ctx.exception.message)
        self.assertIn("Int", ctx.exception.message)
        self.assertIn("Text", ctx.exception.message)

    def test_equality_on_generic_values_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn same[T](a: T, b: T) -> Bool {
                    return a == b;
                }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("cannot compare values of generic type T",
                      ctx.exception.message)

    def test_equality_on_list_of_generic_rejected(self):
        # The ban is recursive: a List[T] is just as opaque as a T.
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn same[T](a: List[T], b: List[T]) -> Bool {
                    return a != b;
                }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("cannot compare values of generic type",
                      ctx.exception.message)

    def test_type_param_colliding_with_record_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Box { v: Int }
                fn f[Box](x: Box) -> Box {
                    return x;
                }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("collides with record 'Box'", ctx.exception.message)

    def test_generic_record_field_rejected(self):
        # Records are non-generic; a type parameter can never be in scope for
        # a field, so 'T' is simply an unknown record there.
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Box { v: T }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("unknown record 'T'", ctx.exception.message)

    def test_cannot_infer_from_empty_list_literal(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn first[T](xs: List[T]) -> T
                    requires len(xs) > 0
                {
                    return xs[0];
                }
                fn main(c: Console) -> Unit {
                    let x: Int = first([]);
                }
            """)
        self.assertIn("cannot infer T", ctx.exception.message)

    def test_str_on_generic_value_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn show[T](x: T) -> Text {
                    return str(x);
                }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("str takes one Int, Bool, or Text argument",
                      ctx.exception.message)

    def test_duplicate_type_param_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn f[T, T](x: T) -> T {
                    return x;
                }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("duplicate type parameter 'T'", ctx.exception.message)

    def test_lowercase_type_param_rejected(self):
        with self.assertRaises((CheckError, ParseError)):
            check_only("""
                fn f[t](x: t) -> t {
                    return x;
                }
                fn main(c: Console) -> Unit {
                }
            """)

    def test_concrete_mismatch_keeps_standard_error(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn first[T](xs: List[T]) -> T
                    requires len(xs) > 0
                {
                    return xs[0];
                }
                fn main(c: Console) -> Unit {
                    let x: Int = first(5);
                }
            """)
        self.assertIn("needs List[T], got Int", ctx.exception.message)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestGenericVerification(unittest.TestCase):
    def test_int_requires_of_generic_fn_proven_at_literal_call_site(self):
        # Var-typed values are Opaque to the engine; the Int-typed clause
        # must still prove, and nothing may crash along the way.
        source = """
            fn nth[T](xs: List[T], i: Int) -> T
                requires i >= 0
            {
                return xs[i];
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(nth([1, 2, 3], 1)));
            }
        """
        program = parse(source)
        check(program)
        report = verify(program)
        self.assertIsNotNone(report)
        nth = next(f for f in program.functions if f.name == "nth")
        self.assertTrue(nth.contracts[0].proven,
                        "i >= 0 must prove at the literal call site")


@unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
class TestGenericNativeBackend(unittest.TestCase):
    def test_native_matches_interpreter(self):
        # Differential test: the monomorphized binary's stdout must equal the
        # interpreter's byte for byte (Int, Text, and List[Int] instantiations
        # plus generic-calling-generic propagation).
        from sigil.build import build
        expected = run(GENERICS_SINK)
        self.assertEqual(expected, GENERICS_SINK_EXPECTED)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "generics.sg"
            src.write_text(GENERICS_SINK, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "generics.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)


if __name__ == "__main__":
    unittest.main()
