"""Tree-walking interpreter for Sigil v0.1.

Runs only checked programs. Contracts are enforced at runtime with blame:
a failed `requires` blames the caller, a failed `ensures` blames the callee,
and a failed loop `invariant` blames the loop itself. Capabilities are
unforgeable runtime objects injected only into main.
"""

import os
import sys
from typing import Any, Optional, TextIO

from . import ast_nodes as A
from .checker import FnSig
from .errors import CapabilityFault, ContractViolation, RuntimeFault


class ConsoleCap:
    def __repr__(self) -> str:
        return "<capability Console>"


class FsCap:
    """A filesystem capability, possibly attenuated: read-only and/or jailed
    to a directory prefix. Attenuation only ever shrinks authority."""

    def __init__(self, can_write: bool = True, root: Optional[str] = None):
        self.can_write = can_write
        self.root = root

    def __repr__(self) -> str:
        scope = []
        if not self.can_write:
            scope.append("read-only")
        if self.root is not None:
            scope.append(f"root={self.root}")
        return f"<capability Fs{' ' + ', '.join(scope) if scope else ''}>"


def clean_path_parts(path: str, line: int, col: int, what: str) -> list[str]:
    """Validate a path against capability scoping rules: no absolute paths,
    no '..' escapes. Returns normalized components."""
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise CapabilityFault(
            f"absolute path '{path}' is not permitted {what}", line, col)
    parts: list[str] = []
    for comp in normalized.split("/"):
        if comp == "..":
            raise CapabilityFault(
                f"path '{path}' escapes its capability scope", line, col)
        if comp in ("", "."):
            continue
        parts.append(comp)
    return parts


class _ReturnSignal(Exception):
    def __init__(self, value: Any):
        self.value = value


class Frame:
    """One function activation: a stack of block scopes."""

    def __init__(self, initial: dict[str, Any], fn_name: str = ""):
        self.scopes: list[dict[str, Any]] = [initial]
        self.fn_name = fn_name  # for blame in loop invariant violations

    def push(self) -> None:
        self.scopes.append({})

    def pop(self) -> None:
        self.scopes.pop()

    def lookup(self, name: str) -> Any:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise KeyError(name)

    def declare(self, name: str, value: Any) -> None:
        self.scopes[-1][name] = value

    def assign(self, name: str, value: Any) -> None:
        for scope in reversed(self.scopes):
            if name in scope:
                scope[name] = value
                return
        raise KeyError(name)


class Interpreter:
    def __init__(self, program: A.Program, sigs: dict[str, FnSig],
                 stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None):
        self.functions = {fn.name: fn for fn in program.functions}
        self.sigs = sigs
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout

    # ------------------------------------------------------------ entry

    def run_main(self) -> None:
        main = self.functions.get("main")
        if main is None:
            raise RuntimeFault("no 'main' function defined", 0, 0)
        args: list[Any] = []
        for pname, ptype in main.params:
            if ptype == A.CONSOLE:
                args.append(ConsoleCap())
            elif ptype == A.FS:
                args.append(FsCap())
            else:
                raise RuntimeFault(
                    f"main parameter '{pname}' must be a capability "
                    f"(Console or Fs), got {ptype}; capabilities are the only "
                    f"thing the runtime can inject", main.line, main.col)
        self.call("main", args, main.line, main.col)

    # ------------------------------------------------------------ calls

    def call(self, name: str, args: list[Any], line: int, col: int) -> Any:
        builtin = getattr(self, f"builtin_{name}", None)
        if name not in self.functions and builtin is not None:
            return builtin(args, line, col)

        fn = self.functions[name]
        frame = Frame({pname: value for (pname, _), value in zip(fn.params, args)},
                      fn.name)

        for contract in fn.contracts:
            if contract.kind != "requires":
                continue
            if not self.eval(contract.expr, frame):
                raise ContractViolation(
                    f"requires clause of '{fn.name}' failed: "
                    f"`{contract.source}` — blame the CALLER at line {line}",
                    line, col, blame="caller")

        result: Any = None
        try:
            self.exec_block(fn.body, frame)
        except _ReturnSignal as signal:
            result = signal.value

        for contract in fn.contracts:
            if contract.kind != "ensures":
                continue
            frame.push()
            frame.declare("result", result)
            ok = self.eval(contract.expr, frame)
            frame.pop()
            if not ok:
                raise ContractViolation(
                    f"ensures clause of '{fn.name}' failed: "
                    f"`{contract.source}` — blame the CALLEE '{fn.name}' "
                    f"(line {fn.line})", contract.line, contract.col,
                    blame="callee")
        return result

    # ------------------------------------------------------------ statements

    def exec_block(self, stmts: list[A.Stmt], frame: Frame) -> None:
        frame.push()
        try:
            for stmt in stmts:
                self.exec_stmt(stmt, frame)
        finally:
            frame.pop()

    def exec_stmt(self, stmt: A.Stmt, frame: Frame) -> None:
        if isinstance(stmt, A.Let):
            frame.declare(stmt.name, self.eval(stmt.value, frame))
        elif isinstance(stmt, A.Assign):
            frame.assign(stmt.name, self.eval(stmt.value, frame))
        elif isinstance(stmt, A.Return):
            value = self.eval(stmt.value, frame) if stmt.value is not None else None
            raise _ReturnSignal(value)
        elif isinstance(stmt, A.If):
            if self.eval(stmt.cond, frame):
                self.exec_block(stmt.then_body, frame)
            elif stmt.else_body is not None:
                self.exec_block(stmt.else_body, frame)
        elif isinstance(stmt, A.While):
            # Reference semantics: invariants hold at every loop head — once
            # before the first iteration and again after each body execution.
            self.check_invariants(stmt, frame)
            while self.eval(stmt.cond, frame):
                self.exec_block(stmt.body, frame)
                self.check_invariants(stmt, frame)
        elif isinstance(stmt, A.Match):
            self.exec_match(stmt, frame)
        elif isinstance(stmt, A.ExprStmt):
            self.eval(stmt.expr, frame)
        else:
            raise RuntimeFault(f"unhandled statement {type(stmt).__name__}",
                               stmt.line, stmt.col)

    def exec_match(self, stmt: A.Match, frame: Frame) -> None:
        tag, payload = self.eval(stmt.scrutinee, frame)
        arm = next((a for a in stmt.arms if a.variant == tag), None)
        if arm is None:  # the checker proved a wildcard exists if needed
            arm = next(a for a in stmt.arms if a.variant is None)
        frame.push()
        try:
            if arm.variant is not None:
                for binder, value in zip(arm.binders, payload):
                    frame.declare(binder, value)
            for inner in arm.body:
                self.exec_stmt(inner, frame)
        finally:
            frame.pop()

    def check_invariants(self, stmt: A.While, frame: Frame) -> None:
        for inv in stmt.invariants:
            if not self.eval(inv.expr, frame):
                raise ContractViolation(
                    f"invariant of while loop in '{frame.fn_name}' failed: "
                    f"`{inv.source}` — blame the loop (line {stmt.line})",
                    inv.line, inv.col, blame="loop")

    # ------------------------------------------------------------ expressions

    def eval(self, expr: A.Expr, frame: Frame) -> Any:
        if isinstance(expr, A.IntLit):
            return expr.value
        if isinstance(expr, A.BoolLit):
            return expr.value
        if isinstance(expr, A.TextLit):
            return expr.value
        if isinstance(expr, A.ListLit):
            return [self.eval(item, frame) for item in expr.items]
        if isinstance(expr, A.Var):
            # A nullary enum variant, stamped by the checker. Variant values
            # are ('Name', [payloads]) tuples; equality falls out of tuples.
            if getattr(expr, "variant_of", None) is not None:
                return (expr.name, [])
            return frame.lookup(expr.name)
        if isinstance(expr, A.Call):
            if getattr(expr, "variant_of", None) is not None:
                return (expr.name, [self.eval(a, frame) for a in expr.args])
            args = [self.eval(a, frame) for a in expr.args]
            return self.call(expr.name, args, expr.line, expr.col)
        if isinstance(expr, A.RecordLit):
            return {fname: self.eval(fexpr, frame)
                    for fname, fexpr in expr.fields}
        if isinstance(expr, A.FieldAccess):
            return self.eval(expr.base, frame)[expr.field_name]
        if isinstance(expr, A.IfExpr):
            # Only the taken branch is evaluated (reference semantics).
            if self.eval(expr.cond, frame):
                return self.eval(expr.then_expr, frame)
            return self.eval(expr.else_expr, frame)
        if isinstance(expr, A.RecordUpdate):
            # The base is evaluated FIRST, then the fields left to right.
            updated = dict(self.eval(expr.base, frame))
            for fname, fexpr in expr.fields:
                updated[fname] = self.eval(fexpr, frame)
            return updated
        if isinstance(expr, A.Index):
            base = self.eval(expr.base, frame)
            index = self.eval(expr.index, frame)
            if index < 0 or index >= len(base):
                raise RuntimeFault(
                    f"index {index} out of range for list of length {len(base)}",
                    expr.line, expr.col)
            return base[index]
        if isinstance(expr, A.Unary):
            value = self.eval(expr.operand, frame)
            return (not value) if expr.op == "not" else -value
        if isinstance(expr, A.Binary):
            return self.eval_binary(expr, frame)
        raise RuntimeFault(f"unhandled expression {type(expr).__name__}",
                           expr.line, expr.col)

    def eval_binary(self, expr: A.Binary, frame: Frame) -> Any:
        op = expr.op
        if op == "and":
            return self.eval(expr.left, frame) and self.eval(expr.right, frame)
        if op == "or":
            return self.eval(expr.left, frame) or self.eval(expr.right, frame)

        left = self.eval(expr.left, frame)
        right = self.eval(expr.right, frame)
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            if right == 0:
                raise RuntimeFault("division by zero", expr.line, expr.col)
            quotient = abs(left) // abs(right)
            return quotient if (left >= 0) == (right >= 0) else -quotient
        if op == "%":
            if right == 0:
                raise RuntimeFault("modulo by zero", expr.line, expr.col)
            quotient = abs(left) // abs(right)
            if (left >= 0) != (right >= 0):
                quotient = -quotient
            return left - quotient * right
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        raise RuntimeFault(f"unhandled operator '{op}'", expr.line, expr.col)

    # ------------------------------------------------------------ builtins

    def builtin_print(self, args: list[Any], line: int, col: int) -> None:
        cap, msg = args
        self._require_cap(cap, ConsoleCap, "print", line, col)
        self.stdout.write(msg + "\n")

    def builtin_read_line(self, args: list[Any], line: int, col: int) -> str:
        self._require_cap(args[0], ConsoleCap, "read_line", line, col)
        return self.stdin.readline().rstrip("\n")

    def builtin_read_file(self, args: list[Any], line: int, col: int) -> str:
        cap, path = args
        self._require_cap(cap, FsCap, "read_file", line, col)
        effective = self._fs_effective(cap, path, line, col)
        try:
            with open(effective, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise RuntimeFault(f"read_file failed: {exc}", line, col)

    def builtin_write_file(self, args: list[Any], line: int, col: int) -> None:
        cap, path, data = args
        self._require_cap(cap, FsCap, "write_file", line, col)
        if not cap.can_write:
            raise CapabilityFault(
                f"write_file('{path}') denied: this Fs capability is read-only",
                line, col)
        effective = self._fs_effective(cap, path, line, col)
        try:
            parent = os.path.dirname(effective)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(effective, "w", encoding="utf-8") as handle:
                handle.write(data)
        except OSError as exc:
            raise RuntimeFault(f"write_file failed: {exc}", line, col)

    def builtin_file_exists(self, args: list[Any], line: int, col: int) -> bool:
        cap, path = args
        self._require_cap(cap, FsCap, "file_exists", line, col)
        effective = self._fs_effective(cap, path, line, col)
        return os.path.exists(effective)

    def builtin_read_only(self, args: list[Any], line: int, col: int) -> FsCap:
        cap = args[0]
        self._require_cap(cap, FsCap, "read_only", line, col)
        return FsCap(can_write=False, root=cap.root)

    def builtin_subdir(self, args: list[Any], line: int, col: int) -> FsCap:
        cap, prefix = args
        self._require_cap(cap, FsCap, "subdir", line, col)
        parts = clean_path_parts(prefix, line, col, "as an Fs scope")
        if not parts:
            raise CapabilityFault(
                f"'{prefix}' is an empty path; it cannot scope an Fs", line, col)
        cleaned = "/".join(parts)
        root = cleaned if cap.root is None else f"{cap.root}/{cleaned}"
        return FsCap(can_write=cap.can_write, root=root)

    def _fs_effective(self, cap: FsCap, path: str, line: int, col: int) -> str:
        if cap.root is None:
            return path
        parts = clean_path_parts(path, line, col, "through a scoped Fs")
        return f"{cap.root}/{'/'.join(parts)}"

    def builtin_len(self, args: list[Any], line: int, col: int) -> int:
        return len(args[0])

    def builtin_str(self, args: list[Any], line: int, col: int) -> str:
        value = args[0]
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def builtin_push(self, args: list[Any], line: int, col: int) -> list:
        xs, x = args
        return xs + [x]

    def builtin_slice(self, args: list[Any], line: int, col: int) -> str:
        s, start, end = args
        if start < 0 or end < start or end > len(s):
            raise RuntimeFault(
                f"slice({start}, {end}) out of range for text of length "
                f"{len(s)}", line, col)
        return s[start:end]

    def builtin_ord(self, args: list[Any], line: int, col: int) -> int:
        s = args[0]
        if len(s) != 1:
            raise RuntimeFault(
                f"ord needs a single character, got text of length {len(s)}",
                line, col)
        return ord(s)

    def builtin_chr(self, args: list[Any], line: int, col: int) -> str:
        n = args[0]
        # Surrogates rejected for parity with the native backend (Rust char).
        if n < 0 or n > 0x10FFFF or 0xD800 <= n <= 0xDFFF:
            raise RuntimeFault(f"chr({n}) is not a valid character code",
                               line, col)
        return chr(n)

    def _require_cap(self, value: Any, cap_type: type, name: str,
                     line: int, col: int) -> None:
        # Defense in depth: the checker already guarantees this by type.
        if not isinstance(value, cap_type):
            raise RuntimeFault(
                f"'{name}' invoked without a {cap_type.__name__[:-3]} "
                f"capability", line, col)
