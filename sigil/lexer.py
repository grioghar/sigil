"""Lexer for Sigil. Produces tokens with absolute offsets so later stages can
quote exact source spans (used to print contract clauses in violations)."""

from dataclasses import dataclass

from .errors import LexError

KEYWORDS = {
    "fn", "record", "let", "var", "return", "if", "else", "while",
    "true", "false", "requires", "ensures", "invariant", "and", "or", "not",
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


@dataclass(frozen=True)
class Comment:
    text: str        # content after '//', surrounding whitespace stripped
    line: int
    col: int
    pos: int         # absolute offset of the '//'
    trailing: bool   # True when code preceded the comment on its line


def lex(source: str) -> list[Token]:
    """Token stream only; comments are discarded (the historical contract)."""
    return lex_with_comments(source)[0]


def lex_with_comments(source: str) -> tuple[list[Token], list[Comment]]:
    """Like lex(), but also returns the comments encountered, in source
    order, so the canonical formatter can re-emit them."""
    tokens: list[Token] = []
    comments: list[Comment] = []
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
            c_line, c_col, c_pos = line, col, i
            trailing = bool(tokens) and tokens[-1].line == line
            while i < n and source[i] != "\n":
                advance(1)
            comments.append(Comment(source[c_pos + 2:i].strip(),
                                    c_line, c_col, c_pos, trailing))
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
                    if esc == "u":
                        # \u{HEX} — the canonical spelling of control chars.
                        if j + 2 >= n or source[j + 2] != "{":
                            raise LexError("expected '{' after \\u", start_line, start_col)
                        k = source.find("}", j + 3)
                        if k < 0 or k > n or "\n" in source[j + 3:k]:
                            raise LexError("unterminated \\u{...} escape", start_line, start_col)
                        digits = source[j + 3:k]
                        if not digits or any(d not in "0123456789abcdefABCDEF" for d in digits):
                            raise LexError(f"bad hex in \\u{{{digits}}}", start_line, start_col)
                        code = int(digits, 16)
                        if code > 0x10FFFF:
                            raise LexError(f"\\u{{{digits}}} is not a valid codepoint",
                                           start_line, start_col)
                        chars.append(chr(code))
                        j = k + 1
                        continue
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
    return tokens, comments
