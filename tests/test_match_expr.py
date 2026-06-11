"""Tests for match-EXPRESSIONS (dogfood friction item #6): `match s {
Variant(b) => expr, ... }` in expression position. The statement form (arms
with blocks) is untouched; the expression form mirrors IfExpr — lowest
precedence, only the selected arm evaluates, arm types unify to one result
type — while keeping every static rule of the statement match (exhaustive,
no dead wildcard, globally unique variants, binder arity)."""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.canon import ast_equal, format_source, program_json
from sigil.checker import check
from sigil.emit_rust import emit_rust
from sigil.errors import CheckError, ContractViolation
from sigil.interp import Interpreter
from sigil.modules import load_program
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


def verified(source: str):
    program = parse(source)
    check(program)
    report = verify(program)
    return program, report


def clause(program, fn_name: str, kind: str, index: int = 0):
    fn = next(f for f in program.functions if f.name == fn_name)
    return [c for c in fn.contracts if c.kind == kind][index]


SHAPE = """
    enum Shape {
        Circle(Int),
        Rect(Int, Int),
        Empty,
    }
"""


class TestExecution(unittest.TestCase):
    def test_payload_binding_and_selection(self):
        out = run(SHAPE + """
            fn area(s: Shape) -> Int {
                return match s {
                    Circle(r) => 3 * r * r,
                    Rect(w, h) => w * h,
                    Empty => 0,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(area(Circle(2))) + " " + str(area(Rect(3, 4)))
                    + " " + str(area(Empty)));
            }
        """)
        self.assertEqual(out, "12 12 0\n")

    def test_wildcard_arm(self):
        out = run(SHAPE + """
            fn pick(s: Shape) -> Int {
                return match s {
                    Circle(r) => r,
                    _ => 99,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(pick(Circle(5))) + " " + str(pick(Rect(1, 2)))
                    + " " + str(pick(Empty)));
            }
        """)
        self.assertEqual(out, "5 99 99\n")

    def test_only_selected_arm_evaluated(self):
        # boom's requires would fire if an unselected arm were evaluated.
        out = run(SHAPE + """
            fn boom(n: Int) -> Int
                requires n > 0
            {
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let a: Int = match (Empty) {
                    Circle(r) => boom(0),
                    Rect(w, h) => boom(0 - w),
                    Empty => 7,
                };
                print(console, str(a));
            }
        """)
        self.assertEqual(out, "7\n")

    def test_selected_arm_still_enforces_contracts(self):
        with self.assertRaises(ContractViolation):
            run(SHAPE + """
                fn boom(n: Int) -> Int
                    requires n > 0
                {
                    return n;
                }
                fn main(console: Console) -> Unit ! {io.write} {
                    print(console, str(match Circle(1) {
                        Circle(r) => boom(0),
                        _ => 1,
                    }));
                }
            """)

    def test_arm_types_unify_toward_specific(self):
        out = run(SHAPE + """
            fn main(console: Console) -> Unit ! {io.write} {
                let xs: List[Int] = match (Empty) {
                    Circle(r) => [],
                    _ => [1, 2, 3],
                };
                print(console, str(len(xs)));
            }
        """)
        self.assertEqual(out, "3\n")


class TestCheckerRejections(unittest.TestCase):
    def assert_check_error(self, source: str, *needles: str):
        with self.assertRaises(CheckError) as ctx:
            check_only(source)
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))

    def test_non_exhaustive(self):
        self.assert_check_error(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    Circle(r) => r,
                };
            }
        """, "not exhaustive", "Rect, Empty")

    def test_dead_wildcard(self):
        self.assert_check_error(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    Circle(r) => r,
                    Rect(w, h) => w,
                    Empty => 0,
                    _ => 1,
                };
            }
        """, "wildcard '_' arm is dead")

    def test_wildcard_must_be_last(self):
        self.assert_check_error(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    _ => 1,
                    Circle(r) => r,
                };
            }
        """, "must be the last arm")

    def test_duplicate_arm(self):
        self.assert_check_error(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    Circle(r) => r,
                    Circle(q) => q,
                    _ => 0,
                };
            }
        """, "duplicate arm for variant 'Circle'")

    def test_binder_arity(self):
        self.assert_check_error(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    Rect(w) => w,
                    _ => 0,
                };
            }
        """, "variant 'Rect' has 2 payload(s); this arm binds 1")

    def test_unknown_variant(self):
        self.assert_check_error(SHAPE + """
            enum Other {
                Thing,
            }
            fn f(s: Shape) -> Int {
                return match s {
                    Thing => 1,
                    _ => 0,
                };
            }
        """, "'Thing' is not a variant of enum 'Shape'")

    def test_incompatible_arm_types(self):
        self.assert_check_error(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    Circle(r) => r,
                    _ => "nope",
                };
            }
        """, "arms must produce one type", "Int", "Text")

    def test_non_enum_scrutinee(self):
        self.assert_check_error("""
            fn f(n: Int) -> Int {
                return match n {
                    _ => 0,
                };
            }
        """, "match needs an enum value", "Int")

    def test_binders_scoped_to_their_arm(self):
        self.assert_check_error(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    Circle(r) => r,
                    _ => r,
                };
            }
        """, "unknown name 'r'")


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestVerification(unittest.TestCase):
    def test_all_arms_nonnegative_proves_ensures(self):
        program, _ = verified(SHAPE + """
            fn area(s: Shape) -> Int
                ensures result >= 0
            {
                return match s {
                    Circle(r) => r * r,
                    Rect(w, h) => 0,
                    Empty => 1,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(area(Empty)));
            }
        """)
        self.assertTrue(clause(program, "area", "ensures").proven)

    def test_one_negative_arm_blocks_ensures(self):
        # Rect's w * h can be negative; the proof must fail.
        program, _ = verified(SHAPE + """
            fn area(s: Shape) -> Int
                ensures result >= 0
            {
                return match s {
                    Circle(r) => r * r,
                    Rect(w, h) => w * h,
                    Empty => 0,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(area(Empty)));
            }
        """)
        self.assertFalse(clause(program, "area", "ensures").proven)

    def test_sized_arms_merge_by_length(self):
        program, _ = verified(SHAPE + """
            fn label(s: Shape) -> Text
                ensures len(result) >= 4
            {
                return match s {
                    Circle(r) => "circle",
                    Rect(w, h) => "rect" + str(w),
                    Empty => "none",
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, label(Empty));
            }
        """)
        self.assertTrue(clause(program, "label", "ensures").proven)

    def test_unselected_arm_ensures_do_not_poison(self):
        # lie's ensures clauses are contradictory; if an unselected arm's
        # callee facts were assumed unguarded, the path would become UNSAT
        # and g's false ensures would "prove". Mirror of the short-circuit
        # soundness test in test_verify_len.py.
        program, _ = verified(SHAPE + """
            fn lie(n: Int) -> Int
                ensures result < 0
                ensures result > 0
            {
                return 0;
            }
            fn g(s: Shape) -> Int
                ensures result == 1
            {
                let x: Int = match s {
                    Circle(r) => lie(r),
                    _ => 7,
                };
                return 2;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(g(Empty)));
            }
        """)
        self.assertFalse(clause(program, "g", "ensures").proven)

    def test_total_match_inside_clause_does_not_block_proof(self):
        # The match itself is total: nothing in the clause can fault, so the
        # partial_ops guard must not be tripped and the proof goes through.
        program, _ = verified(SHAPE + """
            fn f(s: Shape) -> Int
                ensures result >= (match s {
                    Circle(r) => 0,
                    _ => 0,
                })
            {
                return 1;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(Empty)));
            }
        """)
        self.assertTrue(clause(program, "f", "ensures").proven)

    def test_partial_op_inside_clause_match_blocks_proof(self):
        # An arm divides; the clause's own evaluation can fault, so erasing
        # its check would mask the fault — never proven.
        program, _ = verified(SHAPE + """
            fn f(s: Shape) -> Int
                ensures result >= (match s {
                    Circle(r) => r / 1,
                    _ => 0,
                })
            {
                return 1000000;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(Empty)));
            }
        """)
        self.assertFalse(clause(program, "f", "ensures").proven)

    def test_arm_obligations_are_recorded(self):
        # A division inside an arm with an unconstrained divisor must surface
        # as an unproven finding (the arm may run).
        program, report = verified(SHAPE + """
            fn f(s: Shape, d: Int) -> Int {
                return match s {
                    Circle(r) => r / d,
                    _ => 0,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(Empty, 0)));
            }
        """)
        divisions = [f for f in report.findings
                     if f.fn == "f" and f.kind == "division"]
        self.assertEqual(len(divisions), 1)
        self.assertFalse(divisions[0].proven)


class TestCanon(unittest.TestCase):
    def assert_canonical(self, source: str) -> str:
        formatted = format_source(source)
        self.assertEqual(formatted, format_source(formatted),
                         "fmt is not idempotent")
        self.assertTrue(ast_equal(parse(source), parse(formatted)),
                        "fmt does not round-trip the AST")
        return formatted

    def test_let_renders_multiline_with_trailing_commas(self):
        formatted = self.assert_canonical(SHAPE + """
            fn f(s: Shape) -> Int {
                let a: Int = match s { Circle(r) => 3*r*r, Rect(w,h) => w*h, Empty => 0 };
                return a;
            }
        """)
        self.assertIn(
            "    let a: Int = match s {\n"
            "        Circle(r) => 3 * r * r,\n"
            "        Rect(w, h) => w * h,\n"
            "        Empty => 0,\n"
            "    };\n", formatted)

    def test_return_position(self):
        formatted = self.assert_canonical(SHAPE + """
            fn f(s: Shape) -> Int {
                return (match s {
                    Circle(r) => r,
                    _ => 0,
                });
            }
        """)
        # A bare value position drops redundant parens, like IfExpr.
        self.assertIn(
            "    return match s {\n"
            "        Circle(r) => r,\n"
            "        _ => 0,\n"
            "    };\n", formatted)

    def test_call_argument_position(self):
        formatted = self.assert_canonical(SHAPE + """
            fn f(s: Shape) -> Text {
                return str(match s {
                    Circle(r) => r,
                    _ => 0,
                });
            }
        """)
        self.assertIn(
            "    return str(match s {\n"
            "        Circle(r) => r,\n"
            "        _ => 0,\n"
            "    });\n", formatted)

    def test_nested_match_in_arm_and_operand_parens(self):
        formatted = self.assert_canonical(SHAPE + """
            enum Sig {
                Hi(Int),
                Lo,
            }
            fn f(s: Shape, t: Sig) -> Int {
                let b: Int = match s {
                    Circle(r) => match t {
                        Hi(n) => n + r,
                        Lo => r,
                    },
                    _ => 0,
                };
                return (match t {
                    Hi(n) => n,
                    Lo => 1,
                }) + b;
            }
        """)
        self.assertIn(
            "    let b: Int = match s {\n"
            "        Circle(r) => match t {\n"
            "            Hi(n) => n + r,\n"
            "            Lo => r,\n"
            "        },\n"
            "        _ => 0,\n"
            "    };\n", formatted)
        # As an operand of '+' the match keeps its parens (lowest precedence).
        self.assertIn(
            "    return (match t {\n"
            "        Hi(n) => n,\n"
            "        Lo => 1,\n"
            "    }) + b;\n", formatted)

    def test_scrutinee_nullary_variant_guard(self):
        formatted = self.assert_canonical(SHAPE + """
            fn f(s: Shape, c: Bool) -> Int {
                return match (if c then s else Empty) {
                    Circle(r) => r,
                    _ => 0,
                };
            }
        """)
        # The scrutinee rendering ends in a nullary variant; bare, `Empty {`
        # would swallow the arms as a record literal.
        self.assertIn("return match (if c then s else Empty) {", formatted)

    def test_expression_statement_keeps_parens(self):
        # Statement position parses the `match` keyword as the statement
        # form, so a match-expression used as an expression statement must
        # keep its parentheses to round-trip — exactly like IfExpr.
        formatted = self.assert_canonical(SHAPE + """
            fn f(s: Shape) -> Unit {
                (match s {
                    Circle(r) => r,
                    _ => 0,
                });
                return;
            }
        """)
        self.assertIn(
            "    (match s {\n"
            "        Circle(r) => r,\n"
            "        _ => 0,\n"
            "    });\n", formatted)

    def test_statement_form_untouched(self):
        formatted = self.assert_canonical(SHAPE + """
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
        """)
        self.assertIn("    match s {\n        Circle(r) => {\n", formatted)

    def test_json_nodes(self):
        program = parse(SHAPE + """
            fn f(s: Shape) -> Int {
                return match s {
                    Circle(r) => r,
                    _ => 0,
                };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(Empty)));
            }
        """)
        check(program)
        doc = program_json(program)
        ret = doc["functions"][0]["body"][0]["value"]
        self.assertEqual(ret["node"], "MatchExpr")
        self.assertEqual(ret["scrutinee"]["node"], "Var")
        arms = ret["arms"]
        self.assertEqual(arms[0]["node"], "MatchExprArm")
        self.assertEqual(arms[0]["variant"], "Circle")
        self.assertEqual(arms[0]["binders"], ["r"])
        self.assertEqual(arms[0]["expr"]["node"], "Var")
        self.assertIsNone(arms[1]["variant"])


class TestModules(unittest.TestCase):
    SHAPES = ("pub enum Shape {\n"
              "    Circle(Int),\n"
              "    Empty,\n"
              "}\n\n"
              "pub fn area(s: Shape) -> Int {\n"
              "    return match s {\n"
              "        Circle(r) => 3 * r * r,\n"
              "        Empty => 0,\n"
              "    };\n"
              "}\n")
    APP = ("use shapes { Shape, area }\n\n"
           "fn main(console: Console) -> Unit ! {io.write} {\n"
           "    let s: Shape = Circle(2);\n"
           "    let local: Int = match s {\n"
           "        Circle(r) => r,\n"
           "        Empty => 0,\n"
           "    };\n"
           "    print(console, str(area(s)) + \" \" + str(local));\n"
           "}\n")

    def test_match_expr_qualifies_across_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shapes.sg").write_text(self.SHAPES, encoding="utf-8")
            (root / "app.sg").write_text(self.APP, encoding="utf-8")
            program = load_program(str(root / "app.sg"))
            sigs = check(program)

            # The library's own match-expr arms are qualified, and the entry
            # module's arms resolve to the imported qualified variants.
            lib_fn = next(f for f in program.functions
                          if f.name == "shapes.area")
            lib_match = lib_fn.body[0].value
            self.assertEqual([a.variant for a in lib_match.arms],
                             ["shapes.Circle", "shapes.Empty"])
            app_fn = next(f for f in program.functions if f.name == "main")
            app_match = app_fn.body[1].value
            self.assertEqual([a.variant for a in app_match.arms],
                             ["shapes.Circle", "shapes.Empty"])

            out = io.StringIO()
            Interpreter(program, sigs, stdin=io.StringIO(""),
                        stdout=out).run_main()
            self.assertEqual(out.getvalue(), "12 2\n")

            # Dots never reach a Rust identifier.
            rust = emit_rust(program)
            self.assertIn("s_shapes__Shape::s_shapes__Circle(s_r)", rust)
            self.assertNotIn("s_shapes.", rust)


# One feature-dense program for the single rustc differential build:
# payload binding, wildcard, nested match-in-arm, a match-expression as a
# call argument and as a parenthesized operand, and — crucially — an
# unselected arm whose contract-violating call must not fire natively either.
DIFFERENTIAL = SHAPE + """
    fn boom(n: Int) -> Int
        requires n > 0
    {
        return n;
    }

    fn area(s: Shape) -> Int
        ensures result >= 0
    {
        return match s {
            Circle(r) => 3 * r * r,
            Rect(w, h) => w * h,
            Empty => 0,
        };
    }

    fn label(s: Shape) -> Text {
        return match s {
            Circle(r) => "circle " + str(r),
            _ => "other",
        };
    }

    fn main(console: Console) -> Unit ! {io.write} {
        print(console, str(area(Circle(3))) + " " + str(area(Rect(2, 5)))
            + " " + str(area(Empty)));
        let safe: Int = match (Empty) {
            Circle(r) => boom(0),
            Rect(w, h) => boom(0 - w),
            Empty => 7,
        };
        print(console, str(safe));
        print(console, label(Circle(4)) + " " + label(Rect(1, 1)));
        let nested: Int = match Circle(2) {
            Circle(r) => match Rect(r, r + 1) {
                Circle(q) => q,
                Rect(w, h) => w * h,
                Empty => 0,
            },
            _ => 0,
        };
        print(console, str(nested) + " " + str((match Circle(1) {
            Circle(r) => r,
            _ => 0,
        }) + 100));
    }
"""


def interpret(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


@unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
class TestNativeDifferential(unittest.TestCase):
    def test_native_matches_interpreter(self):
        expected = interpret(DIFFERENTIAL)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "matchexpr.sg"
            src.write_text(DIFFERENTIAL, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "matchexpr.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)
        self.assertEqual(expected, "27 10 0\n7\ncircle 4 other\n6 101\n")


if __name__ == "__main__":
    unittest.main()
