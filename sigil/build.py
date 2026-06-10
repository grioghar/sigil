"""`sigil build`: lower checked Sigil to Rust and compile to a native binary."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import ast_nodes as A
from .checker import check
from .emit_rust import emit_rust
from .errors import SigilError
from .parser import parse


def find_rustc() -> str:
    rustc = shutil.which("rustc")
    if rustc is None:
        raise SigilError(
            "rustc not found on PATH; the native backend compiles via Rust "
            "(install from https://rustup.rs)", 0, 0)
    return rustc


def build(source_path: str, output: str | None = None,
          emit_rust_path: str | None = None, optimize: bool = True) -> Path:
    """Compile a .sg file to a native executable. Returns the binary path."""
    src_file = Path(source_path)
    source = src_file.read_text(encoding="utf-8")

    program = parse(source)
    check(program)  # stamps expression types the emitter needs
    rust_source = emit_rust(program)

    if output is None:
        suffix = ".exe" if sys.platform == "win32" else ""
        out_path = Path.cwd() / (src_file.stem + suffix)
    else:
        out_path = Path(output)

    rustc = find_rustc()

    with tempfile.TemporaryDirectory(prefix="sigilc_") as tmp:
        rs_path = Path(tmp) / (src_file.stem + ".rs")
        rs_path.write_text(rust_source, encoding="utf-8")
        if emit_rust_path is not None:
            Path(emit_rust_path).write_text(rust_source, encoding="utf-8")

        cmd = [rustc, "--edition", "2021", str(rs_path), "-o", str(out_path)]
        if optimize:
            cmd.insert(1, "-O")
        result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        # Checked Sigil should always lower to valid Rust; this is our bug.
        raise SigilError(
            "internal codegen error — the generated Rust did not compile. "
            "Please report this. rustc said:\n" + result.stderr.strip(), 0, 0)
    return out_path
