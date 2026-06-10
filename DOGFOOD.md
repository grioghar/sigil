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

1. **The verifier doesn't model `len`.** 6 of this program's 12 contract
   clauses stay at runtime, and *every single one* is `len`-dependent
   (`requires i < len(s)`, `ensures len(result) == len(tasks)`...). Modeling
   `len(Text)`/`len(List)` as an uninterpreted non-negative function — with
   axioms for `slice`, `push`, and literals — would likely flip most of
   them to PROVED. Highest-value verifier improvement by far.
2. **No if-expression / ternary.** The `var flag: Text = "0"; if t.done {
   flag = "1"; }` dance appears three times in ~200 lines. Galling in a
   language that prefers `let`. A `cond ? a : b` or if-expression would
   remove most remaining `var`s in this program.
3. **No early `break`,** so search loops thread `var scanning: Bool`
   state through the condition (see `parse_tasks`). Verifier-friendly
   design for `break` needs thought (it changes the post-loop fact from
   `¬cond` to `¬cond ∨ broke`), but the workaround is noisy.
4. **List update means rebuild** (`set_done` copies the whole list to flip
   one field). Fine at this scale, quadratic in spirit. Either a `set(xs,
   i, x)` builtin or functional-update syntax.
5. **No record functional update** — `Task { done: true, title: t.title }`
   re-lists every field. Wants `t with { done: true }` (canonical-form
   friendly).
6. **Match arms can't share a tail.** Both arms of `handle`'s match end in
   `return tasks;` patterns; match-as-expression would also subsume #2.
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
