# The road to 1.0 — self-hosting

**Sigil is not 1.0 until it compiles with its own toolchain.** Today the
compiler is written in Python (the *bootstrap* toolchain). 1.0 means the
compiler is written in Sigil and can compile itself.

This is the ultimate dogfood: the largest, most demanding Sigil program is the
Sigil compiler, and writing it will surface every real gap in the language.

## The bootstrap plan

The compile path is `parse → check → emit → native executable`. (The Z3
verifier and the formatter are separate `verify` / `fmt` commands, not on the
compile path, so they are **not** required for self-hosting — a self-hosted
*compiler* must parse, check, and emit.) We rewrite it in Sigil one component
at a time, smallest first. Each component is:

1. written in Sigil under [`selfhost/`](../selfhost),
2. compiled by the **current** (Python) toolchain — `sigil build`,
3. conformance-tested against its Python counterpart over the
   `examples/` + `programs/` corpus (same input → same output).

| component | status | conformance target |
|---|---|---|
| **lexer** | in progress | token stream matches `sigil.lexer` |
| **parser** | next | AST matches `sigil.parser` (canonical dump) |
| **checker** | | accept/reject + types match `sigil.checker` |
| **emitter** | | generated code matches `sigil.emit_rust` |
| **driver** | | a `sigil`-in-Sigil CLI that ties them together |

When the Sigil-written compiler reproduces the Python compiler's behavior on
the whole corpus *including its own source*, Sigil is self-hosting → 1.0.

## Gaps self-hosting will force (and that's the point)

The compiler needs things the language does not have yet. Each becomes its own
dogfood-driven feature:

- **Command-line arguments.** A real `sigil` CLI must learn *which* file to
  compile. Sigil's `main(console, fs)` has no argv — this needs an `Args`
  capability (read-only, injected like `Console`/`Fs`).
- **A `Map`.** Symbol tables, the variant registry, the monomorphization
  worklist. Today only `List` exists (assoc-list as a stopgap).
- Likely more as we go (richer text ops, perhaps a growable buffer).

## The one open decision — the backend dependency line

The classic self-hosting bar is "the compiler is written in its own language
and compiles itself," with the *backend* (assembler, linker, or a codegen
library like LLVM) conventionally allowed to be external — gcc emits assembly
and shells out to an assembler; early Rust used LLVM. By that bar, a
Sigil-written compiler that emits Rust and invokes `rustc` is self-hosting:
no Python in the toolchain, and the compiler compiles itself.

The stricter bar is a fully dependency-free toolchain: Sigil emitting machine
code (or assembly) directly, no `rustc`. That is a much larger effort (its own
register allocation, object-file emission, linking).

**This is the one fork that needs a human call**, and only at the emit stage —
the lexer, parser, and checker are identical work under either answer, so the
bootstrap starts now and the decision is made when codegen comes into view.
