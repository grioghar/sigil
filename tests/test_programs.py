"""End-to-end test of programs/tasks — the first real Sigil program.

Runs the same scripted session interpreted and natively; both must produce
identical output and an identical persisted data file.
"""

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sigil.build import build
from sigil.checker import check
from sigil.interp import Interpreter
from sigil.modules import load_program

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "programs" / "tasks" / "main.sg"

HAVE_RUSTC = shutil.which("rustc") is not None

SESSION = (
    "add write the parser\n"
    "add prove the contracts\n"
    "list\n"
    "done 1\n"
    "nonsense here\n"
    "done 99\n"
    "done xyz\n"
    "list\n"
    "quit\n"
)

EXPECTED_OUT = (
    "tasks — add <title> | done <n> | list | quit\n"
    "1 [ ] write the parser\n"
    "2 [ ] prove the contracts\n"
    "unknown command: nonsense\n"
    "no task 99\n"
    "done needs a number\n"
    "1 [x] write the parser\n"
    "2 [ ] prove the contracts\n"
    "bye\n"
)

EXPECTED_FILE = "1|write the parser\n0|prove the contracts\n"


class TestTasksProgram(unittest.TestCase):
    def run_interpreted(self, cwd: str) -> str:
        program = load_program(str(APP))
        sigs = check(program)
        out = io.StringIO()
        old_cwd = os.getcwd()
        os.chdir(cwd)
        try:
            Interpreter(program, sigs, stdin=io.StringIO(SESSION),
                        stdout=out).run_main()
        finally:
            os.chdir(old_cwd)
        return out.getvalue()

    def test_interpreted_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self.run_interpreted(tmp)
            persisted = (Path(tmp) / "data" / "tasks.txt").read_text(
                encoding="utf-8")
        self.assertEqual(output, EXPECTED_OUT)
        self.assertEqual(persisted, EXPECTED_FILE)

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_interpreted(tmp)
            program = load_program(str(APP))
            sigs = check(program)
            out = io.StringIO()
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                Interpreter(program, sigs, stdin=io.StringIO("list\nquit\n"),
                            stdout=out).run_main()
            finally:
                os.chdir(old_cwd)
        self.assertIn("1 [x] write the parser", out.getvalue())
        self.assertIn("2 [ ] prove the contracts", out.getvalue())

    def test_program_is_fully_proven(self):
        # The flagship claim: every contract and invariant in the first real
        # Sigil program is a compile-time theorem — zero obligations remain.
        from sigil.verify import HAVE_Z3, verify
        if not HAVE_Z3:
            self.skipTest("z3-solver not installed")
        program = load_program(str(APP))
        check(program)
        report = verify(program)
        unproven = [f for f in report.findings if not f.proven]
        self.assertEqual(unproven, [])

    @unittest.skipUnless(HAVE_RUSTC, "rustc not installed")
    def test_native_matches_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = build(str(APP), output=str(Path(tmp) / "tasks.exe"),
                        optimize=False, quiet=True)
            result = subprocess.run([str(exe)], input=SESSION,
                                    capture_output=True, encoding="utf-8",
                                    cwd=tmp)
            persisted = (Path(tmp) / "data" / "tasks.txt").read_text(
                encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), EXPECTED_OUT)
        self.assertEqual(persisted, EXPECTED_FILE)


if __name__ == "__main__":
    unittest.main()
