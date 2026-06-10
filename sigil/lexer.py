"""Lexer for Sigil. Produces tokens with absolute offsets so later stages can
quote exact source spans (used to print contract clauses in violations)."""

from dataclasses import dataclass

from .errors import LexError

KEYWORDS = {
    "fn", "let", "var", "return", "if", "else", "while",
    "true", "false", "requires", "ensures", "and", "or", "not",
}

# Multi-char operators first so maximal munch works.
OPERATORS = [
    "->", "==", "!=", "<=", ">=",
    "+", "-", "*", "/", "%", "<", ">", "=", "!",
    "(", ")", "{", "}", "[", "]", ",", ";", ":", ".",
]


@dataclass(frozen=True)
class Token:
    kind: str        # 'INT' | 'TEXT' | 'IDENT' | keyword | operator | 'EOF'
    value: str
    line: int
    col: int
    pos: int         # absolute start offset in source
    end: int         # absolute end offset (exclusive)


def lex(source: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    line = 1
    col = 1
    n = len(source)

    def advance(count: int) -> None:
        nonlocal i, line, col
        for _ in range(count):
            if source[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        ch = source[i]

        if ch in " \t\r\n":
            advance(1)
            continue

        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                advance(1)
            continue

        start_line, start_col, start_pos = line, col, i

        if ch.isdigit():
            j = i
            while j < n and source[j].isdigit():
                j += 1
            text = source[i:j]
            advance(j - i)
            tokens.append(Token("INT", text, start_line, start_col, start_pos, j))
            continue

        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            text = source[i:j]
            advance(j - i)
            kind = text if text in KEYWORDS else "IDENT"
            tokens.append(Token(kind, text, start_line, start_col, start_pos, j))
            continue

        if ch == '"':
            j = i + 1
            chars: list[str] = []
            while True:
                if j >= n or source[j] == "\n":
                    raise LexError("unterminated text literal", start_line, start_col)
                c = source[j]
                if c == '"':
                    j += 1
                    break
                if c == "\\":
                    if j + 1 >= n:
                        raise LexError("unterminated escape in text literal", start_line, start_col)
                    esc = source[j + 1]
                    mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
                    if esc not in mapping:
                        raise LexError(f"unknown escape '\\{esc}'", start_line, start_col)
                    chars.append(mapping[esc])
                    j += 2
                    continue
                chars.append(c)
                j += 1
            advance(j - i)
            tokens.append(Token("TEXT", "".join(chars), start_line, start_col, start_pos, j))
            continue

        for op in OPERATORS:
            if source.startswith(op, i):
                advance(len(op))
                tokens.append(Token(op, op, start_line, start_col, start_pos, start_pos + len(op)))
                break
        else:
            raise LexError(f"unexpected character {ch!r}", line, col)

    tokens.append(Token("EOF", "", line, col, n, n))
    return tokens
