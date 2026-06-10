"""Recursive-descent parser for Sigil v0.1."""

from . import ast_nodes as A
from .errors import ParseError
from .lexer import Token, lex

TYPE_NAMES = {"Int", "Bool", "Text", "Unit", "Console", "Fs"}

COMPARISON_OPS = {"==", "!=", "<", "<=", ">", ">="}


class Parser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = lex(source)
        self.i = 0

    # ------------------------------------------------------------ helpers

    @property
    def cur(self) -> Token:
        return self.tokens[self.i]

    @property
    def prev(self) -> Token:
        return self.tokens[self.i - 1]

    def at(self, kind: str) -> bool:
        return self.cur.kind == kind

    def advance(self) -> Token:
        tok = self.cur
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def expect(self, kind: str, what: str = "") -> Token:
        if self.cur.kind != kind:
            found = self.cur.value or self.cur.kind
            wanted = what or f"'{kind}'"
            raise ParseError(f"expected {wanted}, found '{found}'", self.cur.line, self.cur.col)
        return self.advance()

    def match(self, kind: str) -> bool:
        if self.at(kind):
            self.advance()
            return True
        return False

    # ------------------------------------------------------------ program

    def parse_program(self) -> A.Program:
        functions = []
        records = []
        while not self.at("EOF"):
            if self.at("record"):
                records.append(self.parse_record())
            else:
                functions.append(self.parse_fn())
        return A.Program(functions, records)

    def parse_record(self) -> A.RecordDecl:
        start = self.expect("record")
        name = self.expect("IDENT", "record name").value
        self.expect("{")
        fields: list[tuple[str, A.Type]] = []
        while not self.at("}"):
            fname = self.expect("IDENT", "field name").value
            self.expect(":")
            fields.append((fname, self.parse_type()))
            if not self.at("}"):
                self.expect(",", "',' between record fields")
        self.expect("}")
        return A.RecordDecl(name, fields, start.line, start.col)

    def parse_fn(self) -> A.FnDecl:
        start = self.expect("fn", "'fn' to start a declaration")
        name = self.expect("IDENT", "function name").value
        # Optional type parameters: fn first[T](xs: List[T]) -> T. There is no
        # call-site instantiation syntax; types are inferred from arguments.
        type_params: list[str] = []
        if self.match("["):
            while True:
                type_params.append(self.expect("IDENT", "type parameter name").value)
                if not self.match(","):
                    break
            self.expect("]")
        self.expect("(")
        params: list[tuple[str, A.Type]] = []
        if not self.at(")"):
            while True:
                pname = self.expect("IDENT", "parameter name").value
                self.expect(":")
                params.append((pname, self.parse_type()))
                if not self.match(","):
                    break
        self.expect(")")
        self.expect("->", "'->' and a return type (signatures are explicit in Sigil)")
        ret = self.parse_type()

        effects: set[str] = set()
        if self.match("!"):
            self.expect("{")
            while True:
                effects.add(self.parse_effect_name())
                if not self.match(","):
                    break
            self.expect("}")

        contracts: list[A.Contract] = []
        while self.cur.kind in ("requires", "ensures"):
            kw = self.advance()
            start_tok = self.cur
            expr = self.parse_expr()
            src = self.source[start_tok.pos:self.prev.end]
            contracts.append(A.Contract(kw.kind, expr, src, kw.line, kw.col))

        body = self.parse_block()
        return A.FnDecl(name, params, ret, frozenset(effects), contracts, body,
                        start.line, start.col, type_params)

    def parse_effect_name(self) -> str:
        first = self.expect("IDENT", "effect name (e.g. io.write)").value
        if self.match("."):
            second = self.expect("IDENT", "effect name after '.'").value
            return f"{first}.{second}"
        return first

    def parse_type(self) -> A.Type:
        tok = self.expect("IDENT", "a type name")
        name = tok.value
        if name == "List":
            self.expect("[")
            elem = self.parse_type()
            self.expect("]")
            return A.Type("List", elem)
        if name in TYPE_NAMES:
            return A.Type(name)
        if name[0].isupper():
            # A record reference; the checker verifies it is declared.
            return A.Type("Record", name=name)
        raise ParseError(f"unknown type '{name}' (record names start with an "
                         f"uppercase letter)", tok.line, tok.col)

    # ------------------------------------------------------------ statements

    def parse_block(self) -> list[A.Stmt]:
        self.expect("{")
        stmts: list[A.Stmt] = []
        while not self.at("}"):
            if self.at("EOF"):
                raise ParseError("unterminated block, expected '}'", self.cur.line, self.cur.col)
            stmts.append(self.parse_stmt())
        self.expect("}")
        return stmts

    def parse_stmt(self) -> A.Stmt:
        tok = self.cur

        if tok.kind in ("let", "var"):
            self.advance()
            name = self.expect("IDENT", "binding name").value
            self.expect(":", "':' and a type (Sigil bindings are explicitly typed)")
            ty = self.parse_type()
            self.expect("=")
            value = self.parse_expr()
            self.expect(";")
            return A.Let(tok.line, tok.col, name, ty, value, mutable=(tok.kind == "var"))

        if tok.kind == "return":
            self.advance()
            value = None if self.at(";") else self.parse_expr()
            self.expect(";")
            return A.Return(tok.line, tok.col, value)

        if tok.kind == "if":
            return self.parse_if()

        if tok.kind == "while":
            self.advance()
            cond = self.parse_expr()
            invariants: list[A.Contract] = []
            while self.at("invariant"):
                kw = self.advance()
                start_tok = self.cur
                expr = self.parse_expr()
                src = self.source[start_tok.pos:self.prev.end]
                invariants.append(A.Contract(kw.kind, expr, src, kw.line, kw.col))
            body = self.parse_block()
            return A.While(tok.line, tok.col, cond, body, invariants)

        # assignment or expression statement
        if tok.kind == "IDENT" and self.tokens[self.i + 1].kind == "=":
            self.advance()
            self.advance()
            value = self.parse_expr()
            self.expect(";")
            return A.Assign(tok.line, tok.col, tok.value, value)

        expr = self.parse_expr()
        self.expect(";")
        return A.ExprStmt(tok.line, tok.col, expr)

    def parse_if(self) -> A.If:
        tok = self.expect("if")
        cond = self.parse_expr()
        then_body = self.parse_block()
        else_body = None
        if self.match("else"):
            if self.at("if"):
                else_body = [self.parse_if()]
            else:
                else_body = self.parse_block()
        return A.If(tok.line, tok.col, cond, then_body, else_body)

    # ------------------------------------------------------------ expressions

    def parse_expr(self) -> A.Expr:
        return self.parse_or()

    def parse_or(self) -> A.Expr:
        left = self.parse_and()
        while self.at("or"):
            tok = self.advance()
            right = self.parse_and()
            left = A.Binary(tok.line, tok.col, "or", left, right)
        return left

    def parse_and(self) -> A.Expr:
        left = self.parse_comparison()
        while self.at("and"):
            tok = self.advance()
            right = self.parse_comparison()
            left = A.Binary(tok.line, tok.col, "and", left, right)
        return left

    def parse_comparison(self) -> A.Expr:
        left = self.parse_additive()
        while self.cur.kind in COMPARISON_OPS:
            tok = self.advance()
            right = self.parse_additive()
            left = A.Binary(tok.line, tok.col, tok.kind, left, right)
        return left

    def parse_additive(self) -> A.Expr:
        left = self.parse_multiplicative()
        while self.cur.kind in ("+", "-"):
            tok = self.advance()
            right = self.parse_multiplicative()
            left = A.Binary(tok.line, tok.col, tok.kind, left, right)
        return left

    def parse_multiplicative(self) -> A.Expr:
        left = self.parse_unary()
        while self.cur.kind in ("*", "/", "%"):
            tok = self.advance()
            right = self.parse_unary()
            left = A.Binary(tok.line, tok.col, tok.kind, left, right)
        return left

    def parse_unary(self) -> A.Expr:
        if self.cur.kind in ("not", "-"):
            tok = self.advance()
            operand = self.parse_unary()
            return A.Unary(tok.line, tok.col, tok.kind, operand)
        return self.parse_postfix()

    def parse_postfix(self) -> A.Expr:
        expr = self.parse_primary()
        while True:
            if self.at("["):
                tok = self.advance()
                index = self.parse_expr()
                self.expect("]")
                expr = A.Index(tok.line, tok.col, expr, index)
            elif self.at("."):
                tok = self.advance()
                fname = self.expect("IDENT", "field name after '.'").value
                expr = A.FieldAccess(tok.line, tok.col, expr, fname)
            else:
                return expr

    def parse_primary(self) -> A.Expr:
        tok = self.cur

        if tok.kind == "INT":
            self.advance()
            return A.IntLit(tok.line, tok.col, int(tok.value))
        if tok.kind == "TEXT":
            self.advance()
            return A.TextLit(tok.line, tok.col, tok.value)
        if tok.kind in ("true", "false"):
            self.advance()
            return A.BoolLit(tok.line, tok.col, tok.kind == "true")
        if tok.kind == "(":
            self.advance()
            expr = self.parse_expr()
            self.expect(")")
            return expr
        if tok.kind == "[":
            self.advance()
            items: list[A.Expr] = []
            while not self.at("]"):
                items.append(self.parse_expr())
                if not self.match(","):
                    break
            self.expect("]")
            return A.ListLit(tok.line, tok.col, items)
        if tok.kind == "IDENT":
            self.advance()
            if self.match("("):
                args: list[A.Expr] = []
                while not self.at(")"):
                    args.append(self.parse_expr())
                    if not self.match(","):
                        break
                self.expect(")")
                return A.Call(tok.line, tok.col, tok.value, args)
            # Record literal: uppercase name + '{'. Unambiguous because value
            # names are required to start lowercase, so `if flag {` can never
            # look like a record literal.
            if tok.value[0].isupper() and self.at("{"):
                self.advance()
                fields: list[tuple[str, A.Expr]] = []
                while not self.at("}"):
                    fname = self.expect("IDENT", "field name").value
                    self.expect(":")
                    fields.append((fname, self.parse_expr()))
                    if not self.at("}"):
                        self.expect(",", "',' between record fields")
                self.expect("}")
                return A.RecordLit(tok.line, tok.col, tok.value, fields)
            return A.Var(tok.line, tok.col, tok.value)

        found = tok.value or tok.kind
        raise ParseError(f"expected an expression, found '{found}'", tok.line, tok.col)


def parse(source: str) -> A.Program:
    return Parser(source).parse_program()
