# Run chains

The scripts that actually produced the 2026-08-07 voice-library results. Kept
because a result whose invocation is lost is a result nobody can re-run, and
several of these encode ordering that is not obvious from the experiment
scripts alone.

Each waits for the GPU via `gpu_job.sh`, so two of them running at once queue
rather than fight over VRAM.

| script | what it produced |
|---|---|
| `run_fidelity.sh` | `library_voice_fidelity.json` — every adapter scored against its own narrator |
| `run_consistency.sh` | `dataset_speaker_consistency.json` — is each dataset one speaker |
| `ref_audit_chain.sh` | `dataset_ref_audit.json` + the n=10 reclassification |
| `run_rebuild_retrain.sh` | `retrain_rebuild_group.json` — do mixed-speaker datasets recover |
| `determinism_chain.sh` | `training_determinism.json` — three runs at one seed |
| `verify_gate_test.sh` | `gate_known_good/bad.json` — proves the identity gate refuses a bad adapter |
| `intervention_chain.sh` | the two-arm reference intervention |
| `sharp_intervention_chain.sh` | the three-arm version with a foreign narrator |
| `rescore_anchor.sh` | re-scored all three language sets after the anchor fix |

## Two things that will bite

**Do not switch git branches while one of these is running.** They read the
working tree. `sharp_intervention_chain.sh` died with `ModuleNotFoundError`
because `voice_reference.py` existed only on an unmerged branch and a
`checkout` pulled it out from under the running job. See CLAUDE.md Rule 20 —
a working tree is shared mutable state in the same way a local ref is.

**Waiting on a PID beats waiting on a pattern.** Several of these take a PID to
wait for. `pgrep -f <pattern>` also matches the shell that ran it, which is one
of the three mistakes `gpu_job.sh`'s own header calls out.

## Paths

`ALEXANDRIA_VOICE_ZIPS` overrides the dataset-zip location,
`ALEXANDRIA_SIBLING_PYTHON` the interpreter holding speechbrain. The scripts
themselves still carry an absolute `REPO=` line, since they were written to be
launched by hand rather than to be portable.

## Writing a chain (2026-08-18 onward)

Source `lib/stage.sh` and drive every stage through `run_stage`. It exists
because 21 of the 30 chains here captured a per-item exit code, printed it, and
never looked at it again — bash discards a loop iteration's status and `set -e`
does not reach inside a loop body. On 2026-08-18 that let the re-gate chain
print `REGATE COMPLETE`, exit 0, and be logged as `OK` while all 67 of its
adapters failed. Two GPU hours measured nothing.

```bash
STAGE_LOG_DIR="$runtime/logs/my_chain"
source "$(dirname "$0")/lib/stage.sh"

run_stage prepare 30m -- "$python" prepare.py
run_stage measure 2h --requires-ok prepare -- "$REPO/gpu_job.sh" measure ...
stage_commit_artifacts measure "$REPO"
stage_summary my_chain      # LAST. Nothing after it may restore a zero exit.
```

- `--requires-ok NAME` is task-spooler's `-W`: run only if that stage ended
  *well*, not merely ended. A stage never run counts as unsatisfied, because a
  resumed chain must not read "I did not see it fail" as success.
- `stage_commit_artifacts` keeps a stage's own output from dirtying the tree
  and getting the next stage refused by `gpu_job.sh`'s gate.
- Failures do not stop the chain: one dead stage at 2am must not take the
  remaining ten hours with it. `stage_summary` still exits non-zero.

**Never edit a chain while it is running.** Bash reads a script incrementally
by byte offset, so rewriting it under a running shell makes it resume at a
meaningless position — this cost an hour of GPU time on 2026-08-18, with a
syntax error the file did not contain. Copy it to a new name instead. The same
applies to `gpu_job.sh`: replace it by atomic rename (write a temp file, then
`mv`), never by truncating in place, since the running wrapper holds the old
inode.
