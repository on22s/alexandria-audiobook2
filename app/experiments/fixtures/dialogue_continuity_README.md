# Dialogue continuity experiments

These synthetic fixtures were authored for this experiment and contain no book
excerpts. The passage covers attached and repeated attributions, resumed speech,
questions, exclamations, ellipses, interrupted speech, independent narration and
ambiguous pronouns. One manual empty instruction and zero pause are deliberate.
The drift fixture contains 100 distinct lines for the same character.

From `app/`, export a plan to a new file:

```sh
python -m experiments.plan_dialogue_continuity \
  --input experiments/fixtures/dialogue_continuity_passage.json --out /your/new/plan.json
```

From a clean committed checkout, capture a single baseline/deterministic pair:

```sh
python -m experiments.run_dialogue_continuity \
  --input experiments/fixtures/dialogue_continuity_passage.json \
  --voices /your/experiment/voices.json --config /your/experiment/config.json \
  --row 1 --out /your/new/run-directory
```

The voice file uses the existing AA2 speaker-to-voice configuration format.
Provide the selected speaker's actual locked voice and an explicit integer seed
from 0 through 2147483647 for every effective leaf voice. Shared AA2 alias,
narrator and row timeline selection is materialized once; both arms receive
that mapping. Aliased and ensemble member seeds are checked and recorded,
including seeds selected by a voice version. The configuration must explicitly select local TTS.
The controller copies these inputs into its new isolated workspace before
claiming the normal audio task. It requires a fresh process without an existing
data-directory override. Interrupting requests cancellation and waits for model
cleanup; existing output directories are preserved.
Output must be outside the source checkout. If native model cleanup fails, the
disposable experiment process records `cleanup-failure.json` and exits with code
70 while still holding ownership, so it cannot release a task and keep resident
models alive. Completed pairs remain capture evidence; failed cleanup prevents
successful run completion.

Each completed pair has two validated WAVs and a receipt containing separate
requested and actually submitted text, audio hashes, captured synthesis metadata,
model settings and elapsed time. This is capture evidence only. The controller
does not certify model weights, compare words or endpoint phonemes, measure
voice drift/memory, assemble joins or conduct blinded listening. Its fixture
owner tests do not establish native task or GPU qualification.

Hidden-context rendering/cropping and synthesis-only comma/lowercase transforms
remain pending. No application default is changed. Keep all unqualified #1258
acceptance criteria open until actual native measurements and listening support
them; the 100-line file alone is not a drift result.

## Bounded CPU evaluation workflow (#1258)

`experiments.dialogue_continuity_eval` is the resumable collection/package
framework for this fixture. It uses the existing continuity plan and shared
voice/seed materialization, and runs only the deterministic CPU dry renderer
until a native controller is supplied separately.

```sh
cd app
python -m experiments.dialogue_continuity_eval run \
  --source experiments/fixtures/dialogue_continuity_passage.json \
  --voices experiments/fixtures/dialogue_continuity_dry_voices.json \
  --config experiments/fixtures/dialogue_continuity_dry_config.json \
  --out "$HOME/.cache/aa2-1258-demo/run" --rows 0,1,2 --dry-run

python -m experiments.dialogue_continuity_eval package \
  --run "$HOME/.cache/aa2-1258-demo/run" \
  --package-dir "$HOME/.cache/aa2-1258-demo/package" \
  --out "$HOME/.cache/aa2-1258-demo/public.json" \
  --key "$HOME/.cache/aa2-1258-demo/key.json"
```

The collection is capped at 128 selected rows (the supplied passage has 20; the
drift fixture has 100). The run manifest records the source/voices/config hashes,
source revision,
planner code identity, selected rows, effective voice inputs, seeds, requested
and actually submitted text, audio hashes and completion/failure state. Resume
only reuses a completed pair whose inputs and audio hashes still match; changed
inputs refuse the run, and changed/invalid audio refuses reuse. Cancelled,
failed and incomplete attempts remain under `pairs/<row>/attempt-N/`.
The run record binds each arm's request, path and audio identity as well as its
submitted text. Resume refuses malformed or duplicate pair records and symlinked
pair paths rather than normalizing or following them.

The package public manifest contains only A/B sample names and hashes; the
separate key contains the arm mapping. `package` uses an explicit `--seed` or a
random 64-bit seed when none is given; do not publish the seed or reuse a public
seed for a real blinded study. Packing re-reads every selected pair, checks it
against the run's authoritative selected-row text, and recomputes the
input-fidelity gate; a cached pass or a consistently edited pair state is never
reused. A run that lacks a complete pair for any selected row is refused.
Package, public and key paths are published with a no-overwrite move, so a
failed build cannot delete another actor's output. Any outputs already
published before a later failure are preserved for inspection; remove them
manually before retrying. Staging cleanup clears only the directory it opened
by descriptor, so it may leave an empty staging directory but never deletes a
replacement path.

Answers must include the public manifest's `concealed_key_sha256`. A missing or
different value is refused, so answers cannot be replayed against another
randomized package and credited to the opposite arm. Answer import reports
missing preference answers rather than filling them, keeps the imported
voice-rating values, and keeps input fidelity, content verification, voice
scoring and human preference as separate result sections. A failed or cancelled
collection, a collection whose input-fidelity gate failed, and an import with
missing preference answers exit nonzero. Voice scores remain a separate
category and are retained when supplied.
Import checks the selected-row inventory and distinct per-arm audio before
decoding, and checks fidelity metadata against the concealed key. Public, key
and answers JSON files each have a 4 MiB limit; imports also have limits of 128
pairs, 1,024 listeners and 64 MiB per audio sample. These limits apply together;
an input within the count limits must also fit the byte limits.

The earlier native 100-pair drift experiment (source `4050a23`, recorded in the
#1258 issue discussion; local-only evidence, not checked into this repo)
**failed** its authored/submitted-text fidelity gate: 15 question pairs (30
clips) acquired an extra period after `?`. That result remains failed and is not
relabelled by this framework; the framework only makes the gate explicit before
native work.

This dry-run does not create native audio, run ASR, measure voice drift/memory,
or support a claim that #1258 is complete. A native controller must still bind
the real `run_dialogue_continuity`/AA2 ownership path, model/serving identity,
hidden-context cropping and endpoint phonemes.

In the CPU dry renderer, submitted text equals the requested text by construction;
the fidelity gate only becomes meaningful when a native renderer supplies the
actual text it submitted.
