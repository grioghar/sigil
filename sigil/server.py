"""Compiler-as-a-service for Sigil (roadmap 0.5).

A machine-facing query API: instead of generating code blind and checking it
afterwards, an LLM (or any tool) interrogates the compiler WHILE generating —
"does this check?", "what signatures exist?", "what must I still prove?".

Protocol: newline-delimited JSON over stdio. `python -m sigil serve` reads one
JSON object per line from stdin and writes exactly one JSON response object
per line to stdout (flushed after each). The server never crashes and never
emits non-JSON: malformed requests, unknown methods, missing fields, and
internal errors all become {"ok": false, "error": ...}. EOF ends the loop.

`python -m sigil query "<json>"` answers a single request and exits — handy
for scripting. The pure entry point for both (and for tests) is
handle_request(req) -> dict.
"""

import json
import sys

from . import ast_nodes as A
from .checker import BUILTINS, FnSig, check
from .errors import SigilError
from .parser import parse

DESCRIPTIONS = {
    "check": "type/effect/capability-check a program; first diagnostic on failure",
    "signatures": "every function, record, enum, and builtin signature in a program",
    "effects": "declared, capability, and transitive effects of one function",
    "verify": "prove contracts and divisions statically; all findings + summary",
    "obligations": "unproven findings only — what must still be made true",
    "methods": "list the available methods (this call; takes no source)",
}


# ------------------------------------------------------------ dispatch

def handle_request(req) -> dict:
    """Answer one request object. Never raises: every failure mode becomes
    {"ok": false, "error": ...} so the serving loop cannot crash."""
    try:
        return _dispatch(req)
    except Exception as exc:  # the protocol absorbs everything
        return {"ok": False, "error": f"internal error: {exc}"}


def _dispatch(req) -> dict:
    if not isinstance(req, dict):
        return {"ok": False, "error": "request must be a JSON object"}
    method = req.get("method")
    if not isinstance(method, str):
        return {"ok": False, "error": "missing or non-text 'method' field"}
    if method == "methods":
        return {"ok": True, "methods": sorted(DESCRIPTIONS),
                "descriptions": DESCRIPTIONS}
    if method not in DESCRIPTIONS:
        return {"ok": False,
                "error": f"unknown method '{method}'; "
                         f"send {{\"method\": \"methods\"}} for the list"}

    source = req.get("source")
    if not isinstance(source, str):
        return {"ok": False, "error": "missing or non-text 'source' field"}

    if method == "check":
        return _method_check(source)
    if method == "signatures":
        return _method_signatures(source)
    if method == "effects":
        fn = req.get("fn")
        if not isinstance(fn, str):
            return {"ok": False, "error": "missing or non-text 'fn' field"}
        return _method_effects(source, fn)
    if method == "verify":
        return _method_verify(source)
    return _method_obligations(source)


def _check_failure(exc: SigilError) -> dict:
    """Sigil stops at the first lex/parse/check error; the list shape leaves
    room for multi-error reporting later."""
    return {"ok": False,
            "diagnostics": [{"line": exc.line, "col": exc.col,
                             "label": exc.LABEL, "message": exc.message}]}


# ------------------------------------------------------------ methods

def _method_check(source: str) -> dict:
    try:
        check(parse(source))
    except SigilError as exc:
        return _check_failure(exc)
    return {"ok": True, "diagnostics": []}


def _fn_json(sig: FnSig) -> dict:
    return {"name": sig.name,
            "type_params": list(sig.type_params),
            "params": [{"name": pname, "type": str(ptype)}
                       for pname, ptype in sig.params],
            "ret": str(sig.ret),
            "effects": sorted(sig.effects)}


def _method_signatures(source: str) -> dict:
    try:
        program = parse(source)
        sigs = check(program)
    except SigilError as exc:
        return _check_failure(exc)

    functions = []
    for fn in program.functions:
        entry = _fn_json(sigs[fn.name])
        entry["contracts"] = [{"kind": c.kind, "source": c.source}
                              for c in fn.contracts]
        functions.append(entry)
    records = [{"name": rec.name,
                "fields": [{"name": fname, "type": str(ftype)}
                           for fname, ftype in rec.fields]}
               for rec in program.records]
    enums = [{"name": enum.name,
              "variants": [{"name": vname,
                            "payloads": [str(ptype) for ptype in payloads]}
                           for vname, payloads in enum.variants]}
             for enum in program.enums]
    builtins = [_fn_json(sig) for sig in BUILTINS.values()]
    return {"ok": True, "functions": functions, "records": records,
            "enums": enums, "builtins": builtins}


def _called_names(stmts: list[A.Stmt]) -> set[str]:
    """Names of every function called anywhere in a body (direct calls only;
    Sigil has no first-class functions, so this is the whole call graph)."""
    names: set[str] = set()

    def walk_expr(expr: A.Expr) -> None:
        if isinstance(expr, A.Call):
            names.add(expr.name)
            for arg in expr.args:
                walk_expr(arg)
        elif isinstance(expr, A.Binary):
            walk_expr(expr.left)
            walk_expr(expr.right)
        elif isinstance(expr, A.Unary):
            walk_expr(expr.operand)
        elif isinstance(expr, A.Index):
            walk_expr(expr.base)
            walk_expr(expr.index)
        elif isinstance(expr, A.FieldAccess):
            walk_expr(expr.base)
        elif isinstance(expr, A.ListLit):
            for item in expr.items:
                walk_expr(item)
        elif isinstance(expr, A.RecordLit):
            for _, fexpr in expr.fields:
                walk_expr(fexpr)

    def walk_stmt(stmt: A.Stmt) -> None:
        if isinstance(stmt, (A.Let, A.Assign)):
            walk_expr(stmt.value)
        elif isinstance(stmt, A.Return):
            if stmt.value is not None:
                walk_expr(stmt.value)
        elif isinstance(stmt, A.If):
            walk_expr(stmt.cond)
            for s in stmt.then_body:
                walk_stmt(s)
            for s in stmt.else_body or []:
                walk_stmt(s)
        elif isinstance(stmt, A.While):
            walk_expr(stmt.cond)
            for s in stmt.body:
                walk_stmt(s)
        elif isinstance(stmt, A.Match):
            walk_expr(stmt.scrutinee)
            for arm in stmt.arms:
                for s in arm.body:
                    walk_stmt(s)
        elif isinstance(stmt, A.ExprStmt):
            walk_expr(stmt.expr)

    for stmt in stmts:
        walk_stmt(stmt)
    return names


def _method_effects(source: str, fn_name: str) -> dict:
    try:
        sigs = check(parse(source))
    except SigilError as exc:
        return _check_failure(exc)
    sig = sigs.get(fn_name)
    if sig is None:
        return {"ok": False, "error": f"unknown function '{fn_name}'"}

    # Transitive effects: union of declared effects of everything reachable
    # from this function through direct calls (builtins included). Comparing
    # against `declared` reveals over-declaration: an effect declared but not
    # in `transitive` is never actually demanded by any callee.
    transitive: set[str] = set()
    seen: set[str] = set()
    stack = list(_called_names(sig.decl.body)) if sig.decl is not None else []
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        callee = sigs.get(name)
        if callee is None:
            continue
        transitive |= callee.effects
        if callee.decl is not None:
            stack.extend(_called_names(callee.decl.body))

    capabilities = [str(ptype) for _, ptype in sig.params
                    if ptype.kind in A.CAPABILITY_KINDS]
    return {"ok": True, "fn": fn_name, "declared": sorted(sig.effects),
            "capabilities": capabilities, "transitive": sorted(transitive)}


def _run_verifier(source: str):
    """(findings, failure) — exactly one is None. Findings are JSON-ready and
    sorted by (fn, line) like the CLI report."""
    from .verify import verify
    try:
        program = parse(source)
        check(program)
    except SigilError as exc:
        return None, _check_failure(exc)
    report = verify(program)
    if report is None:
        return None, {"ok": False,
                      "error": "verifier unavailable: pip install z3-solver"}
    findings = [{"fn": f.fn, "kind": f.kind, "source": f.source,
                 "proven": f.proven, "line": f.line}
                for f in sorted(report.findings, key=lambda f: (f.fn, f.line))]
    return (findings, report), None


def _method_verify(source: str) -> dict:
    result, failure = _run_verifier(source)
    if failure is not None:
        return failure
    findings, report = result
    return {"ok": True, "findings": findings,
            "summary": {"contracts_proven": report.contracts_proven,
                        "contracts_total": report.contracts_total,
                        "divisions_proven": report.divisions_proven,
                        "divisions_total": report.divisions_total}}


def _method_obligations(source: str) -> dict:
    """THE method for an AI author: what must I still make true? An empty
    list means the program is fully proven."""
    result, failure = _run_verifier(source)
    if failure is not None:
        return failure
    findings, _ = result
    return {"ok": True,
            "obligations": [f for f in findings if not f["proven"]]}


# ------------------------------------------------------------ entry points

def _answer(line: str) -> dict:
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"malformed JSON: {exc}"}
    return handle_request(req)


def serve(stdin=None, stdout=None) -> int:
    """One JSON request per stdin line, one JSON response per stdout line,
    flushed after each. EOF ends the loop cleanly."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    for line in stdin:
        if not line.strip():
            continue  # blank lines are not requests
        stdout.write(json.dumps(_answer(line)) + "\n")
        stdout.flush()
    return 0


def query_once(request: str, stdout=None) -> int:
    """Answer a single request string and exit (for scripting)."""
    stdout = sys.stdout if stdout is None else stdout
    response = _answer(request)
    stdout.write(json.dumps(response) + "\n")
    stdout.flush()
    return 0 if response.get("ok") else 1
