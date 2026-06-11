# Dogfooding report #2 — programs/json

A JSON parser + printer written entirely in Sigil: `textutil.sg` (pure text
helpers, copied from programs/tasks), `json.sg` (recursive-descent parser +
compact printer), `main.sg` (demo over Console only). The program parsed,
rendered, and round-tripped correctly on the first run, interpreted and
native, and **37 of 38 contract clauses prove** (`python -m sigil verify
programs/json/json.sg`). The one runtime check left is structural, not
accidental — see item 1. Friction below, ranked by pain.

## Postscript (resolved for 1.0)

Friction #1 and #2 are now closed, and the parser is **49/49 clauses proven**
— a fully verified JSON parser with zero runtime contract checks:

- **#1 (verifier blind to enum payloads) — fixed by payload-aware
  verification.** An enum value is now modeled as a tag plus payload slots,
  so a `match` learns the exact index a `Done` carries. Each parse function
  gained `ensures match result { Done(v, j) => j >= 0 and j <= len(s),
  Fail(m, p) => true }`; the verifier proves these at the return sites and
  assumes them at call sites, so the index threaded back into `skip_ws`
  arrives bounded and its `requires` — the lone holdout — now proves.
- **#2 (no tuples; two copy-paste step enums) — fixed by generic types.**
  `Step` and `TextStep` collapsed into one `enum Step[T]`; `parse_value`
  returns `Step[Json]`, `scan_string` returns `Step[Text]`, and the
  cross-enum rebuild (`TFail(m, p) => Fail(m, p)`) is gone.

The remaining items (no `?`-propagation, the `textutil` copy / stdlib
question, no Float) stand as 1.x candidates. The list below is preserved as
the historical round-2 record.

## Open friction, ranked by pain

1. **The verifier cannot see inside enum payloads.** The contract this
   parser actually lives by — "every index a `Step` carries is `>= i` and
   `<= len(s)`" — is *inexpressible*: `ensures` sees only params and
   `result`, and `result` is an opaque enum. Worse, the limitation is
   contagious: the moment an index is recovered by `match` (`Done(v, after)
   => ...`), `after` is an arbitrary Int to the prover, so every contract
   downstream of a payload-derived index dies. `skip_ws`'s `requires` is the
   single unproven clause in the program (poisoned by exactly those call
   sites — one bad site marks the clause for the whole program), and
   `tests/test_json_program.py` pins that fact so any improvement shows up
   as a test failure. What clawed the rest back: giving `skip_ws` an
   unconditional `ensures result >= 0 and result <= len(s)` (provable from
   its own requires) so facts regenerate after every whitespace skip, and
   writing entry guards (`if j >= len(s) { return Fail(...) }`) that double
   as error reporting and as path facts. That combination made every
   `char_at` call site prove. Refinement-typed payloads, or ensures over
   match (`ensures result matches Done(_, k) implies k <= len(s)`), would
   delete the whole workaround layer.

2. **No tuples.** Every parsing function wants to return (value, next
   index); instead each payload shape needs its own nominal enum. `Step`
   (Json + Int) was fine — the brief's design — but string scanning needed
   the *same shape again* as `TextStep` (Text + Int) because payload types
   are hard-wired, and converting between them is a manual rebuild
   (`TFail(msg, pos) => { return Fail(msg, pos); }`). A third shape
   (key + value + index for object members) was avoided only by inlining
   member parsing into `parse_object`, which is why that function ends five
   indentation levels deep.

3. **No match-expression and no error-propagation sugar.** Every fallible
   call costs a statement-form `match` with a pass-the-error arm — the
   parser is mostly plumbing that a `?` operator or a match-expression would
   erase. One idiom softened it: bind the step first (`let step: Step =
   parse_value(s, j);`) and re-return it through a wildcard arm (`_ => {
   return step; }`) instead of destructuring and rebuilding the Fail. Five
   of the six match statements in `json.sg` exist only to unpack a Step or
   TextStep; only `render`'s does real case analysis.
   This is the same family as tasks-round friction #6 (arms can't share a
   tail), now at parser scale.

4. **textutil.sg is a copy, not an import.** Modules resolve only in the
   importing file's directory, so the tasks program's text library was
   duplicated wholesale (this round added `has_at`/`is_digit` to the copy —
   the two files have already drifted, proving the point). A stdlib or any
   shared module path is the obvious fix; until then every program re-ships
   its own string utilities.

5. **No Float.** JSON numbers are integers only here; `3.14` or `2e8` is a
   `Fail("numbers are integers only...")` with the position of the `.`/`e`.
   Honest and clearly reported, but it means this is a JSON-subset parser by
   construction, and no workaround inside the language can fix that.

6. **The checker doesn't know `while true` diverges.** `parse_array` /
   `parse_object` loop forever and exit only by `return`, yet "not all paths
   end in a return statement" forces a dead `return Fail("unreachable: ...")`
   after each loop. Dead code mandated by the checker is exactly what a
   canonical-style language says it doesn't want.

7. **No string interpolation.** Error construction is concatenation:
   `"unsupported escape '\\" + e + "'"`, `msg + " at " + str(pos)`. Minor
   per occurrence, constant per parser.

8. Minor notes. (a) The partial-op guardrail means a predicate like
   `is_digit` can never carry `ensures result == (ord(c) >= 48 and ...)` as
   a *proven* clause (any clause containing `ord` stays runtime), so the
   digit-accumulation loop inlines the `ord` comparisons to keep
   `invariant value >= 0` provable, and `is_digit` is used only where proofs
   don't depend on it. (b) `str()` on an enum would have made debugging the
   Step values pleasant; tests reach into the interpreter's tuple encoding
   instead. (c) Duplicate object keys are allowed (kept in order) and
   trailing commas rejected — both were trivial to implement, neither
   stressed the language.

## What was pleasant (worth protecting)

- **37/38 proven in a real parser**, and the unproven clause is a language
  limitation with a name, not a verifier flake. `skip_ws`'s ensures, loop
  invariants on every scan loop, and `char_at`'s requires all proving
  *through* mutual recursion (parse_value <-> parse_array/parse_object) on
  the first `verify` run was genuinely impressive.
- `verify`'s clause-by-clause output made the proof work iterative and
  fast: the single `RUNTIME` line pointed at exactly the right limitation.
- The program worked on the first run, interpreted and native, with
  byte-identical stdout (the differential test pins this).
- `slice`/`ord`/`chr` remain a sufficient primitive set: a full JSON string
  scanner with escape handling needed nothing more.
- `break` checking invariants at every exit (tasks-round fix #3) is exactly
  what the number and whitespace scan loops needed; no flag variables.
- Statement-form exhaustive `match` is verbose (item 3) but its
  exhaustiveness paid: adding the `\u` rejection arm meant touching one
  obvious place, and no other escape path could be forgotten silently.
