"""End-to-end tests of programs/json — a JSON parser + printer in Sigil.

The library tests drive the interpreter directly (parse/render are pure, so
no capabilities are needed); the demo tests run main.sg interpreted and
natively and require byte-identical output.
"""

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.checker import check
from sigil.interp import Interpreter
from sigil.modules import load_program

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "programs" / "json" / "json.sg"
APP = REPO / "programs" / "json" / "main.sg"

HAVE_RUSTC = shutil.which("rustc") is not None

# (document, expected compact rendering)
ROUND_TRIPS = [
    ("null", "null"),
    ("true", "true"),
    ("false", "false"),
    ("0", "0"),
    ("-0", "0"),
    ("42", "42"),
    ("-137", "-137"),
    ('""', '""'),
    ('"hello"', '"hello"'),
    # every supported escape: \" \\ \n \t \/ (the solidus renders bare)
    ('"a\\"b\\\\c\\nd\\te\\/f"', '"a\\"b\\\\c\\nd\\te/f"'),
    ("[]", "[]"),
    ("[ ]", "[]"),
    ("{}", "{}"),
    ("{ }", "{}"),
    ("[1, 2, 3]", "[1,2,3]"),
    ("[[],[[]],[[],[]]]", "[[],[[]],[[],[]]]"),
    ('{"a": 1, "b": [true, null]}', '{"a":1,"b":[true,null]}'),
    # duplicate keys are allowed and kept in order
    ('{"a": 1, "a": 2}', '{"a":1,"a":2}'),
    # whitespace variants: space, tab, newline, carriage return
    (' \t\r\n [ 1 ,\t-2 ,\n{"x" : "y"} ] \r\n ', '[1,-2,{"x":"y"}]'),
]

# (document, substring of the failure message, failure position)
FAILURES = [
    ("", "unexpected end of input", 0),
    ("   ", "unexpected end of input", 3),
    ("nul", "expected 'null'", 0),
    ("falsy", "expected 'false'", 0),
    ("@", "expected a JSON value", 0),
    ('{"a":1} x', "unexpected trailing characters", 8),
    ('"abc', "unterminated string", 0),
    ('"ab\\', "unterminated escape", 3),
    ('"a\\qb"', "unsupported escape '\\q'", 2),
    ('"a\\u0041"', "\\u escapes are not supported", 2),
    ("3.14", "integers only", 1),
    ("2e8", "integers only", 1),
    ("-", "expected a digit", 1),
    ("-x", "expected a digit", 1),
    ("[1 2]", "expected ',' or ']' in array", 3),
    ("[1,]", "trailing comma in array", 3),
    ("[1,", "unexpected end of input", 3),
    ("[1", "unterminated array", 2),
    ("[", "unexpected end of input", 1),
    ('{"a" 1}', "expected ':' after object key", 5),
    ('{"a":1,}', "trailing comma in object", 7),
    ('{"a":1', "unterminated object", 6),
    ("{1: 2}", "start an object key", 1),
    ("{", "start an object key", 1),
]

EXPECTED_DEMO = (
    'ok   {"name":"sigil","verified":true,"stars":128,'
    '"tags":["json","dogfood"]}\n'
    'ok   [1,-20,[true,null,{}],"line\\nbreak \\"quoted\\""]\n'
    "fail trailing comma in array at 6\n"
    "fail numbers are integers only (no fraction or exponent) at 8\n"
    "fail \\u escapes are not supported at 7\n"
    "fail unexpected trailing characters at 9\n"
)


class TestJsonLibrary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        program = load_program(str(LIB))
        sigs = check(program)
        cls.interp = Interpreter(program, sigs, stdin=io.StringIO(""),
                                 stdout=io.StringIO())

    def parse(self, text):
        return self.interp.call("parse", [text], 0, 0)

    def render(self, value):
        return self.interp.call("render", [value], 0, 0)

    def parse_ok(self, text):
        tag, payload = self.parse(text)
        self.assertEqual(tag, "Done", f"{text!r} failed: {payload}")
        value, end = payload
        self.assertEqual(end, len(text), text)
        return value

    def test_parse_then_render(self):
        for doc, expected in ROUND_TRIPS:
            self.assertEqual(self.render(self.parse_ok(doc)), expected, doc)

    def test_round_trip_is_stable(self):
        # render . parse is idempotent: re-parsing the compact output and
        # rendering again reproduces it byte for byte.
        for doc, _ in ROUND_TRIPS:
            once = self.render(self.parse_ok(doc))
            self.assertEqual(self.render(self.parse_ok(once)), once, doc)

    def test_failures_with_positions(self):
        for doc, expected_msg, expected_pos in FAILURES:
            tag, payload = self.parse(doc)
            self.assertEqual(tag, "Fail",
                             f"{doc!r} unexpectedly parsed: {payload}")
            message, position = payload
            self.assertIn(expected_msg, message, doc)
            self.assertEqual(position, expected_pos, doc)

    def test_deep_nesting(self):
        depth = 50
        doc = "[" * depth + "7" + "]" * depth
        old = sys.getrecursionlimit()
        sys.setrecursionlimit(40000)
        try:
            self.assertEqual(self.render(self.parse_ok(doc)), doc)
        finally:
            sys.setrecursionlimit(old)

    def test_verification_obligations_pinned(self):
        # 37 of 38 contract clauses prove; the single runtime check left is
        # skip_ws's requires, because indices recovered from Step payloads
        # carry no facts (the verifier cannot see inside enum payloads).
        # If the language ever fixes that, this test should start failing.
        from sigil.verify import HAVE_Z3, verify
        if not HAVE_Z3:
            self.skipTest("z3-solver not installed")
        program = load_program(str(LIB))
        check(program)
        report = verify(program)
        unproven = sorted((f.fn, f.kind) for f in report.findings
                          if not f.proven)
        self.assertEqual(unproven, [("skip_ws", "requires")])


class TestJsonDemo(unittest.TestCase):
    def run_interpreted(self):
        program = load_program(str(APP))
        sigs = check(program)
        out = io.StringIO()
        Interpreter(program, sigs, stdin=io.StringIO(""),
                    stdout=out).run_main()
        return out.getvalue()

    def test_interpreted_demo(self):
        self.assertEqual(self.run_interpreted(), EXPECTED_DEMO)

    @unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
    def test_native_matches_interpreter(self):
        # The one rustc differential test: byte-identical stdout.
        interpreted = self.run_interpreted()
        with tempfile.TemporaryDirectory() as tmp:
            exe = build(str(APP), output=str(Path(tmp) / "json_demo.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True,
                                    encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), interpreted)
        self.assertEqual(interpreted, EXPECTED_DEMO)


if __name__ == "__main__":
    unittest.main()
