"""Tests for the expression-layer ergonomics features (dogfood friction
items #2 and #5): if-expressions `if cond then a else b` and record
functional update `base with { field: expr, ... }`."""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.canon import ast_equal, format_source, program_json
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


def verified(source: str):
    program = parse(source)
    check(program)
    report = verify(program)
    return program, report


def clause(program, fn_name: str, kind: str, index: int = 0):
    fn = next(f for f in program.functions if f.name == fn_name)
    return [c for c in fn.contracts if c.kind == kind][index]


TASK = """
    record Task {
        title: Text,
        done: Bool,
    }
"""


class TestIfExprExecution(unittest.TestCase):
    def test_basic_and_chained(self):
        out = run("""
            fn classify(n: Int) -> Text {
                return if n < 0 then "neg" else if n == 0 then "zero" else "pos";
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, classify(0 - 7) + " " + classify(0) + " " + classify(7));
                let n: Int = if true then 1 + 1 else 0;
                print(console, str(n));
            }
        """)
        self.assertEqual(out, "neg zero pos\n2\n")

    def test_untaken_branch_does_not_run(self):
        # boom's requires would fire if the untaken branch were evaluated.
        out = run("""
            fn boom(n: Int) -> Int
                requires n > 0
            {
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let a: Int = if true then 1 else boom(0);
                let b: Int = if false then boom(0) else 2;
                print(console, str(a + b));
            }
        """)
        self.assertEqual(out, "3\n")

    def test_taken_branch_still_enforces_contracts(self):
        with self.assertRaises(ContractViolation):
            run("""
                fn boom(n: Int) -> Int
                    requires n > 0
                {
                    return n;
                }
                fn main(console: Console) -> Unit ! {io.write} {
                    print(console, str(if false then 1 else boom(0)));
                }
            """)

    def test_branch_types_unify_toward_specific(self):
        out = run("""
            fn main(console: Console) -> Unit ! {io.write} {
                let xs: List[Int] = if false then [] else [1, 2, 3];
                print(console, str(len(xs)));
            }
        """)
        self.assertEqual(out, "3\n")


class TestRecordUpdateExecution(unittest.TestCase):
    def test_single_and_all_fields(self):
        out = run(TASK + """
            fn main(console: Console) -> Unit ! {io.write} {
                let t: Task = Task { title: "write tests", done: false };
                let u: Task = t with { done: true };
                let v: Task = t with { title: "rewrite", done: true };
                print(console, u.title + " " + str(u.done));
                print(console, v.title + " " + str(v.done));
                print(console, t.title + " " + str(t.done));
            }
        """)
        # The base is untouched: update copies.
        self.assertEqual(out, "write tests true\nrewrite true\nwrite tests false\n")

    def test_base_evaluated_first_then_fields_left_to_right(self):
        # Effectful helpers make the evaluation order visible: base, then
        # fields in the order written.
        out = run(TASK + """
            fn tag(c: Console, label: Text, t: Task) -> Task ! {io.write} {
                print(c, "eval " + label);
                return t;
            }
            fn tag_text(c: Console, label: Text, s: Text) -> Text ! {io.write} {
                print(c, "eval " + label);
                return s;
            }
            fn tag_bool(c: Console, label: Text, b: Bool) -> Bool ! {io.write} {
                print(c, "eval " + label);
                return b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let t: Task = Task { title: "a", done: false };
                let u: Task = tag(console, "base", t) with {
                    title: tag_text(console, "title", "b"),
                    done: tag_bool(console, "done", true),
                };
                print(console, u.title + " " + str(u.done));
            }
        """)
        self.assertEqual(out, "eval base\neval title\neval done\nb true\n")

    def test_parenthesized_update_chains(self):
        out = run(TASK + """
            fn main(console: Console) -> Unit ! {io.write} {
                let t: Task = Task { title: "a", done: false };
                let u: Task = (t with { title: "b" }) with { done: true };
                print(console, u.title + " " + str(u.done));
            }
        """)
        self.assertEqual(out, "b true\n")

    def test_both_features_compose(self):
        out = run(TASK + """
            fn main(console: Console) -> Unit ! {io.write} {
                let t: Task = Task { title: "a", done: false };
                let u: Task = (if t.done then t else t with { done: true })
                    with { title: "b" };
                print(console, u.title + " " + str(u.done));
            }
        """)
        self.assertEqual(out, "b true\n")


class TestCheckerRejections(unittest.TestCase):
    def assert_check_error(self, source: str, *needles: str):
        with self.assertRaises(CheckError) as ctx:
            check_only(source)
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))

    def test_non_bool_condition(self):
        self.assert_check_error("""
            fn f() -> Int { return if 1 then 2 else 3; }
        """, "condition must be Bool", "Int")

    def test_incompatible_branches(self):
        self.assert_check_error("""
            fn f(b: Bool) -> Int { return if b then 1 else "two"; }
        """, "branches must produce one type", "Int", "Text")

    def test_unknown_update_field(self):
        self.assert_check_error(TASK + """
            fn f(t: Task) -> Task { return t with { owner: "me" }; }
        """, "no field 'owner'", "title, done")

    def test_duplicate_update_field(self):
        self.assert_check_error(TASK + """
            fn f(t: Task) -> Task { return t with { done: true, done: false }; }
        """, "duplicate field 'done'")

    def test_out_of_order_update_fields(self):
        self.assert_check_error(TASK + """
            fn f(t: Task) -> Task { return t with { done: true, title: "x" }; }
        """, "declaration order", "title, done")

    def test_empty_update(self):
        self.assert_check_error(TASK + """
            fn f(t: Task) -> Task { return t with {}; }
        """, "at least one field")

    def test_update_on_non_record(self):
        self.assert_check_error("""
            fn f(n: Int) -> Int { return n with { x: 1 }; }
        """, "updates a record value", "Int")

    def test_update_field_type_mismatch(self):
        self.assert_check_error(TASK + """
            fn f(t: Task) -> Task { return t with { done: 1 }; }
        """, "field 'done'", "needs Bool, got Int")

    def test_chained_with_is_a_parse_error(self):
        with self.assertRaises(ParseError) as ctx:
            parse(TASK + """
                fn f(t: Task) -> Task {
                    return t with { title: "a" } with { done: true };
                }
            """)
        self.assertIn("parenthesize", str(ctx.exception))


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestIfExprVerification(unittest.TestCase):
    def test_branch_values_merge_as_z3_if(self):
        program, _ = verified("""
            fn absval(n: Int) -> Int
                ensures result >= 0
            {
                return if n >= 0 then n else 0 - n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(absval(0 - 3)));
            }
        """)
        self.assertTrue(clause(program, "absval", "ensures").proven)

    def test_sized_branches_merge_by_length(self):
        program, _ = verified("""
            fn pick(a: Text, b: Text) -> Text
                ensures len(result) <= len(a) + len(b)
            {
                return if len(a) > 0 then a else b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, pick("x", "y"));
            }
        """)
        self.assertTrue(clause(program, "pick", "ensures").proven)

    def test_branch_facts_discharge_requires(self):
        program, _ = verified("""
            fn pos(n: Int) -> Int
                requires n > 0
            {
                return n;
            }
            fn f(n: Int) -> Int {
                return if n > 0 then pos(n) else 0;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(f(2)));
            }
        """)
        self.assertTrue(clause(program, "pos", "requires").proven)

    def test_untaken_branch_ensures_do_not_poison(self):
        # lie's ensures clauses are contradictory; if the untaken branch's
        # callee facts were assumed unguarded, the path would become UNSAT
        # and g's false ensures would "prove". Mirror of the short-circuit
        # soundness test in test_verify_len.py.
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
                let x: Int = if b then lie(1) else 7;
                return 2;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(g(false)));
            }
        """)
        self.assertFalse(clause(program, "g", "ensures").proven)

    def test_record_update_records_nested_obligations(self):
        # The field expression divides; the divisor is unconstrained, so the
        # division must surface as an unproven finding (not be skipped).
        program, report = verified("""
            record Box {
                value: Int,
            }
            fn f(b: Box, d: Int) -> Box {
                return b with { value: 10 / d };
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let b: Box = Box { value: 1 };
                print(console, str(f(b, 2).value));
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

    def test_if_expr_rendering_and_minimal_parens(self):
        formatted = self.assert_canonical("""
            fn f(c: Bool, d: Bool, a: Int, b: Int) -> Int {
                let w: Int = if c then a else b;
                let x: Int = if c then a else if d then b else 0;
                let y: Int = if c then (if d then a else b) else 0;
                let z: Int = (if c then a else b) + 1;
                let v: Int = ((if c then a else b));
                return if c and d then w + x else y * z + v;
            }
        """)
        self.assertIn("let w: Int = if c then a else b;", formatted)
        # else-position chains stay bare; then-position keeps its parens.
        self.assertIn("let x: Int = if c then a else if d then b else 0;",
                      formatted)
        self.assertIn("let y: Int = if c then (if d then a else b) else 0;",
                      formatted)
        # An operand position always parenthesizes; a bare value position
        # drops redundant parens.
        self.assertIn("let z: Int = (if c then a else b) + 1;", formatted)
        self.assertIn("let v: Int = if c then a else b;", formatted)
        self.assertIn("return if c and d then w + x else y * z + v;", formatted)

    def test_if_expr_in_call_and_index_positions(self):
        formatted = self.assert_canonical("""
            fn f(c: Bool, xs: List[Int], ys: List[Int]) -> Int {
                let a: Int = str_len(str(if c then 1 else 2));
                return (if c then xs else ys)[if c then 0 else 1];
            }
            fn str_len(s: Text) -> Int { return len(s); }
        """)
        # A call argument and an index expression are bare; an index BASE
        # binds postfix-tight and keeps its parens.
        self.assertIn("str(if c then 1 else 2)", formatted)
        self.assertIn("return (if c then xs else ys)[if c then 0 else 1];",
                      formatted)

    def test_if_expr_statement_keeps_parens(self):
        # Statement position parses the `if` keyword as the statement form,
        # so an if-expression used as an expression statement must keep its
        # parentheses to round-trip.
        formatted = self.assert_canonical("""
            fn f(c: Bool) -> Unit {
                (if c then 1 else 2);
                return;
            }
        """)
        self.assertIn("    (if c then 1 else 2);\n", formatted)

    def test_record_update_rendering(self):
        formatted = self.assert_canonical(TASK + """
            fn f(t: Task, c: Bool) -> Task {
                let u: Task = t with {title:"x",done:true,};
                let v: Task = (t with { title: "y" }) with { done: false };
                let w: Task = (if c then t else u) with { done: true };
                return f(t with { done: true }, not c);
            }
        """)
        self.assertIn('let u: Task = t with { title: "x", done: true };',
                      formatted)
        self.assertIn('let v: Task = (t with { title: "y" }) with { done: false };',
                      formatted)
        self.assertIn("let w: Task = (if c then t else u) with { done: true };",
                      formatted)
        self.assertIn("return f(t with { done: true }, not c);", formatted)

    def test_match_scrutinee_if_expr(self):
        formatted = self.assert_canonical("""
            enum Sig {
                Hi(Int),
                Lo,
            }
            fn f(c: Bool, a: Sig, b: Sig) -> Int {
                match if c then a else b {
                    Hi(n) => {
                        return n;
                    }
                    Lo => {
                        return 0;
                    }
                }
            }
            fn g(c: Bool, a: Sig) -> Int {
                match (if c then a else Lo) {
                    Hi(n) => {
                        return n;
                    }
                    Lo => {
                        return 0;
                    }
                }
            }
        """)
        # Bare when safe; parenthesized when the rendering would end in a
        # nullary variant that could swallow the match body.
        self.assertIn("match if c then a else b {", formatted)
        self.assertIn("match (if c then a else Lo) {", formatted)

    def test_json_nodes(self):
        program = parse(TASK + """
            fn f(t: Task, c: Bool) -> Task {
                return if c then t with { done: true } else t;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let t: Task = Task { title: "a", done: false };
                print(console, f(t, true).title);
            }
        """)
        check(program)
        doc = program_json(program)
        ret = doc["functions"][0]["body"][0]["value"]
        self.assertEqual(ret["node"], "IfExpr")
        self.assertEqual(ret["cond"]["node"], "Var")
        update = ret["then_expr"]
        self.assertEqual(update["node"], "RecordUpdate")
        self.assertEqual(update["base"]["node"], "Var")
        self.assertEqual(update["fields"][0][0], "done")


# One feature-dense program for the single rustc differential build: both
# features, if-expression short-circuit (the untaken branch's contract
# violation must not fire natively either), update evaluation order made
# visible through effectful helpers, chained and parenthesized forms.
DIFFERENTIAL = TASK + """
    fn boom(n: Int) -> Int
        requires n > 0
    {
        return n;
    }

    fn tag(c: Console, label: Text, t: Task) -> Task ! {io.write} {
        print(c, "eval " + label);
        return t;
    }

    fn tag_text(c: Console, label: Text, s: Text) -> Text ! {io.write} {
        print(c, "eval " + label);
        return s;
    }

    fn main(console: Console) -> Unit ! {io.write} {
        let t: Task = Task { title: "a", done: false };
        let u: Task = tag(console, "base", t) with {
            title: tag_text(console, "title", "b"),
            done: true,
        };
        print(console, u.title + " " + str(u.done) + " " + t.title);
        let v: Task = (u with { title: "c" }) with { done: false };
        print(console, v.title + " " + str(v.done));
        let safe: Int = if true then 1 else boom(0);
        print(console, str(safe + (if v.done then 10 else 20)));
        print(console, if u.done then if v.done then "tt" else "tf" else "ff");
        let w: Task = (if v.done then v else u) with { title: "w" };
        print(console, w.title + " " + str(w.done));
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
            src = Path(tmp) / "ergonomics.sg"
            src.write_text(DIFFERENTIAL, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "ergonomics.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)
        # Evaluation order is part of the contract: base before fields.
        self.assertIn("eval base\neval title\n", expected)


if __name__ == "__main__":
    unittest.main()
