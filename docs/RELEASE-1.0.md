# Sigil 1.0.0 — release notes (draft, held for sign-off)

Sigil is an **AI-native programming language**: code is written to be *audited*
as much as executed. What a function may do (capabilities, effects) and what it
promises (contracts) are machine-checked facts you read off its signature — not
conventions you hope a dependency honors. 1.0 freezes the single-file language
surface and its toolchain.

## What 1.0 is

A complete, conformance-tested language (390 automated tests, CI on Linux and
Windows) with:

- **Capability security.** No ambient authority. Side effects need an
  unforgeable `Console`/`Fs` value the runtime injects only into `main`;
  capabilities *attenuate* (`read_only(subdir(fs, "sandbox"))`). A dependency
  you never hand a capability provably cannot reach it — most supply-chain
  exfiltration simply will not compile.
- **Effects in the type.** Every function declares what it may do
  (`! {io.write}`); no annotation means provably pure. Checked, transitive,
  visible at the audit surface.
- **Contracts, proven.** `requires` / `ensures` / loop `invariant`, with
  caller / callee / loop blame at runtime — and, with Z3, proven statically by
  `sigil verify`. A proven clause emits **no** runtime check in the native
  binary. The verifier models integer and boolean values exactly, Text/List by
  length, and enum values algebraically (tag + payload slots), so it discharges
  real programs: the bundled [JSON parser](../programs/json) proves **all 49**
  of its contract clauses.
- **Data:** immutable records and exhaustive sum types (`match` as statement
  and expression; a missing variant or a dead arm is a compile error).
- **Generics** over functions, records, and enums, inferred at use sites and
  monomorphized to native code (zero runtime cost).
- **Modules:** `pub` exports and explicit `use mod { item }` imports (no
  globs); importing grants zero authority.
- **Two backends from one checked AST:** a tree-walking interpreter (the
  reference semantics) and native executables via `sigil build` (lowered to
  Rust, compiled with `rustc -O`), held byte-identical by differential tests.
- **Machine-facing tooling:** a canonical formatter with stable content-hash
  declaration ids and semantic diff (`sigil fmt` / `ast` / `sdiff`), and a JSON
  query API (`sigil serve` / `query`) whose `obligations` method hands an LLM
  exactly the unproven clauses to address — code written *against* the prover.

## What 1.0 is NOT (yet)

Honest boundaries, all on the 1.x roadmap:

- **The toolchain is not yet self-contained.** The compiler runs on Python; the
  prover needs `pip install z3-solver`; `sigil build` shells out to `rustc`.
  *Programs* are fully native; the *toolchain* is not. A dependency-free `sigil`
  binary (Z3 statically linked) plus a Cranelift dev backend is the next arc.
- **No `Float`**, no closures or first-class functions, no `Map`, no `?`-style
  error propagation, no `let`-else, and no shared standard library yet (small
  programs copy a `textutil` module).

## Numbers

- 390 tests, green on Linux + Windows.
- `fib(30)`: ~87× faster native than interpreted.
- JSON parser: 49/49 contract clauses proven; 0 runtime contract checks in the
  compiled binary.

## Try it

```
python -m sigil run     examples/hello.sg
python -m sigil verify  programs/json/json.sg     # 49/49 proven
python -m sigil build   programs/json/main.sg     # native executable
python -m sigil fmt --check examples/hello.sg
```

Requires Python 3.12+. `sigil build` needs Rust; `sigil verify` needs
`z3-solver` (without it, every contract simply keeps its runtime check).
