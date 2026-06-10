"""Error types for the Sigil toolchain.

Every error carries a source position so tooling (and LLMs generating code
against the checker) gets machine-usable feedback.
"""


class SigilError(Exception):
    def __init__(self, message: str, line: int = 0, col: int = 0):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def render(self, filename: str = "<source>") -> str:
        return f"{filename}:{self.line}:{self.col}: {self.LABEL}: {self.message}"

    LABEL = "error"


class LexError(SigilError):
    LABEL = "lex error"


class ParseError(SigilError):
    LABEL = "parse error"


class CheckError(SigilError):
    LABEL = "check error"


class ModuleError(SigilError):
    """The module loader could not resolve an import: missing file, cycle,
    visibility (not pub), alias canon, or a name collision. Carries the path
    of the file that caused it so diagnostics blame the right file."""
    LABEL = "module error"

    def __init__(self, message: str, line: int = 0, col: int = 0,
                 path: str | None = None):
        super().__init__(message, line, col)
        self.path = path


class RuntimeFault(SigilError):
    LABEL = "runtime fault"


class CapabilityFault(SigilError):
    """An attenuated capability refused an action outside its scope."""
    LABEL = "capability fault"


class ContractViolation(SigilError):
    LABEL = "contract violation"

    def __init__(self, message: str, line: int = 0, col: int = 0, blame: str = ""):
        super().__init__(message, line, col)
        self.blame = blame
