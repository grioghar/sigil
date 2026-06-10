"""Static contract verification for Sigil via Z3 (roadmap 0.3).

The verifier symbolically executes each function over Int/Bool values and
tries to discharge four kinds of obligations:

  * every `requires` clause at every call site (caller obligations)
  * every `ensures` clause at every return site (callee obligations)
  * every loop `invariant` on entry and across an arbitrary iteration (0.3b)
  * every division/modulo having a nonzero divisor

Proven obligations are stamped onto the AST (Contract.proven, Binary.div_safe)
and the native backend then emits NO runtime check for them — safety pays for
speed. Text and List values are modeled by their LENGTH (a symbolic Int with
exact semantics for literals, slice, push, concatenation, str, and chr), which
is what lets contracts like `requires i < len(s)` prove in real programs.
Everything else the engine cannot model (record/enum structure, capability
state, loop-carried values not captured by an invariant) becomes an opaque
unknown, so failure to prove is always conservative: the runtime check stays.
Two guardrails keep erasure sound: facts arising inside the right operand of
a short-circuit `and`/`or` are weakened to implications (that code may not
run), and a clause whose own evaluation contains a partial operation (slice,
ord, chr, indexing, division, any user call) is never marked proven — erasing
a check that could itself fault would silently mask the fault.

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
    kind: str      # 'requires' | 'ensures' | 'invariant' | 'division'
    source: str
    proven: bool
    line: int


CONTRACT_KINDS = ("requires", "ensures", "invariant")


@dataclass
class Report:
    findings: list[Finding]

    @property
    def contracts_proven(self) -> int:
        return sum(1 for f in self.findings
                   if f.proven and f.kind in CONTRACT_KINDS)

    @property
    def contracts_total(self) -> int:
        return sum(1 for f in self.findings if f.kind in CONTRACT_KINDS)

    @property
    def divisions_proven(self) -> int:
        return sum(1 for f in self.findings if f.proven and f.kind == "division")

    @property
    def divisions_total(self) -> int:
        return sum(1 for f in self.findings if f.kind == "division")


class Opaque:
    """A value the engine does not model structurally. Text and List opaques
    carry a symbolic LENGTH (a Z3 Int), which is what lets contracts about
    len() prove; records, enums, and capabilities have length None."""

    _next = 0

    def __init__(self, length=None):
        Opaque._next += 1
        self.oid = Opaque._next
        self.length = length


class State:
    def __init__(self, vars_: dict, path: list):
        self.vars = vars_
        self.path = path
        self.alive = True

    def clone(self) -> "State":
        return State(dict(self.vars), list(self.path))


class LoopCtx:
    """Verification context of one lexically enclosing while loop. A `break`
    is a loop exit, so every break site is an additional proof obligation for
    each invariant (ANDed into break_ok, like preservation); and a loop whose
    body can break may NOT assume ¬cond after the loop."""

    def __init__(self, invariants: list):
        self.invariants = invariants
        self.break_ok = [True] * len(invariants)
        self.has_break = False


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
        self.inv_findings: list[Finding] = []
        self.current_fn: str = ""
        # Bumped whenever translation encounters a PARTIAL operation (slice,
        # ord, chr, indexing, division, any user-function call — things that
        # can fault or violate a requires at runtime). A clause may only be
        # marked proven if translating it bumped nothing: erasing a check
        # whose own evaluation could fault would silently mask that fault.
        self.partial_ops = 0
        # Stack of enclosing loops; a Break statement targets the innermost.
        self.loop_stack: list[LoopCtx] = []

    # ------------------------------------------------------------ utilities

    def fresh_int(self):
        self.counter += 1
        return z3.Int(f"_v{self.counter}")

    def fresh_bool(self):
        self.counter += 1
        return z3.Bool(f"_v{self.counter}")

    def fresh_by_type(self, ty: A.Type, state: "State"):
        if ty is not None and ty.kind == "Int":
            return self.fresh_int()
        if ty is not None and ty.kind == "Bool":
            return self.fresh_bool()
        if ty is not None and ty.kind in ("Text", "List"):
            return self.fresh_sized(state)
        return Opaque()

    def fresh_sized(self, state: "State") -> Opaque:
        """An unknown Text/List value: the only fact is len >= 0."""
        length = self.fresh_int()
        state.path.append(length >= 0)
        return Opaque(length)

    def havoc_like(self, value, state: "State"):
        if is_z3(value):
            if value.sort() == z3.IntSort():
                return self.fresh_int()
            if value.sort() == z3.BoolSort():
                return self.fresh_bool()
        if isinstance(value, Opaque) and value.length is not None:
            return self.fresh_sized(state)
        return Opaque()

    @staticmethod
    def length_of(value):
        return value.length if isinstance(value, Opaque) else None

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

        findings.extend(self.inv_findings)
        findings.extend(self.div_findings)
        return Report(findings)

    # ------------------------------------------------------------ functions

    def verify_fn(self, fn: A.FnDecl, findings: list[Finding]) -> None:
        self.current_fn = fn.name
        state = State({}, [])
        for pname, ptype in fn.params:
            state.vars[pname] = self.fresh_by_type(ptype, state)

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
            mark = self.partial_ops
            value = self.translate(contract.expr, shadow)
            if not (self.partial_ops == mark and is_z3(value)
                    and self.prove(state, value)):
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
        elif isinstance(stmt, A.Break):
            self.exec_break(state)
        elif isinstance(stmt, A.Match):
            self.exec_match(stmt, state)
        elif isinstance(stmt, A.ExprStmt):
            self.translate(stmt.expr, state)

    def exec_break(self, state: State) -> None:
        # A break exits the innermost loop, and invariants must hold at every
        # loop exit: each invariant becomes an obligation HERE, exactly like
        # preservation at the end of the body.
        ctx = self.loop_stack[-1]
        for idx, inv in enumerate(ctx.invariants):
            mark = self.partial_ops
            value = self.translate(inv.expr, state)
            ok = (self.partial_ops == mark and is_z3(value)
                  and self.prove(state, value))
            ctx.break_ok[idx] = ctx.break_ok[idx] and ok
        ctx.has_break = True
        state.alive = False  # control leaves the body here

    def exec_match(self, stmt: A.Match, state: State) -> None:
        # Enum values are Opaque, so no arm can be ruled out: run every arm
        # on a clone, binders bound to fresh values of their stamped payload
        # types (Int/Bool binders stay modeled; everything else is Opaque).
        self.translate(stmt.scrutinee, state)  # record nested obligations
        arm_states: list[State] = []
        for arm in stmt.arms:
            arm_state = state.clone()
            for binder, btype in zip(arm.binders,
                                     getattr(arm, "binder_types", [])):
                arm_state.vars[binder] = self.fresh_by_type(btype, arm_state)
            self.exec_block(arm.body, arm_state)
            arm_states.append(arm_state)

        survivors = [s for s in arm_states if s.alive]
        if not survivors:
            state.alive = False  # every arm returned
            return
        # Conservative merge: which arm ran is unknown, so any variable whose
        # value is not identical across all surviving arms is havocked, and
        # arm-local path facts are dropped (sound — only knowledge is lost).
        for name in list(state.vars):
            values = [s.vars[name] for s in survivors]
            if any(value is not values[0] for value in values):
                state.vars[name] = self.havoc_like(state.vars[name], state)

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

        # Install the merged path BEFORE merging variables: havoc may append
        # freshness facts (len >= 0) and they must land on the live path.
        state.path = merged_path
        for name in list(state.vars):
            tval, eval_ = then_state.vars[name], else_state.vars[name]
            if tval is eval_:
                continue
            if is_z3(cond) and is_z3(tval) and is_z3(eval_) \
                    and tval.sort() == eval_.sort():
                state.vars[name] = z3.If(cond, tval, eval_)
            elif is_z3(cond) and isinstance(tval, Opaque) \
                    and isinstance(eval_, Opaque) \
                    and tval.length is not None and eval_.length is not None:
                # Sized values merge by length: the structure is unknown but
                # the length is exactly one of the two.
                merged = self.fresh_sized(state)
                state.path.append(merged.length ==
                                  z3.If(cond, tval.length, eval_.length))
                state.vars[name] = merged
            else:
                state.vars[name] = self.havoc_like(tval, state)

    def exec_while(self, stmt: A.While, state: State) -> None:
        # Loop invariants (0.3b), classic inductive recipe. ENTRY: each
        # invariant must hold in the state that first reaches the loop.
        entry_ok: list[bool] = []
        for inv in stmt.invariants:
            mark = self.partial_ops
            value = self.translate(inv.expr, state)
            entry_ok.append(self.partial_ops == mark and is_z3(value)
                            and self.prove(state, value))

        # Havoc everything the body assigns: from here on the state models
        # an arbitrary iteration, about which only the invariants are known.
        for name in self.collect_assigned(stmt.body):
            if name in state.vars:
                state.vars[name] = self.havoc_like(state.vars[name], state)

        # The invariants hold at EVERY loop head (entry-checked, preserved,
        # and runtime-enforced when unproven), so they join the path before
        # the condition is translated — obligations inside the condition
        # (e.g. a requires on a call in a short-circuit guard) may use them.
        for inv in stmt.invariants:
            value = self.translate(inv.expr, state)
            if is_z3(value):
                state.path.append(value)

        cond = self.translate(stmt.cond, state)

        # The body may additionally assume the condition.
        body_state = state.clone()
        if is_z3(cond):
            body_state.path.append(cond)
        ctx = LoopCtx(stmt.invariants)
        self.loop_stack.append(ctx)
        self.exec_block(stmt.body, body_state)
        self.loop_stack.pop()

        # PRESERVATION: an arbitrary iteration must re-establish each
        # invariant. A body that never falls off the end (every path
        # returns or breaks) preserves vacuously — no next loop head is
        # reached. Break sites contributed their own obligations above.
        for ok, inv, brk in zip(entry_ok, stmt.invariants, ctx.break_ok):
            proven = ok and brk
            if proven and body_state.alive:
                mark = self.partial_ops
                value = self.translate(inv.expr, body_state)
                proven = (self.partial_ops == mark and is_z3(value)
                          and self.prove(body_state, value))
            inv.proven = proven
            self.inv_findings.append(Finding(
                self.current_fn, "invariant", inv.source, proven, inv.line))

        # POST-LOOP: the invariants are already on the path (appended above);
        # the condition is false ONLY when no break exists in the body — a
        # broken exit does not imply the condition turned false.
        if is_z3(cond) and not ctx.has_break:
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
            elif isinstance(stmt, A.Match):
                for arm in stmt.arms:
                    names |= self.collect_assigned(arm.body)
        return names

    # ------------------------------------------------------------ expressions

    def translate(self, expr: A.Expr, state: State):
        ty = getattr(expr, "ty", None)

        if isinstance(expr, A.IntLit):
            return z3.IntVal(expr.value)
        if isinstance(expr, A.BoolLit):
            return z3.BoolVal(expr.value)
        if isinstance(expr, A.TextLit):
            return Opaque(z3.IntVal(len(expr.value)))
        if isinstance(expr, A.ListLit):
            for item in expr.items:
                self.translate(item, state)  # record nested obligations
            return Opaque(z3.IntVal(len(expr.items)))
        if isinstance(expr, A.Var):
            value = state.vars.get(expr.name)
            return value if value is not None else self.fresh_by_type(ty, state)
        if isinstance(expr, A.Index):
            base = self.translate(expr.base, state)
            index = self.translate(expr.index, state)
            # Partial: faults unless 0 <= index < len(base). Execution
            # continuing past it is what justifies the bound facts.
            self.partial_ops += 1
            base_len = self.length_of(base)
            if base_len is not None and is_z3(index):
                state.path.append(index >= 0)
                state.path.append(index < base_len)
            return self.fresh_by_type(ty, state)
        if isinstance(expr, A.Unary):
            operand = self.translate(expr.operand, state)
            if not is_z3(operand):
                return self.fresh_by_type(ty, state)
            return z3.Not(operand) if expr.op == "not" else -operand
        if isinstance(expr, A.Binary):
            return self.translate_binary(expr, state)
        if isinstance(expr, A.Call):
            return self.translate_call(expr, state)
        # Record literals, field access: structure not modeled.
        for child in self.subexprs(expr):
            self.translate(child, state)  # still record nested obligations
        return self.fresh_by_type(ty, state)

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

    def translate_guarded(self, expr: A.Expr, state: State, guard):
        """Translate `expr` as code that only runs when `guard` holds (the
        right operand of a short-circuit operator). Facts the translation
        appends are weakened to implications, since the code may not run."""
        state.path.append(guard)
        base = len(state.path)
        value = self.translate(expr, state)
        appended = state.path[base:]
        del state.path[base - 1:]
        for fact in appended:
            state.path.append(z3.Implies(guard, fact))
        return value

    def translate_binary(self, expr: A.Binary, state: State):
        op = expr.op

        if op in ("and", "or"):
            left = self.translate(expr.left, state)
            if is_z3(left):
                guard = left if op == "and" else z3.Not(left)
                right = self.translate_guarded(expr.right, state, guard)
            else:
                right = self.translate(expr.right, state)
            if is_z3(left) and is_z3(right):
                return (z3.And if op == "and" else z3.Or)(left, right)
            return self.fresh_bool()

        left = self.translate(expr.left, state)
        right = self.translate(expr.right, state)
        bothz3 = is_z3(left) and is_z3(right)

        if op in ("==", "!="):
            if bothz3 and left.sort() == right.sort():
                eq = left == right
                return eq if op == "==" else z3.Not(eq)
            llen, rlen = self.length_of(left), self.length_of(right)
            if llen is not None and rlen is not None:
                # Structure is unknown, but equal values have equal lengths.
                equal = self.fresh_bool()
                state.path.append(z3.Implies(equal, llen == rlen))
                return equal if op == "==" else z3.Not(equal)
            return self.fresh_bool()
        if op in ("<", "<=", ">", ">="):
            if bothz3:
                return {"<": left < right, "<=": left <= right,
                        ">": left > right, ">=": left >= right}[op]
            return self.fresh_bool()
        if op in ("/", "%"):
            # Partial. Obligation: divisor nonzero. Result stays
            # unconstrained because Sigil division truncates toward zero,
            # which is not Z3's div.
            self.partial_ops += 1
            safe = bothz3 and self.prove(state, right != 0)
            expr.div_safe = safe
            self.div_findings.append(Finding(
                self.current_fn, "division", f"divisor != 0 at line {expr.line}",
                safe, expr.line))
            return self.fresh_int()
        if op == "+":
            if getattr(expr, "ty", None) == A.TEXT:
                llen, rlen = self.length_of(left), self.length_of(right)
                if llen is not None and rlen is not None:
                    return Opaque(llen + rlen)
                return self.fresh_sized(state)
            if bothz3:
                return left + right
            return self.fresh_int()
        if op in ("-", "*"):
            if bothz3:
                return left - right if op == "-" else left * right
            return self.fresh_int()
        return self.fresh_by_type(getattr(expr, "ty", None), state)

    def translate_call(self, expr: A.Call, state: State):
        callee = self.fns.get(expr.name)
        arg_values = [self.translate(a, state) for a in expr.args]

        if callee is None:
            # Variant construction or builtin. Arguments were translated
            # above, so their nested obligations are already recorded.
            if getattr(expr, "variant_of", None) is not None:
                return Opaque()
            return self.translate_builtin(expr, arg_values, state)

        # A user call is partial from a clause's point of view: its body can
        # fault, and its (unproven) requires can fail.
        self.partial_ops += 1

        env = {pname: val for (pname, _), val in zip(callee.params, arg_values)}

        req_idx = 0
        for contract in callee.contracts:
            if contract.kind != "requires":
                continue
            key = (callee.name, req_idx)
            shadow = State(env, state.path)
            mark = self.partial_ops
            value = self.translate(contract.expr, shadow)
            ok = (self.partial_ops == mark and is_z3(value)
                  and self.prove(state, value))
            self.requires_ok[key] = self.requires_ok.get(key, True) and ok
            self.requires_called.add(key)
            req_idx += 1

        result = self.fresh_by_type(getattr(expr, "ty", None), state)

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

    def translate_builtin(self, expr: A.Call, args: list, state: State):
        """Builtins with modeled length semantics. Partial builtins (slice,
        ord, chr) contribute execution facts: control flow continuing past
        them is what justifies assuming their bounds held."""
        name = expr.name
        ty = getattr(expr, "ty", None)

        if name == "len":
            length = self.length_of(args[0])
            if length is not None:
                return length
            return self.fresh_int()

        if name == "slice":
            s, start, end = args
            self.partial_ops += 1
            s_len = self.length_of(s)
            if s_len is not None and is_z3(start) and is_z3(end):
                state.path.append(start >= 0)
                state.path.append(end >= start)
                state.path.append(end <= s_len)
                return Opaque(end - start)
            return self.fresh_sized(state)

        if name == "push":
            length = self.length_of(args[0])
            if length is not None:
                return Opaque(length + 1)
            return self.fresh_sized(state)

        if name == "set":
            xs, i, _x = args
            self.partial_ops += 1
            length = self.length_of(xs)
            if length is not None and is_z3(i):
                state.path.append(i >= 0)
                state.path.append(i < length)
                # The result is the input list with one element replaced:
                # structure unknown, length EXACTLY that of the input.
                return Opaque(length)
            return self.fresh_sized(state)

        if name == "str":
            arg_ty = getattr(expr.args[0], "ty", None)
            if arg_ty == A.TEXT:
                return args[0]  # str(Text) is the identity
            # str(Int)/str(Bool) renders at least one character.
            result = self.fresh_sized(state)
            state.path.append(result.length >= 1)
            return result

        if name == "ord":
            self.partial_ops += 1
            length = self.length_of(args[0])
            if length is not None:
                state.path.append(length == 1)
            code = self.fresh_int()
            state.path.append(code >= 0)
            state.path.append(code <= 0x10FFFF)
            return code

        if name == "chr":
            self.partial_ops += 1
            if is_z3(args[0]):
                state.path.append(args[0] >= 0)
                state.path.append(args[0] <= 0x10FFFF)
            return Opaque(z3.IntVal(1))

        return self.fresh_by_type(ty, state)


def verify(program: A.Program) -> Report | None:
    """Run the verifier, stamping proofs onto the AST. Returns None when
    z3-solver is not installed (everything keeps its runtime check)."""
    if not HAVE_Z3:
        return None
    return Verifier(program).verify()
