# Sigil

A prototype of an **AI-native programming language**: capability-secure,
effect-typed, contract-carrying — designed to be written by AI and audited by
humans. See [DESIGN.md](DESIGN.md) for the full rationale and roadmap.

```sigil
fn safe_div(a: Int, b: Int) -> Int
    requires b != 0
{
    return a / b;
}

fn main(console: Console) -> Unit ! {io.write} {
    print(console, "10 / 3 = " + str(safe_div(10, 3)));
}
```

## The pitch in three sentences

1. **No ambient authority** — side effects require an unforgeable capability
   value handed down from `main`; a dependency you never gave the filesystem
   *cannot* touch it. Capabilities attenuate: `read_only(subdir(fs, "sandbox"))`
   mints a strictly weaker one to hand to code you trust less.
2. **Effects in the type** — every function declares what it may do (`! {io.write}`);
   undeclared effects are compile errors, and no annotation means provably pure.
3. **Contracts with blame, proven where possible** — `requires`/`ensures` on
   functions and `invariant` on loops; violations name the guilty party
   (caller, callee, or loop). With `z3-solver` installed, `sigil verify`
   proves clauses statically (recursion inductively, loops via invariants)
   and `sigil build` erases every proven runtime check from the binary.
   Unproven clauses conservatively stay.

Plus records (`record Point { x: Int, y: Int }`), sum types with statically
exhaustive `match` (`enum Shape { Circle(Int), Empty }` — a missing variant
is a compile error, a dead arm too), generics over functions, records, and
enums (`fn first[T](xs: List[T]) -> T`, `enum Step[T]` — monomorphized to
native code), and modules (`pub fn` + `use geometry { area, Shape }` —
explicit exports, explicit imports, no globs; importing a module grants zero
authority, because capabilities still only flow through parameters).

The verifier models Text/List lengths and enum payloads, so contracts about
indexing and about what a variant carries prove statically — the bundled
[JSON parser](programs/json) verifies all 49 of its clauses, compiling with
zero runtime contract checks.

## Coming from another language

Sigil deliberately removes conveniences you lean on elsewhere and asks for
things other languages let you omit. The point is that *what a function can do
and what it promises are facts you can read off its signature* — so the cost
is paid at the boundaries. The fastest way to stop fighting it:

**Everyone, on day one:**

- **There is no global `print`, no global filesystem.** I/O needs a
  *capability* value, and capabilities enter only through `main`'s parameters
  and flow down as arguments. A function that needs to write takes a
  `Console`; one that doesn't take a capability provably cannot use it. This
  is the single biggest adjustment.
- **Functions are pure unless they say so.** Side effects are declared after
  `!` (`! {io.write}`); with no annotation the compiler forbids I/O in that
  body.
- **Types are mandatory at every boundary** — parameters, returns,
  `let`/`var`. There is no inference across a signature: the signature *is* the
  audit surface.
- **Immutable by default.** `let` can't be reassigned; reach for `var` only
  when you must, and it stays local.
- **No `null`, no exceptions.** Model absence and failure as ordinary values
  with an `enum` (a `Maybe[T]` / `Result`-style type) and `match`. The only
  thing that "throws" is a contract violation, which halts with blame.
- **`match` is exhaustive, with no fallthrough.** A missing variant won't
  compile; a wildcard that can never fire won't compile either.
- **The error-handling notes you'd write in comments become `requires` /
  `ensures`** — and the verifier tries to prove them.

**From Python / JavaScript / Ruby:** the dynamic freedom is gone — everything
is typed, immutable by default, and `print` is not ambient. But there's no
class/`self` ceremony either: it's free functions over records and enums.
Think "typed, capability-passing scripting."

**From Java / C# / Go:** no classes, interfaces, inheritance, or methods —
records hold data, free functions are behavior. No `null`; errors are return
values (enums), not exceptions. Treat a `Console`/`Fs` like a dependency you
must inject — Sigil enforces the injection and tracks it as an effect.

**From C / C++:** memory safety is automatic and total — no pointers, no
manual allocation, no undefined behavior; values are immutable and copied.
Bounds checks exist, but the verifier deletes the ones it can prove
unnecessary. The discipline you spent on memory you now spend on contracts.

**From Rust:** much will feel familiar — sum types, exhaustive `match`,
"make illegal states unrepresentable," safety-as-proof. The trade: no borrow
checker, lifetimes, or ownership to manage (immutability buys the safety).
New on top: **capabilities** (authority is a value you pass, so a dependency
can't reach a filesystem you never handed it) and **effects + contracts in the
signature**, machine-verified.

**From Haskell / ML / F#:** you'll recognize ADTs, exhaustive matching,
purity-by-default, and the effect discipline (a capability feels like passing
an explicit `IO` handle as a value). Differences: records/enums are nominal,
not structural; no type classes; no laziness; and recursive data currently
routes through `List` (see the quirk below).

**One quirk regardless of background:** a recursive type cannot hold itself
directly yet (there is no `Box`/indirection type), so route recursion through
`List` — `enum Tree { Node(Int, List[Tree]) }`. And there is exactly one
canonical layout (`sigil fmt`), so don't litigate style.

## Quickstart

Requires Python 3.12+. `sigil build` needs Rust (`rustup.rs`); the static
verifier needs `pip install z3-solver` (optional — without it, all contract
checks simply stay at runtime).

```
python -m sigil run examples\hello.sg          # interpreter (reference semantics)
python -m sigil verify examples\contracts.sg   # prove contracts with Z3
python -m sigil build examples\hello.sg        # native executable via rustc
.\hello.exe

python -m sigil fmt --check examples\hello.sg  # THE one canonical rendering
python -m sigil ast examples\hello.sg          # typed AST as JSON, stable ids
python -m sigil sdiff old.sg new.sg            # semantic diff (rename-aware)
python -m sigil serve                          # JSON query API for tools/LLMs
python -m sigil query '{\"method\": \"obligations\", \"source\": \"...\"}'

# These two are SUPPOSED to fail — rejected programs are the product:
python -m sigil check examples\bad_sandbox.sg
python -m sigil check examples\bad_capability.sg
```

The native backend lowers the checked AST to Rust and compiles with `rustc -O`
(`--emit-rust out.rs` to inspect the generated code, `--debug` for fast
unoptimized builds). All safety checks happen at compile time and erase:
capabilities are zero-sized types occupying zero bytes at runtime. Contracts
remain as real branches until the SMT milestone proves them away. Measured on
naive `fib(30)`: 41.2s interpreted, 0.47s native — **~87×**.

Run the tests:

```
python -m unittest discover -s tests
```

## Layout

| path | what |
|---|---|
| [sigil/lexer.py](sigil/lexer.py) | tokenizer (tracks source spans for contract blame) |
| [sigil/parser.py](sigil/parser.py) | recursive-descent parser |
| [sigil/ast_nodes.py](sigil/ast_nodes.py) | AST + type definitions |
| [sigil/checker.py](sigil/checker.py) | type / effect / capability / contract checker |
| [sigil/interp.py](sigil/interp.py) | tree-walking interpreter with runtime contracts |
| [sigil/verify.py](sigil/verify.py) | static contract verifier (Z3 symbolic execution) |
| [sigil/canon.py](sigil/canon.py) | canonical formatter, AST JSON with stable ids, semantic diff |
| [sigil/server.py](sigil/server.py) | compiler-as-a-service JSON API (`serve`/`query`) |
| [sigil/emit_rust.py](sigil/emit_rust.py) | native backend: checked AST → Rust source |
| [sigil/build.py](sigil/build.py) | build driver: emit + `rustc -O` → executable |
| [examples/](examples/) | demo programs, good and (deliberately) bad |
| [tests/](tests/) | end-to-end tests + native-vs-interpreter differential tests |
