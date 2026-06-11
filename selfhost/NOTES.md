# Self-hosting friction log

The road to 1.0 is the Sigil toolchain rewritten in Sigil, compiling itself,
fully dependency-free (see [../docs/SELFHOST.md](../docs/SELFHOST.md)). Writing
the compiler *in* Sigil is the ultimate dogfood; what hurts gets recorded here
and drives the language. Ranked by how much it bites.

## Open

1. **No inline recursive types (no `Box[T]`).** Sigil rejects direct
   enum-in-enum / record-in-record recursion as infinite-size, so an AST
   `Expr` cannot hold a bare `Expr` child. Every recursive child is encoded
   through `List[Expr]` — a binary node carries `[left, right]`, an index node
   `[base, index]`. It works (heap indirection via List), and it round-trips
   correctly, but it is noisy and loses arity in the type (`EBin(Text,
   List[Expr])` where the list is always length 2). A single-slot heap
   indirection — `Box[T]`, a one-element owned reference — would let the AST
   read like the Python one (`EBin(Text, Box[Expr], Box[Expr])`). This is the
   #1 ergonomic gap the compiler surfaces.

2. **No argv.** A real compiler is invoked as `sigil build foo.sg` — it needs
   the path of the file to compile. Sigil's `main(console, fs)` has no access
   to command-line arguments; the bootstrap components read a path on stdin as
   a stopgap. 1.0 needs an `Args` capability (a read-only list of argument
   strings injected into `main`, like `Console`/`Fs`) — capability-shaped, so
   it stays auditable.

3. **No Map / dict.** The checker keeps symbol tables, the monomorphizer keys
   instantiations, the lexer classifies keywords — all naturally `Map`-shaped.
   Today they are association lists over `List` (linear scans) or long `or`
   chains (`is_keyword`). Workable at small scale; a built-in `Map[K, V]`
   (or at least `Map[Text, V]`) would matter once the tables get large.

4. **Module-private types can't be imported.** To let `parser.sg` consume the
   lexer's tokens, `Token` had to be marked `pub`. That is correct, but note
   that there is no way to share a type across modules without exporting it
   from one — there is no shared "types" module concept beyond ordinary `pub`.

## Resolved

- **No raw byte output.** `Text` is UTF-8 and cannot hold arbitrary bytes, so
  a compiler could not write a binary executable. Fixed with
  `write_bytes(fs, path, List[Int])` (each 0..255), which also marks the file
  executable. This is what the dependency-free ELF emitter writes.

## Progress

- **Lexer** (`lexer.sg`): complete; conformance-tested against `sigil.lexer`
  over the example + program corpus.
- **Parser** (`parser.sg`): expression grammar complete and conformance-tested
  against `sigil.parser` (precedence, associativity, unary, calls, indexing,
  field access, lists). Statements, declarations, contracts, and modules next.
- **Backend feasibility** (`elf_exit.sg`): proven — a Sigil program emits a
  static x86-64 Linux ELF that runs and exits 42 (verified on CI).
