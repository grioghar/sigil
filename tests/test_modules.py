"""Tests for modules and imports (roadmap 0.7).

The resolver flattens a multi-file program to the single-program pipeline:
entry declarations keep their bare names, every other module's declarations
are qualified ('geometry.area'), and the import header is the only way a
name crosses a file boundary. The security demo at the heart of this file
proves the milestone's point: capabilities thread across module boundaries
explicitly, and a module function that does not declare a capability cannot
acquire one no matter what the importing app does.
"""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.canon import ast_equal, format_source, program_json, sdiff
from sigil.checker import CheckError, check
from sigil.emit_rust import emit_rust
from sigil.errors import ContractViolation, ModuleError, ParseError
from sigil.interp import Interpreter
from sigil.modules import load_program
from sigil.parser import parse
from sigil.server import handle_request
from sigil.verify import HAVE_Z3, verify

HAVE_RUSTC = shutil.which("rustc") is not None
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def write_tree(tmp: str, **modules: str) -> Path:
    """Write each keyword argument as <name>.sg in tmp."""
    root = Path(tmp)
    for name, source in modules.items():
        (root / f"{name}.sg").write_text(source, encoding="utf-8")
    return root


def run_interp(entry: Path) -> str:
    program = load_program(str(entry))
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


def request(**fields) -> dict:
    response = handle_request(fields)
    json.dumps(response)  # every response must be JSON-serializable
    return response


LIB_POINT = """
pub record Point {
    x: Int,
    y: Int,
}

pub fn make(x: Int, y: Int) -> Point {
    return Point { x: x, y: y };
}
"""

APP_ALIAS = """
use lib { Point as Spot, make as build }

fn main(console: Console) -> Unit ! {io.write} {
    let p: Spot = build(2, 3);
    print(console, str(p.x + p.y));
}
"""


class TestResolver(unittest.TestCase):
    def test_single_file_flattens_to_itself(self):
        source = (EXAMPLES / "hello.sg").read_text(encoding="utf-8")
        program = load_program(str(EXAMPLES / "hello.sg"))
        self.assertTrue(ast_equal(program, parse(source)))

    def test_entry_bare_module_qualified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, lib=LIB_POINT, app=APP_ALIAS)
            program = load_program(str(root / "app.sg"))
        self.assertEqual([f.name for f in program.functions],
                         ["lib.make", "main"])
        self.assertEqual([r.name for r in program.records], ["lib.Point"])
        self.assertEqual(program.uses, [])
        check(program)  # the flattened program is a valid single program

    def test_alias_usage_including_type_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, lib=LIB_POINT, app=APP_ALIAS)
            self.assertEqual(run_interp(root / "app.sg"), "5\n")

    def test_diamond_loads_shared_module_once(self):
        d = "pub fn base(n: Int) -> Int {\n    return n + 1;\n}\n"
        b = ("use d { base }\n\n"
             "pub fn twice(n: Int) -> Int {\n"
             "    return base(base(n));\n}\n")
        c = ("use d { base }\n\n"
             "pub fn thrice(n: Int) -> Int {\n"
             "    return base(base(base(n)));\n}\n")
        a = ("use b { twice }\n"
             "use c { thrice }\n\n"
             "fn main(console: Console) -> Unit ! {io.write} {\n"
             "    print(console, str(twice(1)) + \" \" + str(thrice(1)));\n"
             "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, a=a, b=b, c=c, d=d)
            program = load_program(str(root / "a.sg"))
            self.assertEqual(
                [f.name for f in program.functions if f.name == "d.base"],
                ["d.base"], "the shared module must load exactly once")
            check(program)  # no duplicate-declaration errors
            self.assertEqual(run_interp(root / "a.sg"), "3 4\n")

    def test_import_cycle_names_the_chain(self):
        app = ("use geometry { area }\n\n"
               "fn main() -> Unit {\n    return;\n}\n")
        geometry = ("use app { main }\n\n"
                    "pub fn area(n: Int) -> Int {\n    return n;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, app=app, geometry=geometry)
            with self.assertRaises(ModuleError) as ctx:
                load_program(str(root / "app.sg"))
        self.assertIn("import cycle: app -> geometry -> app",
                      ctx.exception.message)

    def test_self_import_is_a_cycle(self):
        app = ("use app { f }\n\n"
               "pub fn f() -> Int {\n    return 1;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, app=app)
            with self.assertRaises(ModuleError) as ctx:
                load_program(str(root / "app.sg"))
        self.assertIn("import cycle: app -> app", ctx.exception.message)

    def test_missing_module_names_the_tried_path(self):
        app = ("use nope { f }\n\n"
               "fn main() -> Unit {\n    return;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, app=app)
            with self.assertRaises(ModuleError) as ctx:
                load_program(str(root / "app.sg"))
            self.assertIn("module 'nope' not found", ctx.exception.message)
            self.assertIn("nope.sg", ctx.exception.message)

    def test_missing_entry_file(self):
        with self.assertRaises(ModuleError) as ctx:
            load_program("no_such_file.sg")
        self.assertIn("cannot read", ctx.exception.message)

    def test_import_of_undeclared_name(self):
        lib = "pub fn f() -> Int {\n    return 1;\n}\n"
        app = ("use lib { frob }\n\n"
               "fn main() -> Unit {\n    return;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, lib=lib, app=app)
            with self.assertRaises(ModuleError) as ctx:
                load_program(str(root / "app.sg"))
        self.assertIn("module 'lib' has no declaration named 'frob'",
                      ctx.exception.message)

    def test_module_cannot_lean_on_entry_names(self):
        # lib calls a function it neither declares nor imports; the app
        # declaring one with that name must NOT satisfy it — a module's
        # meaning cannot depend on its importer.
        lib = "pub fn f(n: Int) -> Int {\n    return helper(n);\n}\n"
        app = ("use lib { f }\n\n"
               "fn helper(n: Int) -> Int {\n    return n;\n}\n\n"
               "fn main() -> Unit {\n    f(1);\n    return;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, lib=lib, app=app)
            with self.assertRaises(ModuleError) as ctx:
                load_program(str(root / "app.sg"))
        self.assertIn("module 'lib' references unknown function 'helper'",
                      ctx.exception.message)
        self.assertEqual(ctx.exception.path, str(root / "lib.sg"))

    def test_entry_unknown_names_stay_with_the_checker(self):
        lib = "pub fn f() -> Int {\n    return 1;\n}\n"
        app = ("use lib { f }\n\n"
               "fn main() -> Unit {\n    nope();\n    return;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, lib=lib, app=app)
            program = load_program(str(root / "app.sg"))
        with self.assertRaises(CheckError) as ctx:
            check(program)
        self.assertIn("unknown function 'nope'", ctx.exception.message)


class TestImportRules(unittest.TestCase):
    def load_error(self, app: str, **modules: str) -> ModuleError:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, app=app, **modules)
            with self.assertRaises(ModuleError) as ctx:
                load_program(str(root / "app.sg"))
        return ctx.exception

    LIB = ("pub fn area(n: Int) -> Int {\n    return n;\n}\n\n"
           "fn helper(n: Int) -> Int {\n    return n;\n}\n")

    def test_private_declaration_is_not_importable(self):
        exc = self.load_error(
            "use lib { helper }\n\nfn main() -> Unit {\n    return;\n}\n",
            lib=self.LIB)
        self.assertIn("module 'lib' does not export 'helper' "
                      "(mark it pub to allow this)", exc.message)

    def test_import_collides_with_local_declaration(self):
        exc = self.load_error(
            "use lib { area }\n\n"
            "fn area(n: Int) -> Int {\n    return n;\n}\n\n"
            "fn main() -> Unit {\n    return;\n}\n",
            lib=self.LIB)
        self.assertIn("imported name 'area' collides with a declaration",
                      exc.message)

    def test_import_collides_with_import(self):
        other = "pub fn area(n: Int) -> Int {\n    return n + n;\n}\n"
        exc = self.load_error(
            "use lib { area }\nuse other { area }\n\n"
            "fn main() -> Unit {\n    return;\n}\n",
            lib=self.LIB, other=other)
        self.assertIn("imported name 'area' collides with another import "
                      "('lib.area')", exc.message)

    def test_import_collides_with_builtin(self):
        exc = self.load_error(
            "use lib { area as len }\n\nfn main() -> Unit {\n    return;\n}\n",
            lib=self.LIB)
        self.assertIn("collides with the builtin 'len'", exc.message)

    def test_alias_canon_record_must_be_uppercase(self):
        exc = self.load_error(
            "use lib { Point as spot }\n\nfn main() -> Unit {\n    return;\n}\n",
            lib=LIB_POINT)
        self.assertIn("alias 'spot' for record 'Point' must start with an "
                      "uppercase letter", exc.message)

    def test_alias_canon_fn_must_be_lowercase(self):
        exc = self.load_error(
            "use lib { make as Build }\n\nfn main() -> Unit {\n    return;\n}\n",
            lib=LIB_POINT)
        self.assertIn("alias 'Build' for function 'make' must start with a "
                      "lowercase letter", exc.message)

    def test_variants_cannot_be_imported_individually(self):
        colors = "pub enum Color {\n    Red,\n    Green(Int),\n}\n"
        exc = self.load_error(
            "use colors { Red }\n\nfn main() -> Unit {\n    return;\n}\n",
            colors=colors)
        self.assertIn("'Red' is a variant of enum 'Color'", exc.message)
        self.assertIn("cannot be imported individually", exc.message)

    def test_imported_enum_variants_collide_across_imports(self):
        m1 = "pub enum ColorA {\n    Dup,\n}\n"
        m2 = "pub enum ColorB {\n    Dup,\n}\n"
        exc = self.load_error(
            "use m1 { ColorA }\nuse m2 { ColorB }\n\n"
            "fn main() -> Unit {\n    return;\n}\n",
            m1=m1, m2=m2)
        self.assertIn("variant 'Dup' of imported enum 'ColorB' collides with "
                      "another import ('m1.Dup')", exc.message)

    def test_imported_variant_collides_with_local_variant(self):
        m1 = "pub enum ColorA {\n    Red,\n}\n"
        exc = self.load_error(
            "use m1 { ColorA }\n\n"
            "enum Local {\n    Red,\n}\n\n"
            "fn main() -> Unit {\n    return;\n}\n",
            m1=m1)
        self.assertIn("variant 'Red' of imported enum 'ColorA' collides with "
                      "a declaration", exc.message)


class TestParserSyntax(unittest.TestCase):
    def test_use_after_declaration_is_a_parse_error(self):
        with self.assertRaises(ParseError) as ctx:
            parse("fn f() -> Int { return 1; }\nuse lib { x }\n")
        self.assertIn("top of the file", ctx.exception.message)

    def test_pub_parses_on_all_declaration_kinds(self):
        program = parse(
            "pub record P { x: Int }\n"
            "pub enum E { V }\n"
            "pub fn f() -> Int { return 1; }\n"
            "fn g() -> Int { return 2; }\n")
        self.assertTrue(program.records[0].public)
        self.assertTrue(program.enums[0].public)
        self.assertTrue(program.functions[0].public)
        self.assertFalse(program.functions[1].public)

    def test_uses_normalized_by_parser(self):
        program = parse(
            "use zoo { lion }\n"
            "use alpha { gamma as g, beta, Alpha }\n"
            "fn f() -> Int { return 1; }\n")
        self.assertEqual([u.module for u in program.uses], ["alpha", "zoo"])
        self.assertEqual(program.uses[0].items,
                         [("Alpha", None), ("beta", None), ("gamma", "g")])

    def test_empty_use_rejected(self):
        with self.assertRaises(ParseError):
            parse("use lib {}\nfn f() -> Int { return 1; }\n")

    def test_module_name_must_be_lowercase(self):
        with self.assertRaises(ParseError) as ctx:
            parse("use Lib { f }\nfn g() -> Int { return 1; }\n")
        self.assertIn("lowercase", ctx.exception.message)


class TestEnumsAcrossModules(unittest.TestCase):
    COLORS = ("pub enum Color {\n    Red,\n    Green(Int),\n}\n\n"
              "pub fn brightness(c: Color) -> Int {\n"
              "    match c {\n"
              "        Red => {\n            return 100;\n        }\n"
              "        Green(level) => {\n            return level;\n        }\n"
              "    }\n}\n")
    APP = ("use colors { Color, brightness }\n\n"
           "fn main(console: Console) -> Unit ! {io.write} {\n"
           "    let c: Color = Green(7);\n"
           "    var tally: Int = 0;\n"
           "    match c {\n"
           "        Red => {\n            tally = 1;\n        }\n"
           "        Green(level) => {\n            tally = level;\n        }\n"
           "    }\n"
           "    print(console, str(brightness(c)) + \" \" + str(tally)\n"
           "        + \" \" + str(brightness(Red)));\n"
           "}\n")

    def test_enum_import_brings_variants_into_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, colors=self.COLORS, app=self.APP)
            program = load_program(str(root / "app.sg"))
            enum = program.enums[0]
            self.assertEqual(enum.name, "colors.Color")
            self.assertEqual([v for v, _ in enum.variants],
                             ["colors.Red", "colors.Green"])
            self.assertEqual(run_interp(root / "app.sg"), "7 7 100\n")


class TestGenericsAcrossModules(unittest.TestCase):
    UTIL = ("pub fn first[T](xs: List[T]) -> T\n"
            "    requires len(xs) > 0\n"
            "{\n    return xs[0];\n}\n")
    APP = ("use util { first }\n\n"
           "fn main(console: Console) -> Unit ! {io.write} {\n"
           "    print(console, str(first([7, 8])) + \" \" + first([\"a\", \"b\"]));\n"
           "}\n")

    def test_monomorphization_and_sym(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, util=self.UTIL, app=self.APP)
            self.assertEqual(run_interp(root / "app.sg"), "7 a\n")
            program = load_program(str(root / "app.sg"))
            check(program)
            rust = emit_rust(program)
        # Each instantiation is its own Rust function; dots never reach a
        # Rust identifier, but contract blame TEXT keeps the dotted name.
        self.assertIn("fn s_util__first__Int(", rust)
        self.assertIn("fn s_util__first__Text(", rust)
        self.assertNotIn("s_util.first", rust)
        self.assertIn("requires clause of 'util.first'", rust)


class TestContractsAcrossModules(unittest.TestCase):
    MATHLIB = ("pub fn safe_div(a: Int, b: Int) -> Int\n"
               "    requires b != 0\n"
               "{\n    return a / b;\n}\n")

    def app(self, divisor: int) -> str:
        return ("use mathlib { safe_div }\n\n"
                "fn main(console: Console) -> Unit ! {io.write} {\n"
                f"    print(console, str(safe_div(10, {divisor})));\n"
                "}\n")

    def test_violation_blames_the_qualified_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, mathlib=self.MATHLIB, app=self.app(0))
            with self.assertRaises(ContractViolation) as ctx:
                run_interp(root / "app.sg")
        self.assertIn("requires clause of 'mathlib.safe_div' failed",
                      ctx.exception.message)
        self.assertIn("CALLER", ctx.exception.message)

    @unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
    def test_imported_requires_proven_at_entry_call_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, mathlib=self.MATHLIB, app=self.app(2))
            program = load_program(str(root / "app.sg"))
        check(program)
        report = verify(program)
        requires = next(f for f in report.findings
                        if f.fn == "mathlib.safe_div" and f.kind == "requires")
        self.assertTrue(requires.proven,
                        "safe_div(10, 2) must discharge b != 0")
        division = next(f for f in report.findings if f.kind == "division")
        self.assertTrue(division.proven)


class TestSecurityDemo(unittest.TestCase):
    """The milestone's point, on the shipped examples: capability threading
    works across module boundaries, and a module function that does not
    declare a capability cannot acquire one."""

    EXPECTED = "circle area = 75\narea = 12\n"

    def test_cross_module_capability_threading_interpreted(self):
        self.assertEqual(run_interp(EXAMPLES / "modules_app.sg"),
                         self.EXPECTED)

    @unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
    def test_cross_module_capability_threading_native(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = build(str(EXAMPLES / "modules_app.sg"),
                        output=str(Path(tmp) / "app.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), self.EXPECTED)

    def test_module_fn_without_console_cannot_print(self):
        # The module declares the effect and tries to print anyway: with no
        # Console parameter there is nothing to hand to print, and a Console
        # cannot be conjured — the importing app's own capabilities are
        # irrelevant.
        evil = ("pub fn leak(n: Int) -> Int ! {io.write} {\n"
                "    print(n, \"exfiltrated\");\n"
                "    return n;\n}\n")
        app = ("use evil { leak }\n\n"
               "fn main(console: Console) -> Unit ! {io.write} {\n"
               "    print(console, str(leak(1)));\n"
               "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, evil=evil, app=app)
            program = load_program(str(root / "app.sg"))
            with self.assertRaises(CheckError) as ctx:
                check(program)
        self.assertIn("capabilities cannot be created, only received",
                      ctx.exception.message)

    def test_module_fn_with_console_still_needs_the_effect(self):
        evil = ("pub fn quiet(c: Console) -> Unit {\n"
                "    print(c, \"sneaky\");\n}\n")
        app = ("use evil { quiet }\n\n"
               "fn main(console: Console) -> Unit ! {io.write} {\n"
               "    quiet(console);\n"
               "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, evil=evil, app=app)
            program = load_program(str(root / "app.sg"))
            with self.assertRaises(CheckError) as ctx:
                check(program)
        # The diagnostic names the offending module's function, qualified.
        self.assertIn("'evil.quiet' calls 'print'", ctx.exception.message)

    def test_private_helper_unreachable_from_app(self):
        geometry = (EXAMPLES / "geometry.sg").read_text(encoding="utf-8")
        app = ("use geometry { square }\n\n"
               "fn main() -> Unit {\n    return;\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, geometry=geometry, app=app)
            with self.assertRaises(ModuleError) as ctx:
                load_program(str(root / "app.sg"))
        self.assertIn("module 'geometry' does not export 'square' "
                      "(mark it pub to allow this)", ctx.exception.message)


class TestCanonWithModules(unittest.TestCase):
    UNSORTED = ("use zoo { lion }\n"
                "use alpha { gamma as g, beta, Alpha }\n"
                "pub fn f() -> Int {\n"
                "    return 1;\n"
                "}\n")
    CANONICAL = ("use alpha { Alpha, beta, gamma as g }\n"
                 "use zoo { lion }\n"
                 "\n"
                 "pub fn f() -> Int {\n"
                 "    return 1;\n"
                 "}\n")

    def test_use_header_sorted_with_one_blank_line(self):
        self.assertEqual(format_source(self.UNSORTED), self.CANONICAL)

    def test_fmt_idempotent_and_round_trips(self):
        formatted = format_source(self.UNSORTED)
        self.assertEqual(formatted, format_source(formatted))
        self.assertTrue(ast_equal(parse(self.UNSORTED), parse(formatted)))

    def test_pub_rendering_preserved(self):
        source = ("pub record P {\n    x: Int,\n}\n\n"
                  "pub enum E {\n    V,\n}\n\n"
                  "pub fn f() -> Int {\n    return 1;\n}\n")
        self.assertEqual(format_source(source), source)

    def test_shipped_module_examples_are_canonical(self):
        for name in ("geometry.sg", "modules_app.sg"):
            with self.subTest(example=name):
                text = (EXAMPLES / name).read_text(encoding="utf-8")
                self.assertEqual(format_source(text), text)

    def test_program_json_has_public_and_uses(self):
        program = parse(self.UNSORTED)
        doc = program_json(program)
        self.assertEqual(doc["uses"], [
            {"module": "alpha", "items": [
                {"name": "Alpha", "alias": None},
                {"name": "beta", "alias": None},
                {"name": "gamma", "alias": "g"},
            ]},
            {"module": "zoo", "items": [{"name": "lion", "alias": None}]},
        ])
        self.assertTrue(doc["functions"][0]["public"])
        plain = program_json(parse("fn f() -> Int { return 1; }"))
        self.assertFalse(plain["functions"][0]["public"])
        self.assertEqual(plain["uses"], [])

    def test_sdiff_public_flip_is_a_signature_change(self):
        lines = sdiff(parse("fn f() -> Int { return 1; }"),
                      parse("pub fn f() -> Int { return 1; }"))
        self.assertEqual(lines, ["signature   fn f"])
        lines = sdiff(parse("record P { x: Int }"),
                      parse("pub record P { x: Int }"))
        self.assertEqual(lines, ["signature   record P"])


class TestServerPathMode(unittest.TestCase):
    def test_path_mode_check_and_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, lib=LIB_POINT, app=APP_ALIAS)
            entry = str(root / "app.sg")
            self.assertEqual(request(method="check", path=entry),
                             {"ok": True, "diagnostics": []})
            response = request(method="signatures", path=entry)
        self.assertTrue(response["ok"])
        names = [f["name"] for f in response["functions"]]
        self.assertEqual(names, ["lib.make", "main"])
        self.assertEqual(response["records"][0]["name"], "lib.Point")

    def test_path_mode_module_error_diagnostics(self):
        app = "use nope { f }\n\nfn main() -> Unit {\n    return;\n}\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(tmp, app=app)
            response = request(method="check", path=str(root / "app.sg"))
            self.assertFalse(response["ok"])
            diag = response["diagnostics"][0]
            self.assertEqual(diag["label"], "module error")
            self.assertIn("module 'nope' not found", diag["message"])
            self.assertEqual(diag["path"], str(root / "app.sg"))

    def test_source_with_use_is_rejected(self):
        response = request(method="check", source=APP_ALIAS)
        self.assertFalse(response["ok"])
        self.assertIn("imports require the path-based form", response["error"])

    def test_source_and_path_are_mutually_exclusive(self):
        response = request(method="check", source="fn f() -> Int { return 1; }",
                           path="x.sg")
        self.assertFalse(response["ok"])
        self.assertIn("not both", response["error"])


if __name__ == "__main__":
    unittest.main()
