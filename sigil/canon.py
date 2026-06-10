"""Canonical form for Sigil (roadmap 0.4).

Three pieces live here:

1. The canonical formatter: ``format_source`` renders THE one canonical text
   for a program. Fully deterministic; idempotent; parsing the output yields
   an AST equal (modulo positions and comments) to parsing the input.

2. Stable declaration identities: every declaration gets an ``id`` (sha256 of
   its canonical rendering, first 12 hex chars) and a ``shape`` (same hash,
   but with the declaration's *own* name replaced by ``@`` in the rendering,
   so a pure rename keeps its shape). ``program_json`` serializes the whole
   typed AST, declarations stamped with both.

3. Semantic diff: ``sdiff`` compares two programs declaration-by-declaration
   using those ids/shapes and classifies each change.

Comment policy (the lexer collects comments; they are not AST nodes):
  - Standalone comment lines are re-emitted, in order, immediately above the
    next statement/declaration in source order, at that construct's indent.
  - Trailing same-line comments (``let x: Int = 1; // note``) are HOISTED
    onto their own line above the statement they trailed.
  - Comment text is normalized to ``// `` + content (inner whitespace kept).
  - Comments inside record bodies, fn headers, or after the last statement
    of a block attach to the nearest emitted construct (the declaration
    itself, or the next declaration/statement); they migrate but are never
    destroyed. Comments after all code attach at end of file.
"""

import dataclasses
import hashlib
import json
import re

from . import ast_nodes as A
from .lexer import Comment, lex_with_comments
from .parser import Parser

# Precedence levels, mirroring the parser's recursive-descent ladder.
# Higher binds tighter.
_LEVEL = {
    "or": 1,
    "and": 2,
    "==": 3, "!=": 3, "<": 3, "<=": 3, ">": 3, ">=": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5, "%": 5,
}
_IF_LEVEL = 0       # if-expressions bind loosest of all expressions
_UNARY_LEVEL = 6
_POSTFIX_LEVEL = 7
_WITH_LEVEL = 6     # `x with { ... }` follows the postfix chain, below it

# Any expression rendered immediately before a block's `{` and ending in a
# bare uppercase identifier (a nullary variant) must keep parentheses:
# `match x == Empty {` / `if x == Empty {` / a last `invariant y == Empty`
# would all re-parse with `Empty {` as a record literal swallowing the block.
_TRAILING_NAME = re.compile(r"[A-Za-z0-9_]+$")


def _scrutinee_needs_parens(text: str) -> bool:
    tail = _TRAILING_NAME.search(text)
    return tail is not None and tail.group()[0].isupper()


def _guard_before_block(text: str) -> str:
    return f"({text})" if _scrutinee_needs_parens(text) else text


def escape_text(value: str) -> str:
    """Canonical re-escaping of a Text literal's content."""
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{{{ord(ch):x}}}")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- renderer

class Renderer:
    """Renders AST nodes in canonical style.

    ``placeholder_for`` substitutes ``@`` for one identifier wherever it is
    *referenced as a name* (fn header, self-calls, record header, record
    literals, type positions) — used for shape hashes.
    """

    def __init__(self, placeholder_for: str | None = None):
        self.placeholder_for = placeholder_for

    def _name(self, name: str) -> str:
        return "@" if name == self.placeholder_for else name

    def _flush(self, node, depth: int) -> list[str]:
        """Comment hook; the formatting subclass emits attached comments."""
        return []

    # ---------------------------------------------------------- types

    def render_type(self, ty: A.Type) -> str:
        if ty.kind == "List":
            elem = "?" if ty.elem is None else self.render_type(ty.elem)
            return f"List[{elem}]"
        if ty.kind in ("Record", "Enum", "Var"):
            return self._name(ty.name)
        return ty.kind

    # ---------------------------------------------------------- expressions

    def render_expr(self, expr: A.Expr, context: int = 0) -> str:
        """Render with minimal parentheses: parenthesize exactly when this
        node binds looser than the position it appears in."""
        text, level = self._expr(expr)
        if level < context:
            return f"({text})"
        return text

    def _expr(self, expr: A.Expr) -> tuple[str, int]:
        if isinstance(expr, A.IntLit):
            return str(expr.value), _POSTFIX_LEVEL
        if isinstance(expr, A.BoolLit):
            return ("true" if expr.value else "false"), _POSTFIX_LEVEL
        if isinstance(expr, A.TextLit):
            return f'"{escape_text(expr.value)}"', _POSTFIX_LEVEL
        if isinstance(expr, A.Var):
            return expr.name, _POSTFIX_LEVEL
        if isinstance(expr, A.ListLit):
            items = ", ".join(self.render_expr(e) for e in expr.items)
            return f"[{items}]", _POSTFIX_LEVEL
        if isinstance(expr, A.Call):
            args = ", ".join(self.render_expr(a) for a in expr.args)
            return f"{self._name(expr.name)}({args})", _POSTFIX_LEVEL
        if isinstance(expr, A.RecordLit):
            if not expr.fields:
                return f"{self._name(expr.name)} {{}}", _POSTFIX_LEVEL
            fields = ", ".join(f"{fname}: {self.render_expr(fexpr)}"
                               for fname, fexpr in expr.fields)
            return f"{self._name(expr.name)} {{ {fields} }}", _POSTFIX_LEVEL
        if isinstance(expr, A.Index):
            base = self.render_expr(expr.base, _POSTFIX_LEVEL)
            return f"{base}[{self.render_expr(expr.index)}]", _POSTFIX_LEVEL
        if isinstance(expr, A.FieldAccess):
            base = self.render_expr(expr.base, _POSTFIX_LEVEL)
            return f"{base}.{expr.field_name}", _POSTFIX_LEVEL
        if isinstance(expr, A.Unary):
            operand = self.render_expr(expr.operand, _UNARY_LEVEL)
            text = f"not {operand}" if expr.op == "not" else f"-{operand}"
            return text, _UNARY_LEVEL
        if isinstance(expr, A.Binary):
            level = _LEVEL[expr.op]
            # All binary operators parse left-associatively, so an
            # equal-precedence child needs no parens on the left but DOES on
            # the right (a - (b - c) must keep its parens to round-trip).
            left = self.render_expr(expr.left, level)
            right = self.render_expr(expr.right, level + 1)
            return f"{left} {expr.op} {right}", level
        if isinstance(expr, A.IfExpr):
            # Lowest precedence: parenthesized as the operand of anything.
            # A nested if-expression keeps parens in cond/then position but
            # chains bare in else position (`... else if c then a else b`).
            cond = self.render_expr(expr.cond, _IF_LEVEL + 1)
            then = self.render_expr(expr.then_expr, _IF_LEVEL + 1)
            els = self.render_expr(expr.else_expr, _IF_LEVEL)
            return f"if {cond} then {then} else {els}", _IF_LEVEL
        if isinstance(expr, A.RecordUpdate):
            # The base is a postfix-chain value: a bare name, call, index, or
            # field access needs no parens; anything looser (an if-expression,
            # an inner `with`) does.
            base = self.render_expr(expr.base, _POSTFIX_LEVEL)
            fields = ", ".join(f"{fname}: {self.render_expr(fexpr)}"
                               for fname, fexpr in expr.fields)
            return f"{base} with {{ {fields} }}", _WITH_LEVEL
        raise TypeError(f"unknown expression node {type(expr).__name__}")

    # ---------------------------------------------------------- declarations

    def render_use(self, use: A.UseDecl) -> str:
        items = ", ".join(name if alias is None else f"{name} as {alias}"
                          for name, alias in use.items)
        return f"use {use.module} {{ {items} }}"

    def fn_signature(self, fn: A.FnDecl) -> str:
        """The header line, without contracts or the opening brace."""
        pub = "pub " if fn.public else ""
        type_params = f"[{', '.join(fn.type_params)}]" if fn.type_params else ""
        params = ", ".join(f"{name}: {self.render_type(ty)}"
                           for name, ty in fn.params)
        header = f"{pub}fn {self._name(fn.name)}{type_params}({params}) -> {self.render_type(fn.ret)}"
        if fn.effects:
            header += f" ! {{{', '.join(sorted(fn.effects))}}}"
        return header

    def render_fn(self, fn: A.FnDecl) -> list[str]:
        lines = []
        header = self.fn_signature(fn)
        if fn.contracts:
            lines.append(header)
            # The lexer is newline-blind, so the LAST clause sits directly
            # before the body's '{' and needs the nullary-variant guard.
            for index, contract in enumerate(fn.contracts):
                rendered = self.render_expr(contract.expr)
                if index == len(fn.contracts) - 1:
                    rendered = _guard_before_block(rendered)
                lines.append(f"    {contract.kind} {rendered}")
            lines.append("{")
        else:
            lines.append(header + " {")
        lines.extend(self.render_stmts(fn.body, 1))
        lines.append("}")
        return lines

    def render_record(self, rec: A.RecordDecl) -> list[str]:
        pub = "pub " if rec.public else ""
        if not rec.fields:
            return [f"{pub}record {self._name(rec.name)} {{}}"]
        lines = [f"{pub}record {self._name(rec.name)} {{"]
        for fname, ty in rec.fields:
            lines.append(f"    {fname}: {self.render_type(ty)},")
        lines.append("}")
        return lines

    def render_enum(self, enum: A.EnumDecl) -> list[str]:
        pub = "pub " if enum.public else ""
        if not enum.variants:
            return [f"{pub}enum {self._name(enum.name)} {{}}"]
        lines = [f"{pub}enum {self._name(enum.name)} {{"]
        for vname, payloads in enum.variants:
            if payloads:
                types = ", ".join(self.render_type(p) for p in payloads)
                lines.append(f"    {vname}({types}),")
            else:
                lines.append(f"    {vname},")
        lines.append("}")
        return lines

    def render_decl(self, decl: "A.FnDecl | A.RecordDecl | A.EnumDecl") -> str:
        if isinstance(decl, A.RecordDecl):
            return "\n".join(self.render_record(decl))
        if isinstance(decl, A.EnumDecl):
            return "\n".join(self.render_enum(decl))
        return "\n".join(self.render_fn(decl))

    # ---------------------------------------------------------- statements

    def render_stmts(self, stmts: list[A.Stmt], depth: int) -> list[str]:
        lines: list[str] = []
        for stmt in stmts:
            lines.extend(self.render_stmt(stmt, depth))
        return lines

    def render_stmt(self, stmt: A.Stmt, depth: int) -> list[str]:
        pad = "    " * depth
        if isinstance(stmt, A.Let):
            kw = "var" if stmt.mutable else "let"
            return [f"{pad}{kw} {stmt.name}: {self.render_type(stmt.declared_type)}"
                    f" = {self.render_expr(stmt.value)};"]
        if isinstance(stmt, A.Assign):
            return [f"{pad}{stmt.name} = {self.render_expr(stmt.value)};"]
        if isinstance(stmt, A.Return):
            if stmt.value is None:
                return [f"{pad}return;"]
            return [f"{pad}return {self.render_expr(stmt.value)};"]
        if isinstance(stmt, A.Break):
            return [f"{pad}break;"]
        if isinstance(stmt, A.ExprStmt):
            # Statement position parses the `if` KEYWORD as the statement
            # form, so a bare if-expression cannot be an expression statement
            # — it keeps its parentheses (context just above _IF_LEVEL).
            return [f"{pad}{self.render_expr(stmt.expr, _IF_LEVEL + 1)};"]
        if isinstance(stmt, A.If):
            return self._render_if(stmt, depth)
        if isinstance(stmt, A.Match):
            return self._render_match(stmt, depth)
        if isinstance(stmt, A.While):
            lines = []
            if stmt.invariants:
                lines.append(f"{pad}while {self.render_expr(stmt.cond)}")
                for index, inv in enumerate(stmt.invariants):
                    rendered = self.render_expr(inv.expr)
                    if index == len(stmt.invariants) - 1:
                        rendered = _guard_before_block(rendered)
                    lines.append(f"{pad}    invariant {rendered}")
                lines.append(f"{pad}{{")
            else:
                cond = _guard_before_block(self.render_expr(stmt.cond))
                lines.append(f"{pad}while {cond} {{")
            lines.extend(self.render_stmts(stmt.body, depth + 1))
            lines.append(f"{pad}}}")
            return lines
        raise TypeError(f"unknown statement node {type(stmt).__name__}")

    def _render_match(self, stmt: A.Match, depth: int) -> list[str]:
        pad = "    " * depth
        scrutinee = self.render_expr(stmt.scrutinee)
        if _scrutinee_needs_parens(scrutinee):
            scrutinee = f"({scrutinee})"
        lines = [f"{pad}match {scrutinee} {{"]
        for arm in stmt.arms:
            lines.extend(self._flush(arm, depth + 1))
            if arm.variant is None:
                pattern = "_"
            elif arm.binders:
                pattern = f"{arm.variant}({', '.join(arm.binders)})"
            else:
                pattern = arm.variant
            lines.append(f"{pad}    {pattern} => {{")
            lines.extend(self.render_stmts(arm.body, depth + 2))
            lines.append(f"{pad}    }}")
        lines.append(f"{pad}}}")
        return lines

    def _render_if(self, stmt: A.If, depth: int) -> list[str]:
        pad = "    " * depth
        cond = _guard_before_block(self.render_expr(stmt.cond))
        lines = [f"{pad}if {cond} {{"]
        lines.extend(self.render_stmts(stmt.then_body, depth + 1))
        node = stmt
        # `else { if ... }` with a lone nested if is indistinguishable from
        # `else if` in the AST; both render as the canonical else-if chain.
        while node.else_body is not None:
            if len(node.else_body) == 1 and isinstance(node.else_body[0], A.If):
                node = node.else_body[0]
                lines.extend(self._flush(node, depth))
                cond = _guard_before_block(self.render_expr(node.cond))
                lines.append(f"{pad}}} else if {cond} {{")
                lines.extend(self.render_stmts(node.then_body, depth + 1))
            else:
                lines.append(f"{pad}}} else {{")
                lines.extend(self.render_stmts(node.else_body, depth + 1))
                break
        lines.append(f"{pad}}}")
        return lines


# ---------------------------------------------------------------- formatter

class _CommentingRenderer(Renderer):
    """Renderer that flushes attached comments above each construct."""

    def __init__(self, buckets: dict[int, list[Comment]]):
        super().__init__()
        self.buckets = buckets

    def _flush(self, node, depth: int) -> list[str]:
        pad = "    " * depth
        return [f"{pad}// {c.text}".rstrip() for c in self.buckets.get(id(node), ())]

    def render_stmt(self, stmt: A.Stmt, depth: int) -> list[str]:
        return self._flush(stmt, depth) + super().render_stmt(stmt, depth)

    def render_fn(self, fn: A.FnDecl) -> list[str]:
        return self._flush(fn, 0) + super().render_fn(fn)

    def render_record(self, rec: A.RecordDecl) -> list[str]:
        return self._flush(rec, 0) + super().render_record(rec)

    def render_enum(self, enum: A.EnumDecl) -> list[str]:
        return self._flush(enum, 0) + super().render_enum(enum)


def _walk_anchors(program: A.Program) -> list[tuple[int, int, object]]:
    """Every construct the formatter emits comments above, in source order:
    declarations and (recursively) statements, keyed by (line, col)."""
    anchors: list[tuple[int, int, object]] = []

    def stmts(body: list[A.Stmt]) -> None:
        for stmt in body:
            anchors.append((stmt.line, stmt.col, stmt))
            if isinstance(stmt, A.If):
                stmts(stmt.then_body)
                if stmt.else_body is not None:
                    stmts(stmt.else_body)
            elif isinstance(stmt, A.While):
                stmts(stmt.body)
            elif isinstance(stmt, A.Match):
                for arm in stmt.arms:
                    anchors.append((arm.line, arm.col, arm))
                    stmts(arm.body)

    for use in program.uses:
        anchors.append((use.line, use.col, use))
    for decl in _decls_in_source_order(program):
        anchors.append((decl.line, decl.col, decl))
        if isinstance(decl, A.FnDecl):
            stmts(decl.body)
    anchors.sort(key=lambda a: (a[0], a[1]))
    return anchors


def _decls_in_source_order(program: A.Program) -> list:
    decls = list(program.records) + list(program.enums) + list(program.functions)
    decls.sort(key=lambda d: (d.line, d.col))
    return decls


def _assign_comments(comments: list[Comment],
                     anchors: list[tuple[int, int, object]],
                     ) -> tuple[dict[int, list[Comment]], list[Comment]]:
    """Bucket each comment under the construct it is emitted above.

    Standalone comments attach to the next construct that starts after them;
    trailing comments attach to (are hoisted above) the last construct that
    starts at or before them. Comments after all code go to the EOF bucket.
    """
    buckets: dict[int, list[Comment]] = {}
    eof: list[Comment] = []
    for comment in comments:
        key = (comment.line, comment.col)
        target = None
        if comment.trailing:
            for a_line, a_col, node in anchors:
                if (a_line, a_col) <= key:
                    target = node
                else:
                    break
        else:
            for a_line, a_col, node in anchors:
                if (a_line, a_col) > key:
                    target = node
                    break
        if target is None:
            eof.append(comment)
        else:
            buckets.setdefault(id(target), []).append(comment)
    return buckets, eof


def format_source(source: str) -> str:
    """Render the canonical text of a Sigil program (with its comments)."""
    parser = Parser(source)
    _, comments = lex_with_comments(source)
    program = parser.parse_program()
    return format_program(program, comments)


def format_program(program: A.Program, comments: list[Comment] | None = None) -> str:
    comments = comments or []
    buckets, eof = _assign_comments(comments, _walk_anchors(program))
    renderer = _CommentingRenderer(buckets)
    chunks: list[str] = []
    if program.uses:
        # The import header is one chunk: use lines first (the parser already
        # normalized their order), then exactly one blank line before the
        # first declaration (chunks join with a blank line).
        lines: list[str] = []
        for use in program.uses:
            lines.extend(renderer._flush(use, 0))
            lines.append(renderer.render_use(use))
        chunks.append("\n".join(lines))
    for decl in _decls_in_source_order(program):
        chunks.append(renderer.render_decl(decl))
    if eof:
        chunks.append("\n".join(f"// {c.text}".rstrip() for c in eof))
    return "\n\n".join(chunks) + "\n" if chunks else ""


# ---------------------------------------------------------------- ast_equal

_IGNORED_FIELDS = {"line", "col", "pos", "end", "source", "proven"}


def ast_equal(a, b) -> bool:
    """Structural AST equality, ignoring positions (line/col), contract
    source spans (presentation, not semantics), verifier stamps, and any
    non-field attributes stages may have attached."""
    if dataclasses.is_dataclass(a) or dataclasses.is_dataclass(b):
        if type(a) is not type(b):
            return False
        for f in dataclasses.fields(a):
            if f.name in _IGNORED_FIELDS:
                continue
            if not ast_equal(getattr(a, f.name), getattr(b, f.name)):
                return False
        return True
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
            return False
        return len(a) == len(b) and all(ast_equal(x, y) for x, y in zip(a, b))
    return a == b


# ---------------------------------------------------------------- typed AST

def decl_id(decl) -> str:
    """Stable identity: sha256 of the canonical rendering, truncated."""
    text = Renderer().render_decl(decl)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def decl_shape(decl) -> str:
    """Identity modulo the declaration's own name: a pure rename (including
    of self-recursive calls) keeps the shape; references to OTHER
    declarations keep their names and so still affect it."""
    text = Renderer(placeholder_for=decl.name).render_decl(decl)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _node_json(node, renderer: Renderer):
    if isinstance(node, A.Type):
        return renderer.render_type(node)
    if isinstance(node, A.Contract):
        return {"kind": node.kind, "source": renderer.render_expr(node.expr)}
    if dataclasses.is_dataclass(node):
        out = {"node": type(node).__name__}
        for f in dataclasses.fields(node):
            if f.name in ("line", "col", "source", "proven"):
                continue
            out[f.name] = _node_json(getattr(node, f.name), renderer)
        return out
    if isinstance(node, (list, tuple)):
        return [_node_json(item, renderer) for item in node]
    if isinstance(node, frozenset):
        return sorted(node)
    return node


def program_json(program: A.Program) -> dict:
    """The serialized typed AST: {"version": 1, "uses": [...],
    "records": [...], "enums": [...], "functions": [...]}, every declaration
    stamped with id + shape."""
    renderer = Renderer()
    records = []
    for rec in program.records:
        records.append({
            "id": decl_id(rec),
            "shape": decl_shape(rec),
            "name": rec.name,
            "public": rec.public,
            "fields": [{"name": fname, "type": renderer.render_type(ty)}
                       for fname, ty in rec.fields],
        })
    enums = []
    for enum in program.enums:
        enums.append({
            "id": decl_id(enum),
            "shape": decl_shape(enum),
            "name": enum.name,
            "public": enum.public,
            "variants": [{"name": vname,
                          "payloads": [renderer.render_type(p)
                                       for p in payloads]}
                         for vname, payloads in enum.variants],
        })
    functions = []
    for fn in program.functions:
        functions.append({
            "id": decl_id(fn),
            "shape": decl_shape(fn),
            "name": fn.name,
            "public": fn.public,
            "type_params": list(fn.type_params),
            "params": [{"name": pname, "type": renderer.render_type(ty)}
                       for pname, ty in fn.params],
            "ret": renderer.render_type(fn.ret),
            "effects": sorted(fn.effects),
            "contracts": [{"kind": c.kind, "source": renderer.render_expr(c.expr)}
                          for c in fn.contracts],
            "body": [_node_json(stmt, renderer) for stmt in fn.body],
        })
    uses = [{"module": use.module,
             "items": [{"name": name, "alias": alias}
                       for name, alias in use.items]}
            for use in program.uses]
    return {"version": 1, "uses": uses, "records": records, "enums": enums,
            "functions": functions}


def program_json_text(program: A.Program) -> str:
    return json.dumps(program_json(program), indent=2)


# ---------------------------------------------------------------- sdiff

def _decl_index(program: A.Program) -> dict[tuple[str, str], object]:
    index: dict[tuple[str, str], object] = {}
    for decl in _decls_in_source_order(program):
        if isinstance(decl, A.RecordDecl):
            kind = "record"
        elif isinstance(decl, A.EnumDecl):
            kind = "enum"
        else:
            kind = "fn"
        index[(kind, decl.name)] = decl
    return index


def _classify(kind: str, old, new) -> str:
    """Why two same-named declarations differ. Precedence:
    signature > contracts > body (rename is handled by the caller)."""
    if old.public != new.public:
        # Export status is part of a declaration's contract with the world.
        return "signature"
    if kind in ("record", "enum"):
        # A record IS its field list (and an enum its variant list); any
        # change is its body.
        return "body"
    renderer = Renderer(placeholder_for=old.name)
    new_renderer = Renderer(placeholder_for=new.name)
    if renderer.fn_signature(old) != new_renderer.fn_signature(new):
        return "signature"
    old_contracts = [(c.kind, renderer.render_expr(c.expr)) for c in old.contracts]
    new_contracts = [(c.kind, new_renderer.render_expr(c.expr)) for c in new.contracts]
    if old_contracts != new_contracts:
        return "contracts"
    return "body"


def sdiff(old: A.Program, new: A.Program) -> list[str]:
    """One line per changed declaration; empty list means no differences."""
    old_index = _decl_index(old)
    new_index = _decl_index(new)

    removed = [key for key in old_index if key not in new_index]
    added = [key for key in new_index if key not in old_index]

    # Rename detection: a shape hash shared by exactly one removed and
    # exactly one added declaration of the same kind is a rename.
    renames: list[tuple[tuple[str, str], tuple[str, str]]] = []
    removed_by_shape: dict[tuple[str, str], list[tuple[str, str]]] = {}
    added_by_shape: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key in removed:
        removed_by_shape.setdefault((key[0], decl_shape(old_index[key])), []).append(key)
    for key in added:
        added_by_shape.setdefault((key[0], decl_shape(new_index[key])), []).append(key)
    for shape_key, old_keys in removed_by_shape.items():
        new_keys = added_by_shape.get(shape_key, [])
        if len(old_keys) == 1 and len(new_keys) == 1:
            renames.append((old_keys[0], new_keys[0]))
    renamed_old = {pair[0] for pair in renames}
    renamed_new = {pair[1] for pair in renames}

    lines: list[str] = []
    for key in added:
        if key not in renamed_new:
            lines.append(f"{'added':<12}{key[0]} {key[1]}")
    for key in removed:
        if key not in renamed_old:
            lines.append(f"{'removed':<12}{key[0]} {key[1]}")
    for old_key, new_key in renames:
        lines.append(f"{'renamed':<12}{old_key[0]} {old_key[1]} -> {new_key[0]} {new_key[1]}")
    for key, old_decl in old_index.items():
        new_decl = new_index.get(key)
        if new_decl is None or decl_id(old_decl) == decl_id(new_decl):
            continue
        lines.append(f"{_classify(key[0], old_decl, new_decl):<12}{key[0]} {key[1]}")
    return lines
