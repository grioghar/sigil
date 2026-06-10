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

Plus records (`record Point { x: Int, y: Int }`) and generic functions
(`fn first[T](xs: List[T]) -> T`, monomorphized to native code).

## Quickstart

Requires Python 3.12+. `sigil build` needs Rust (`rustup.rs`); the static
verifier needs `pip install z3-solver` (optional — without it, all contract
checks simply stay at runtime).

```
python -m sigil run examples\hello.sg          # interpreter (reference semantics)
python -m sigil verify examples\contracts.sg   # prove contracts with Z3
python -m sigil build examples\hello.sg        # native executable via rustc
.\hello.exe

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
| [sigil/emit_rust.py](sigil/emit_rust.py) | native backend: checked AST → Rust source |
| [sigil/build.py](sigil/build.py) | build driver: emit + `rustc -O` → executable |
| [examples/](examples/) | demo programs, good and (deliberately) bad |
| [tests/](tests/) | end-to-end tests + native-vs-interpreter differential tests |
