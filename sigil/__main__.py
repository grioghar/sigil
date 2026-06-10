"""Sigil CLI.

    python -m sigil check <file.sg>   type/effect/capability check only
    python -m sigil run <file.sg>     check, then execute main (interpreter)
    python -m sigil build <file.sg>   compile to a native executable
"""

import argparse
import sys

from .checker import check
from .errors import SigilError
from .interp import Interpreter
from .parser import parse


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="sigil", description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    for name in ("check", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("file")
    build_cmd = sub.add_parser("build", help="compile to a native executable")
    build_cmd.add_argument("file")
    build_cmd.add_argument("-o", "--output", default=None,
                           help="output binary path (default: <name>.exe in cwd)")
    build_cmd.add_argument("--emit-rust", default=None, metavar="PATH",
                           help="also write the generated Rust source here")
    build_cmd.add_argument("--debug", action="store_true",
                           help="skip rustc optimizations (faster builds)")
    args = cli.parse_args(argv)

    if args.command == "build":
        from .build import build
        try:
            out_path = build(args.file, args.output, args.emit_rust,
                             optimize=not args.debug)
        except SigilError as exc:
            print(exc.render(args.file), file=sys.stderr)
            return 1
        print(f"built {out_path}")
        return 0

    try:
        with open(args.file, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        print(f"sigil: cannot read {args.file}: {exc}", file=sys.stderr)
        return 1

    try:
        program = parse(source)
        sigs = check(program)
        if args.command == "check":
            pure = sum(1 for f in program.functions if not f.effects)
            contracts = sum(len(f.contracts) for f in program.functions)
            print(f"OK: {len(program.functions)} function(s), {pure} pure, "
                  f"{contracts} contract clause(s)")
            return 0
        Interpreter(program, sigs).run_main()
        return 0
    except SigilError as exc:
        print(exc.render(args.file), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
