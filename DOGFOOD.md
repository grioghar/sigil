# Dogfooding report #1 — programs/tasks

The first real Sigil program: a multi-module task-tracker CLI
([programs/tasks/](programs/tasks/)) — `textutil.sg` (pure text library
written in Sigil), `store.sg` (serialization), `main.sg` (app loop with a
capability jailed to `data/`). Written to find out what actually hurts.
Verdict: the language held up better than expected — the program worked on
the first run, interpreted and native — but the friction list below is real.

## Fixed during this round

1. **Text was opaque.** No way to inspect characters at all, which made any
   parser unwritable. Fixed with the minimal primitive set — `slice`, `ord`,
   `chr` — and everything richer (`split_first`, `trim`, `parse_int`,
   `starts_with`) is now written *in Sigil* in `textutil.sg`. That layering
   felt right and should become the stdlib pattern.
2. **No way to check file existence**, so any app that loads state would
   fault on first run with no recourse. Added `file_exists(fs, path)`
   (effect `fs.read`, respects capability jails).

## Open friction, ranked by pain

1. ~~**The verifier doesn't model `len`.**~~ **FIXED.** Text/List values now
   carry symbolic lengths (exact for literals, `slice`, `push`, `+`, `str`,
   `chr`); partial operations contribute execution facts; short-circuit
   operands and clause-internal partial ops are soundness-guarded. Result:
   **all 16 contract clauses and invariants in this program are now PROVED**
   (was 6/12 before the round; the program gained three strengthening
   invariants like `invariant len(out) == i` along the way — written, not
   weakened). Zero runtime contract checks remain in the native binary, and
   a test pins the program at zero obligations forever.
2. ~~**No if-expression / ternary.**~~ **FIXED**: `if t.done then "[x]"
   else "[ ]"` — branches z3.If-merged in the verifier, untaken-branch facts
   soundness-guarded like short-circuit operands.
3. ~~**No early `break`.**~~ **FIXED**, with the design rule that invariants
   hold at every loop exit: each break site is a proof obligation (or a
   runtime check when unproven), and `¬cond` is no longer assumed after a
   breaking loop. `parse_tasks` lost its `scanning` flag.
4. ~~**List update means rebuild.**~~ **FIXED**: `set(xs, i, x)` builtin;
   the result provably preserves length.
5. ~~**No record functional update.**~~ **FIXED**: `t with { done: true }`,
   base-evaluated-first semantics preserved in the Rust lowering.
   Combined effect of #2+#4+#5: `set_done` went from a 16-line proof-carrying
   loop (three invariants) to `return set(tasks, index, tasks[index] with
   { done: true });` — and the program still proves completely (13/13).
6. **Match arms can't share a tail.** Both arms of `handle`'s match end in
   `return tasks;` patterns; match-as-expression would subsume this.
7. Minor: no `Map` type yet (assoc lists were fine here); `str()` on a
   record/enum for debugging would help; Windows console codepage mangles
   UTF-8 from the interpreter (native binaries print correctly — cosmetic,
   not a language issue).

## What was pleasant (worth protecting)

- The capability jail (`subdir(fs, "data")`) took one line and the program
  is now *provably* unable to touch anything else on disk.
- Exhaustive `match` over `Parsed`/`Split` caught two missing-case bugs
  while writing `mark_done` — before the first run.
- The module layering (`main` → `store` → `textutil`) with explicit `pub`
  fell out naturally; private `parse_line` being invisible to `main` is
  exactly right.
- `parse_int`'s loop invariant proving `value >= 0` end-to-end through the
  prover, in a real program, on the first try.
