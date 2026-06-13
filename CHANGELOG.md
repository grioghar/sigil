# Changelog

All notable changes to Sigil are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); Sigil's pre-1.0 milestones
are grouped by the roadmap phase that introduced them (see
[DESIGN.md](DESIGN.md)).

## [1.0.0] — Self-hosting

**A language is 1.0 when it compiles with its own toolchain.** Sigil now does.

[cc0](selfhost/cc0.sg) is a Sigil compiler written in Sigil. It compiles a
practical subset of Sigil — integer and boolean arithmetic, functions,
recursion, `Text`, `List[T]`, records, enums and exhaustive `match`, generics,
`if`/`while`, capabilities, and file I/O — directly to a **static x86-64 Linux
ELF**, by hand-encoding machine instructions and wrapping them in an ELF with a
writable heap segment. There is **no rustc, LLVM, assembler, linker, or libc**
on the compile path: cc0 reads source via raw `read`/`open` syscalls, emits
bytes via `write`, and exits via `exit`.

The proof is a **byte-identical fixpoint**:

1. The Python reference toolchain runs cc0 to compile cc0's own three sources
   (`lexer.sg` + `parser.sg` + `cc0.sg`) into `cc0_stage1`.
2. `cc0_stage1`, running natively, compiles the same sources into `cc0_stage2`.
3. `cc0_stage1 == cc0_stage2`, byte for byte — and `cc0_stage3` (compiled by
   stage 2) equals them too. The compiler reproduces itself exactly.

`tests/test_selfhost_bootstrap.py` performs this fixpoint on Linux CI. The
Python toolchain remains the reference semantics and the bootstrap compiler;
the road past 1.0 (see [DESIGN.md](DESIGN.md)) widens cc0's accepted subset
toward the full surface and adds the 1.1 source-to-Sigil translator.

How cc0 was built, in committed increments:

- **Native vertical slice → whole programs.** Expressions to exit codes; a
  stack machine with hand-encoded x86-64; locals/params/`if`/`while`;
  functions, parameters, calls, recursion via a two-pass absolute-address
  layout; `print` via `write` with a rodata string section.
- **A heap.** A second writable `PT_LOAD` segment with a bump allocator;
  `List[Int]`, `push`, and heap `Text` (literals, `len`, `ord`, `==`, `+`,
  `slice`) as `[len][…]` blocks; an efficient single-allocation `cat`.
- **Type-directed lowering.** A `type_of` pass picks integer vs. `Text`
  lowering for overloaded `==` and `+`.
- **Records and enums.** Records as fixed-slot heap blocks with field access;
  enums as `[tag][payload…]` blocks with tag-dispatched `match` and payload
  binding; generic type arguments propagated through match binders.
- **Self-host surface.** Multi-file compilation (declarations merge across
  source files); capability params and `print(console, text)`; `chr`; and the
  `read_file` / `write_bytes` / `read_line` syscall runtime — everything cc0's
  own source needs.

## [0.9.0] — Language surface complete

The single-file language and its toolchain are stable and conformance-tested
(390 tests). 0.9 adds the last two surface features and the verifier depth to
make a real program a compile-time theorem — but the toolchain still runs on
Python, so this is 0.9, not 1.0:

- **Generic records and enums.** `record Pair[A, B]` and `enum Step[T]`, with
  type arguments inferred from values and from the construction context, and
  native monomorphization. The nominal answer to the "no tuples" friction: one
  `Step[T]` replaces two copy-paste result enums.
- **Payload-aware verification.** The Z3 verifier models an enum value
  algebraically (tag + payload slots), so contracts reason about what a
  variant carries — `ensures match result { Done(v, j) => j <= len(s), ... }`
  proves at return sites and is assumed at call sites, verifying inductive,
  mutually-recursive parsers. Adversarial soundness tests pin the guards.
- **Fully verified JSON parser.** [programs/json](programs/json) consolidated
  onto `Step[T]` and proven **49/49 clauses** — zero runtime contract checks.

Everything below was developed over the preceding milestones, preserved here
as the project's history.

_Self-hosting — the 1.0 gate — landed next (see the 1.0.0 section above). Still
on the road past 1.0: widening cc0's accepted subset toward the full surface;
`let`-else destructuring; `?`-style error propagation; a shared standard
library; `Float`; and the 1.1 source-to-Sigil translator._

## [0.7] — Modules and imports

- `pub` exports and `use mod { item, x as y }` imports (no globs — every name
  that enters scope is written out).
- Resolver flattens the module graph (cycles, visibility, collisions
  diagnosed against the offending file) into the single-program pipeline.
- Imports grant **zero authority** — capabilities still only flow through
  parameters, so a dependency cannot reach the filesystem it was never handed.

## Dogfooding — first real programs

- **Round 1 ([programs/tasks](programs/tasks)):** a capability-jailed task
  tracker. Drove text primitives (`slice`/`ord`/`chr`), `file_exists`, and
  the verifier's length modeling. Ends fully proven (every contract and loop
  invariant discharged at compile time).
- **Round 2 ([programs/json](programs/json)):** a recursive-descent JSON
  parser + printer in pure Sigil, 37/38 clauses proven. Surfaced the
  payload-verification, tuples, and `?`-propagation items now in 1.0.
- **Ergonomics batch:** if-expressions, record functional update
  (`x with { f: v }`), early `break` (invariants hold at every exit),
  `set(xs, i, x)`, and match-as-expression — each from a recorded friction
  point, each verified to keep the dogfood programs fully proven.

## [0.6] — Sum types

- `enum` with positional payloads and a statically **exhaustive** `match`
  (a missing variant is a compile error; a dead wildcard is too).
- Globally unique variant names; verifier-typed match binders; native Rust
  enums.

## [0.5] — Compiler-as-a-service

- `sigil serve` / `sigil query`: newline-delimited JSON API (check,
  signatures, transitive effects, verify, **obligations**) so an LLM can
  interrogate the compiler while generating instead of generating blind.

## [0.4] — Canonical form

- `sigil fmt`: one idempotent, comment-preserving, AST-round-trip-safe
  rendering. `sigil ast`: serialized typed AST with content-hash ids and
  rename-invariant shape hashes. `sigil sdiff`: semantic diff
  (added/removed/renamed/signature/contracts/body). CI enforces canonical
  examples and programs.

## [0.3] — Static contract verification

- Z3-backed proof of `requires`/`ensures` (0.3) and loop `invariant` clauses
  (0.3b). Proven clauses emit **no** runtime check in the native binary;
  unproven clauses conservatively keep theirs. Recursion handled inductively;
  loop conditions translate under their invariants.

## [0.2] — Data and polymorphism

- **0.2a Capability attenuation:** `read_only(fs)` and path-scoped
  `subdir(fs, p)` — pure, monotonic, enforced by the capability value itself.
- **0.2b Records:** immutable product types, canonical field order,
  capability-aware equality, recursion through `List`.
- **0.2c Generic functions:** `fn first[T](xs: List[T]) -> T`, inference-only
  call sites, native monomorphization.

## [0.1] — Foundations

- Capability security (no ambient authority; `Console`/`Fs` injected only into
  `main`), mandatory effect typing (`! {io.write}`; no annotation = pure),
  and `requires`/`ensures` contracts with caller/callee blame.
- Tree-walking interpreter (the reference semantics) and a native backend that
  lowers the checked AST to Rust and compiles via `rustc -O` — measured ~87×
  over the interpreter on `fib(30)`, with capabilities compiling to
  zero-sized types.
