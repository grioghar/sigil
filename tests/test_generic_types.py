"""Acceptance tests for generic records and enums (the nominal answer to the
"no tuples" friction: one `Step[T]` instead of two copy-paste enums)."""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.canon import ast_equal, format_source
from sigil.checker import check
from sigil.errors import CheckError
from sigil.interp import Interpreter
from sigil.parser import parse

HAVE_RUSTC = shutil.which("rustc") is not None


def run(source: str, stdin: str = "") -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(stdin), stdout=out).run_main()
    return out.getvalue()


def check_only(source: str) -> None:
    check(parse(source))


class TestGenericExecution(unittest.TestCase):
    def test_generic_record_two_instantiations(self):
        out = run("""
            record Pair[A, B] {
                first: A,
                second: B,
            }
            fn fst[A, B](p: Pair[A, B]) -> A {
                return p.first;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let a: Pair[Int, Text] = Pair { first: 7, second: "x" };
                let b: Pair[Text, Int] = Pair { first: "y", second: 9 };
                print(console, str(fst(a)) + " " + fst(b));
            }
        """)
        self.assertEqual(out, "7 y\n")

    def test_generic_enum_match_both_variants(self):
        out = run("""
            enum Step[T] {
                Done(T, Int),
                Fail(Text, Int),
            }
            fn describe(s: Step[Int]) -> Text {
                match s {
                    Done(v, n) => {
                        return "done " + str(v) + "@" + str(n);
                    }
                    Fail(m, p) => {
                        return "fail " + m + "@" + str(p);
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, describe(Done(42, 3)));
                print(console, describe(Fail("nope", 9)));
            }
        """)
        self.assertEqual(out, "done 42@3\nfail nope@9\n")

    def test_generic_record_holding_generic_enum(self):
        out = run("""
            enum Maybe[T] {
                Nothing,
                Just(T),
            }
            record Box[T] {
                label: Text,
                content: Maybe[T],
            }
            fn unwrap_or(b: Box[Int], fallback: Int) -> Int {
                return match b.content {
                    Nothing => fallback,
                    Just(v) => v,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let full: Box[Int] = Box { label: "a", content: Just(5) };
                let empty: Box[Int] = Box { label: "b", content: Nothing };
                print(console, str(unwrap_or(full, 0)) + " " + str(unwrap_or(empty, 0)));
            }
        """)
        self.assertEqual(out, "5 0\n")

    def test_tree_recursion_through_list_same_args(self):
        out = run("""
            record Tree[T] {
                value: T,
                children: List[Tree[T]],
            }
            fn total(t: Tree[Int]) -> Int {
                var sum: Int = t.value;
                var i: Int = 0;
                while i < len(t.children)
                    invariant i >= 0
                {
                    sum = sum + total(t.children[i]);
                    i = i + 1;
                }
                return sum;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let leaf: Tree[Int] = Tree { value: 4, children: [] };
                let root: Tree[Int] = Tree {
                    value: 1,
                    children: [Tree { value: 2, children: [] }, leaf],
                };
                print(console, str(total(root)));
            }
        """)
        self.assertEqual(out, "7\n")

    def test_generic_fn_through_generic_type(self):
        out = run("""
            record Pair[A, B] {
                first: A,
                second: B,
            }
            fn diag[T](p: Pair[T, T]) -> T {
                return p.second;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let p: Pair[Int, Int] = Pair { first: 1, second: 2 };
                print(console, str(diag(p)));
            }
        """)
        self.assertEqual(out, "2\n")

    def test_payload_less_variant_inferred_from_return_context(self):
        out = run("""
            enum Maybe[T] {
                Nothing,
                Just(T),
            }
            fn none_int() -> Maybe[Int] {
                return Nothing;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let m: Maybe[Int] = none_int();
                print(console, match m {
                    Nothing => "none",
                    Just(v) => str(v),
                });
            }
        """)
        self.assertEqual(out, "none\n")

    def test_payload_less_variant_inferred_from_let_annotation(self):
        out = run("""
            enum Maybe[T] {
                Nothing,
                Just(T),
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let m: Maybe[Text] = Nothing;
                print(console, match m {
                    Nothing => "empty",
                    Just(v) => v,
                });
            }
        """)
        self.assertEqual(out, "empty\n")


class TestGenericRejection(unittest.TestCase):
    def test_missing_type_arguments(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Pair[A, B] {
                    first: A,
                    second: B,
                }
                fn f(p: Pair) -> Int {
                    return 0;
                }
                fn main(console: Console) -> Unit {
                }
            """)
        self.assertIn("takes 2 type argument", ctx.exception.message)

    def test_arguments_on_non_generic(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Point {
                    x: Int,
                    y: Int,
                }
                fn f(p: Point[Int]) -> Int {
                    return 0;
                }
                fn main(console: Console) -> Unit {
                }
            """)
        self.assertIn("not generic", ctx.exception.message)

    def test_uninferable_construction_outside_context(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                enum Maybe[T] {
                    Nothing,
                    Just(T),
                }
                fn f(console: Console) -> Unit ! {io.write} {
                    print(console, str(len([Nothing])));
                }
                fn main(console: Console) -> Unit {
                }
            """)
        self.assertIn("infer", ctx.exception.message)

    def test_conflicting_field_inference(self):
        with self.assertRaises(CheckError):
            check_only("""
                record Same[T] {
                    a: T,
                    b: T,
                }
                fn main(console: Console) -> Unit {
                    let s: Same[Int] = Same { a: 1, b: "two" };
                }
            """)

    def test_type_param_collides_with_record(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record T {
                    v: Int,
                }
                record Box[T] {
                    item: T,
                }
                fn main(console: Console) -> Unit {
                }
            """)
        self.assertIn("collides", ctx.exception.message)

    def test_capability_content_equality_rejected_under_instantiation(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Holder[T] {
                    item: T,
                }
                fn main(console: Console) -> Unit {
                    let a: Holder[Console] = Holder { item: console };
                    let same: Bool = a == a;
                }
            """)
        self.assertIn("capabilit", ctx.exception.message)


class TestGenericCanon(unittest.TestCase):
    def assert_round_trips(self, source: str) -> None:
        formatted = format_source(source)
        self.assertEqual(formatted, format_source(formatted),
                         "fmt is not idempotent")
        self.assertTrue(ast_equal(parse(source), parse(formatted)),
                        "fmt does not round-trip the AST")

    def test_generic_headers_and_references(self):
        self.assert_round_trips("""
            record Pair[A, B] {
                first: A,
                second: B,
            }
            enum Step[T] {
                Done(T, Int),
                Fail(Text, Int),
            }
            fn use_them[T](p: Pair[T, Int], s: Step[T]) -> Int {
                return p.second;
            }
            fn main(console: Console) -> Unit {
            }
        """)

    def test_canonical_header_rendering(self):
        formatted = format_source("""
            record Pair [ A , B ] { first : A , second : B }
            fn main(console: Console) -> Unit {
            }
        """)
        self.assertIn("record Pair[A, B] {", formatted)


@unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
class TestGenericNative(unittest.TestCase):
    def test_native_matches_interpreter(self):
        source = """
            record Pair[A, B] {
                first: A,
                second: B,
            }
            enum Step[T] {
                Done(T, Int),
                Fail(Text, Int),
            }
            fn fst[A, B](p: Pair[A, B]) -> A {
                return p.first;
            }
            fn run_step(s: Step[Int]) -> Int {
                return match s {
                    Done(v, n) => v + n,
                    Fail(m, p) => 0 - p,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let a: Pair[Int, Text] = Pair { first: 3, second: "x" };
                let b: Pair[Text, Int] = Pair { first: "y", second: 8 };
                print(console, str(fst(a)) + " " + fst(b));
                print(console, str(run_step(Done(5, 2))) + " " + str(run_step(Fail("e", 9))));
            }
        """
        expected = run(source)
        with tempfile.TemporaryDirectory() as tmp:
            from sigil.build import build
            src = Path(tmp) / "gen.sg"
            src.write_text(source, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "gen.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True,
                                    encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)


if __name__ == "__main__":
    unittest.main()
