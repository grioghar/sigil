"""Tests for the compiler-as-a-service query API (roadmap 0.5).

The contract under test: handle_request NEVER raises and always returns a
JSON-serializable dict, so a generating LLM can lean on it mid-stream. The
generation-loop test at the bottom documents the intended workflow.
"""

import json
import os
import subprocess
import sys
import unittest

from sigil.server import handle_request
from sigil.verify import HAVE_Z3

WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HELLO = (
    'fn main(console: Console) -> Unit ! {io.write} {\n'
    '    print(console, "hello");\n'
    '}\n'
)

# 'print' performs io.write but main declares nothing: check error at 2:5.
MISSING_EFFECT = (
    'fn main(console: Console) -> Unit {\n'
    '    print(console, "hello");\n'
    '}\n'
)


def request(**fields) -> dict:
    response = handle_request(fields)
    json.dumps(response)  # every response must be JSON-serializable
    return response


class TestCheck(unittest.TestCase):
    def test_clean_program(self):
        response = request(method="check", source=HELLO)
        self.assertEqual(response, {"ok": True, "diagnostics": []})

    def test_check_error_with_position(self):
        response = request(method="check", source=MISSING_EFFECT)
        self.assertFalse(response["ok"])
        self.assertEqual(len(response["diagnostics"]), 1)
        diag = response["diagnostics"][0]
        self.assertEqual(diag["line"], 2)
        self.assertEqual(diag["col"], 5)
        self.assertEqual(diag["label"], "check error")
        self.assertIn("io.write", diag["message"])

    def test_parse_error(self):
        response = request(method="check", source="fn main(")
        self.assertFalse(response["ok"])
        self.assertEqual(response["diagnostics"][0]["label"], "parse error")

    def test_lex_error(self):
        response = request(method="check", source="fn main() -> Unit { @ }")
        self.assertFalse(response["ok"])
        self.assertEqual(response["diagnostics"][0]["label"], "lex error")


class TestSignatures(unittest.TestCase):
    SOURCE = """
        record Point {
            x: Int,
            y: Int,
        }
        fn first[T](xs: List[T]) -> T
            requires len(xs) > 0
        {
            return xs[0];
        }
        fn main(console: Console) -> Unit ! {io.write} {
            print(console, str(first([1, 2])));
        }
    """

    def test_functions_records_builtins(self):
        response = request(method="signatures", source=self.SOURCE)
        self.assertTrue(response["ok"])

        first = next(f for f in response["functions"] if f["name"] == "first")
        self.assertEqual(first["type_params"], ["T"])
        self.assertEqual(first["params"], [{"name": "xs", "type": "List[T]"}])
        self.assertEqual(first["ret"], "T")
        self.assertEqual(first["effects"], [])
        self.assertEqual(first["contracts"],
                         [{"kind": "requires", "source": "len(xs) > 0"}])

        main = next(f for f in response["functions"] if f["name"] == "main")
        self.assertEqual(main["effects"], ["io.write"])
        self.assertEqual(main["params"], [{"name": "console", "type": "Console"}])

        self.assertEqual(response["records"],
                         [{"name": "Point",
                           "type_params": [],
                           "fields": [{"name": "x", "type": "Int"},
                                      {"name": "y", "type": "Int"}]}])

        builtin_print = next(b for b in response["builtins"]
                             if b["name"] == "print")
        self.assertEqual(builtin_print["effects"], ["io.write"])
        self.assertEqual(builtin_print["ret"], "Unit")
        self.assertNotIn("contracts", builtin_print)

    def test_falls_back_to_check_failure_shape(self):
        response = request(method="signatures", source=MISSING_EFFECT)
        self.assertFalse(response["ok"])
        self.assertEqual(response["diagnostics"][0]["label"], "check error")


class TestEffects(unittest.TestCase):
    CHAIN = """
        fn double(n: Int) -> Int {
            return helper(n) * 2;
        }
        fn helper(n: Int) -> Int {
            return n + 1;
        }
        fn logger(c: Console, msg: Text) -> Unit ! {io.write} {
            print(c, msg);
        }
        fn main(console: Console) -> Unit ! {io.write} {
            logger(console, str(double(20)));
        }
    """

    def test_pure_chain_is_transitively_pure(self):
        response = request(method="effects", source=self.CHAIN, fn="double")
        self.assertEqual(response,
                         {"ok": True, "fn": "double", "declared": [],
                          "capabilities": [], "transitive": []})

    def test_transitive_through_call_chain(self):
        # main -> logger -> print(io.write): the builtin effect surfaces in
        # transitive even though main declares io.write itself.
        response = request(method="effects", source=self.CHAIN, fn="main")
        self.assertTrue(response["ok"])
        self.assertEqual(response["declared"], ["io.write"])
        self.assertEqual(response["transitive"], ["io.write"])
        self.assertEqual(response["capabilities"], ["Console"])

    def test_logger_capability_param(self):
        response = request(method="effects", source=self.CHAIN, fn="logger")
        self.assertEqual(response["capabilities"], ["Console"])
        self.assertEqual(response["declared"], ["io.write"])
        self.assertEqual(response["transitive"], ["io.write"])

    def test_unknown_fn(self):
        response = request(method="effects", source=self.CHAIN, fn="nope")
        self.assertFalse(response["ok"])
        self.assertIn("nope", response["error"])

    def test_check_error_falls_back_to_diagnostics(self):
        response = request(method="effects", source=MISSING_EFFECT, fn="main")
        self.assertFalse(response["ok"])
        self.assertEqual(response["diagnostics"][0]["label"], "check error")


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestVerify(unittest.TestCase):
    MIXED = """
        fn f(a: Int, b: Int) -> Int
            requires b != 0
            ensures result >= 0
        {
            return a / b;
        }
        fn main(console: Console) -> Unit ! {io.write} {
            print(console, str(f(10, 2)));
        }
    """

    def test_findings_and_summary(self):
        response = request(method="verify", source=self.MIXED)
        self.assertTrue(response["ok"])
        by_kind = {f["kind"]: f for f in response["findings"]
                   if f["fn"] == "f"}
        self.assertTrue(by_kind["requires"]["proven"])   # f(10, 2) discharges it
        self.assertTrue(by_kind["division"]["proven"])   # b != 0 is assumed
        self.assertFalse(by_kind["ensures"]["proven"])   # a/b is unconstrained
        self.assertEqual(response["summary"],
                         {"contracts_proven": 1, "contracts_total": 2,
                          "divisions_proven": 1, "divisions_total": 1})
        for finding in response["findings"]:
            self.assertEqual(sorted(finding),
                             ["fn", "kind", "line", "proven", "source"])

    def test_check_error_falls_back_to_diagnostics(self):
        response = request(method="verify", source=MISSING_EFFECT)
        self.assertFalse(response["ok"])
        self.assertEqual(response["diagnostics"][0]["label"], "check error")


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestObligations(unittest.TestCase):
    def test_only_unproven_findings_listed(self):
        response = request(method="obligations", source=TestVerify.MIXED)
        self.assertTrue(response["ok"])
        self.assertEqual(len(response["obligations"]), 1)
        obligation = response["obligations"][0]
        self.assertEqual(obligation["fn"], "f")
        self.assertEqual(obligation["kind"], "ensures")
        self.assertEqual(obligation["source"], "result >= 0")
        self.assertFalse(obligation["proven"])

    def test_fully_proven_program_has_no_obligations(self):
        response = request(method="obligations", source="""
            fn abs(n: Int) -> Int
                ensures result >= 0
            {
                if n < 0 {
                    return 0 - n;
                }
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(abs(0 - 5)));
            }
        """)
        self.assertEqual(response, {"ok": True, "obligations": []})

    def test_check_error_falls_back_to_diagnostics(self):
        response = request(method="obligations", source=MISSING_EFFECT)
        self.assertFalse(response["ok"])
        self.assertEqual(response["diagnostics"][0]["label"], "check error")


class TestProtocolRobustness(unittest.TestCase):
    def test_methods_is_self_describing(self):
        response = request(method="methods")
        self.assertTrue(response["ok"])
        self.assertEqual(response["methods"],
                         sorted(["check", "signatures", "effects", "verify",
                                 "obligations", "methods"]))
        for name in response["methods"]:
            self.assertIn(name, response["descriptions"])

    def test_unknown_method(self):
        response = request(method="transmogrify", source=HELLO)
        self.assertFalse(response["ok"])
        self.assertIn("transmogrify", response["error"])

    def test_missing_method(self):
        response = request(source=HELLO)
        self.assertFalse(response["ok"])
        self.assertIn("method", response["error"])

    def test_missing_source(self):
        response = request(method="check")
        self.assertFalse(response["ok"])
        self.assertIn("source", response["error"])

    def test_missing_fn_for_effects(self):
        response = request(method="effects", source=HELLO)
        self.assertFalse(response["ok"])
        self.assertIn("fn", response["error"])

    def test_malformed_request_objects(self):
        for bad in (None, 42, "check", [1, 2], {"method": 7}):
            response = handle_request(bad)
            json.dumps(response)
            self.assertFalse(response["ok"], bad)
            self.assertIn("error", response)


class TestServeSubprocess(unittest.TestCase):
    def test_serve_round_trip_and_clean_eof(self):
        requests = (json.dumps({"method": "methods"}) + "\n"
                    + json.dumps({"method": "check", "source": HELLO}) + "\n")
        proc = subprocess.run(
            [sys.executable, "-m", "sigil", "serve"],
            input=requests, capture_output=True, text=True,
            cwd=WORKTREE_ROOT, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(line) for line in lines)
        self.assertTrue(first["ok"])
        self.assertIn("obligations", first["methods"])
        self.assertEqual(second, {"ok": True, "diagnostics": []})


@unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
class TestGenerationLoop(unittest.TestCase):
    """The intended workflow: an AI author iterates against the compiler,
    draft by draft, until check passes AND the obligation list is empty."""

    def test_draft_until_proven(self):
        # Draft 1: main forgot to declare io.write. The diagnostic names the
        # missing effect, so the author knows exactly what to add.
        v1 = (
            'fn half(n: Int) -> Int {\n'
            '    return 100 / n;\n'
            '}\n'
            'fn main(console: Console) -> Unit {\n'
            '    print(console, str(half(4)));\n'
            '}\n'
        )
        response = request(method="check", source=v1)
        self.assertFalse(response["ok"])
        self.assertIn("io.write", response["diagnostics"][0]["message"])

        # Draft 2: effect fixed; check is clean, but obligations reports the
        # unproven division (n could be zero for some caller).
        v2 = v1.replace("-> Unit {", "-> Unit ! {io.write} {")
        self.assertEqual(request(method="check", source=v2),
                         {"ok": True, "diagnostics": []})
        response = request(method="obligations", source=v2)
        self.assertTrue(response["ok"])
        kinds = [o["kind"] for o in response["obligations"]]
        self.assertEqual(kinds, ["division"])

        # Draft 3: a requires clause makes the division provable, and the
        # call site half(4) discharges the new precondition. Nothing left.
        v3 = v2.replace("fn half(n: Int) -> Int {",
                        "fn half(n: Int) -> Int\n    requires n != 0\n{")
        response = request(method="obligations", source=v3)
        self.assertEqual(response, {"ok": True, "obligations": []})


if __name__ == "__main__":
    unittest.main()
