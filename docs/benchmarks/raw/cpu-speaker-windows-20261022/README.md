# CPU speaker-window evidence, 2026-10-22

Start with [the measured summary](SUMMARY.md). Both pilots failed their
predeclared acceptance criterion. This archive adds evidence, not a production
threshold or automatic regeneration policy.

## Verify without models

From the repository root, run:

```sh
python docs/benchmarks/raw/cpu-speaker-windows-20261022/verify.py
```

The standard-library checker verifies byte hashes, protocol/runner identities,
all 384 measurement cells, speaker/donor separation, score summaries, and both
pilot decisions. No audio, model downloads, GPU, or network access is needed.

`run1` preserves the initial pre-scoring failure. `run2` contains the
eight-second-window pilot; `run3` contains the four-second-window follow-up on
entirely new speakers. Source audio and model caches are intentionally excluded.
Input IDs, durations, SHA-256 hashes, model identity, per-window scores, and
execution logs are retained. Archive JSON and frozen scripts are byte-identical
to the measured originals. `SHA256SUMS.json` covers those original files.

## Report erratum

The off-grid description in `SUMMARY.md` and `RESULTS_4S.md` is too broad.
All 32 two-second injections start at 12 or 24 seconds, on the two-second
scoring grid. The four- and eight-second injections are off-grid. This corrects
only the description; the measured scores and both NO-GO decisions stand.
The frozen reports and their checksums are preserved.

## Reproduce scoring

Use the Apache-2.0 AISHELL-3 corpus from [OpenSLR 93](https://www.openslr.org/93/)
and SpeechBrain ECAPA revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`.
Environment and source identities are recorded in the protocols and model
metadata. The original corpus installation was partial; a full download may
change eligibility and therefore random speaker selection. The recorded input
manifests, not a new random draw, define the measured examples.

`frozen/*.py.txt` preserves the executed scripts as research snapshots, not app
entry points. To rerun them in the original environment, copy them to their
original `.py` basenames alongside the archived run directories, then use the
runner's fresh-output-directory argument. The scripts mask all GPU devices,
limit numerical libraries to two threads, and load cached weights offline.
Launch them with `nice -n 15` to reproduce the recorded process priority.

These snapshots contain original absolute corpus/model paths. On another
machine, adapt a separate copy to local paths and explicitly select the archived
speaker/file lists. Record the adapted runner hash and a fresh protocol; do not
overwrite these frozen results or describe altered inputs as an exact rerun.
The post-run analyzers target the original `run2`/`run3` names; the portable
checker above verifies the published archive directly.

No transcripts, copyrighted-book material, audio, credentials, or model weights
are included. This is a controlled Mandarin human-speech instrument study,
not a validation of generated audiobook quality or the paper's WavLM wSIM.
