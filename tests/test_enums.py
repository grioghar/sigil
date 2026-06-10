"""Tests for sum types: enum declarations with positional payloads and the
match statement (parse, check, run, verify, canon, native, server).

The rejection tests matter most: exhaustiveness, dead wildcards, duplicate
arms, and infinite-size cycles are what make match auditable.
"""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.canon import ast_equal, format_source, program_json, sdiff
from sigil.checker import check
from sigil.errors import CheckError
from sigil.interp import Interpreter
from sigil.parser import parse
from sigil.server import handle_request
from sigil.verify import HAVE_Z3, verify

HAVE_RUSTC = shutil.which("rustc") is not None

SHAPES = """
    enum Shape {
        Circle(Int),
        Rect(Int, Int),
        Empty,
    }
"""


def run(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


def check_only(source: str) -> None:
    check(parse(source))


class TestEnumExecution(unittest.TestCase):
    def test_construction_and_match_with_payload_binding(self):
        out = run(SHAPES + """
            fn area(s: Shape) -> Int {
                match s {
                    Circle(r) => {
                        return 3 * r * r;
                    }
                    Rect(w, h) => {
                        return w * h;
                    }
                    Empty => {
                        return 0;
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(area(Circle(2))));
                print(console, str(area(Rect(3, 4))));
                print(console, str(area(Empty)));
            }
        """)
        self.assertEqual(out, "12\n12\n0\n")

    def test_nullary_and_wildcard(self):
        out = run(SHAPES + """
            fn label(s: Shape) -> Text {
                match s {
                    Empty => {
                        return "empty";
                    }
                    _ => {
                        return "solid";
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, label(Empty) + " " + label(Circle(1)));
            }
        """)
        self.assertEqual(out, "empty solid\n")

    def test_enum_inside_record_and_list(self):
        out = run(SHAPES + """
            record Drawing {
                title: Text,
                shapes: List[Shape],
            }
            fn count_circles(d: Drawing) -> Int {
                var n: Int = 0;
                var i: Int = 0;
                while i < len(d.shapes) {
                    match d.shapes[i] {
                        Circle(r) => {
                            n = n + 1;
                        }
                        _ => {
                            n = n + 0;
                        }
                    }
                    i = i + 1;
                }
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let d: Drawing = Drawing {
                    title: "art",
                    shapes: [Circle(1), Empty, Circle(2), Rect(1, 1)],
                };
                print(console, d.title + ": " + str(count_circles(d)));
            }
        """)
        self.assertEqual(out, "art: 2\n")

    def test_enum_equality(self):
        out = run(SHAPES + """
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(Circle(3) == Circle(3)));
                print(console, str(Circle(3) == Circle(4)));
                print(console, str(Rect(1, 2) != Empty));
                print(console, str(Empty == Empty));
            }
        """)
        self.assertEqual(out, "true\nfalse\ntrue\ntrue\n")

    def test_recursion_through_list(self):
        out = run("""
            enum Expr2 {
                Num(Int),
                Add2(List[Expr2]),
            }
            fn total(e: Expr2) -> Int {
                match e {
                    Num(n) => {
                        return n;
                    }
                    Add2(parts) => {
                        var sum: Int = 0;
                        var i: Int = 0;
                        while i < len(parts) {
                            sum = sum + total(parts[i]);
                            i = i + 1;
                        }
                        return sum;
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let e: Expr2 = Add2([Num(1), Add2([Num(2), Num(3)]), Num(4)]);
                print(console, str(total(e)));
            }
        """)
        self.assertEqual(out, "10\n")

    def test_capability_payload_is_usable(self):
        # Bundling a capability inside a variant is normal ocap style; only
        # equality is forbidden (tested under rejections).
        out = run("""
            enum Sink {
                Loud(Console),
                Quiet,
            }
            fn say(s: Sink, msg: Text) -> Unit ! {io.write} {
                match s {
                    Loud(c) => {
                        print(c, msg);
                    }
                    Quiet => {
                        return;
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                say(Loud(console), "hi");
                say(Quiet, "dropped");
            }
        """)
        self.assertEqual(out, "hi\n")


class TestEnumRejection(unittest.TestCase):
    def assert_check_error(self, source: str, *needles: str) -> None:
        with self.assertRaises(CheckError) as ctx:
            check_only(source)
        for needle in needles:
            self.assertIn(needle, ctx.exception.message)

    def test_non_exhaustive_lists_missing_variants(self):
        self.assert_check_error(SHAPES + """
            fn f(s: Shape) -> Int {
                match s {
                    Circle(r) => {
                        return r;
                    }
                }
                return 0;
            }
            fn main(c: Console) -> Unit {
            }
        """, "not exhaustive", "Rect, Empty")

    def test_duplicate_arm(self):
        self.assert_check_error(SHAPES + """
            fn f(s: Shape) -> Int {
                match s {
                    Empty => {
                        return 0;
                    }
                    Empty => {
                        return 1;
                    }
                    _ => {
                        return 2;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """, "duplicate arm", "Empty")

    def test_wildcard_must_be_last(self):
        self.assert_check_error(SHAPES + """
            fn f(s: Shape) -> Int {
                match s {
                    _ => {
                        return 0;
                    }
                    Empty => {
                        return 1;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """, "last arm")

    def test_dead_wildcard(self):
        self.assert_check_error(SHAPES + """
            fn f(s: Shape) -> Int {
                match s {
                    Circle(r) => {
                        return r;
                    }
                    Rect(w, h) => {
                        return w;
                    }
                    Empty => {
                        return 0;
                    }
                    _ => {
                        return 9;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """, "dead")

    def test_binder_arity_mismatch(self):
        self.assert_check_error(SHAPES + """
            fn f(s: Shape) -> Int {
                match s {
                    Circle(r, extra) => {
                        return r;
                    }
                    _ => {
                        return 0;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """, "1 payload(s)", "binds 2")

    def test_variant_collision_across_enums(self):
        self.assert_check_error("""
            enum A {
                Hit,
            }
            enum B {
                Hit,
            }
            fn main(c: Console) -> Unit {
            }
        """, "variant 'Hit' is already declared in enum 'A'")

    def test_variant_collision_with_record(self):
        self.assert_check_error("""
            record Point {
                x: Int,
            }
            enum E {
                Point(Int),
            }
            fn main(c: Console) -> Unit {
            }
        """, "collides with record 'Point'")

    def test_direct_enum_in_enum_cycle(self):
        self.assert_check_error("""
            enum E {
                Wrap(E),
            }
            fn main(c: Console) -> Unit {
            }
        """, "infinite", "List")

    def test_enum_record_mutual_cycle(self):
        self.assert_check_error("""
            record R {
                e: E,
            }
            enum E {
                Wrap(R),
            }
            fn main(c: Console) -> Unit {
            }
        """, "infinite")

    def test_equality_with_capability_payload(self):
        self.assert_check_error("""
            enum Sink {
                Loud(Console),
                Quiet,
            }
            fn main(console: Console) -> Unit {
                let same: Bool = Loud(console) == Loud(console);
            }
        """, "capability")

    def test_match_on_non_enum(self):
        self.assert_check_error("""
            fn main(c: Console) -> Unit {
                match 5 {
                    _ => {
                        return;
                    }
                }
            }
        """, "match needs an enum value, got Int")

    def test_match_on_generic_var(self):
        self.assert_check_error("""
            fn f[T](x: T) -> Int {
                match x {
                    _ => {
                        return 0;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """, "match needs an enum value, got T")

    def test_record_called_like_a_function(self):
        self.assert_check_error("""
            record Point {
                x: Int,
            }
            fn main(c: Console) -> Unit {
                let p: Point = Point(1);
            }
        """, "record 'Point' is not callable; use Point { ... }")

    def test_payload_variant_used_bare(self):
        self.assert_check_error(SHAPES + """
            fn main(c: Console) -> Unit {
                let s: Shape = Circle;
            }
        """, "variant 'Circle' takes 1 argument(s)")

    def test_construction_arity_and_type(self):
        self.assert_check_error(SHAPES + """
            fn main(c: Console) -> Unit {
                let s: Shape = Circle(1, 2);
            }
        """, "takes 1 argument(s), got 2")
        self.assert_check_error(SHAPES + """
            fn main(c: Console) -> Unit {
                let s: Shape = Circle("big");
            }
        """, "payload 1 of variant 'Circle' needs Int, got Text")

    def test_unknown_uppercase_name_mentions_variants(self):
        self.assert_check_error(SHAPES + """
            fn main(c: Console) -> Unit {
                let s: Shape = Circl;
            }
        """, "unknown name 'Circl'", "variant")

    def test_arm_for_foreign_variant(self):
        self.assert_check_error(SHAPES + """
            enum Color {
                Red,
            }
            fn f(s: Shape) -> Int {
                match s {
                    Red => {
                        return 0;
                    }
                    _ => {
                        return 1;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """, "'Red' is not a variant of enum 'Shape'")

    def test_binder_shadowing_rejected(self):
        self.assert_check_error(SHAPES + """
            fn f(s: Shape, r: Int) -> Int {
                match s {
                    Circle(r) => {
                        return r;
                    }
                    _ => {
                        return 0;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """, "shadowing")

    def test_enum_name_collides_with_record(self):
        self.assert_check_error("""
            record Shape {
                x: Int,
            }
            enum Shape {
                Dot,
            }
            fn main(c: Console) -> Unit {
            }
        """, "collides with record")

    def test_empty_enum_rejected(self):
        self.assert_check_error("""
            enum Nothing {}
            fn main(c: Console) -> Unit {
            }
        """, "at least one variant")


class TestDefinitelyReturns(unittest.TestCase):
    def test_exhaustive_match_is_a_returning_exit(self):
        # The function's ONLY exit is the match; no trailing return needed.
        check_only(SHAPES + """
            fn sides(s: Shape) -> Int {
                match s {
                    Circle(r) => {
                        return 0;
                    }
                    Rect(w, h) => {
                        return 4;
                    }
                    Empty => {
                        return 0;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """)

    def test_arm_without_return_fails_the_path_check(self):
        with self.assertRaises(CheckError) as ctx:
            check_only(SHAPES + """
                fn sides(s: Shape) -> Int {
                    match s {
                        Circle(r) => {
                            return 0;
                        }
                        _ => {
                            let x: Int = 1;
                        }
                    }
                }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("not all paths end in a return", ctx.exception.message)


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestEnumVerification(unittest.TestCase):
    def verified_clause(self, source: str, fn_name: str, kind: str):
        program = parse(source)
        check(program)
        verify(program)
        fn = next(f for f in program.functions if f.name == fn_name)
        return next(c for c in fn.contracts if c.kind == kind)

    def test_ensures_proven_across_all_arms(self):
        # Int binders become fresh ints, so r * r >= 0 proves per arm.
        clause = self.verified_clause(SHAPES + """
            fn measure(s: Shape) -> Int
                ensures result >= 0
            {
                match s {
                    Circle(r) => {
                        return r * r;
                    }
                    Rect(w, h) => {
                        if w * h < 0 {
                            return 0;
                        }
                        return w * h;
                    }
                    Empty => {
                        return 0;
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(measure(Circle(3))));
            }
        """, "measure", "ensures")
        self.assertTrue(clause.proven)

    def test_text_payload_arm_proves_via_len_modeling(self):
        # len(w) is modeled as >= 0, so this genuinely holds and proves.
        clause = self.verified_clause("""
            enum Val {
                Num(Int),
                Word(Text),
            }
            fn score(v: Val) -> Int
                ensures result >= 0
            {
                match v {
                    Num(n) => {
                        return n * n;
                    }
                    Word(w) => {
                        return len(w);
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(score(Num(2))));
            }
        """, "score", "ensures")
        self.assertTrue(clause.proven)

    def test_falsifiable_text_arm_stays_conservative(self):
        # len(w) - 1 is -1 for empty text: this ensures genuinely may fail
        # and must keep its runtime check.
        clause = self.verified_clause("""
            enum Val {
                Num(Int),
                Word(Text),
            }
            fn score(v: Val) -> Int
                ensures result >= 0
            {
                match v {
                    Num(n) => {
                        return n * n;
                    }
                    Word(w) => {
                        return len(w) - 1;
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(score(Num(2))));
            }
        """, "score", "ensures")
        self.assertFalse(clause.proven)

    def test_unproven_match_ensures_keeps_native_check(self):
        from sigil.emit_rust import emit_rust
        source = """
            enum Val {
                Num(Int),
                Word(Text),
            }
            fn score(v: Val) -> Int
                ensures result >= 0
            {
                match v {
                    Num(n) => {
                        return n * n;
                    }
                    Word(w) => {
                        return len(w) - 1;
                    }
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(score(Num(2))));
            }
        """
        program = parse(source)
        check(program)
        verify(program)
        self.assertIn("ensures clause of 'score'", emit_rust(program))

    def test_assignment_inside_arms_is_havocked(self):
        # The verifier cannot know which arm ran; a variable assigned in one
        # arm must not keep its pre-match value in the proof.
        clause = self.verified_clause(SHAPES + """
            fn f(s: Shape) -> Int
                ensures result == 1
            {
                var x: Int = 1;
                match s {
                    Empty => {
                        x = 2;
                    }
                    _ => {
                        x = x + 0;
                    }
                }
                return x;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(Empty)));
            }
        """, "f", "ensures")
        self.assertFalse(clause.proven)


class TestEnumCanon(unittest.TestCase):
    def assert_canonical(self, source: str) -> str:
        formatted = format_source(source)
        self.assertEqual(formatted, format_source(formatted),
                         "fmt is not idempotent")
        self.assertTrue(ast_equal(parse(source), parse(formatted)),
                        "fmt does not round-trip the AST")
        return formatted

    def test_enum_and_match_rendering(self):
        formatted = self.assert_canonical("""
            enum Shape { Circle(Int), Rect(Int,Int), Empty }
            fn f(s: Shape) -> Int {
                match s { Circle(r) => { return r; }
                Rect(w,h)=>{ return w*h; } Empty => { return 0; } }
            }
        """)
        self.assertIn(
            "enum Shape {\n    Circle(Int),\n    Rect(Int, Int),\n    Empty,\n}",
            formatted)
        self.assertIn(
            "    match s {\n"
            "        Circle(r) => {\n"
            "            return r;\n"
            "        }\n"
            "        Rect(w, h) => {\n"
            "            return w * h;\n"
            "        }\n"
            "        Empty => {\n"
            "            return 0;\n"
            "        }\n"
            "    }\n", formatted)

    def test_wildcard_rendering(self):
        formatted = self.assert_canonical("""
            enum Shape { Circle(Int), Empty }
            fn f(s: Shape) -> Int {
                match s { Circle(r) => { return r; } _ => { return 0; } }
            }
        """)
        self.assertIn("        _ => {\n", formatted)

    def test_bare_variant_scrutinee_keeps_parens(self):
        # `match Empty {` would re-parse `Empty {` as a record literal, so the
        # canonical form parenthesizes a scrutinee ending in a bare uppercase
        # name. Both invariants must survive it.
        formatted = self.assert_canonical("""
            enum Shape { Circle(Int), Empty }
            fn f() -> Int {
                match (Empty) { Circle(r) => { return r; } _ => { return 0; } }
            }
        """)
        self.assertIn("    match (Empty) {\n", formatted)

    def test_comments_survive_in_match(self):
        source = (
            "enum Shape {\n"
            "    Circle(Int),\n"
            "    Empty,\n"
            "}\n"
            "\n"
            "fn f(s: Shape) -> Int {\n"
            "    match s {\n"
            "        // the round case\n"
            "        Circle(r) => {\n"
            "            return r;\n"
            "        }\n"
            "        Empty => {\n"
            "            // nothing to measure\n"
            "            return 0;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        formatted = format_source(source)
        self.assertIn("        // the round case\n        Circle(r) => {\n",
                      formatted)
        self.assertIn("// nothing to measure", formatted)
        self.assertEqual(formatted, format_source(formatted))

    def test_ast_json_serializes_enums(self):
        program = parse(SHAPES + """
            fn main(c: Console) -> Unit {
            }
        """)
        check(program)
        doc = program_json(program)
        enum = doc["enums"][0]
        self.assertEqual(enum["name"], "Shape")
        self.assertEqual(enum["variants"], [
            {"name": "Circle", "payloads": ["Int"]},
            {"name": "Rect", "payloads": ["Int", "Int"]},
            {"name": "Empty", "payloads": []},
        ])
        self.assertEqual(len(enum["id"]), 12)
        self.assertEqual(len(enum["shape"]), 12)

    def test_enum_rename_changes_id_not_shape(self):
        # Recursive payloads mention the enum's own name; a pure rename must
        # keep the shape hash anyway.
        old = parse("enum E { Leaf(Int), Node(List[E]) }").enums[0]
        new = parse("enum F { Leaf(Int), Node(List[F]) }").enums[0]
        from sigil.canon import decl_id, decl_shape
        self.assertNotEqual(decl_id(old), decl_id(new))
        self.assertEqual(decl_shape(old), decl_shape(new))

    def test_match_in_body_json(self):
        program = parse(SHAPES + """
            fn f(s: Shape) -> Int {
                match s {
                    Circle(r) => {
                        return r;
                    }
                    _ => {
                        return 0;
                    }
                }
            }
            fn main(c: Console) -> Unit {
            }
        """)
        check(program)
        doc = program_json(program)
        fn = next(f for f in doc["functions"] if f["name"] == "f")
        match = fn["body"][0]
        self.assertEqual(match["node"], "Match")
        self.assertEqual(match["arms"][0]["node"], "MatchArm")
        self.assertEqual(match["arms"][0]["variant"], "Circle")
        self.assertEqual(match["arms"][0]["binders"], ["r"])
        self.assertIsNone(match["arms"][1]["variant"])


class TestEnumSdiff(unittest.TestCase):
    def diff(self, old: str, new: str) -> list[str]:
        old_prog, new_prog = parse(old), parse(new)
        check(old_prog)
        check(new_prog)
        return sdiff(old_prog, new_prog)

    MAIN = "fn main(c: Console) -> Unit { }"

    def test_added_and_removed(self):
        lines = self.diff(self.MAIN, "enum E { A, B } " + self.MAIN)
        self.assertEqual(lines, ["added       enum E"])
        lines = self.diff("enum E { A, B } " + self.MAIN, self.MAIN)
        self.assertEqual(lines, ["removed     enum E"])

    def test_rename_detection(self):
        lines = self.diff("enum E { A, B } " + self.MAIN,
                          "enum F { A, B } " + self.MAIN)
        self.assertEqual(lines, ["renamed     enum E -> enum F"])

    def test_variant_change_is_body(self):
        lines = self.diff("enum E { A, B } " + self.MAIN,
                          "enum E { A, B(Int) } " + self.MAIN)
        self.assertEqual(lines, ["body        enum E"])

    def test_enum_and_record_kinds_never_match(self):
        # Same name, different kind: an add + a remove, never a rename/body.
        lines = self.diff("record T { x: Int } " + self.MAIN,
                          "enum T { X(Int) } " + self.MAIN)
        self.assertEqual(sorted(lines), [
            "added       enum T",
            "removed     record T",
        ])


class TestEnumServer(unittest.TestCase):
    def test_signatures_includes_enums(self):
        response = handle_request({"method": "signatures", "source": SHAPES + """
            fn main(c: Console) -> Unit {
            }
        """})
        json.dumps(response)
        self.assertTrue(response["ok"])
        self.assertEqual(response["enums"], [
            {"name": "Shape",
             "variants": [{"name": "Circle", "payloads": ["Int"]},
                          {"name": "Rect", "payloads": ["Int", "Int"]},
                          {"name": "Empty", "payloads": []}]},
        ])

    def test_effects_walks_match_arms(self):
        response = handle_request({"method": "effects", "source": SHAPES + """
            fn report(c: Console, s: Shape) -> Unit ! {io.write} {
                match s {
                    Circle(r) => {
                        print(c, str(r));
                    }
                    _ => {
                        return;
                    }
                }
            }
            fn main(c: Console) -> Unit ! {io.write} {
                report(c, Empty);
            }
        """, "fn": "report"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["transitive"], ["io.write"])


# One feature-dense program, one rustc invocation: payload + nullary
# variants, match in a loop, match inside a generic function (two
# instantiations), capability-free enum equality, wildcard arm.
NATIVE_PROGRAM = """
enum Op {
    Add(Int),
    Mul(Int),
    Reset,
}

fn apply[T](tag: List[T], acc: Int, op: Op) -> Int {
    match op {
        Add(n) => {
            return acc + n + len(tag);
        }
        Mul(n) => {
            return acc * n;
        }
        _ => {
            return 0;
        }
    }
}

fn run_ops(ops: List[Op]) -> Int {
    var acc: Int = 1;
    var i: Int = 0;
    while i < len(ops) {
        match ops[i] {
            Add(n) => {
                acc = acc + n;
            }
            Mul(n) => {
                acc = acc * n;
            }
            Reset => {
                acc = 0;
            }
        }
        i = i + 1;
    }
    return acc;
}

fn main(console: Console) -> Unit ! {io.write} {
    print(console, str(run_ops([Add(4), Mul(3), Reset, Add(7)])));
    print(console, str(apply(["x", "y"], 10, Add(5))));
    print(console, str(apply([1, 2, 3], 10, Mul(2))));
    print(console, str(Add(4) == Add(4)));
    print(console, str(Add(4) == Mul(4)));
    print(console, str(Reset == Reset));
}
"""


@unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
class TestEnumNative(unittest.TestCase):
    def test_native_matches_interpreter(self):
        from sigil.build import build
        expected = run(NATIVE_PROGRAM)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "ops.sg"
            src.write_text(NATIVE_PROGRAM, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "ops.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)

    def test_emitted_enum_derives(self):
        from sigil.emit_rust import emit_rust
        program = parse(NATIVE_PROGRAM)
        check(program)
        rust = emit_rust(program)
        self.assertIn("#[derive(Clone, PartialEq)]\nenum s_Op {", rust)
        # A capability payload drops PartialEq (== is rejected anyway).
        program = parse("""
            enum Sink {
                Loud(Console),
                Quiet,
            }
            fn main(c: Console) -> Unit {
                let s: Sink = Loud(c);
            }
        """)
        check(program)
        rust = emit_rust(program)
        self.assertIn("#[derive(Clone)]\nenum s_Sink {", rust)


if __name__ == "__main__":
    unittest.main()
