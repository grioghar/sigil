"""Static contract verification for Sigil via Z3 (roadmap 0.3).

The verifier symbolically executes each function over Int/Bool values and
tries to discharge three kinds of obligations:

  * every `requires` clause at every call site (caller obligations)
  * every `ensures` clause at every return site (callee obligations)
  * every division/modulo having a nonzero divisor

Proven obligations are stamped onto the AST (Contract.proven, Binary.div_safe)
and the native backend then emits NO runtime check for them — safety pays for
speed. Everything the engine cannot model (Text, List, records, capabilities,
loop-carried state) becomes an opaque unknown, so failure to prove is always
conservative: the runtime check simply stays.

Recursion is handled inductively: while proving f's ensures, recursive calls
to f may assume f's ensures (standard partial-correctness reasoning).

This module is optional — without z3-solver installed, everything keeps its
runtime check and nothing changes.
"""

from dataclasses import dataclass

from . import ast_nodes as A

try:
    import z3
    HAVE_Z3 = True
except ImportError:  # pragma: no cover
    HAVE_Z3 = False

SOLVER_TIMEOUT_MS = 2000


@dataclass
class Finding:
    fn: str
    kind: str      # 'requires' | 'ensures' | 'division'
    source: str
    proven: bool
    line: int


@dataclass
class Report:
    findings: list[Finding]

    @property
    def contracts_proven(self) -> int:
        return sum(1 for f in self.findings
                   if f.proven and f.kind in ("requires", "ensures"))

    @property
    def contracts_total(self) -> int:
        return sum(1 for f in self.findings if f.kind in ("requires", "ensures"))

    @property
    def divisions_proven(self) -> int:
        return sum(1 for f in self.findings if f.proven and f.kind == "division")

    @property
    def divisions_total(self) -> int:
        return sum(1 for f in self.findings if f.kind == "division")


class Opaque:
    """A value the engine does not model (Text, List, record, capability)."""

    _next = 0

    def __init__(self):
        Opaque._next += 1
        self.oid = Opaque._next


class State:
    def __init__(self, vars_: dict, path: list):
        self.vars = vars_
        self.path = path
        self.alive = True

    def clone(self) -> "State":
        return State(dict(self.vars), list(self.path))


def is_z3(value) -> bool:
    return HAVE_Z3 and isinstance(value, z3.ExprRef)


class Verifier:
    def __init__(self, program: A.Program):
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.counter = 0
        # (fn name, requires clause index) -> conjunction of call-site proofs
        self.requires_ok: dict[tuple[str, int], bool] = {}
        # (fn name, requires clause index) -> any call site seen at all
        self.requires_called: set[tuple[str, int]] = set()
        self.div_findings: list[Finding] = []
        self.current_fn: str = ""

    # ------------------------------------------------------------ utilities

    def fresh_int(self):
        self.counter += 1
        return z3.Int(f"_v{self.counter}")

    def fresh_bool(self):
        self.counter += 1
        return z3.Bool(f"_v{self.counter}")

    def fresh_by_type(self, ty: A.Type):
        if ty is not None and ty.kind == "Int":
            return self.fresh_int()
        if ty is not None and ty.kind == "Bool":
            return self.fresh_bool()
        return Opaque()

    def havoc_like(self, value):
        if is_z3(value):
            if value.sort() == z3.IntSort():
                return self.fresh_int()
            if value.sort() == z3.BoolSort():
                return self.fresh_bool()
        return Opaque()

    def prove(self, state: State, obligation) -> bool:
        """True iff `obligation` holds on every path satisfying state.path."""
        if not is_z3(obligation):
            return False
        solver = z3.Solver()
        solver.set("timeout", SOLVER_TIMEOUT_MS)
        for fact in state.path:
            solver.add(fact)
        solver.add(z3.Not(obligation))
        return solver.check() == z3.unsat

    # ------------------------------------------------------------ entry

    def verify(self) -> Report:
        findings: list[Finding] = []
        for fn in self.program.functions:
            self.verify_fn(fn, findings)

        # A requires clause is dischargeable only if EVERY call site in the
        # whole program proved it (an uncalled function is vacuously safe).
        for fn in self.program.functions:
            req_idx = 0
            for contract in fn.contracts:
                if contract.kind != "requires":
                    continue
                key = (fn.name, req_idx)
                proven = self.requires_ok.get(key, True)
                contract.proven = proven
                findings.append(Finding(fn.name, "requires", contract.source,
                                        proven, contract.line))
                req_idx += 1

        findings.extend(self.div_findings)
        return Report(findings)

    # ------------------------------------------------------------ functions

    def verify_fn(self, fn: A.FnDecl, findings: list[Finding]) -> None:
        self.current_fn = fn.name
        state = State({}, [])
        for pname, ptype in fn.params:
            state.vars[pname] = self.fresh_by_type(ptype)

        requires = [c for c in fn.contracts if c.kind == "requires"]
        ensures = [c for c in fn.contracts if c.kind == "ensures"]

        # The body may assume its own preconditions.
        for contract in requires:
            value = self.translate(contract.expr, state)
            if is_z3(value):
                state.path.append(value)

        self.ensures_ok = [True] * len(ensures)
        self.ensures_clauses = ensures
        self.fn_params = dict(state.vars)

        self.exec_block(fn.body, state)

        # Unit functions may fall off the end — that is a return site too.
        if state.alive and fn.ret == A.UNIT:
            self.check_ensures(state, Opaque())

        for ok, contract in zip(self.ensures_ok, ensures):
            contract.proven = ok
            findings.append(Finding(fn.name, "ensures", contract.source,
                                    ok, contract.line))

    def check_ensures(self, state: State, result_value) -> None:
        for idx, contract in enumerate(self.ensures_clauses):
            env = dict(self.fn_params)
            env["result"] = result_value
            shadow = State(env, state.path)
            value = self.translate(contract.expr, shadow)
            if not (is_z3(value) and self.prove(state, value)):
                self.ensures_ok[idx] = False

    # ------------------------------------------------------------ statements

    def exec_block(self, stmts: list[A.Stmt], state: State) -> None:
        for stmt in stmts:
            if not state.alive:
                return
            self.exec_stmt(stmt, state)

    def exec_stmt(self, stmt: A.Stmt, state: State) -> None:
        if isinstance(stmt, A.Let):
            state.vars[stmt.name] = self.translate(stmt.value, state)
        elif isinstance(stmt, A.Assign):
            state.vars[stmt.name] = self.translate(stmt.value, state)
        elif isinstance(stmt, A.Return):
            value = (self.translate(stmt.value, state)
                     if stmt.value is not None else Opaque())
            self.check_ensures(state, value)
            state.alive = False
        elif isinstance(stmt, A.If):
            self.exec_if(stmt, state)
        elif isinstance(stmt, A.While):
            self.exec_while(stmt, state)
        elif isinstance(stmt, A.ExprStmt):
            self.translate(stmt.expr, state)

    def exec_if(self, stmt: A.If, state: State) -> None:
        cond = self.translate(stmt.cond, state)

        then_state = state.clone()
        if is_z3(cond):
            then_state.path.append(cond)
        self.exec_block(stmt.then_body, then_state)

        else_state = state.clone()
        if is_z3(cond):
            else_state.path.append(z3.Not(cond))
        if stmt.else_body is not None:
            self.exec_block(stmt.else_body, else_state)

        if not then_state.alive and not else_state.alive:
            state.alive = False
            return
        if not then_state.alive:
            state.vars, state.path = else_state.vars, else_state.path
            return
        if not else_state.alive:
            state.vars, state.path = then_state.vars, then_state.path
            return

        # Both branches continue: merge values; keep branch-local facts as
        # implications so no knowledge is silently dropped.
        base_len = len(state.path)
        merged_path = list(state.path)
        if is_z3(cond):
            for fact in then_state.path[base_len + 1:]:
                merged_path.append(z3.Implies(cond, fact))
            for fact in else_state.path[base_len + 1:]:
                merged_path.append(z3.Implies(z3.Not(cond), fact))

        for name in list(state.vars):
            tval, eval_ = then_state.vars[name], else_state.vars[name]
            if tval is eval_:
                continue
            if is_z3(cond) and is_z3(tval) and is_z3(eval_) \
                    and tval.sort() == eval_.sort():
                state.vars[name] = z3.If(cond, tval, eval_)
            else:
                state.vars[name] = self.havoc_like(tval)
        state.path = merged_path

    def exec_while(self, stmt: A.While, state: State) -> None:
        # No loop invariants yet (roadmap): havoc everything the loop body
        # assigns, verify the body for an arbitrary iteration, and continue
        # afterward knowing only that the condition is now false.
        for name in self.collect_assigned(stmt.body):
            if name in state.vars:
                state.vars[name] = self.havoc_like(state.vars[name])

        cond = self.translate(stmt.cond, state)

        body_state = state.clone()
        if is_z3(cond):
            body_state.path.append(cond)
        self.exec_block(stmt.body, body_state)

        if is_z3(cond):
            state.path.append(z3.Not(cond))

    def collect_assigned(self, stmts: list[A.Stmt]) -> set[str]:
        names: set[str] = set()
        for stmt in stmts:
            if isinstance(stmt, A.Assign):
                names.add(stmt.name)
            elif isinstance(stmt, A.If):
                names |= self.collect_assigned(stmt.then_body)
                if stmt.else_body is not None:
                    names |= self.collect_assigned(stmt.else_body)
            elif isinstance(stmt, A.While):
                names |= self.collect_assigned(stmt.body)
        return names

    # ------------------------------------------------------------ expressions

    def translate(self, expr: A.Expr, state: State):
        ty = getattr(expr, "ty", None)

        if isinstance(expr, A.IntLit):
            return z3.IntVal(expr.value)
        if isinstance(expr, A.BoolLit):
            return z3.BoolVal(expr.value)
        if isinstance(expr, A.Var):
            value = state.vars.get(expr.name)
            return value if value is not None else self.fresh_by_type(ty)
        if isinstance(expr, A.Unary):
            operand = self.translate(expr.operand, state)
            if not is_z3(operand):
                return self.fresh_by_type(ty)
            return z3.Not(operand) if expr.op == "not" else -operand
        if isinstance(expr, A.Binary):
            return self.translate_binary(expr, state)
        if isinstance(expr, A.Call):
            return self.translate_call(expr, state)
        # Text/List/record literals, indexing, field access: not modeled.
        for child in self.subexprs(expr):
            self.translate(child, state)  # still record nested obligations
        return self.fresh_by_type(ty)

    def subexprs(self, expr: A.Expr) -> list[A.Expr]:
        if isinstance(expr, A.ListLit):
            return expr.items
        if isinstance(expr, A.RecordLit):
            return [fexpr for _, fexpr in expr.fields]
        if isinstance(expr, A.Index):
            return [expr.base, expr.index]
        if isinstance(expr, A.FieldAccess):
            return [expr.base]
        if isinstance(expr, A.TextLit):
            return []
        return []

    def translate_binary(self, expr: A.Binary, state: State):
        left = self.translate(expr.left, state)
        right = self.translate(expr.right, state)
        op = expr.op
        bothz3 = is_z3(left) and is_z3(right)

        if op in ("and", "or"):
            if bothz3:
                return (z3.And if op == "and" else z3.Or)(left, right)
            return self.fresh_bool()
        if op in ("==", "!="):
            if bothz3 and left.sort() == right.sort():
                eq = left == right
                return eq if op == "==" else z3.Not(eq)
            return self.fresh_bool()
        if op in ("<", "<=", ">", ">="):
            if bothz3:
                return {"<": left < right, "<=": left <= right,
                        ">": left > right, ">=": left >= right}[op]
            return self.fresh_bool()
        if op in ("/", "%"):
            # Obligation: divisor nonzero. Result stays unconstrained because
            # Sigil division truncates toward zero, which is not Z3's div.
            safe = bothz3 and self.prove(state, right != 0)
            expr.div_safe = safe
            self.div_findings.append(Finding(
                self.current_fn, "division", f"divisor != 0 at line {expr.line}",
                safe, expr.line))
            return self.fresh_int()
        if op == "+":
            if getattr(expr, "ty", None) == A.TEXT:
                return Opaque()
            if bothz3:
                return left + right
            return self.fresh_int()
        if op in ("-", "*"):
            if bothz3:
                return left - right if op == "-" else left * right
            return self.fresh_int()
        return self.fresh_by_type(getattr(expr, "ty", None))

    def translate_call(self, expr: A.Call, state: State):
        callee = self.fns.get(expr.name)
        arg_values = [self.translate(a, state) for a in expr.args]

        if callee is None:
            # Builtin: no contracts; result unknown.
            return self.fresh_by_type(getattr(expr, "ty", None))

        env = {pname: val for (pname, _), val in zip(callee.params, arg_values)}

        req_idx = 0
        for contract in callee.contracts:
            if contract.kind != "requires":
                continue
            key = (callee.name, req_idx)
            shadow = State(env, state.path)
            value = self.translate(contract.expr, shadow)
            ok = is_z3(value) and self.prove(state, value)
            self.requires_ok[key] = self.requires_ok.get(key, True) and ok
            self.requires_called.add(key)
            req_idx += 1

        result = self.fresh_by_type(getattr(expr, "ty", None))

        # Assume the callee's ensures about the result (inductively sound
        # for recursive calls under partial correctness).
        for contract in callee.contracts:
            if contract.kind != "ensures":
                continue
            shadow_env = dict(env)
            shadow_env["result"] = result
            shadow = State(shadow_env, state.path)
            value = self.translate(contract.expr, shadow)
            if is_z3(value):
                state.path.append(value)

        return result


def verify(program: A.Program) -> Report | None:
    """Run the verifier, stamping proofs onto the AST. Returns None when
    z3-solver is not installed (everything keeps its runtime check)."""
    if not HAVE_Z3:
        return None
    return Verifier(program).verify()
