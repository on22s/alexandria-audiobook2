# Retired one-off runners

Nine scripts that lived at the repository root and are invoked by nothing: not
by `run_chains/`, not by CI, not by any skill, not by any guide, and never once
by name in `ab_test_runtime/logs/gpu_jobq.log`. Each was written to drive a
particular run and outlived it.

They are kept rather than deleted because several encode a queue shape that was
tuned by hand — `overnight_chain.sh` and `extended_chain.sh` are the ancestors
of everything in `run_chains/`, and the argument order in the `run_*.sh` batch
scripts records how those batches were actually invoked, which the artifacts
they produced do not.

**Do not add to this directory.** A new chain belongs in `run_chains/`, which
has the skip-if-artifact and fail-loud structure these predate.

What replaced them:

| retired | use instead |
|---|---|
| `overnight_chain.sh`, `extended_chain.sh`, `asr_large_chain.sh`, `tpvs2_chain.sh` | a dated chain in `run_chains/` |
| `run_2book.sh`, `run_new_batch.sh`, `run_remaining_batch.sh`, `run_random_corpus.sh`, `run_smoke.sh` | the batch routes in the app, or a `run_chains/` stage |

## What "unused" had to mean before a file could move

Three scripts were moved here and then moved straight back:
`local_gpu_job.py`, `run_stage6_listening.py` and `run_stage7_pitch.py` each
have a dedicated test module that imports them. The first sweep searched for
the filename *with* its extension and found nothing, because a test imports
`local_gpu_job`, not `local_gpu_job.py`. The suite caught it as four collection
errors — which is the argument for running it before believing a grep.

Four more stayed at the root despite having no code reference at all:
`alexandria_batch_processor.py`, `alexandria_compare.py`, `download_model.py`
and `env_doctor.py` are documented tools a person runs by hand. Four others —
`build_test_corpus.sh`, `run_subset.sh`, `run_with_restart.sh` and
`watch_subset.sh` — are invoked by skills under `.claude/skills/`.

"Nothing imports it" is not the same as "nothing uses it", and it took two
different kinds of miss to establish that.
