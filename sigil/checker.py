"""Static checker for Sigil: types, effects, capabilities, contract sanity.

This is the heart of the security model:
  * every call's effects must be covered by the caller's declared effects
  * capability values cannot be conjured — they only flow through parameters
  * contract clauses must be pure Bool expressions
"""

from dataclasses import dataclass
from typing import Optional

from . import ast_nodes as A
from .errors import CheckError

KNOWN_EFFECTS = {"io.read", "io.write", "fs.read", "fs.write"}

PURE = frozenset()


@dataclass
class FnSig:
    name: str
    params: list[tuple[str, A.Type]]
    ret: A.Type
    effects: frozenset[str]
    decl: Optional[A.FnDecl] = None  # None for builtins


BUILTINS: dict[str, FnSig] = {
    "print": FnSig("print", [("c", A.CONSOLE), ("msg", A.TEXT)], A.UNIT,
                   frozenset({"io.write"})),
    "read_line": FnSig("read_line", [("c", A.CONSOLE)], A.TEXT,
                       frozenset({"io.read"})),
    "read_file": FnSig("read_file", [("fs", A.FS), ("path", A.TEXT)], A.TEXT,
                       frozenset({"fs.read"})),
    "write_file": FnSig("write_file", [("fs", A.FS), ("path", A.TEXT), ("data", A.TEXT)],
                        A.UNIT, frozenset({"fs.write"})),
    # Attenuation: minting a weaker capability is pure — no I/O happens.
    "read_only": FnSig("read_only", [("fs", A.FS)], A.FS, PURE),
    "subdir": FnSig("subdir", [("fs", A.FS), ("prefix", A.TEXT)], A.FS, PURE),
    # len / str / push are polymorphic and special-cased in check_call.
    "len": FnSig("len", [("x", A.Type("List"))], A.INT, PURE),
    "str": FnSig("str", [("x", A.INT)], A.TEXT, PURE),
    "push": FnSig("push", [("xs", A.Type("List")), ("x", A.INT)], A.Type("List"), PURE),
}

POLYMORPHIC = {"len", "str", "push"}


@dataclass
class Binding:
    type: A.Type
    mutable: bool


class Scope:
    def __init__(self, parent: Optional["Scope"] = None):
        self.parent = parent
        self.names: dict[str, Binding] = {}

    def lookup(self, name: str) -> Optional[Binding]:
        scope: Optional[Scope] = self
        while scope is not None:
            if name in scope.names:
                return scope.names[name]
            scope = scope.parent
        return None

    def declare(self, name: str, binding: Binding, line: int, col: int) -> None:
        if self.lookup(name) is not None:
            raise CheckError(
                f"'{name}' is already defined; Sigil forbids shadowing "
                f"(every name means one thing to an auditor)", line, col)
        self.names[name] = binding


class Checker:
    def __init__(self, program: A.Program):
        self.program = program
        self.sigs: dict[str, FnSig] = dict(BUILTINS)

    # ------------------------------------------------------------ entry

    def check(self) -> dict[str, FnSig]:
        for fn in self.program.functions:
            if fn.name in self.sigs:
                kind = "builtin" if self.sigs[fn.name].decl is None else "function"
                raise CheckError(f"'{fn.name}' is already defined as a {kind}",
                                 fn.line, fn.col)
            for eff in fn.effects:
                if eff not in KNOWN_EFFECTS:
                    raise CheckError(
                        f"unknown effect '{eff}'; known effects: "
                        f"{', '.join(sorted(KNOWN_EFFECTS))}", fn.line, fn.col)
            self.sigs[fn.name] = FnSig(fn.name, fn.params, fn.ret, fn.effects, fn)

        for fn in self.program.functions:
            self.check_fn(fn)
        return self.sigs

    # ------------------------------------------------------------ functions

    def check_fn(self, fn: A.FnDecl) -> None:
        scope = Scope()
        seen_params: set[str] = set()
        for pname, ptype in fn.params:
            if pname in seen_params:
                raise CheckError(f"duplicate parameter '{pname}' in '{fn.name}'",
                                 fn.line, fn.col)
            seen_params.add(pname)
            scope.declare(pname, Binding(ptype, mutable=False), fn.line, fn.col)

        for contract in fn.contracts:
            cscope = Scope(scope)
            if contract.kind == "ensures":
                if fn.ret == A.UNIT:
                    cscope.declare("result", Binding(A.UNIT, False),
                                   contract.line, contract.col)
                else:
                    cscope.declare("result", Binding(fn.ret, False),
                                   contract.line, contract.col)
            ctype = self.check_expr(contract.expr, cscope, fn, in_contract=True)
            if ctype != A.BOOL:
                raise CheckError(
                    f"{contract.kind} clause must be Bool, got {ctype}",
                    contract.line, contract.col)

        self.check_block(fn.body, Scope(scope), fn)

        if fn.ret != A.UNIT and not self.definitely_returns(fn.body):
            raise CheckError(
                f"'{fn.name}' returns {fn.ret} but not all paths end in a "
                f"return statement", fn.line, fn.col)

    def definitely_returns(self, stmts: list[A.Stmt]) -> bool:
        for stmt in stmts:
            if isinstance(stmt, A.Return):
                return True
            if isinstance(stmt, A.If) and stmt.else_body is not None:
                if (self.definitely_returns(stmt.then_body)
                        and self.definitely_returns(stmt.else_body)):
                    return True
        return False

    # ------------------------------------------------------------ statements

    def check_block(self, stmts: list[A.Stmt], scope: Scope, fn: A.FnDecl) -> None:
        for stmt in stmts:
            self.check_stmt(stmt, scope, fn)

    def check_stmt(self, stmt: A.Stmt, scope: Scope, fn: A.FnDecl) -> None:
        if isinstance(stmt, A.Let):
            vtype = self.check_expr(stmt.value, scope, fn)
            if not A.compatible(stmt.declared_type, vtype):
                raise CheckError(
                    f"'{stmt.name}' is declared {stmt.declared_type} but its "
                    f"initializer has type {vtype}", stmt.line, stmt.col)
            scope.declare(stmt.name, Binding(stmt.declared_type, stmt.mutable),
                          stmt.line, stmt.col)
            return

        if isinstance(stmt, A.Assign):
            binding = scope.lookup(stmt.name)
            if binding is None:
                raise CheckError(f"unknown name '{stmt.name}'", stmt.line, stmt.col)
            if not binding.mutable:
                raise CheckError(
                    f"'{stmt.name}' is immutable (declared with 'let'); use "
                    f"'var' if it must change", stmt.line, stmt.col)
            vtype = self.check_expr(stmt.value, scope, fn)
            if not A.compatible(binding.type, vtype):
                raise CheckError(
                    f"cannot assign {vtype} to '{stmt.name}' of type "
                    f"{binding.type}", stmt.line, stmt.col)
            return

        if isinstance(stmt, A.Return):
            if stmt.value is None:
                if fn.ret != A.UNIT:
                    raise CheckError(
                        f"'{fn.name}' must return {fn.ret}", stmt.line, stmt.col)
                return
            vtype = self.check_expr(stmt.value, scope, fn)
            if fn.ret == A.UNIT:
                raise CheckError(
                    f"'{fn.name}' returns Unit; remove the return value",
                    stmt.line, stmt.col)
            if not A.compatible(fn.ret, vtype):
                raise CheckError(
                    f"'{fn.name}' returns {fn.ret} but this returns {vtype}",
                    stmt.line, stmt.col)
            return

        if isinstance(stmt, A.If):
            ctype = self.check_expr(stmt.cond, scope, fn)
            if ctype != A.BOOL:
                raise CheckError(f"if condition must be Bool, got {ctype}",
                                 stmt.line, stmt.col)
            self.check_block(stmt.then_body, Scope(scope), fn)
            if stmt.else_body is not None:
                self.check_block(stmt.else_body, Scope(scope), fn)
            return

        if isinstance(stmt, A.While):
            ctype = self.check_expr(stmt.cond, scope, fn)
            if ctype != A.BOOL:
                raise CheckError(f"while condition must be Bool, got {ctype}",
                                 stmt.line, stmt.col)
            self.check_block(stmt.body, Scope(scope), fn)
            return

        if isinstance(stmt, A.ExprStmt):
            self.check_expr(stmt.expr, scope, fn)
            return

        raise CheckError(f"unhandled statement {type(stmt).__name__}",
                         stmt.line, stmt.col)

    # ------------------------------------------------------------ expressions

    def check_expr(self, expr: A.Expr, scope: Scope, fn: A.FnDecl,
                   in_contract: bool = False) -> A.Type:
        ty = self._infer_expr(expr, scope, fn, in_contract)
        expr.ty = ty  # stamp for backends (the Rust emitter reads this)
        return ty

    def _infer_expr(self, expr: A.Expr, scope: Scope, fn: A.FnDecl,
                    in_contract: bool = False) -> A.Type:
        if isinstance(expr, A.IntLit):
            return A.INT
        if isinstance(expr, A.BoolLit):
            return A.BOOL
        if isinstance(expr, A.TextLit):
            return A.TEXT

        if isinstance(expr, A.ListLit):
            if not expr.items:
                return A.Type("List", None)
            first = self.check_expr(expr.items[0], scope, fn, in_contract)
            for item in expr.items[1:]:
                itype = self.check_expr(item, scope, fn, in_contract)
                if not A.compatible(first, itype):
                    raise CheckError(
                        f"list elements must share one type; found {first} "
                        f"and {itype}", item.line, item.col)
            return A.Type("List", first)

        if isinstance(expr, A.Var):
            binding = scope.lookup(expr.name)
            if binding is None:
                raise CheckError(f"unknown name '{expr.name}'", expr.line, expr.col)
            return binding.type

        if isinstance(expr, A.Call):
            return self.check_call(expr, scope, fn, in_contract)

        if isinstance(expr, A.Index):
            btype = self.check_expr(expr.base, scope, fn, in_contract)
            itype = self.check_expr(expr.index, scope, fn, in_contract)
            if btype.kind != "List":
                raise CheckError(f"only List values can be indexed, got {btype}",
                                 expr.line, expr.col)
            if itype != A.INT:
                raise CheckError(f"index must be Int, got {itype}",
                                 expr.line, expr.col)
            if btype.elem is None:
                raise CheckError("cannot index an empty list literal",
                                 expr.line, expr.col)
            return btype.elem

        if isinstance(expr, A.Unary):
            otype = self.check_expr(expr.operand, scope, fn, in_contract)
            if expr.op == "not":
                if otype != A.BOOL:
                    raise CheckError(f"'not' needs Bool, got {otype}",
                                     expr.line, expr.col)
                return A.BOOL
            if expr.op == "-":
                if otype != A.INT:
                    raise CheckError(f"unary '-' needs Int, got {otype}",
                                     expr.line, expr.col)
                return A.INT

        if isinstance(expr, A.Binary):
            ltype = self.check_expr(expr.left, scope, fn, in_contract)
            rtype = self.check_expr(expr.right, scope, fn, in_contract)
            op = expr.op

            if op in ("and", "or"):
                if ltype != A.BOOL or rtype != A.BOOL:
                    raise CheckError(
                        f"'{op}' needs Bool operands, got {ltype} and {rtype}",
                        expr.line, expr.col)
                return A.BOOL

            if op in ("==", "!="):
                if ltype.kind in A.CAPABILITY_KINDS or rtype.kind in A.CAPABILITY_KINDS:
                    raise CheckError("capability values cannot be compared",
                                     expr.line, expr.col)
                if not (A.compatible(ltype, rtype) or A.compatible(rtype, ltype)):
                    raise CheckError(
                        f"cannot compare {ltype} with {rtype}", expr.line, expr.col)
                return A.BOOL

            if op in ("<", "<=", ">", ">="):
                if ltype != A.INT or rtype != A.INT:
                    raise CheckError(
                        f"'{op}' needs Int operands, got {ltype} and {rtype}",
                        expr.line, expr.col)
                return A.BOOL

            if op == "+":
                if ltype == A.INT and rtype == A.INT:
                    return A.INT
                if ltype == A.TEXT and rtype == A.TEXT:
                    return A.TEXT
                raise CheckError(
                    f"'+' needs two Ints or two Texts, got {ltype} and {rtype}"
                    + (" (use str(...) to convert)" if A.TEXT in (ltype, rtype) else ""),
                    expr.line, expr.col)

            if op in ("-", "*", "/", "%"):
                if ltype != A.INT or rtype != A.INT:
                    raise CheckError(
                        f"'{op}' needs Int operands, got {ltype} and {rtype}",
                        expr.line, expr.col)
                return A.INT

        raise CheckError(f"unhandled expression {type(expr).__name__}",
                         expr.line, expr.col)

    # ------------------------------------------------------------ calls

    def check_call(self, expr: A.Call, scope: Scope, fn: A.FnDecl,
                   in_contract: bool) -> A.Type:
        sig = self.sigs.get(expr.name)
        if sig is None:
            hint = ""
            if scope.lookup(expr.name) is not None:
                hint = " (Sigil v0.1 has no first-class functions)"
            raise CheckError(f"unknown function '{expr.name}'{hint}",
                             expr.line, expr.col)

        # Effect discipline — the core security check.
        missing = sig.effects - fn.effects
        if missing:
            declared = ("{" + ", ".join(sorted(fn.effects)) + "}"
                        if fn.effects else "none (it is pure)")
            raise CheckError(
                f"'{fn.name}' calls '{expr.name}', which performs "
                f"{{{', '.join(sorted(missing))}}}, but '{fn.name}' declares "
                f"{declared}; add the effect to '{fn.name}' or remove the call",
                expr.line, expr.col)

        if in_contract and sig.effects:
            raise CheckError(
                f"contract clauses must be pure; '{expr.name}' performs "
                f"{{{', '.join(sorted(sig.effects))}}}", expr.line, expr.col)

        arg_types = [self.check_expr(a, scope, fn, in_contract) for a in expr.args]

        if expr.name in POLYMORPHIC:
            return self.check_polymorphic(expr, arg_types)

        if len(arg_types) != len(sig.params):
            raise CheckError(
                f"'{expr.name}' takes {len(sig.params)} argument(s), got "
                f"{len(arg_types)}", expr.line, expr.col)
        for (pname, ptype), atype, arg in zip(sig.params, arg_types, expr.args):
            if not A.compatible(ptype, atype):
                hint = ""
                if ptype.kind in A.CAPABILITY_KINDS:
                    hint = (f" — capabilities cannot be created, only received; "
                            f"thread a {ptype} parameter from main")
                raise CheckError(
                    f"argument '{pname}' of '{expr.name}' needs {ptype}, got "
                    f"{atype}{hint}", arg.line, arg.col)
        return sig.ret

    def check_polymorphic(self, expr: A.Call, arg_types: list[A.Type]) -> A.Type:
        name = expr.name
        if name == "len":
            if len(arg_types) != 1 or arg_types[0].kind not in ("List", "Text"):
                raise CheckError("len takes one List or Text argument",
                                 expr.line, expr.col)
            return A.INT
        if name == "str":
            if len(arg_types) != 1 or arg_types[0] not in (A.INT, A.BOOL, A.TEXT):
                raise CheckError("str takes one Int, Bool, or Text argument",
                                 expr.line, expr.col)
            return A.TEXT
        if name == "push":
            if len(arg_types) != 2 or arg_types[0].kind != "List":
                raise CheckError("push takes a List and an element",
                                 expr.line, expr.col)
            xs, x = arg_types
            if xs.elem is not None and not A.compatible(xs.elem, x):
                raise CheckError(
                    f"cannot push {x} onto {xs}", expr.line, expr.col)
            return A.Type("List", xs.elem if xs.elem is not None else x)
        raise CheckError(f"unhandled polymorphic builtin '{name}'",
                         expr.line, expr.col)


def check(program: A.Program) -> dict[str, FnSig]:
    return Checker(program).check()
