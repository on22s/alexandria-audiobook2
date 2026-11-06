# Speech retry

Speech retry is an opt-in, default-off setting under `tts.speech_retry`,
with an `enabled` strict boolean. It is available only together with
speech verification (`tts.speech_verification.enabled`). Both settings
must be enabled for a retry to be possible. Saving retry enabled while
verification is disabled is accepted but refuses every render with an
explicit admission refusal; enable both settings. The existing
`speech_verification` dictionary shape is unchanged.

## When a retry may happen

At most one content retry is made for a line, and only after finished
speech-verification evidence corroborates missing or extra speech in a
newly rendered attempt. A corroborated material defect may reject the
new attempt, keep the prior take, and permit the single retry.

All opted-in attempts refuse publication on unavailable, acoustic-review,
or unknown verification results. On a line that already has a selected
take the prior audio is kept. **On a first render there is no prior take,
so the line ends in `error` with no audio for that attempt.** With retry
off, ordinary speech-verification behaviour is unchanged: unavailable
checks remain explicitly labelled and may publish audio. Turn retry off
to publish a labelled take when the verifier cannot produce a complete
result (unsupported input, missing cached model, unowned render path).

Provider failure, admission refusal, and cancellation are distinct
outcomes. Provider failures are not content-retried. Cancellation is
immediate and does not start a retry.

## Attempt and deadline accounting

There is one shared budget of two parent attempts and a 600 second
deadline. Quality rounds and parallel memory step-down rounds share
that budget; nested counts are not multiplied. Resource-allocation and
codec memory-safety retries remain intact inside one parent render and
consume wall time toward the same deadline. Synthesis windows are not
utterance attempts, but external submissions on the opted-in route are
capped by the same deadline.

The deadline begins on the first line attempt, not when the line is
enqueued. Local and GPU calls cannot be hard-interrupted. Late output
cannot be published after the deadline or after cancellation.

## Seeds, batching, and production path

An opt-in native-batch render is routed to the serial per-line
production worker. That is a throughput cost, and batch seed, group,
and cancellation semantics are preserved through the serial route. The
routed worker runs one line per request (native batch size 1), so the
configured batch-size OOM step-down is not exercised on that path; no
same-size retry is added for it.

The first requested seed honours an explicit batch seed or the selected
voice seed, or is deterministically derived from text plus take for random
or unset seeds. Digit-shaped saved seeds are read as integers, matching
every other consumer of `voice_config.json`. A seed that cannot be bound
to the retry decision's range (non-numeric, or a uint32 seed above
`2**31 - 1`) is not refused: the attempt renders unchanged with the
configured seed and a confirmed mismatch is recorded as `seed_unbound`
without a retry. The retry requests a distinct seed in a call-local copy. There
is no persistent voice or take mutation. A requested seed is not proof
that a remote provider honours the seed or produces different audio.
Ensemble members receive the same requested seed for each opted-in
attempt, including the first; separate member seeds are not retained.

A changed seed may make restoration require a matching seed under the
existing fail-closed restoration rules. No fake certificate or bypass
is provided.

## History and evidence

A bounded `speech_retry_attempts` history, capped at two entries, is
retained with the take. Entries include identifiers, the requested
seed, book and input digests, and complete bounded verification
receipts. A successful publication commits the history that produced the
selected take; a failed regeneration records the rejected attempts while
the selected take, its audio, and its own evidence are preserved. It never
replaces ordinary per-take checks. Raw retry history stays in local stored
metadata; the ordinary chunk API omits it rather than expose unvalidated
attempt receipts as current selected-take evidence.

Replacing or restoring selected audio clears its live retry history unless
the publisher supplies new history for that audio. Retained take snapshots
keep their captured historical evidence.

Existing archive and receipt size limits still apply. Oversized retained
metadata refuses replacement rather than increasing those limits.
The live history is bounded per line, not by the retained-archive book
budget, so large books can carry substantial verification metadata.

Admission failures before a render starts have no attempt receipt. A
cancelled or changed book/line cannot receive writes under stale authority.
If evidence persistence itself fails, the operation reports failure; it
cannot promise a durable failure receipt on an unwritable book. Cancellation
or expiry before verifier-output binding may leave that output unrecorded.
Unexpected verification/publication failures use the admission-refusal
category, with the diagnostic message retained.

Retained history is advisory, not authority. It does not establish
speaker identity, pronunciation correctness, or listening acceptance.
Listening review is still needed.

## Resource and model boundaries

Speech retry downloads no additional ASR or speech models and relaxes
no verification thresholds. It reuses the already installed and cached
resources governed by ordinary speech verification.

## Qualification boundary

Synthetic policy, configuration, UI-handler and publication tests do not
establish actual GPU, browser or real-speech qualification. Those claims
require separate real-environment evidence.
