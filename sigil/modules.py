"""Module resolver for Sigil (roadmap 0.7).

Multi-file Sigil flattens to ONE single-file Program before the checker runs,
so every downstream stage — checker, interpreter, verifier, native backend —
stays name-agnostic. ``load_program`` is the single entry path used by the
check / run / verify / build pipeline; a file with no ``use`` declarations
flattens to itself unchanged.

The scheme:
  * ``use geometry { area, Shape, parse as parse_shape }`` resolves
    ``geometry.sg`` in the importing file's directory. Every name that enters
    scope is written out (no glob imports) — the header IS the audit surface.
  * Only ``pub`` declarations are importable. Importing an enum imports its
    variant names too (match needs them); variants cannot be aliased and
    cannot be imported individually.
  * Declarations of the ENTRY module keep their bare names (``main`` stays
    ``main``; entry-local diagnostics read clean). Every other module's
    declarations are qualified as ``<module>.<name>`` — enum variants
    included — and the module's internal references are rewritten to match.
    Qualified names survive into diagnostics, verifier findings, and contract
    blame on purpose: an auditor sees exactly which module's bargain failed.
  * Imported names must not collide with local declarations, builtins, or
    other imports: no shadowing, consistent with the language.
"""

import os
from dataclasses import dataclass, field

from . import ast_nodes as A
from .checker import BUILTIN_TYPE_NAMES, BUILTINS
from .errors import ModuleError, SigilError
from .parser import parse


def load_program(entry_path: str) -> A.Program:
    """Parse the entry file, resolve its use graph (each module loaded once,
    cycles rejected), and flatten everything into one Program. Runs BEFORE
    the checker; the result is an unchecked single program."""
    return _Loader().load(entry_path)


@dataclass
class _DeclInfo:
    kind: str                                   # 'function' | 'record' | 'enum'
    public: bool
    qualified: str
    variants: list[str] = field(default_factory=list)  # bare names (enums)


@dataclass
class _Module:
    name: str
    path: str
    program: A.Program
    decls: dict[str, _DeclInfo]
    variant_owner: dict[str, str]   # bare variant name -> bare enum name


class _Loader:
    def __init__(self):
        self.cache: dict[str, _Module] = {}   # abs path -> resolved module
        self.loading: list[tuple[str, str]] = []  # DFS stack: (abs path, name)
        self.order: list[_Module] = []        # post-order: dependencies first

    def load(self, entry_path: str) -> A.Program:
        self._load_module(entry_path, is_entry=True)
        functions: list[A.FnDecl] = []
        records: list[A.RecordDecl] = []
        enums: list[A.EnumDecl] = []
        for module in self.order:
            functions.extend(module.program.functions)
            records.extend(module.program.records)
            enums.extend(module.program.enums)
        return A.Program(functions, records, enums)

    # ------------------------------------------------------------ one module

    def _load_module(self, path: str, is_entry: bool, line: int = 0,
                     col: int = 0, importer: str | None = None) -> _Module:
        key = os.path.normcase(os.path.abspath(path))
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        name = os.path.splitext(os.path.basename(path))[0]
        keys = [k for k, _ in self.loading]
        if key in keys:
            chain = [n for _, n in self.loading[keys.index(key):]] + [name]
            raise ModuleError("import cycle: " + " -> ".join(chain),
                              line, col, importer)

        try:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
        except OSError as exc:
            raise ModuleError(f"cannot read {path}: {exc}", line, col, importer)

        try:
            program = parse(source)
        except SigilError as exc:
            exc.path = path   # blame the file that failed, not the entry
            raise

        self.loading.append((key, name))
        try:
            deps: dict[str, _Module] = {}
            for use in program.uses:
                dep_path = os.path.join(
                    os.path.dirname(os.path.abspath(path)),
                    use.module + ".sg")
                if not os.path.isfile(dep_path):
                    raise ModuleError(
                        f"module '{use.module}' not found "
                        f"(tried '{dep_path}')", use.line, use.col, path)
                deps[use.module] = self._load_module(
                    dep_path, is_entry=False,
                    line=use.line, col=use.col, importer=path)
        finally:
            self.loading.pop()

        module = self._resolve(name, path, program, deps, is_entry)
        self.cache[key] = module
        self.order.append(module)
        return module

    # ------------------------------------------------------------ resolution

    def _resolve(self, name: str, path: str, program: A.Program,
                 deps: dict[str, _Module], is_entry: bool) -> _Module:
        prefix = "" if is_entry else name + "."

        decls: dict[str, _DeclInfo] = {}
        variant_owner: dict[str, str] = {}
        for fn in program.functions:
            if not is_entry and fn.name in BUILTINS:
                # The checker rejects this for the entry module; qualification
                # would hide it for everyone else, so reject it here.
                raise ModuleError(f"'{fn.name}' is already defined as a "
                                  f"builtin", fn.line, fn.col, path)
            decls[fn.name] = _DeclInfo("function", fn.public, prefix + fn.name)
        for rec in program.records:
            decls[rec.name] = _DeclInfo("record", rec.public, prefix + rec.name)
        for enum in program.enums:
            decls[enum.name] = _DeclInfo("enum", enum.public,
                                         prefix + enum.name,
                                         [vname for vname, _ in enum.variants])
            for vname, _ in enum.variants:
                variant_owner[vname] = enum.name

        import_map = self._import_map(name, path, program, deps,
                                      decls, variant_owner)
        local_names = set(decls) | set(variant_owner)
        _Rewriter(name, path, is_entry, local_names, import_map).run(program)
        return _Module(name, path, program, decls, variant_owner)

    def _import_map(self, name: str, path: str, program: A.Program,
                    deps: dict[str, _Module], decls: dict[str, _DeclInfo],
                    variant_owner: dict[str, str]) -> dict[str, str]:
        import_map: dict[str, str] = {}

        def bind(local: str, target: str, what: str,
                 line: int, col: int) -> None:
            if local in decls or local in variant_owner:
                raise ModuleError(
                    f"{what} collides with a declaration in this file; Sigil "
                    f"forbids shadowing (every name means one thing to an "
                    f"auditor)", line, col, path)
            if local in BUILTINS or local in BUILTIN_TYPE_NAMES:
                raise ModuleError(
                    f"{what} collides with the builtin '{local}'",
                    line, col, path)
            if local in import_map:
                raise ModuleError(
                    f"{what} collides with another import "
                    f"('{import_map[local]}')", line, col, path)
            import_map[local] = target

        for use in program.uses:
            dep = deps[use.module]
            for item, alias in use.items:
                info = dep.decls.get(item)
                if info is None:
                    owner = dep.variant_owner.get(item)
                    if owner is not None:
                        raise ModuleError(
                            f"'{item}' is a variant of enum '{owner}'; import "
                            f"the enum and its variants come with it "
                            f"(variants cannot be imported individually)",
                            use.line, use.col, path)
                    raise ModuleError(
                        f"module '{use.module}' has no declaration named "
                        f"'{item}'", use.line, use.col, path)
                if not info.public:
                    raise ModuleError(
                        f"module '{use.module}' does not export '{item}' "
                        f"(mark it pub to allow this)", use.line, use.col, path)
                if alias is not None:
                    if info.kind == "function" and not alias[0].islower():
                        raise ModuleError(
                            f"alias '{alias}' for function '{item}' must "
                            f"start with a lowercase letter (Sigil's one "
                            f"canonical style)", use.line, use.col, path)
                    if info.kind != "function" and not alias[0].isupper():
                        raise ModuleError(
                            f"alias '{alias}' for {info.kind} '{item}' must "
                            f"start with an uppercase letter (Sigil's one "
                            f"canonical style)", use.line, use.col, path)
                local = alias if alias is not None else item
                bind(local, info.qualified, f"imported name '{local}'",
                     use.line, use.col)
                if info.kind == "enum":
                    for vname in info.variants:
                        bind(vname, f"{dep.name}.{vname}",
                             f"variant '{vname}' of imported enum '{item}'",
                             use.line, use.col)
        return import_map


class _Rewriter:
    """Rewrites one module's AST in place: its own declarations get their
    qualified names, its imports resolve through its import map, builtins
    stay bare. In a non-entry module anything else is an error HERE, with the
    module's own file to blame — a module's meaning must not depend on what
    the importing program happens to declare."""

    def __init__(self, module: str, path: str, is_entry: bool,
                 local_names: set[str], import_map: dict[str, str]):
        self.module = module
        self.path = path
        self.is_entry = is_entry
        self.local_names = local_names
        self.import_map = import_map
        self.type_params: set[str] = set()   # active generic fn's parameters

    def qualify(self, name: str) -> str:
        return name if self.is_entry else f"{self.module}.{name}"

    def resolve(self, name: str, what: str, line: int, col: int) -> str:
        if name in self.local_names:
            return self.qualify(name)
        mapped = self.import_map.get(name)
        if mapped is not None:
            return mapped
        if name in BUILTINS or name in BUILTIN_TYPE_NAMES:
            return name
        if self.is_entry:
            return name   # the checker reports unknown names with positions
        raise ModuleError(
            f"module '{self.module}' references unknown {what} '{name}' "
            f"(not declared in this file, not imported)",
            line, col, self.path)

    # ------------------------------------------------------------ traversal

    def run(self, program: A.Program) -> None:
        for rec in program.records:
            rec.fields = [(fname, self.rewrite_type(ftype, rec.line, rec.col))
                          for fname, ftype in rec.fields]
            rec.name = self.qualify(rec.name)
        for enum in program.enums:
            enum.variants = [
                (self.qualify(vname),
                 [self.rewrite_type(p, enum.line, enum.col) for p in payloads])
                for vname, payloads in enum.variants]
            enum.name = self.qualify(enum.name)
        for fn in program.functions:
            self.type_params = set(fn.type_params)
            for tp in fn.type_params:
                if tp in self.import_map:
                    raise ModuleError(
                        f"type parameter '{tp}' of '{fn.name}' collides with "
                        f"imported '{self.import_map[tp]}'; pick another name",
                        fn.line, fn.col, self.path)
            fn.params = [(pname, self.rewrite_type(ptype, fn.line, fn.col))
                         for pname, ptype in fn.params]
            fn.ret = self.rewrite_type(fn.ret, fn.line, fn.col)
            for contract in fn.contracts:
                self.rewrite_expr(contract.expr)
            self.rewrite_block(fn.body)
            fn.name = self.qualify(fn.name)
            self.type_params = set()
        program.uses = []

    def rewrite_type(self, ty: A.Type, line: int, col: int) -> A.Type:
        if ty.kind == "List" and ty.elem is not None:
            return A.Type("List", self.rewrite_type(ty.elem, line, col))
        # The parser produces kind 'Record' for every user type name; a name
        # bound by the enclosing fn's type parameters is a type variable and
        # never module-qualified.
        if ty.kind == "Record" and ty.name not in self.type_params:
            return A.Type("Record",
                          name=self.resolve(ty.name, "type", line, col))
        return ty

    def rewrite_block(self, stmts: list[A.Stmt]) -> None:
        for stmt in stmts:
            self.rewrite_stmt(stmt)

    def rewrite_stmt(self, stmt: A.Stmt) -> None:
        if isinstance(stmt, A.Let):
            stmt.declared_type = self.rewrite_type(stmt.declared_type,
                                                   stmt.line, stmt.col)
            self.rewrite_expr(stmt.value)
        elif isinstance(stmt, A.Assign):
            self.rewrite_expr(stmt.value)
        elif isinstance(stmt, A.Return):
            if stmt.value is not None:
                self.rewrite_expr(stmt.value)
        elif isinstance(stmt, A.If):
            self.rewrite_expr(stmt.cond)
            self.rewrite_block(stmt.then_body)
            if stmt.else_body is not None:
                self.rewrite_block(stmt.else_body)
        elif isinstance(stmt, A.While):
            self.rewrite_expr(stmt.cond)
            for inv in stmt.invariants:
                self.rewrite_expr(inv.expr)
            self.rewrite_block(stmt.body)
        elif isinstance(stmt, A.Match):
            self.rewrite_expr(stmt.scrutinee)
            for arm in stmt.arms:
                if arm.variant is not None:
                    arm.variant = self.resolve(arm.variant, "variant",
                                               arm.line, arm.col)
                self.rewrite_block(arm.body)
        elif isinstance(stmt, A.ExprStmt):
            self.rewrite_expr(stmt.expr)

    def rewrite_expr(self, expr: A.Expr) -> None:
        if isinstance(expr, A.Var):
            # Lowercase names are local bindings (parameters, lets, binders,
            # 'result'); an uppercase bare name is a nullary variant.
            if expr.name[0].isupper():
                expr.name = self.resolve(expr.name, "name",
                                         expr.line, expr.col)
        elif isinstance(expr, A.Call):
            for arg in expr.args:
                self.rewrite_expr(arg)
            what = "variant" if expr.name[0].isupper() else "function"
            expr.name = self.resolve(expr.name, what, expr.line, expr.col)
        elif isinstance(expr, A.RecordLit):
            expr.name = self.resolve(expr.name, "record", expr.line, expr.col)
            for _, fexpr in expr.fields:
                self.rewrite_expr(fexpr)
        elif isinstance(expr, A.Binary):
            self.rewrite_expr(expr.left)
            self.rewrite_expr(expr.right)
        elif isinstance(expr, A.Unary):
            self.rewrite_expr(expr.operand)
        elif isinstance(expr, A.Index):
            self.rewrite_expr(expr.base)
            self.rewrite_expr(expr.index)
        elif isinstance(expr, A.ListLit):
            for item in expr.items:
                self.rewrite_expr(item)
