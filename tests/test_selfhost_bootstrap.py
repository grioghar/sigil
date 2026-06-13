"""The self-hosting fixpoint: cc0 compiles its own source into a native ELF
that, run on bare metal, compiles cc0's source again to a byte-identical copy.

Stage 0: the Python reference interpreter runs cc0 (selfhost/cc0.sg) to compile
the three Sigil sources (lexer.sg + parser.sg + cc0.sg) into cc0_stage1, a
static x86-64 Linux ELF -- no rustc/LLVM/assembler/linker/libc.
Stage 1: cc0_stage1 (native) compiles the same three sources into cc0_stage2.
Fixpoint: cc0_stage1 == cc0_stage2, byte for byte. That equality is the proof
that cc0 is a self-hosting compiler.

Linux-only: the emitted ELF runs only on Linux. The stage-0 compile is deep
recursion over cc0's own large AST, so it runs on a worker thread with a big
stack and a raised recursion limit.
"""

import io
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from sigil.checker import check
from sigil.interp import Interpreter
from sigil.modules import load_program

REPO = Path(__file__).resolve().parent.parent
SELFHOST = REPO / "selfhost"
SOURCES = ["lexer.sg", "parser.sg", "cc0.sg"]


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "the emitted compiler ELF only runs on Linux")
class TestSelfhostBootstrap(unittest.TestCase):
    def _stage0_compile(self, workdir: Path, out_name: str) -> None:
        """Run cc0 under the Python interpreter to compile the sources."""
        program = load_program(str(SELFHOST / "cc0.sg"))
        sigs = check(program)
        stdin = out_name + "\n" + "\n".join(SOURCES) + "\n"
        out = io.StringIO()
        err = {}

        def run():
            old = os.getcwd()
            os.chdir(workdir)
            try:
                Interpreter(program, sigs, stdin=io.StringIO(stdin),
                            stdout=out).run_main()
            except BaseException as exc:  # surface thread errors to the test
                err["exc"] = exc
            finally:
                os.chdir(old)

        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(3_000_000)
        threading.stack_size(256 * 1024 * 1024)
        t = threading.Thread(target=run)
        try:
            t.start()
            t.join()
        finally:
            sys.setrecursionlimit(old_limit)
        if "exc" in err:
            raise err["exc"]
        self.assertIn("compiled", out.getvalue(), out.getvalue())

    def test_bootstrap_fixpoint(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            for name in SOURCES:
                (work / name).write_text((SELFHOST / name).read_text(
                    encoding="utf-8"), encoding="utf-8")

            # stage 0 -> stage 1 (Python reference compiles cc0)
            self._stage0_compile(work, "cc0_stage1.bin")
            stage1 = work / "cc0_stage1.bin"
            self.assertTrue(stage1.exists(), "stage1 not produced")
            stage1_bytes = stage1.read_bytes()
            self.assertEqual(stage1_bytes[:4], b"\x7fELF")
            stage1.chmod(stage1.stat().st_mode | stat.S_IXUSR)

            # stage 1 -> stage 2 (native cc0 compiles cc0)
            stdin = b"cc0_stage2.bin\n" + b"\n".join(
                s.encode() for s in SOURCES) + b"\n"
            r = subprocess.run([str(stage1)], input=stdin,
                               capture_output=True, cwd=work)
            self.assertEqual(r.returncode, 0,
                             f"stage1 self-compile failed: {r.stderr!r}")
            stage2 = work / "cc0_stage2.bin"
            self.assertTrue(stage2.exists(),
                            f"stage2 not produced; stdout={r.stdout!r}")
            stage2_bytes = stage2.read_bytes()

            # the fixpoint
            self.assertEqual(
                stage1_bytes, stage2_bytes,
                f"stage1 ({len(stage1_bytes)} bytes) != stage2 "
                f"({len(stage2_bytes)} bytes): cc0 is not a fixpoint")

            # and stage2 is itself a working compiler
            (work / "t.sg").write_text(
                "fn main() -> Int { return (8 - 6) * (10 + 11); }")
            r2 = subprocess.run([str(stage2)], input=b"t.bin\nt.sg\n",
                                capture_output=True, cwd=work)
            self.assertEqual(r2.returncode, 0)
            prog = work / "t.bin"
            prog.chmod(prog.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(subprocess.run([str(prog)]).returncode, 42)


if __name__ == "__main__":
    unittest.main()
