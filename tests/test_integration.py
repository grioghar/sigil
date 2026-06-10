"""Cross-feature integration: generics x loop invariants x verification x
native codegen. Each feature was built and tested separately; this file
covers their composition."""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.checker import check
from sigil.interp import Interpreter
from sigil.parser import parse
from sigil.verify import HAVE_Z3, verify

HAVE_RUSTC = shutil.which("rustc") is not None

# A generic function whose body is a contract-carrying loop, instantiated at
# two types, with a record in the mix.
PROGRAM = """
record Pair {
    label: Text,
    count: Int,
}

fn count_items[T](xs: List[T]) -> Int
    ensures result >= 0
{
    var i: Int = 0;
    while i < len(xs)
        invariant i >= 0
    {
        i = i + 1;
    }
    return i;
}

fn main(console: Console) -> Unit ! {io.write} {
    let ints: List[Int] = [10, 20, 30];
    let texts: List[Text] = ["a", "b"];
    let p: Pair = Pair { label: "total", count: count_items(ints) + count_items(texts) };
    print(console, p.label + " = " + str(p.count));
}
"""


def interpret(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


class TestGenericInvariantComposition(unittest.TestCase):
    def test_interpreter(self):
        self.assertEqual(interpret(PROGRAM), "total = 5\n")

    @unittest.skipUnless(HAVE_Z3, "z3-solver not installed")
    def test_loop_ensures_proven_inside_generic_fn(self):
        program = parse(PROGRAM)
        check(program)
        verify(program)
        fn = next(f for f in program.functions if f.name == "count_items")
        ensures = next(c for c in fn.contracts if c.kind == "ensures")
        self.assertTrue(ensures.proven,
                        "invariant i >= 0 must carry the ensures through the loop")

    @unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
    def test_native_matches_interpreter(self):
        expected = interpret(PROGRAM)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "compose.sg"
            src.write_text(PROGRAM, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "compose.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)


if __name__ == "__main__":
    unittest.main()
