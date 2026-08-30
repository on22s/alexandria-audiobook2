# Reconciling two definitions of `meta["gold_files"]` — 2026-08-30

Two sessions changed the same field on the same day, in a way neither side's
tests could detect. Recorded because the *resolution* was the opposite of what
the session doing the reconciling first proposed, and the reason generalises.

## What collided

| | shape | where |
|---|---|---|
| `fix/eval-exclusion-provenance-20260830` (`3ce828051`) | `{book: sha256}` | assigned in `distill_eval.py` **after** the constructor |
| `agent/ingest-cloud-evals-20260830` (PR #423) | `[{gold_path, gold_sha256, gold_lines}]` | inside `ExperimentRecord.__init__` |

Because the first assigns after construction, it **silently overwrote** the
second and changed the field's type. Both branches were green. Any later reader
of `gold_files` would have got whichever shape happened to run last.

## Why the field was being touched at all

A multi-book run stamped one book's gold. The nine cloud evaluations of
2026-08-28/29 scored 383 rows across owarimonogatari3 (162), mushoku16 (133)
and index18 (88), and recorded `gold_path: attribution_gold_index18.json` with
a **correct** hash — verifying 88 of 383 rows. A change to the other two golds
would have left no trace in the artifact.

`ExperimentRecord` could not have done better: it accepted a single path.

## The resolution, and why it inverted

PR #423 proposed keeping its own list shape and dropping the dict. Writing the
Rule 15 guard first — *no script outside `manifest.py` assigns `gold_files`* —
turned up two more sites immediately:

- `lora_serving_eval.py:142`
- `pdnc_narrator_prior.py:113`

both already building `{book: sha256}`. And **34 committed artifacts already
carry that dict**. So the dict was the established convention, not one branch's
invention, and #423's list was the newcomer.

**#423's shape was withdrawn.** Adding a second shape to a corpus that already
has one is worse than the gap it closes, and matching the existing readers cost
nothing.

## What landed

- `ExperimentRecord` builds `gold_files` as `{book: sha256}` — one definition,
  in the class every experiment shares.
- The three per-caller assignments are deleted.
- All three callers pass **every** gold instead of `books[0]`, so the field
  covers every row a run scores.
- `gold_path` / `gold_sha256` / `gold_lines` are unchanged and still describe
  the first file: `narration_signal.py` and `length_bins.py` both select
  artifacts by `os.path.basename(meta["gold_path"])` and would otherwise have
  started matching nothing.
- Kept in full from the other branch: `classify_gold_population` and the
  exclusion ledger, which are orthogonal to hashing and answered four dropped
  rows GOALS 1.3 had recorded as unexplained.

Pinned by `app/tests/test_record_stamps_every_gold.py`, including the guard
that found the extra sites and a test that editing a *second* gold changes the
record — the property the old shape could not see.

## One number that is not settled

The other branch validated `14 excluded = 10 special_speaker + 4
missing_from_checkpoint` against the corrected checkpoint it used. Against
`ab_test_runtime/dialogue_map_5_3/index18__single.json` the count is **56**,
and `index18-00513` has 1 occurrence, so it would not classify as missing
there.

Not a contradiction — a different checkpoint. But the exclusion count is a
property of the gold **and** the checkpoint together, so **do not quote 4 as a
fixed number.** That is an argument for the ledger, not against it.

## The generalisable part

A guard written to enforce a convention proved the author of the guard was the
one breaking it. Before choosing between two shapes, count how many artifacts
already carry each — the corpus is the tiebreaker, not the argument.
