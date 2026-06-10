"""Tests for canonical form (0.4): the formatter (idempotence + AST
round-trip, with comments preserved), the serialized typed AST with stable
declaration ids/shapes, and the semantic diff."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from sigil.__main__ import main
from sigil.canon import (ast_equal, decl_id, decl_shape, format_source,
                         program_json, sdiff)
from sigil.checker import check
from sigil.parser import parse

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

SNIPPETS = {
    "record": """
        record Point { x: Int, y: Int }
        fn main(console: Console) -> Unit ! {io.write} {
            let p: Point = Point {x:1,y:2};
            print(console, str(p.x));
        }
    """,
    "generics": """
        fn first[T](xs: List[T]) -> T requires len(xs) > 0 { return xs[0]; }
        fn pair[T, U](a: T, b: U) -> Unit { return; }
    """,
    "invariants": """
        fn total(n: Int) -> Int requires n >= 0 ensures result >= 0 {
            var sum: Int = 0;
            var i: Int = 0;
            while i < n invariant sum >= 0 invariant i >= 0 {
                sum = sum + i; i = i + 1;
            }
            return sum;
        }
    """,
    "else_if": """
        fn sign(n: Int) -> Int {
            if n > 0 { return 1; } else { if n < 0 { return -1; } else { return 0; } }
        }
    """,
    "parens": """
        fn f(a: Int, b: Int, c: Int) -> Bool {
            let x: Int = (a + b) * c;
            let y: Int = a + (b * c);
            let z: Int = a - (b - c);
            let w: Int = -(a + b) % c;
            return (x > 0 and y > 0) or not (z == (w == 1 and true) and a < b + 1);
        }
    """,
    "literals": """
        fn lits(flag: Bool) -> Text {
            let xs: List[Int] = [1, 2, 3];
            let ys: List[Int] = [];
            return "tab\\t quote\\" back\\\\ nl\\n bell\\u{7}";
        }
    """,
}


def fmt_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestFormatterInvariants(unittest.TestCase):
    def assert_canonical(self, source: str) -> str:
        formatted = format_source(source)
        self.assertEqual(formatted, format_source(formatted), "fmt is not idempotent")
        self.assertTrue(ast_equal(parse(source), parse(formatted)),
                        "fmt does not round-trip the AST")
        return formatted

    def test_examples(self):
        examples = sorted(EXAMPLES.glob("*.sg"))
        self.assertTrue(examples, "no example programs found")
        for path in examples:
            with self.subTest(example=path.name):
                self.assert_canonical(path.read_text(encoding="utf-8"))

    def test_snippets(self):
        for name, source in SNIPPETS.items():
            with self.subTest(snippet=name):
                self.assert_canonical(source)

    def test_nullary_variant_before_block_keeps_parens(self):
        # `if x == Empty {` would re-parse with `Empty {` as a record literal
        # swallowing the block; the formatter must keep the parens at every
        # block-introducing position: if, else-if, while, last invariant.
        source = """
            enum Opt { Empty, Got(Int) }
            fn f(x: Opt, y: Opt) -> Int {
                if (x == Empty) {
                    return 0;
                } else if (y == Empty) {
                    return 1;
                }
                while (x == Empty) {
                    return 2;
                }
                var i: Int = 0;
                while i < 1
                    invariant (x == Empty)
                {
                    i = i + 1;
                }
                return 3;
            }
            fn main(c: Console) -> Unit {
            }
        """
        formatted = self.assert_canonical(source)
        for needle in ("if (x == Empty) {", "} else if (y == Empty) {",
                       "while (x == Empty) {", "invariant (x == Empty)"):
            self.assertIn(needle, formatted)

    def test_nullary_variant_in_last_contract_keeps_parens(self):
        # The lexer is newline-blind: a final contract clause ending in a
        # nullary variant sits directly before the body's '{'.
        source = """
            enum Opt { Empty, Got(Int) }
            fn f(x: Opt) -> Opt
                requires (x == Empty)
            {
                return x;
            }
            fn main(c: Console) -> Unit {
            }
        """
        formatted = self.assert_canonical(source)
        self.assertIn("requires (x == Empty)", formatted)

    def test_record_rendering(self):
        formatted = self.assert_canonical(SNIPPETS["record"])
        self.assertIn("record Point {\n    x: Int,\n    y: Int,\n}", formatted)
        self.assertIn("Point { x: 1, y: 2 }", formatted)

    def test_generic_header_rendering(self):
        formatted = self.assert_canonical(SNIPPETS["generics"])
        self.assertIn("fn first[T](xs: List[T]) -> T\n    requires len(xs) > 0\n{", formatted)
        self.assertIn("fn pair[T, U](a: T, b: U) -> Unit {", formatted)

    def test_effects_sorted_and_pure_omitted(self):
        formatted = self.assert_canonical("""
            fn io(c: Console, f: Fs) -> Unit ! {io.write, fs.write, fs.read} { return; }
            fn pure() -> Int { return 1; }
        """)
        self.assertIn("! {fs.read, fs.write, io.write}", formatted)
        self.assertIn("fn pure() -> Int {", formatted)

    def test_while_invariants_rendering(self):
        formatted = self.assert_canonical(SNIPPETS["invariants"])
        self.assertIn(
            "    while i < n\n"
            "        invariant sum >= 0\n"
            "        invariant i >= 0\n"
            "    {\n", formatted)
        # A while without invariants keeps its brace on the header line.
        plain = self.assert_canonical(
            "fn f() -> Unit { while true { return; } }")
        self.assertIn("    while true {\n", plain)

    def test_else_if_chain(self):
        formatted = self.assert_canonical(SNIPPETS["else_if"])
        self.assertIn("    if n > 0 {\n", formatted)
        self.assertIn("    } else if n < 0 {\n", formatted)
        self.assertIn("    } else {\n", formatted)

    def test_minimal_parens(self):
        formatted = self.assert_canonical(SNIPPETS["parens"])
        self.assertIn("let x: Int = (a + b) * c;", formatted)
        self.assertIn("let y: Int = a + b * c;", formatted)       # redundant parens dropped
        self.assertIn("let z: Int = a - (b - c);", formatted)     # left-assoc: right parens kept
        self.assertIn("let w: Int = -(a + b) % c;", formatted)
        # and > or, so the redundant left parens are dropped; the necessary
        # ones (Bool operand of ==, and-expr under not) are kept.
        self.assertIn(
            "return x > 0 and y > 0 or not (z == (w == 1 and true) and a < b + 1);",
            formatted)

    def test_comparisons_inside_and(self):
        formatted = self.assert_canonical(
            "fn f(a: Int, b: Int) -> Bool { return ((a < b) and (b <= 10)) or (a == b); }")
        self.assertIn("return a < b and b <= 10 or a == b;", formatted)

    def test_text_literal_canonical_escapes(self):
        formatted = self.assert_canonical(SNIPPETS["literals"])
        self.assertIn(r'"tab\t quote\" back\\ nl\n bell\u{7}"', formatted)

    def test_whitespace_discipline(self):
        formatted = self.assert_canonical(SNIPPETS["record"])
        self.assertTrue(formatted.endswith("}\n"))
        self.assertFalse(formatted.endswith("\n\n"))
        for line in formatted.splitlines():
            self.assertEqual(line, line.rstrip(), "trailing whitespace emitted")
        # Exactly one blank line between declarations.
        self.assertIn("}\n\nfn main", formatted)
        self.assertNotIn("\n\n\n", formatted)


class TestCommentPreservation(unittest.TestCase):
    def test_standalone_comments(self):
        source = (
            "// file header\n"
            "// second line\n"
            "fn f() -> Int {\n"
            "    // about x\n"
            "    let x: Int = 1;\n"
            "    return x;\n"
            "}\n"
            "// trailing remark at end of file\n"
        )
        formatted = format_source(source)
        self.assertEqual(formatted, format_source(formatted))
        self.assertTrue(formatted.startswith("// file header\n// second line\nfn f"))
        self.assertIn("    // about x\n    let x: Int = 1;\n", formatted)
        self.assertTrue(formatted.endswith("\n\n// trailing remark at end of file\n"))

    def test_trailing_comment_hoisted(self):
        source = (
            "fn f() -> Int {\n"
            "    let x: Int = 1; // hoist me\n"
            "    return x;\n"
            "}\n"
        )
        formatted = format_source(source)
        self.assertIn("    // hoist me\n    let x: Int = 1;\n", formatted)
        self.assertEqual(formatted, format_source(formatted))

    def test_comments_survive_everywhere(self):
        source = (
            "// before record\n"
            "record Point {\n"
            "    x: Int, // field note\n"
            "}\n"
            "fn f(p: Point) -> Int { // header note\n"
            "    if p.x > 0 {\n"
            "        return 1;\n"
            "    // why else\n"
            "    } else if p.x < 0 {\n"
            "        return -1;\n"
            "    } else {\n"
            "        // fallthrough\n"
            "        return 0;\n"
            "    }\n"
            "}\n"
        )
        formatted = format_source(source)
        for text in ("// before record", "// field note", "// header note",
                     "// why else", "// fallthrough"):
            self.assertIn(text, formatted)
        self.assertIn("    // why else\n    } else if p.x < 0 {\n", formatted)
        self.assertEqual(formatted, format_source(formatted))

    def test_write_never_destroys_comments(self):
        source = "// one\nfn f() -> Int {\n    return 1; // two\n}\n// three\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prog.sg"
            path.write_text(source, encoding="utf-8")
            code, _, _ = fmt_main("fmt", "--write", str(path))
            self.assertEqual(code, 0)
            rewritten = path.read_text(encoding="utf-8")
        for text in ("// one", "// two", "// three"):
            self.assertIn(text, rewritten)


class TestFmtCli(unittest.TestCase):
    def test_fmt_prints_canonical_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prog.sg"
            path.write_text("fn f()->Int{return 1+2;}", encoding="utf-8")
            code, out, _ = fmt_main("fmt", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(out, "fn f() -> Int {\n    return 1 + 2;\n}\n")

    def test_check_flags_non_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prog.sg"
            path.write_text("fn f()->Int{return 1;}", encoding="utf-8")
            code, _, err = fmt_main("fmt", "--check", str(path))
            self.assertEqual(code, 1)
            self.assertIn("not canonical", err)
            # --write makes it canonical; --check then passes.
            self.assertEqual(fmt_main("fmt", "--write", str(path))[0], 0)
            code, _, err = fmt_main("fmt", "--check", str(path))
            self.assertEqual(code, 0)
            self.assertEqual(err, "")

    def test_fmt_reports_parse_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.sg"
            path.write_text("fn f( {", encoding="utf-8")
            code, _, err = fmt_main("fmt", str(path))
        self.assertEqual(code, 1)
        self.assertIn("parse error", err)


FN_ADD = "fn add(a: Int, b: Int) -> Int { return a + b; }"
FN_PLUS = "fn plus(a: Int, b: Int) -> Int { return a + b; }"


class TestSerializedAst(unittest.TestCase):
    def info(self, source: str) -> dict:
        program = parse(source)
        check(program)
        return program_json(program)

    def test_stable_ids(self):
        first = self.info(FN_ADD)
        second = self.info("fn add(a:Int,b:Int)->Int{return a+b;}")
        self.assertEqual(first, second, "ids must not depend on formatting")
        self.assertEqual(len(first["functions"][0]["id"]), 12)
        self.assertEqual(first["version"], 1)

    def test_rename_changes_id_not_shape(self):
        add = self.info(FN_ADD)["functions"][0]
        plus = self.info(FN_PLUS)["functions"][0]
        self.assertNotEqual(add["id"], plus["id"])
        self.assertEqual(add["shape"], plus["shape"])

    def test_recursive_rename_keeps_shape(self):
        fact = parse("fn fact(n: Int) -> Int { if n <= 1 { return 1; } "
                     "return n * fact(n - 1); }").functions[0]
        gact = parse("fn gact(n: Int) -> Int { if n <= 1 { return 1; } "
                     "return n * gact(n - 1); }").functions[0]
        self.assertNotEqual(decl_id(fact), decl_id(gact))
        self.assertEqual(decl_shape(fact), decl_shape(gact))

    def test_calls_to_others_affect_shape(self):
        a = parse("fn f(n: Int) -> Int { return helper(n); } "
                  "fn helper(n: Int) -> Int { return n; }").functions[0]
        b = parse("fn f(n: Int) -> Int { return other(n); } "
                  "fn other(n: Int) -> Int { return n; }").functions[0]
        self.assertNotEqual(decl_shape(a), decl_shape(b))

    def test_json_structure(self):
        doc = self.info("""
            record Point { x: Int, y: Int }
            fn norm(p: Point) -> Int
                requires p.x >= 0
                ensures result >= 0
            { return p.x + p.y; }
        """)
        rec = doc["records"][0]
        self.assertEqual(rec["name"], "Point")
        self.assertEqual(rec["fields"], [{"name": "x", "type": "Int"},
                                         {"name": "y", "type": "Int"}])
        fn = doc["functions"][0]
        self.assertEqual(fn["params"], [{"name": "p", "type": "Point"}])
        self.assertEqual(fn["ret"], "Int")
        self.assertEqual(fn["contracts"], [
            {"kind": "requires", "source": "p.x >= 0"},
            {"kind": "ensures", "source": "result >= 0"},
        ])
        ret = fn["body"][0]
        self.assertEqual(ret["node"], "Return")
        self.assertEqual(ret["value"]["node"], "Binary")
        self.assertEqual(ret["value"]["op"], "+")

    def test_ast_cli_checks_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.sg"
            good.write_text(FN_ADD, encoding="utf-8")
            code, out, _ = fmt_main("ast", str(good))
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(doc["functions"][0]["name"], "add")
            bad = Path(tmp) / "bad.sg"
            bad.write_text("fn f() -> Int { return true; }", encoding="utf-8")
            code, _, err = fmt_main("ast", str(bad))
            self.assertEqual(code, 1)
            self.assertIn("check error", err)


class TestSemanticDiff(unittest.TestCase):
    def diff(self, old: str, new: str) -> list[str]:
        old_prog, new_prog = parse(old), parse(new)
        check(old_prog)
        check(new_prog)
        return sdiff(old_prog, new_prog)

    def test_no_differences(self):
        self.assertEqual(self.diff(FN_ADD, "fn add(a:Int, b:Int) -> Int { return a+b; }"), [])

    def test_added_and_removed(self):
        lines = self.diff(
            FN_ADD + " record Temp { t: Int }",
            FN_ADD + " fn helper(x: Int) -> Int { return x + x; }")
        self.assertIn("added       fn helper", lines)
        self.assertIn("removed     record Temp", lines)
        self.assertEqual(len(lines), 2)

    def test_rename_detection(self):
        lines = self.diff(
            "fn total(n: Int) -> Int { return n; } " + FN_ADD,
            "fn sum(n: Int) -> Int { return n; } " + FN_ADD)
        self.assertEqual(lines, ["renamed     fn total -> fn sum"])

    def test_ambiguous_rename_is_add_remove(self):
        # Two removed functions share the added one's shape: not a rename.
        lines = self.diff(
            "fn a(n: Int) -> Int { return n; } fn b(n: Int) -> Int { return n; }",
            "fn c(n: Int) -> Int { return n; }")
        self.assertEqual(sorted(lines), [
            "added       fn c",
            "removed     fn a",
            "removed     fn b",
        ])

    def test_signature_change(self):
        lines = self.diff("fn fib(n: Int) -> Int { return n; }",
                          "fn fib(n: Int, k: Int) -> Int { return n; }")
        self.assertEqual(lines, ["signature   fn fib"])
        lines = self.diff("fn f(c: Console) -> Unit { return; }",
                          "fn f(c: Console) -> Unit ! {io.write} { print(c, \"x\"); }")
        self.assertEqual(lines, ["signature   fn f"])

    def test_signature_beats_contracts_and_body(self):
        lines = self.diff(
            "fn f(n: Int) -> Int requires n > 0 { return n; }",
            "fn f(n: Int, k: Int) -> Int requires n > 1 { return n + k; }")
        self.assertEqual(lines, ["signature   fn f"])

    def test_contracts_change(self):
        lines = self.diff(
            "fn safe_div(a: Int, b: Int) -> Int requires b != 0 { return a / b; }",
            "fn safe_div(a: Int, b: Int) -> Int requires b > 0 { return a / b; }")
        self.assertEqual(lines, ["contracts   fn safe_div"])

    def test_body_change(self):
        lines = self.diff("fn main() -> Int { return 1; }",
                          "fn main() -> Int { return 2; }")
        self.assertEqual(lines, ["body        fn main"])

    def test_record_field_change(self):
        lines = self.diff("record P { x: Int }", "record P { x: Int, y: Int }")
        self.assertEqual(lines, ["body        record P"])

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.sg"
            new = Path(tmp) / "new.sg"
            old.write_text(FN_ADD, encoding="utf-8")
            new.write_text(FN_PLUS, encoding="utf-8")
            code, out, _ = fmt_main("sdiff", str(old), str(new))
            self.assertEqual(code, 1)
            self.assertEqual(out, "renamed     fn add -> fn plus\n")
            code, out, _ = fmt_main("sdiff", str(old), str(old))
            self.assertEqual(code, 0)
            self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
