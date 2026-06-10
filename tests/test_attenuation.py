"""Tests for capability attenuation (v0.2): read_only() and subdir()."""

import io
import os
import tempfile
import unittest

from sigil.checker import check
from sigil.errors import CapabilityFault
from sigil.interp import Interpreter
from sigil.parser import parse


def run_in(tmp: str, source: str) -> str:
    """Run a program with the process cwd set to tmp (the unattenuated Fs
    root is the cwd, so this jails test files)."""
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    finally:
        os.chdir(cwd)
    return out.getvalue()


class TestAttenuation(unittest.TestCase):
    def test_read_only_blocks_write(self):
        src = """
            fn main(fs: Fs) -> Unit ! {fs.write} {
                let ro: Fs = read_only(fs);
                write_file(ro, "x.txt", "data");
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CapabilityFault) as ctx:
                run_in(tmp, src)
        self.assertIn("read-only", ctx.exception.message)

    def test_read_only_still_reads(self):
        src = """
            fn main(console: Console, fs: Fs) -> Unit ! {io.write, fs.read, fs.write} {
                write_file(fs, "x.txt", "data");
                let ro: Fs = read_only(fs);
                print(console, read_file(ro, "x.txt"));
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_in(tmp, src), "data\n")

    def test_subdir_rebases_paths(self):
        src = """
            fn main(console: Console, fs: Fs) -> Unit ! {io.write, fs.read, fs.write} {
                let jail: Fs = subdir(fs, "inner");
                write_file(jail, "x.txt", "jailed");
                print(console, read_file(fs, "inner/x.txt"));
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_in(tmp, src), "jailed\n")

    def test_subdir_blocks_dotdot_escape(self):
        src = """
            fn main(console: Console, fs: Fs) -> Unit ! {io.write, fs.read, fs.write} {
                write_file(fs, "secrets.txt", "hunter2");
                let jail: Fs = subdir(fs, "inner");
                print(console, read_file(jail, "../secrets.txt"));
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CapabilityFault) as ctx:
                run_in(tmp, src)
        self.assertIn("escapes", ctx.exception.message)

    def test_subdir_blocks_absolute_paths(self):
        src = """
            fn main(console: Console, fs: Fs) -> Unit ! {io.write, fs.read} {
                let jail: Fs = subdir(fs, "inner");
                print(console, read_file(jail, "C:/Windows/win.ini"));
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CapabilityFault) as ctx:
                run_in(tmp, src)
        self.assertIn("absolute", ctx.exception.message)

    def test_attenuation_composes_and_only_shrinks(self):
        src = """
            fn main(console: Console, fs: Fs) -> Unit ! {io.write, fs.write} {
                let weak: Fs = subdir(read_only(fs), "a");
                write_file(weak, "x.txt", "data");
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CapabilityFault) as ctx:
                run_in(tmp, src)
        self.assertIn("read-only", ctx.exception.message)

    def test_attenuation_is_pure(self):
        # Minting a weaker capability is not an effect; a pure function
        # may attenuate (it just can't USE the result for I/O).
        src = """
            fn sandbox_of(fs: Fs) -> Fs {
                return read_only(subdir(fs, "sandbox"));
            }
            fn main(fs: Fs) -> Unit {
                let s: Fs = sandbox_of(fs);
            }
        """
        check(parse(src))  # must not raise

    def test_empty_scope_rejected(self):
        src = """
            fn main(fs: Fs) -> Unit {
                let jail: Fs = subdir(fs, "./");
            }
        """
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CapabilityFault) as ctx:
                run_in(tmp, src)
        self.assertIn("empty", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
