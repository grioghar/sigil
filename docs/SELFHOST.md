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

## The backend: fully dependency-free (decided)

The bar is the strict one: **no external tools anywhere on the compile path.**
No `rustc`, no LLVM, no assembler, no linker, no libc. The Sigil compiler emits
**native machine code directly** and writes a complete executable file itself.

What that entails, concretely:

- **Target: Linux x86-64, static ELF, raw syscalls.** This is the only target
  that can be *truly* dependency-free: the Linux syscall ABI is stable, so a
  static ELF can `read`/`write`/`open`/`mmap`/`exit` with zero linked
  libraries. (macOS and Windows both *mandate* linking a system library —
  Apple does not support raw syscalls, Windows syscall numbers are unstable —
  so neither can be dependency-free. Sigil's dependency-free output therefore
  targets Linux; on the Windows dev box it is built and run under WSL or in
  CI's `ubuntu-latest`.)
- **Codegen** the compiler must contain: instruction selection to x86-64
  machine code, register allocation, layout of an ELF (header + program
  headers + code + data), and direct emission of the executable's bytes.
- **A runtime, also dependency-free.** Sigil values (List/Text/records/enums)
  are heap-allocated; with no libc the runtime allocates via `mmap` and does
  I/O via the `read`/`write` syscalls. Capabilities lower to nothing (as
  today); `print`/`read_file`/etc. become syscall sequences.

This is a large, deep effort — its own code generator, register allocator,
object-file writer, and syscall runtime. It is the genuine cost of "compiles
with its own toolchain, dependency-free," and it is the work between here and
1.0.

### First: prove it's physically possible

Before building the full codegen, the riskiest unknown is settled with a
minimal vertical slice: a Sigil program that emits the bytes of a hand-built
static x86-64 ELF doing `exit(42)` via a raw syscall, writes them with the new
`write_bytes` primitive, and — run on Linux — exits 42. If Sigil can produce a
working dependency-free executable at all, the rest is (large but) known
engineering. That demo lives in [`selfhost/`](../selfhost) and is exercised in
CI on `ubuntu-latest`.
