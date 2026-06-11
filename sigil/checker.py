"""Static checker for Sigil: types, effects, capabilities, contract sanity.

This is the heart of the security model:
  * every call's effects must be covered by the caller's declared effects
  * capability values cannot be conjured — they only flow through parameters
  * contract clauses must be pure Bool expressions
"""

from dataclasses import dataclass, field
from typing import Optional

from . import ast_nodes as A
from .errors import CheckError

KNOWN_EFFECTS = {"io.read", "io.write", "fs.read", "fs.write"}

PURE = frozenset()


def last_segment(name: str) -> str:
    """The declared name inside a possibly module-qualified name. The module
    loader (0.7) qualifies non-entry declarations as '<module>.<name>'; the
    naming canon applies to the name the author wrote, while duplicate and
    collision checks operate on the full qualified name."""
    return name.rsplit(".", 1)[-1]


@dataclass
class FnSig:
    name: str
    params: list[tuple[str, A.Type]]
    ret: A.Type
    effects: frozenset[str]
    decl: Optional[A.FnDecl] = None  # None for builtins
    type_params: list[str] = field(default_factory=list)  # generic functions


BUILTINS: dict[str, FnSig] = {
    "print": FnSig("print", [("c", A.CONSOLE), ("msg", A.TEXT)], A.UNIT,
                   frozenset({"io.write"})),
    "read_line": FnSig("read_line", [("c", A.CONSOLE)], A.TEXT,
                       frozenset({"io.read"})),
    "read_file": FnSig("read_file", [("fs", A.FS), ("path", A.TEXT)], A.TEXT,
                       frozenset({"fs.read"})),
    "write_file": FnSig("write_file", [("fs", A.FS), ("path", A.TEXT), ("data", A.TEXT)],
                        A.UNIT, frozenset({"fs.write"})),
    "file_exists": FnSig("file_exists", [("fs", A.FS), ("path", A.TEXT)],
                         A.BOOL, frozenset({"fs.read"})),
    # Attenuation: minting a weaker capability is pure — no I/O happens.
    "read_only": FnSig("read_only", [("fs", A.FS)], A.FS, PURE),
    "subdir": FnSig("subdir", [("fs", A.FS), ("prefix", A.TEXT)], A.FS, PURE),
    # Text primitives: the minimal set that makes text processing possible.
    # Everything richer (split, trim, parse_int) is written in Sigil itself.
    "slice": FnSig("slice", [("s", A.TEXT), ("start", A.INT), ("end", A.INT)],
                   A.TEXT, PURE),
    "ord": FnSig("ord", [("s", A.TEXT)], A.INT, PURE),
    "chr": FnSig("chr", [("n", A.INT)], A.TEXT, PURE),
    # len / str / push / set are polymorphic and special-cased in check_call.
    "len": FnSig("len", [("x", A.Type("List"))], A.INT, PURE),
    "str": FnSig("str", [("x", A.INT)], A.TEXT, PURE),
    "push": FnSig("push", [("xs", A.Type("List")), ("x", A.INT)], A.Type("List"), PURE),
    "set": FnSig("set", [("xs", A.Type("List")), ("i", A.INT), ("x", A.INT)],
                 A.Type("List"), PURE),
}

POLYMORPHIC = {"len", "str", "push", "set"}


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


BUILTIN_TYPE_NAMES = {"Int", "Bool", "Text", "Unit", "Console", "Fs", "List"}


class Checker:
    def __init__(self, program: A.Program):
        self.program = program
        self.sigs: dict[str, FnSig] = dict(BUILTINS)
        self.records: dict[str, A.RecordDecl] = {}
        self.enums: dict[str, A.EnumDecl] = {}
        # variant name -> (enum name, payload types); variant names are
        # GLOBALLY unique, so a bare uppercase name resolves unambiguously.
        self.variants: dict[str, tuple[str, list[A.Type]]] = {}
        # How many while loops enclose the statement being checked. `break`
        # is legal only when this is positive; if/match arms inside a loop
        # count as inside it.
        self.loop_depth = 0

    # ------------------------------------------------------------ entry

    def check(self) -> dict[str, FnSig]:
        self.declare_records()
        self.declare_enums()
        self.check_record_fields()
        self.check_enum_payloads()
        self.check_size_cycles()
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
            fn.params = [(pname, self.resolve_enum_refs(ptype))
                         for pname, ptype in fn.params]
            fn.ret = self.resolve_enum_refs(fn.ret)
            if fn.type_params:
                self.resolve_fn_type_params(fn)
            self.sigs[fn.name] = FnSig(fn.name, fn.params, fn.ret, fn.effects,
                                       fn, fn.type_params)

        for fn in self.program.functions:
            self.check_fn(fn)
        return self.sigs

    # ------------------------------------------------------------ records

    def declare_records(self) -> None:
        for rec in self.program.records:
            if not last_segment(rec.name)[0].isupper():
                raise CheckError(
                    f"record name '{rec.name}' must start with an uppercase "
                    f"letter (Sigil's one canonical style)", rec.line, rec.col)
            if last_segment(rec.name) in BUILTIN_TYPE_NAMES:
                raise CheckError(f"'{rec.name}' is a builtin type",
                                 rec.line, rec.col)
            if rec.name in self.records:
                raise CheckError(f"record '{rec.name}' is already defined",
                                 rec.line, rec.col)
            self.records[rec.name] = rec

    def check_record_fields(self) -> None:
        for rec in self.program.records:
            seen: set[str] = set()
            resolved: list[tuple[str, A.Type]] = []
            for fname, ftype in rec.fields:
                if fname in seen:
                    raise CheckError(
                        f"duplicate field '{fname}' in record '{rec.name}'",
                        rec.line, rec.col)
                seen.add(fname)
                ftype = self.resolve_enum_refs(ftype)
                self.validate_type(ftype, rec.line, rec.col)
                resolved.append((fname, ftype))
            rec.fields = resolved

    # ------------------------------------------------------------ enums

    def declare_enums(self) -> None:
        for enum in self.program.enums:
            if not last_segment(enum.name)[0].isupper():
                raise CheckError(
                    f"enum name '{enum.name}' must start with an uppercase "
                    f"letter (Sigil's one canonical style)", enum.line, enum.col)
            if last_segment(enum.name) in BUILTIN_TYPE_NAMES:
                raise CheckError(f"'{enum.name}' is a builtin type",
                                 enum.line, enum.col)
            if enum.name in self.records:
                raise CheckError(
                    f"enum '{enum.name}' collides with record '{enum.name}'",
                    enum.line, enum.col)
            if enum.name in self.enums:
                raise CheckError(f"enum '{enum.name}' is already defined",
                                 enum.line, enum.col)
            self.enums[enum.name] = enum

        for enum in self.program.enums:
            if not enum.variants:
                raise CheckError(
                    f"enum '{enum.name}' must declare at least one variant",
                    enum.line, enum.col)
            for vname, _ in enum.variants:
                if not last_segment(vname)[0].isupper():
                    raise CheckError(
                        f"variant name '{vname}' must start with an uppercase "
                        f"letter (Sigil's one canonical style)",
                        enum.line, enum.col)
                if vname in self.records:
                    raise CheckError(
                        f"variant '{vname}' of enum '{enum.name}' collides "
                        f"with record '{vname}'", enum.line, enum.col)
                if vname in self.variants:
                    other = self.variants[vname][0]
                    raise CheckError(
                        f"variant '{vname}' is already declared in enum "
                        f"'{other}'; variant names are global", enum.line, enum.col)
                self.variants[vname] = (enum.name, [])

    def check_enum_payloads(self) -> None:
        for enum in self.program.enums:
            resolved: list[tuple[str, list[A.Type]]] = []
            for vname, payloads in enum.variants:
                types = [self.resolve_enum_refs(p) for p in payloads]
                for ptype in types:
                    self.validate_type(ptype, enum.line, enum.col)
                self.variants[vname] = (enum.name, types)
                resolved.append((vname, types))
            enum.variants = resolved

    def check_size_cycles(self) -> None:
        # Direct containment cycles (through records AND enums) have infinite
        # size. Recursion through List is fine (it is heap-indirected), so
        # trees are expressible.
        def direct_deps(name: str) -> list[str]:
            if name in self.records:
                types = [t for _, t in self.records[name].fields]
            else:
                types = [t for _, payloads in self.enums[name].variants
                         for t in payloads]
            return [t.name for t in types if t.kind in ("Record", "Enum")]

        for start, decl in {**self.records, **self.enums}.items():
            what = "record" if start in self.records else "enum"
            stack, visited = [start], set()
            while stack:
                current = stack.pop()
                for dep in direct_deps(current):
                    if dep == start:
                        raise CheckError(
                            f"{what} '{start}' contains itself (directly or "
                            f"via other records or enums), which would be "
                            f"infinite; use List for recursion",
                            decl.line, decl.col)
                    if dep not in visited:
                        visited.add(dep)
                        stack.append(dep)

    # ------------------------------------------------------------ types

    def resolve_enum_refs(self, ty: A.Type) -> A.Type:
        """The parser cannot tell a record reference from an enum reference;
        reinterpret parsed Record types whose name is a declared enum."""
        if ty.kind == "Record" and ty.name in self.enums:
            return A.Type("Enum", name=ty.name)
        if ty.kind == "List" and ty.elem is not None:
            return A.Type("List", self.resolve_enum_refs(ty.elem))
        return ty

    def validate_type(self, ty: A.Type, line: int, col: int) -> None:
        if ty.kind == "Record" and ty.name not in self.records:
            raise CheckError(f"unknown record '{ty.name}'", line, col)
        if ty.kind == "Enum" and ty.name not in self.enums:
            raise CheckError(f"unknown enum '{ty.name}'", line, col)
        if ty.kind == "List" and ty.elem is not None:
            self.validate_type(ty.elem, line, col)

    def contains_capability(self, ty: A.Type,
                            visiting: Optional[set[str]] = None) -> bool:
        if ty.kind in A.CAPABILITY_KINDS:
            return True
        if ty.kind == "List":
            return ty.elem is not None and self.contains_capability(ty.elem, visiting)
        # Records and enums share one visited set: their names cannot collide.
        if ty.kind == "Record":
            visiting = visiting or set()
            if ty.name in visiting:
                return False
            visiting.add(ty.name)
            return any(self.contains_capability(ftype, visiting)
                       for _, ftype in self.records[ty.name].fields)
        if ty.kind == "Enum":
            visiting = visiting or set()
            if ty.name in visiting:
                return False
            visiting.add(ty.name)
            return any(self.contains_capability(ptype, visiting)
                       for _, payloads in self.enums[ty.name].variants
                       for ptype in payloads)
        return False

    # ------------------------------------------------------------ generics

    def resolve_fn_type_params(self, fn: A.FnDecl) -> None:
        """Validate a generic function's type parameters and reinterpret the
        parsed types: a Record reference whose name is a type parameter is
        really a type variable."""
        seen: set[str] = set()
        for tp in fn.type_params:
            if not tp[0].isupper():
                raise CheckError(
                    f"type parameter '{tp}' must start with an uppercase "
                    f"letter (Sigil's one canonical style)", fn.line, fn.col)
            if tp in seen:
                raise CheckError(
                    f"duplicate type parameter '{tp}' in '{fn.name}'",
                    fn.line, fn.col)
            seen.add(tp)
            if tp in self.records:
                raise CheckError(
                    f"type parameter '{tp}' of '{fn.name}' collides with "
                    f"record '{tp}'; pick another name", fn.line, fn.col)
            if tp in self.enums:
                raise CheckError(
                    f"type parameter '{tp}' of '{fn.name}' collides with "
                    f"enum '{tp}'; pick another name", fn.line, fn.col)
            if tp in BUILTIN_TYPE_NAMES:
                raise CheckError(
                    f"type parameter '{tp}' of '{fn.name}' collides with the "
                    f"builtin type '{tp}'; pick another name", fn.line, fn.col)

        fn.params = [(pname, self.resolve_type(ptype, fn.type_params))
                     for pname, ptype in fn.params]
        fn.ret = self.resolve_type(fn.ret, fn.type_params)

        # Sigil has no call-site instantiation syntax, so every type parameter
        # must be inferable from the arguments alone.
        used: set[str] = set()
        for _, ptype in fn.params:
            used |= self.collect_vars(ptype)
        for tp in fn.type_params:
            if tp not in used:
                raise CheckError(
                    f"type parameter '{tp}' of '{fn.name}' does not appear in "
                    f"any parameter type, so no call site could ever infer it",
                    fn.line, fn.col)

    def resolve_type(self, ty: A.Type, type_params: list[str]) -> A.Type:
        if ty.kind == "Record" and ty.name in type_params:
            return A.Type("Var", name=ty.name)
        if ty.kind == "List" and ty.elem is not None:
            return A.Type("List", self.resolve_type(ty.elem, type_params))
        return ty

    def collect_vars(self, ty: A.Type) -> set[str]:
        if ty.kind == "Var":
            return {ty.name}
        if ty.kind == "List" and ty.elem is not None:
            return self.collect_vars(ty.elem)
        return set()

    # ------------------------------------------------------------ functions

    def check_fn(self, fn: A.FnDecl) -> None:
        if not last_segment(fn.name)[0].islower():
            raise CheckError(
                f"function name '{fn.name}' must start with a lowercase "
                f"letter (Sigil's one canonical style)", fn.line, fn.col)
        self.validate_type(fn.ret, fn.line, fn.col)
        scope = Scope()
        seen_params: set[str] = set()
        for pname, ptype in fn.params:
            if not pname[0].islower():
                raise CheckError(
                    f"parameter '{pname}' must start with a lowercase letter",
                    fn.line, fn.col)
            if pname in seen_params:
                raise CheckError(f"duplicate parameter '{pname}' in '{fn.name}'",
                                 fn.line, fn.col)
            seen_params.add(pname)
            self.validate_type(ptype, fn.line, fn.col)
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
            # A match is exhaustive (the checker enforced it), so it returns
            # definitely iff every arm's body does.
            if isinstance(stmt, A.Match) and stmt.arms:
                if all(self.definitely_returns(arm.body) for arm in stmt.arms):
                    return True
        return False

    # ------------------------------------------------------------ statements

    def check_block(self, stmts: list[A.Stmt], scope: Scope, fn: A.FnDecl) -> None:
        for stmt in stmts:
            self.check_stmt(stmt, scope, fn)

    def check_stmt(self, stmt: A.Stmt, scope: Scope, fn: A.FnDecl) -> None:
        if isinstance(stmt, A.Let):
            if not stmt.name[0].islower():
                raise CheckError(
                    f"binding '{stmt.name}' must start with a lowercase letter",
                    stmt.line, stmt.col)
            stmt.declared_type = self.resolve_enum_refs(stmt.declared_type)
            if fn.type_params:
                stmt.declared_type = self.resolve_type(stmt.declared_type,
                                                       fn.type_params)
            self.validate_type(stmt.declared_type, stmt.line, stmt.col)
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
            # Invariants see the enclosing scope at the loop, not the body's.
            for inv in stmt.invariants:
                itype = self.check_expr(inv.expr, scope, fn, in_contract=True)
                if itype != A.BOOL:
                    raise CheckError(
                        f"invariant clause must be Bool, got {itype}",
                        inv.line, inv.col)
            self.loop_depth += 1
            self.check_block(stmt.body, Scope(scope), fn)
            self.loop_depth -= 1
            return

        if isinstance(stmt, A.Break):
            if self.loop_depth == 0:
                raise CheckError(
                    "break outside of a loop ('break' exits the innermost "
                    "enclosing while loop)", stmt.line, stmt.col)
            return

        if isinstance(stmt, A.Match):
            self.check_match(stmt, scope, fn)
            return

        if isinstance(stmt, A.ExprStmt):
            self.check_expr(stmt.expr, scope, fn)
            return

        raise CheckError(f"unhandled statement {type(stmt).__name__}",
                         stmt.line, stmt.col)

    def check_match(self, stmt: A.Match, scope: Scope, fn: A.FnDecl) -> None:
        stype = self.check_expr(stmt.scrutinee, scope, fn)
        if stype.kind != "Enum":
            raise CheckError(f"match needs an enum value, got {stype}",
                             stmt.line, stmt.col)
        enum = self.enums[stype.name]
        payloads_of = dict(enum.variants)
        covered: set[str] = set()
        wildcard: Optional[A.MatchArm] = None
        for arm in stmt.arms:
            if wildcard is not None:
                raise CheckError(
                    "wildcard '_' arm must be the last arm of a match",
                    arm.line, arm.col)
            if arm.variant is None:
                wildcard = arm
                arm.binder_types = []
                self.check_block(arm.body, Scope(scope), fn)
                continue
            payloads = payloads_of.get(arm.variant)
            if payloads is None:
                raise CheckError(
                    f"'{arm.variant}' is not a variant of enum '{stype.name}'",
                    arm.line, arm.col)
            if arm.variant in covered:
                raise CheckError(f"duplicate arm for variant '{arm.variant}'",
                                 arm.line, arm.col)
            covered.add(arm.variant)
            if len(arm.binders) != len(payloads):
                raise CheckError(
                    f"variant '{arm.variant}' has {len(payloads)} payload(s); "
                    f"this arm binds {len(arm.binders)}", arm.line, arm.col)
            arm_scope = Scope(scope)
            for binder, ptype in zip(arm.binders, payloads):
                if not binder[0].islower():
                    raise CheckError(
                        f"binder '{binder}' must start with a lowercase letter",
                        arm.line, arm.col)
                arm_scope.declare(binder, Binding(ptype, mutable=False),
                                  arm.line, arm.col)
            # Payloads are concrete, but stamp the (trivially) substituted
            # types so downstream stages never re-derive them.
            arm.binder_types = list(payloads)
            self.check_block(arm.body, Scope(arm_scope), fn)

        missing = [vname for vname, _ in enum.variants if vname not in covered]
        if wildcard is not None and not missing:
            raise CheckError(
                "wildcard '_' arm is dead: every variant is already covered",
                wildcard.line, wildcard.col)
        if wildcard is None and missing:
            raise CheckError(
                f"match on '{stype.name}' is not exhaustive; missing "
                f"variant(s): {', '.join(missing)} (or add a '_' arm)",
                stmt.line, stmt.col)
        stmt.enum_name = stype.name

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
                variant = self.variants.get(expr.name)
                if variant is not None:
                    enum_name, payloads = variant
                    if payloads:
                        raise CheckError(
                            f"variant '{expr.name}' takes "
                            f"{len(payloads)} argument(s)", expr.line, expr.col)
                    # Nullary variant: stamp for the interpreter and emitter.
                    expr.variant_of = enum_name
                    return A.Type("Enum", name=enum_name)
                hint = ""
                if last_segment(expr.name)[0].isupper() and self.enums:
                    hint = " (no enum declares a variant with this name)"
                raise CheckError(f"unknown name '{expr.name}'{hint}",
                                 expr.line, expr.col)
            return binding.type

        if isinstance(expr, A.Call):
            return self.check_call(expr, scope, fn, in_contract)

        if isinstance(expr, A.RecordLit):
            rec = self.records.get(expr.name)
            if rec is None:
                raise CheckError(f"unknown record '{expr.name}'",
                                 expr.line, expr.col)
            written = [fname for fname, _ in expr.fields]
            declared = [fname for fname, _ in rec.fields]
            if written != declared:
                raise CheckError(
                    f"'{expr.name}' literal must list exactly its fields in "
                    f"declaration order: {', '.join(declared)} (got: "
                    f"{', '.join(written) if written else 'none'})",
                    expr.line, expr.col)
            for (fname, fexpr), (_, ftype) in zip(expr.fields, rec.fields):
                atype = self.check_expr(fexpr, scope, fn, in_contract)
                if not A.compatible(ftype, atype):
                    raise CheckError(
                        f"field '{fname}' of '{expr.name}' needs {ftype}, "
                        f"got {atype}", fexpr.line, fexpr.col)
            return A.Type("Record", name=expr.name)

        if isinstance(expr, A.IfExpr):
            ctype = self.check_expr(expr.cond, scope, fn, in_contract)
            if ctype != A.BOOL:
                raise CheckError(
                    f"if-expression condition must be Bool, got {ctype}",
                    expr.line, expr.col)
            ttype = self.check_expr(expr.then_expr, scope, fn, in_contract)
            etype = self.check_expr(expr.else_expr, scope, fn, in_contract)
            if not (A.compatible(ttype, etype) or A.compatible(etype, ttype)):
                raise CheckError(
                    f"if-expression branches must produce one type; got "
                    f"{ttype} and {etype}", expr.line, expr.col)
            # Prefer the more specific branch type (a List with a known
            # element type beats an empty list literal's List[None]).
            merged = self.merge_binding(ttype, etype)
            return merged if merged is not None else ttype

        if isinstance(expr, A.MatchExpr):
            return self.check_match_expr(expr, scope, fn, in_contract)

        if isinstance(expr, A.RecordUpdate):
            btype = self.check_expr(expr.base, scope, fn, in_contract)
            if btype.kind != "Record":
                raise CheckError(
                    f"'with' updates a record value; got {btype}",
                    expr.line, expr.col)
            rec = self.records[btype.name]
            if not expr.fields:
                raise CheckError(
                    f"a 'with' update must change at least one field of "
                    f"'{btype.name}'", expr.line, expr.col)
            declared = [fname for fname, _ in rec.fields]
            seen: set[str] = set()
            for fname, fexpr in expr.fields:
                if fname not in declared:
                    raise CheckError(
                        f"record '{btype.name}' has no field '{fname}' "
                        f"(fields: {', '.join(declared)})",
                        fexpr.line, fexpr.col)
                if fname in seen:
                    raise CheckError(
                        f"duplicate field '{fname}' in 'with' update",
                        fexpr.line, fexpr.col)
                seen.add(fname)
            written = [fname for fname, _ in expr.fields]
            in_order = [fname for fname in declared if fname in seen]
            if written != in_order:
                raise CheckError(
                    f"'with' update must list fields in declaration order: "
                    f"{', '.join(in_order)} (got: {', '.join(written)})",
                    expr.line, expr.col)
            ftype_of = dict(rec.fields)
            for fname, fexpr in expr.fields:
                atype = self.check_expr(fexpr, scope, fn, in_contract)
                if not A.compatible(ftype_of[fname], atype):
                    raise CheckError(
                        f"field '{fname}' of '{btype.name}' needs "
                        f"{ftype_of[fname]}, got {atype}", fexpr.line, fexpr.col)
            return btype

        if isinstance(expr, A.FieldAccess):
            btype = self.check_expr(expr.base, scope, fn, in_contract)
            if btype.kind != "Record":
                raise CheckError(
                    f"only record values have fields; got {btype}",
                    expr.line, expr.col)
            rec = self.records[btype.name]
            for fname, ftype in rec.fields:
                if fname == expr.field_name:
                    return ftype
            raise CheckError(
                f"record '{btype.name}' has no field '{expr.field_name}' "
                f"(fields: {', '.join(f for f, _ in rec.fields)})",
                expr.line, expr.col)

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
                if A.contains_var(ltype) or A.contains_var(rtype):
                    generic = ltype if A.contains_var(ltype) else rtype
                    raise CheckError(
                        f"cannot compare values of generic type {generic} "
                        f"(directly or inside a list); their concrete type is "
                        f"unknown here", expr.line, expr.col)
                if self.contains_capability(ltype) or self.contains_capability(rtype):
                    raise CheckError(
                        "capability values cannot be compared (directly or "
                        "inside a record or list)", expr.line, expr.col)
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

    def check_match_expr(self, expr: A.MatchExpr, scope: Scope, fn: A.FnDecl,
                         in_contract: bool) -> A.Type:
        """The statement form's rules verbatim — enum scrutinee, each variant
        at most once, wildcard last, exhaustive, no dead wildcard, binder
        arity/canon, binders scoped to their arm — plus IfExpr's branch rule:
        every arm's expression produces ONE type (more-specific-wins merge)."""
        stype = self.check_expr(expr.scrutinee, scope, fn, in_contract)
        if stype.kind != "Enum":
            raise CheckError(f"match needs an enum value, got {stype}",
                             expr.line, expr.col)
        enum = self.enums[stype.name]
        payloads_of = dict(enum.variants)
        covered: set[str] = set()
        wildcard: Optional[A.MatchExprArm] = None
        result: Optional[A.Type] = None
        for arm in expr.arms:
            if wildcard is not None:
                raise CheckError(
                    "wildcard '_' arm must be the last arm of a match",
                    arm.line, arm.col)
            if arm.variant is None:
                wildcard = arm
                arm.binder_types = []
                atype = self.check_expr(arm.expr, Scope(scope), fn, in_contract)
            else:
                payloads = payloads_of.get(arm.variant)
                if payloads is None:
                    raise CheckError(
                        f"'{arm.variant}' is not a variant of enum "
                        f"'{stype.name}'", arm.line, arm.col)
                if arm.variant in covered:
                    raise CheckError(
                        f"duplicate arm for variant '{arm.variant}'",
                        arm.line, arm.col)
                covered.add(arm.variant)
                if len(arm.binders) != len(payloads):
                    raise CheckError(
                        f"variant '{arm.variant}' has {len(payloads)} "
                        f"payload(s); this arm binds {len(arm.binders)}",
                        arm.line, arm.col)
                arm_scope = Scope(scope)
                for binder, ptype in zip(arm.binders, payloads):
                    if not binder[0].islower():
                        raise CheckError(
                            f"binder '{binder}' must start with a lowercase "
                            f"letter", arm.line, arm.col)
                    arm_scope.declare(binder, Binding(ptype, mutable=False),
                                      arm.line, arm.col)
                arm.binder_types = list(payloads)
                atype = self.check_expr(arm.expr, arm_scope, fn, in_contract)
            if result is None:
                result = atype
            else:
                if not (A.compatible(result, atype)
                        or A.compatible(atype, result)):
                    raise CheckError(
                        f"match-expression arms must produce one type; got "
                        f"{result} and {atype}", arm.line, arm.col)
                # Prefer the more specific type (a List with a known element
                # type beats an empty list literal's List[None]) — the same
                # merge if-expression branches use.
                merged = self.merge_binding(result, atype)
                result = merged if merged is not None else result

        missing = [vname for vname, _ in enum.variants if vname not in covered]
        if wildcard is not None and not missing:
            raise CheckError(
                "wildcard '_' arm is dead: every variant is already covered",
                wildcard.line, wildcard.col)
        if wildcard is None and missing:
            raise CheckError(
                f"match on '{stype.name}' is not exhaustive; missing "
                f"variant(s): {', '.join(missing)} (or add a '_' arm)",
                expr.line, expr.col)
        expr.enum_name = stype.name  # the emitter reads this
        # An enum has at least one variant, so an armless match was rejected
        # as non-exhaustive above: result is always set here.
        return result

    # ------------------------------------------------------------ calls

    def check_call(self, expr: A.Call, scope: Scope, fn: A.FnDecl,
                   in_contract: bool) -> A.Type:
        # Variant construction resolves BEFORE function lookup. There is no
        # ambiguity: function names must start lowercase, variants uppercase.
        if expr.name in self.variants:
            return self.check_variant_call(expr, scope, fn, in_contract)
        if expr.name in self.records:
            raise CheckError(
                f"record '{expr.name}' is not callable; use "
                f"{expr.name} {{ ... }}", expr.line, expr.col)

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

        if sig.type_params:
            return self.check_generic_call(expr, sig, arg_types)

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

    def check_variant_call(self, expr: A.Call, scope: Scope, fn: A.FnDecl,
                           in_contract: bool) -> A.Type:
        """Construction of an enum variant. Pure — no effects to cover, so
        construction is legal anywhere, contracts included."""
        enum_name, payloads = self.variants[expr.name]
        arg_types = [self.check_expr(a, scope, fn, in_contract)
                     for a in expr.args]
        if len(arg_types) != len(payloads):
            raise CheckError(
                f"variant '{expr.name}' takes {len(payloads)} argument(s), "
                f"got {len(arg_types)}", expr.line, expr.col)
        for idx, (ptype, atype, arg) in enumerate(zip(payloads, arg_types,
                                                      expr.args)):
            if not A.compatible(ptype, atype):
                raise CheckError(
                    f"payload {idx + 1} of variant '{expr.name}' needs "
                    f"{ptype}, got {atype}", arg.line, arg.col)
        expr.variant_of = enum_name  # the interpreter and emitter read this
        return A.Type("Enum", name=enum_name)

    def check_generic_call(self, expr: A.Call, sig: FnSig,
                           arg_types: list[A.Type]) -> A.Type:
        """Infer each type parameter by unifying declared parameter types
        against the actual argument types, then re-check the arguments under
        the inferred bindings. The bindings are stamped onto the Call node
        (expr.type_bindings) for the monomorphizing native backend."""
        bindings: dict[str, A.Type] = {}
        for (pname, ptype), atype in zip(sig.params, arg_types):
            self.unify(ptype, atype, bindings, expr)

        for (pname, ptype), atype, arg in zip(sig.params, arg_types, expr.args):
            concrete = A.substitute(ptype, bindings)
            if not A.compatible(concrete, atype):
                raise CheckError(
                    f"argument '{pname}' of '{expr.name}' needs {concrete}, "
                    f"got {atype}", arg.line, arg.col)

        for tp in sig.type_params:
            bound = bindings.get(tp)
            if bound is None or not self.fully_known(bound):
                raise CheckError(
                    f"cannot infer {tp} for this call of '{expr.name}' (an "
                    f"empty list literal carries no element type)",
                    expr.line, expr.col)

        expr.type_bindings = bindings
        return A.substitute(sig.ret, bindings)

    def unify(self, ptype: A.Type, atype: A.Type,
              bindings: dict[str, A.Type], expr: A.Call) -> None:
        """Bind type variables in a declared parameter type against the actual
        argument type. Only binding conflicts are raised here; plain type
        mismatches get the standard argument error from the caller."""
        if ptype.kind == "Var":
            existing = bindings.get(ptype.name)
            if existing is None:
                bindings[ptype.name] = atype
                return
            merged = self.merge_binding(existing, atype)
            if merged is None:
                raise CheckError(
                    f"conflicting types for {ptype.name} in this call of "
                    f"'{expr.name}': {existing} and {atype}",
                    expr.line, expr.col)
            bindings[ptype.name] = merged
            return
        if ptype.kind == "List" and atype.kind == "List":
            if ptype.elem is not None and atype.elem is not None:
                self.unify(ptype.elem, atype.elem, bindings, expr)

    def merge_binding(self, a: A.Type, b: A.Type) -> Optional[A.Type]:
        """The more specific of two bindings for one type parameter, or None
        when they conflict. A List with unknown element type (an empty list
        literal) defers to the other side."""
        if a.kind == "List" and b.kind == "List":
            if a.elem is None:
                return b
            if b.elem is None:
                return a
            elem = self.merge_binding(a.elem, b.elem)
            return A.Type("List", elem) if elem is not None else None
        if a.kind != b.kind:
            return None
        if a.kind in ("Record", "Var"):
            return a if a.name == b.name else None
        return a

    def fully_known(self, ty: A.Type) -> bool:
        """A binding still containing an unknown List element (from an empty
        list literal) cannot drive monomorphization."""
        if ty.kind == "List":
            return ty.elem is not None and self.fully_known(ty.elem)
        return True

    def check_polymorphic(self, expr: A.Call, arg_types: list[A.Type]) -> A.Type:
        name = expr.name
        if name == "len":
            if len(arg_types) != 1 or arg_types[0].kind not in ("List", "Text"):
                raise CheckError("len takes one List or Text argument",
                                 expr.line, expr.col)
            return A.INT
        if name == "str":
            if len(arg_types) != 1 or arg_types[0] not in (A.INT, A.BOOL, A.TEXT):
                got = f", got {arg_types[0]}" if len(arg_types) == 1 else ""
                raise CheckError(f"str takes one Int, Bool, or Text argument{got}",
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
        if name == "set":
            if len(arg_types) != 3 or arg_types[0].kind != "List":
                raise CheckError("set takes a List, an Int index, and an element",
                                 expr.line, expr.col)
            xs, i, x = arg_types
            if i != A.INT:
                raise CheckError(f"set index must be Int, got {i}",
                                 expr.line, expr.col)
            if xs.elem is not None and not A.compatible(xs.elem, x):
                raise CheckError(
                    f"cannot set {x} into {xs}", expr.line, expr.col)
            return A.Type("List", xs.elem if xs.elem is not None else x)
        raise CheckError(f"unhandled polymorphic builtin '{name}'",
                         expr.line, expr.col)


def check(program: A.Program) -> dict[str, FnSig]:
    return Checker(program).check()
