# Script Review diagnostics and editable annotations

## Diagnosing startup (#1959)

Script Review now records a small durable startup trace before API admission,
then carries the same correlation ID into the background worker, subprocess and
review executor threads. The Script Review panel has an always-visible **Download
full review log** link. It works after a rejected start even when no worker log
exists. Batch review diagnostics are available from its task log download too.

The download prepends the startup JSONL trace to the existing worker log. Look
for the latest `admission_received` ID, then follow that ID through
`admission_returned`, `worker_received`, `subprocess_launch`, `subprocess_started`,
`cli_imports_started`, `cli_imports_complete`, `arguments_parsed`,
`config_load_started`, `config_loaded`, `model_setup_started`,
`client_create_started`, `client_created`, `concurrency_setup_started`,
`concurrency_setup_complete` and `model_request_started`. A missing next stage
helps identify where work stopped. `_failed` events record exception classes;
`subprocess_finished` records the exit code. Cancellation/process termination
can leave a last stage without a CLI completion event. A worker callback returning
only describes callback completion; inspect the subprocess exit code and task
status for the actual result.

Each record has a UTC Unix timestamp, correlation ID, process/parent IDs and process-local
elapsed milliseconds. Subprocess start/finish events also identify the direct child PID. On supervised
native runtimes that is the owner process, linked to the CLI through its parent ID.
Elapsed values restart in each process; use timestamps and stages to correlate
processes. The trace allows numeric counts/options and exception class names,
never book text, prompts, names, paths, endpoint URLs or credentials. Existing
worker logs retain their existing contents and may include more detailed/private
information: inspect them before sharing. A failed admission never truncates an
active or previous worker log.

Each task's active `*-startup.log` is bounded to 256 KiB, with one bounded
`.previous` file retained beside it. Writers coordinate rotation and appends
using the existing cross-process kernel file lock. The download includes both retained files (up to 512 KiB total), including recent
prior attempts. Very long runs can outlive this bounded history; early stages may
then be unavailable. A read/lock failure produces an explicit retry message. If trace persistence fails, the task continues with a warning;
this diagnostic facility must not prevent review. Direct CLI runs also record
startup events in the runtime data directory's `logs/api/review-startup.log`.

The original reporter's startup hang remains unreproduced. This change exposes
future failures and API admission errors; it does not establish that every cause
of a stalled review has been fixed.

## Editable review annotations (#1958)

Both ordinary full-text review and experimental indexed labels-only review can
return these optional corrections, in addition to their required fields:

| Field | Accepted values |
| --- | --- |
| `type` | `NARRATOR`, `SPOKEN` |
| `spoken` | JSON Boolean |
| `speaker_gender` | `male`, `female`, `genderless`, `unknown` |
| `speaker_age_group` | Canonical age-group names from `speaker_traits.py` |
| `speaker_ageless` | JSON Boolean |

Omitting type keeps the source role hint. Omitting another annotation keeps the
source value, or the unambiguous known-speaker fallback when changing speakers. Explicit `null` clears that annotation. Explicit valid
corrections take precedence over fallback traits. Invalid types/enums trigger the
normal retry policy; string Booleans are not coerced. Noncanonical legacy
annotations are omitted from model input, including surrounding context, so the
model is not asked to echo values forbidden by the response contract. Their
original values remain source-bound unless a valid correction is returned. Corrected annotations are
included in changed-entry counts, the dedicated annotations-changed counter,
total changes and checkpoint/partial recovery. Older counters that lack annotation
accounting preserve corrections while marking inherited counts unverified. This
conservative warning can appear once when resuming a pre-upgrade checkpoint:
its saved contribution rows do not prove the new annotation-count contract. Annotation-only
partial recovery requires exact, unique source text at the same position.

Source text, order, spans, provenance, pauses and arbitrary other source metadata
remain protected by source binding. Full-text review can still perform lossless
boundary corrections; conflicting source facts are partitioned rather than
collapsed. Indexed mode cannot split or merge entries. HTTP and manual transport
use the same response validation and application logic. Custom review templates
receive the runtime response contract too. Review algorithm identity is version
11, so receipts/checkpoints from the old annotation policy cannot satisfy a new
review.

## References consulted before model review

- [Starlette exceptions](https://starlette.dev/exceptions/): background task
  failures occur after the response is sent, requiring worker-side diagnostics.
- [Python logging cookbook](https://docs.python.org/3.10/howto/logging-cookbook.html#logging-to-a-single-file-from-multiple-processes):
  ordinary file handlers do not serialize multiple processes; coordinate writes.
- [JSON Schema object reference](https://json-schema.org/understanding-json-schema/reference/object):
  optional properties, required fields and additional-properties validation are
  separate concerns. Indexed replies keep their existing required labels while
  permitting the documented optional annotations.
