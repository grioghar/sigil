"""AST node definitions for Sigil.

A design note for the roadmap: these nodes are the real program. The 0.4
milestone makes a serialized, ID-stamped form of this tree the canonical
on-disk format, with text as a projection.
"""

from dataclasses import dataclass, field
from typing import Optional, Union


# ---------------------------------------------------------------- types

@dataclass(frozen=True)
class Type:
    kind: str                      # Int | Bool | Text | Unit | Console | Fs | List | Record | Enum | Var
    elem: Optional["Type"] = None  # element type when kind == 'List'; None = unknown
    name: Optional[str] = None     # record name when kind == 'Record'; enum
                                   # name when kind == 'Enum'; type parameter
                                   # name when kind == 'Var'

    def __str__(self) -> str:
        if self.kind == "List":
            return f"List[{self.elem if self.elem is not None else '?'}]"
        if self.kind in ("Record", "Enum", "Var"):
            return self.name
        return self.kind


INT = Type("Int")
BOOL = Type("Bool")
TEXT = Type("Text")
UNIT = Type("Unit")
CONSOLE = Type("Console")
FS = Type("Fs")

CAPABILITY_KINDS = {"Console", "Fs"}


def compatible(expected: Type, actual: Type) -> bool:
    """Structural compatibility; a List with unknown element type (an empty
    list literal) is compatible with any List."""
    if expected.kind != actual.kind:
        return False
    if expected.kind == "List":
        if expected.elem is None or actual.elem is None:
            return True
        return compatible(expected.elem, actual.elem)
    if expected.kind in ("Record", "Enum", "Var"):
        return expected.name == actual.name
    return True


def contains_var(ty: Type) -> bool:
    """True when a type mentions a type variable anywhere (recursively)."""
    if ty.kind == "Var":
        return True
    if ty.kind == "List":
        return ty.elem is not None and contains_var(ty.elem)
    return False


def substitute(ty: Type, bindings: dict[str, Type]) -> Type:
    """Replace type variables with their bindings, recursively through List.
    Variables without a binding are left in place."""
    if ty.kind == "Var":
        return bindings.get(ty.name, ty)
    if ty.kind == "List" and ty.elem is not None:
        return Type("List", substitute(ty.elem, bindings))
    return ty


# ---------------------------------------------------------------- expressions

@dataclass
class Expr:
    line: int = 0
    col: int = 0


@dataclass
class IntLit(Expr):
    value: int = 0


@dataclass
class BoolLit(Expr):
    value: bool = False


@dataclass
class TextLit(Expr):
    value: str = ""


@dataclass
class ListLit(Expr):
    items: list[Expr] = field(default_factory=list)


@dataclass
class Var(Expr):
    name: str = ""


@dataclass
class Call(Expr):
    name: str = ""
    args: list[Expr] = field(default_factory=list)


@dataclass
class Binary(Expr):
    op: str = ""
    left: Expr = None
    right: Expr = None


@dataclass
class Unary(Expr):
    op: str = ""
    operand: Expr = None


@dataclass
class Index(Expr):
    base: Expr = None
    index: Expr = None


@dataclass
class RecordLit(Expr):
    name: str = ""
    fields: list[tuple[str, Expr]] = field(default_factory=list)


@dataclass
class FieldAccess(Expr):
    base: Expr = None
    field_name: str = ""


@dataclass
class IfExpr(Expr):
    """`if cond then a else b` — an expression, both branches mandatory.
    Distinct from the statement If: only the taken branch is evaluated, and
    the whole thing has a value. An if-expression can never appear as a bare
    expression statement (statement position parses the statement form), but
    that costs nothing: it would be pure and therefore useless there."""
    cond: Expr = None
    then_expr: Expr = None
    else_expr: Expr = None


@dataclass
class MatchExprArm:
    """One arm of a match-EXPRESSION: `pattern => expr`. Patterns are the
    statement form's exactly: `Variant(b1, b2)`, a nullary `Variant`, or the
    wildcard `_` (variant None) last."""
    variant: Optional[str]         # None means the wildcard arm '_'
    binders: list[str] = field(default_factory=list)
    expr: Expr = None
    line: int = 0
    col: int = 0
    # The checker stamps `binder_types` as a plain attribute, like the
    # statement form's MatchArm — deliberately NOT a dataclass field, so
    # structural AST equality ignores it.


@dataclass
class MatchExpr(Expr):
    """`match s { Variant(b) => expr, ... }` — an expression, statically
    exhaustive like the statement form. Only the selected arm's expression
    is evaluated, and the whole thing has a value; all arm expressions
    produce one type. Statement position still parses the statement form
    (arms with blocks), exactly as IfExpr defers to the statement If."""
    scrutinee: Expr = None
    arms: list[MatchExprArm] = field(default_factory=list)


@dataclass
class RecordUpdate(Expr):
    """`base with { field: expr, ... }` — functional record update. The base
    is evaluated first, then the field expressions left to right; the result
    is a copy of the base with the listed fields replaced. A single clause
    only (chaining requires parentheses around the inner update)."""
    base: Expr = None
    fields: list[tuple[str, Expr]] = field(default_factory=list)


# ---------------------------------------------------------------- statements

@dataclass
class Stmt:
    line: int = 0
    col: int = 0


@dataclass
class Let(Stmt):
    name: str = ""
    declared_type: Type = None
    value: Expr = None
    mutable: bool = False


@dataclass
class Assign(Stmt):
    name: str = ""
    value: Expr = None


@dataclass
class Return(Stmt):
    value: Optional[Expr] = None


@dataclass
class If(Stmt):
    cond: Expr = None
    then_body: list[Stmt] = field(default_factory=list)
    else_body: Optional[list[Stmt]] = None   # may contain a single nested If


@dataclass
class While(Stmt):
    cond: Expr = None
    body: list[Stmt] = field(default_factory=list)
    invariants: list["Contract"] = field(default_factory=list)


@dataclass
class Break(Stmt):
    """Exit the innermost enclosing while loop (no labels). The loop's
    invariants must hold at every exit, so a break is a check/proof site."""


@dataclass
class ExprStmt(Stmt):
    expr: Expr = None


@dataclass
class MatchArm:
    variant: Optional[str]         # None means the wildcard arm '_'
    binders: list[str] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)
    line: int = 0
    col: int = 0
    # The checker stamps `binder_types` (the payload types each binder
    # receives) as a plain attribute, like Expr.ty — deliberately NOT a
    # dataclass field, so structural AST equality ignores it.


@dataclass
class Match(Stmt):
    scrutinee: Expr = None
    arms: list[MatchArm] = field(default_factory=list)


# ---------------------------------------------------------------- declarations

@dataclass
class Contract:
    kind: str          # 'requires' | 'ensures' | 'invariant'
    expr: Expr = None
    source: str = ""   # exact source text of the clause, for blame messages
    line: int = 0
    col: int = 0
    # Set by the verifier: a proven clause needs no runtime check. For
    # 'ensures' this means every return site satisfies it; for 'requires'
    # it means every call site in the program provably satisfies it; for
    # 'invariant' it means the clause holds on loop entry and every
    # iteration of the body preserves it.
    proven: bool = False


@dataclass
class FnDecl:
    name: str
    params: list[tuple[str, Type]]
    ret: Type
    effects: frozenset[str]
    contracts: list[Contract]
    body: list[Stmt]
    line: int = 0
    col: int = 0
    type_params: list[str] = field(default_factory=list)  # generic functions
    public: bool = False                                  # 'pub' — exported


@dataclass
class RecordDecl:
    name: str
    fields: list[tuple[str, Type]]
    line: int = 0
    col: int = 0
    public: bool = False


@dataclass
class EnumDecl:
    name: str
    variants: list[tuple[str, list[Type]]]   # (variant name, payload types)
    line: int = 0
    col: int = 0
    public: bool = False


@dataclass
class UseDecl:
    """One import header line: `use geometry { area, parse as parse_shape }`.
    Items are (exported name, alias-or-None) pairs; every name that enters
    scope is written out (no glob imports — auditability)."""
    module: str
    items: list[tuple[str, Optional[str]]]
    line: int = 0
    col: int = 0


@dataclass
class Program:
    functions: list[FnDecl]
    records: list[RecordDecl] = field(default_factory=list)
    enums: list[EnumDecl] = field(default_factory=list)
    uses: list[UseDecl] = field(default_factory=list)
