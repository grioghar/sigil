"""Tests for the text primitives slice/ord/chr (added during dogfooding —
the minimal set that makes text processing possible in Sigil)."""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.checker import check
from sigil.errors import RuntimeFault
from sigil.interp import Interpreter
from sigil.parser import parse

HAVE_RUSTC = shutil.which("rustc") is not None

PROGRAM = """
fn main(console: Console) -> Unit ! {io.write} {
    let s: Text = "héllo";
    print(console, slice(s, 1, 4));
    print(console, slice(s, 0, 0));
    print(console, str(ord("A")) + " " + str(ord("é")));
    print(console, chr(66) + chr(233));
    print(console, str(len(slice(s, 0, len(s)))));
}
"""

EXPECTED = "éll\n\n65 233\nBé\n5\n"


def run(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


class TestTextBuiltins(unittest.TestCase):
    def test_interpreter(self):
        self.assertEqual(run(PROGRAM), EXPECTED)

    def test_slice_out_of_range_faults(self):
        for call in ("slice(\"abc\", 0, 4)", "slice(\"abc\", 2, 1)",
                     "slice(\"abc\", 0 - 1, 2)"):
            src = ("fn main(console: Console) -> Unit ! {io.write} { "
                   f"print(console, {call}); }}")
            with self.assertRaises(RuntimeFault, msg=call):
                run(src)

    def test_ord_needs_single_char(self):
        for text in ('""', '"ab"'):
            src = ("fn main(console: Console) -> Unit ! {io.write} { "
                   f"print(console, str(ord({text}))); }}")
            with self.assertRaises(RuntimeFault, msg=text):
                run(src)

    def test_chr_rejects_invalid_codes(self):
        for n in ("0 - 1", "1114112", "55296"):  # negative, > max, surrogate
            src = ("fn main(console: Console) -> Unit ! {io.write} { "
                   f"print(console, chr({n})); }}")
            with self.assertRaises(RuntimeFault, msg=n):
                run(src)

    def test_pure_in_contracts(self):
        # slice/ord/chr are pure, so contracts may use them.
        check(parse("""
            fn initial(s: Text) -> Text
                requires len(s) > 0
                ensures len(result) == 1
            {
                return slice(s, 0, 1);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, initial("sigil"));
            }
        """))

    @unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
    def test_native_matches_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "textops.sg"
            src.write_text(PROGRAM, encoding="utf-8")
            exe = build(str(src), output=str(Path(tmp) / "textops.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], capture_output=True,
                                    encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), EXPECTED)


if __name__ == "__main__":
    unittest.main()
