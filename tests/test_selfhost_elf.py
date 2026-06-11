"""Feasibility proof for the dependency-free backend: the Sigil program
selfhost/elf_exit.sg emits a complete static x86-64 Linux ELF that exits 42,
using only write_bytes — no rustc, no linker, no libc. We validate the ELF
structure everywhere, and on Linux we run the emitted binary and assert it
exits 42. See docs/SELFHOST.md.

Also covers the write_bytes primitive itself (round-trip, range, read-only)."""

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sigil.checker import check
from sigil.errors import CapabilityFault, RuntimeFault
from sigil.interp import Interpreter
from sigil.parser import parse

REPO = Path(__file__).resolve().parent.parent
ELF_PROG = REPO / "selfhost" / "elf_exit.sg"


def run_program(source: str, stdin: str, cwd: Path) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        Interpreter(program, sigs, stdin=io.StringIO(stdin),
                    stdout=out).run_main()
    finally:
        os.chdir(old)
    return out.getvalue()


class TestWriteBytes(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_program(
                'fn main(c: Console, fs: Fs) -> Unit ! {fs.write} {\n'
                '  write_bytes(fs, "b.bin", [0, 1, 127, 128, 255]);\n'
                '}\n', "", Path(tmp))
            data = (Path(tmp) / "b.bin").read_bytes()
        self.assertEqual(data, bytes([0, 1, 127, 128, 255]))

    def test_out_of_range_faults(self):
        for bad in ("256", "0 - 1"):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeFault):
                    run_program(
                        'fn main(c: Console, fs: Fs) -> Unit ! {fs.write} {\n'
                        f'  write_bytes(fs, "b.bin", [{bad}]);\n'
                        '}\n', "", Path(tmp))

    def test_read_only_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CapabilityFault):
                run_program(
                    'fn main(c: Console, fs: Fs) -> Unit ! {fs.write} {\n'
                    '  write_bytes(read_only(fs), "b.bin", [1, 2, 3]);\n'
                    '}\n', "", Path(tmp))


class TestElfEmission(unittest.TestCase):
    def emit(self, tmp: Path) -> bytes:
        out = run_program(ELF_PROG.read_text(encoding="utf-8"),
                          "a.out\n", tmp)
        self.assertIn("wrote 132 bytes", out)
        return (tmp / "a.out").read_bytes()

    def test_elf_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = self.emit(Path(tmp))
        self.assertEqual(len(blob), 132)
        self.assertEqual(blob[:4], b"\x7fELF")
        self.assertEqual(blob[4], 2)                 # ELFCLASS64
        self.assertEqual(blob[5], 1)                 # little-endian
        self.assertEqual(blob[16], 2)                # ET_EXEC
        self.assertEqual(blob[18], 62)               # EM_X86_64
        # entry == 0x400078, just past the 120 bytes of headers.
        entry = int.from_bytes(blob[24:32], "little")
        self.assertEqual(entry, 0x400078)
        # the 12 code bytes: mov edi,42 / mov eax,60 / syscall
        self.assertEqual(blob[120:132],
                         bytes([191, 42, 0, 0, 0, 184, 60, 0, 0, 0, 15, 5]))

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "a Linux ELF only runs on Linux")
    def test_emitted_binary_exits_42(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.emit(Path(tmp))
            exe = Path(tmp) / "a.out"
            # write_bytes marks it executable; belt-and-suspenders here.
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run([str(exe)])
        self.assertEqual(result.returncode, 42)


if __name__ == "__main__":
    unittest.main()
