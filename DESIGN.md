# Sigil — an AI-native programming language

**Status:** v0.1 design + prototype interpreter
**File extension:** `.sg`

## Why Sigil exists

Every mainstream language was designed around *human* limitations: short working
memory, typo-proneness, hatred of boilerplate. Those pressures gave us type
inference, terse syntax, and ambient authority (any line of code can open a
socket). When an AI writes most of the code and a human *audits* it, the
pressures invert:

1. **Verbosity is cheap.** An AI doesn't mind writing explicit contracts and
   effect annotations. The marginal cost is near zero, so Sigil *requires* them.
2. **Verifiability is the bottleneck.** The human's job is review. Sigil has no
   hidden control flow, no overloading ambiguity, no ambient authority — what a
   function *can do* is visible in its signature.
3. **Security must be structural, not procedural.** Code review doesn't scale to
   AI-generated volume. Sigil makes entire vulnerability classes unrepresentable
   instead of merely discouraged.

## The three load-bearing ideas

### 1. Capabilities: no ambient authority

There is no global `print`, no global filesystem. Side effects require an
**unforgeable capability value** that must be passed in explicitly. The runtime
hands root capabilities (`Console`, `Fs`) only to `main`; everything downstream
gets exactly what it is given.

```sigil
fn main(console: Console) -> Unit ! {io.write} {
    greet(console, "world");
}

fn greet(c: Console, who: Text) -> Unit ! {io.write} {
    print(c, "hello, " + who);
}
```

A dependency that was never handed `Fs` **cannot** touch the filesystem — not
"is trusted not to," *cannot*. This kills most supply-chain attacks at the
language level: a malicious JSON parser has no handle to exfiltrate with.

Capabilities also **attenuate**: from a full `Fs` you can mint strictly weaker
ones and hand those down instead.

```sigil
let plugin_fs: Fs = read_only(subdir(fs, "sandbox"));
```

The holder of `plugin_fs` can read inside `sandbox/` and do nothing else —
writes are refused, paths are rebased under the jail, and `..` or absolute
paths are capability faults at the moment of the attempt. Attenuation is pure
(minting a weaker capability does no I/O) and monotonic: authority only ever
shrinks. Effects are the *static* layer (what kind of action, checked at
compile time); capability scope is the *dynamic* layer (exactly which files,
enforced by the value itself).

### 2. Effects: what a function does is in its type

Every function declares its effect set after `!`. No annotation means **pure** —
no I/O, fully deterministic. Effects propagate: you cannot call an effectful
function from one that doesn't declare (a superset of) those effects. Checked at
compile time.

```sigil
// Pure — the checker REJECTS any io.* call inside this body.
fn parse_port(cfg: Text) -> Int {
    ...
}

fn load(fs: Fs, path: Text) -> Text ! {fs.read} {
    return read_file(fs, path);
}
```

v0.1 effect alphabet: `io.read`, `io.write`, `fs.read`, `fs.write`.

Capabilities and effects reinforce each other: the effect tells the *auditor*
what the function may do; the capability ensures the *code* can't exceed it.
Purity also unlocks the performance roadmap — a pure function can be memoized,
reordered, and parallelized without analysis.

### 3. Contracts: every function states its bargain

`requires` (preconditions) and `ensures` (postconditions, with `result` bound)
are part of the function header. In v0.1 they are checked at runtime with
precise **blame**: a `requires` failure blames the caller, an `ensures` failure
blames the callee. The roadmap moves these to static verification (SMT), at
which point proven contracts also *delete* runtime checks — safety pays for
speed instead of costing it.

```sigil
fn safe_div(a: Int, b: Int) -> Int
    requires b != 0
    ensures result * b <= a
{
    return a / b;
}
```

## What v0.1 deliberately is

- **Types:** `Int`, `Bool`, `Text`, `Unit`, `List[T]`, capability types
  `Console`, `Fs`. Static, no inference at function boundaries (signatures are
  the audit surface).
- **Immutability by default.** `let` is final; `var` is opt-in and local-only.
- **No nulls.** No exceptions in user code (contract violations halt with
  blame). No globals. No reflection.
- **One canonical style.** The grammar admits no formatting wars; a formatter
  will enforce a single rendering (roadmap).

Loops carry **invariants** the same way functions carry contracts:

```sigil
fn total(n: Int) -> Int
    requires n >= 0
    ensures result >= 0
{
    var sum: Int = 0;
    var i: Int = 0;
    while i < n
        invariant sum >= 0
        invariant i >= 0
    {
        sum = sum + i;
        i = i + 1;
    }
    return sum;
}
```

The verifier proves each invariant on entry and across an arbitrary
iteration, then gets to assume it after the loop — which is what makes the
`ensures` provable. Unproven invariants stay as runtime checks (before the
loop and after every iteration) that blame the loop.

**Generic functions** (`fn first[T](xs: List[T]) -> T`) infer their type
arguments at call sites; generic values are opaque (no `==`, no `str`).
The native backend monomorphizes — each instantiation compiles to its own
Rust function, so generics cost nothing at runtime.

## Grammar (v0.1)

```
program   := (fnDecl | recordDecl)*
recordDecl:= "record" UPPER_IDENT "{" (IDENT ":" type ","?)* "}"
fnDecl    := "fn" IDENT "(" params? ")" "->" type effects? contract* block
params    := param ("," param)*
param     := IDENT ":" type
type      := "Int" | "Bool" | "Text" | "Unit" | "Console" | "Fs"
           | "List" "[" type "]" | UPPER_IDENT
effects   := "!" "{" EFFECT ("," EFFECT)* "}"
contract  := "requires" expr | "ensures" expr
block     := "{" stmt* "}"
stmt      := "let" IDENT ":" type "=" expr ";"
           | "var" IDENT ":" type "=" expr ";"
           | IDENT "=" expr ";"
           | "return" expr? ";"
           | "if" expr block ("else" (block | ifStmt))?
           | "while" expr block
           | expr ";"
expr      := standard precedence: or > and > == != < <= > >= > + - > * / % > unary (not, -)
primary   := INT | TEXT | "true" | "false" | IDENT | call | "(" expr ")"
           | "[" (expr ",")* "]" | primary "[" expr "]"
           | UPPER_IDENT "{" (IDENT ":" expr ","?)* "}" | primary "." IDENT
```

**Naming is part of the grammar's canon:** record names start uppercase,
value and function names start lowercase (enforced by the checker). This is
what makes `Point { x: 0 }` unambiguous against `if flag { ... }` — and it
means an auditor can classify any identifier at a glance. Record literals
must list fields in declaration order: one program, one rendering.

**Records** are immutable user-defined product types. They may hold
capabilities (bundling a `Console` with a log prefix is normal
object-capability style), but a record containing a capability cannot be
compared with `==`. Direct record-in-record recursion is rejected (infinite
size); recursion through `List` is allowed, so trees are expressible.

## Builtins (v0.1)

| signature | effects |
|---|---|
| `print(c: Console, msg: Text) -> Unit` | `io.write` |
| `read_line(c: Console) -> Text` | `io.read` |
| `read_file(fs: Fs, path: Text) -> Text` | `fs.read` |
| `write_file(fs: Fs, path: Text, data: Text) -> Unit` | `fs.write` (creates parent dirs) |
| `read_only(fs: Fs) -> Fs` | pure (attenuation) |
| `subdir(fs: Fs, prefix: Text) -> Fs` | pure (attenuation) |
| `len(x: List[T] | Text) -> Int` | pure |
| `str(x: Int | Bool | Text) -> Text` | pure |
| `push(xs: List[T], x: T) -> List[T]` | pure (returns new list) |

`main` may take any subset of `(Console, Fs)` parameters; the runtime injects
the root capabilities by type. There is no other way to obtain one.

## Roadmap

| phase | goal | status |
|---|---|---|
| **0.1** | Tree-walking interpreter, type + effect checker, runtime contracts with blame. Prove the model end to end. | **done** |
| **0.1.5** | Native backend: checked AST → Rust → `rustc -O` → executable (`sigil build`). Interpreter retained as reference semantics; differential tests enforce agreement. Capabilities compile to zero-sized types. ~87× over the interpreter on `fib(30)`. | **done** |
| **0.2a** | Capability *attenuation*: `read_only(fs)`, path-scoped `subdir(fs, p)`; pure, monotonic, enforced by the capability value in both backends. | **done** |
| **0.2b** | Records: immutable product types, canonical field order, capability-aware equality, recursion via List. Compile to plain Rust structs. | **done** |
| **0.2c** | Generics for user functions: `fn first[T](xs: List[T]) -> T`, inference-only call sites, generic values opaque (no `==`/`str`). Native backend monomorphizes (worklist, mangled instantiations); uncalled generic functions emit no code. | **done** |
| **0.3** | Static contract verification via SMT (Z3): symbolic execution over Int/Bool, inductive recursion, callee-ensures propagation, division-safety proofs. Proven clauses emit no runtime check in the native backend; everything unmodeled (Text/List/records/loops) conservatively keeps its check. `sigil verify` reports clause-by-clause. | **done** |
| **0.3b** | Loop invariants: `while cond invariant e { ... }` — proven on entry + preserved per iteration (Z3), assumed after the loop, so loop-carried `ensures` become provable. Unproven invariants are runtime-checked before the loop and after each iteration, with "blame the loop" diagnostics. | **done** |
| **0.4** | Canonical typed AST as the on-disk format; stable declaration IDs; semantic diff. Text becomes a projection. | |
| **0.5** | Compiler-as-a-service API: an LLM queries types/effects/obligations *while generating* instead of generating blind. | |
| **1.0** | Self-contained backend (LLVM/Cranelift, dropping the rustc dependency) — purity/effect info drives parallelization and check elision. | |

## Prior art and how Sigil differs

Rust (ownership/perf), Pony & Austral (capabilities), Koka (effects), Dafny/F*
(contracts/verification), Unison (content-addressed AST code). Each has one
pillar; none combine **capability security + mandatory effects + contracts +
machine-canonical form** with *AI authorship* as the explicit design center.
That combination is the bet.
