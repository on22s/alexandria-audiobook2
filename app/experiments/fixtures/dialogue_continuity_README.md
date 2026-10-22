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
