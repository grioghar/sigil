"""Sigil CLI.

    python -m sigil check <file.sg>    type/effect/capability check only
    python -m sigil run <file.sg>      check, then execute main (interpreter)
    python -m sigil verify <file.sg>   prove contracts statically (needs z3)
    python -m sigil build <file.sg>    compile to a native executable
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
    verify_cmd = sub.add_parser("verify", help="prove contracts statically")
    verify_cmd.add_argument("file")
    build_cmd = sub.add_parser("build", help="compile to a native executable")
    build_cmd.add_argument("file")
    build_cmd.add_argument("-o", "--output", default=None,
                           help="output binary path (default: <name>.exe in cwd)")
    build_cmd.add_argument("--emit-rust", default=None, metavar="PATH",
                           help="also write the generated Rust source here")
    build_cmd.add_argument("--debug", action="store_true",
                           help="skip rustc optimizations (faster builds)")
    build_cmd.add_argument("--no-verify", action="store_true",
                           help="skip static verification; keep all runtime checks")
    args = cli.parse_args(argv)

    if args.command == "build":
        from .build import build
        try:
            out_path = build(args.file, args.output, args.emit_rust,
                             optimize=not args.debug,
                             verify_contracts=not args.no_verify)
        except SigilError as exc:
            print(exc.render(args.file), file=sys.stderr)
            return 1
        print(f"built {out_path}")
        return 0

    if args.command == "verify":
        from .verify import verify
        try:
            with open(args.file, "r", encoding="utf-8") as handle:
                source = handle.read()
            program = parse(source)
            check(program)
        except (OSError, SigilError) as exc:
            msg = exc.render(args.file) if isinstance(exc, SigilError) else str(exc)
            print(msg, file=sys.stderr)
            return 1
        report = verify(program)
        if report is None:
            print("verifier unavailable: pip install z3-solver", file=sys.stderr)
            return 1
        for finding in sorted(report.findings, key=lambda f: (f.fn, f.line)):
            status = "PROVED " if finding.proven else "RUNTIME"
            print(f"  {status}  {finding.fn}: {finding.kind} {finding.source}")
        print(f"{report.contracts_proven}/{report.contracts_total} contract "
              f"clause(s) and {report.divisions_proven}/{report.divisions_total} "
              f"division(s) proven safe")
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
