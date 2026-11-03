# Local support report

In **Setup > Speech engine**, open **TTS timing, loaded models and support report**
and choose **Refresh report**. The JSON preview is the complete download, not a
summary. Download saves those same UTF-8 contents locally. Refresh replaces the
preview; failed refreshes invalidate the old download. Sharing is a separate choice.

This reuses the existing read-only `/api/tts/diagnostics` route and process-wide
50-operation buffer. It reads this AA2 server only: no model initialization,
model-server call, book/settings write, upload, telemetry, issue creation or retry.
Optional collection failures are marked `unavailable` without discarding the
original recorded failure. There is no raw-log archive or free-text opt-in field.

## What it contains

- Checkout/build revision, dirty observation, Python, OS and fixed dependency
  versions. Unknown or excluded values are `null`; custom version suffixes may
  be omitted. Checkout identity is not proof of loaded Python module identity.
- Saved configuration/default backend/device, collected even with no engine or
  a busy engine, separately labelled from retained-engine configuration and observed resident devices
  and recorded operation backend. Dtype stays `null`: this observer does not
  measure it. GPU availability is not memory-capacity qualification.
- Controlled operation, outcome, measured stages and aggregate cache/audio counts.
  Overlapping stage times must not be summed; unmeasured is not zero. Operations
  are process-wide across books, not a selected book's history or a live task log.
- Failure category/stage groups and a `tts-v1-` fingerprint of the controlled
  operation/backend/outcome and sorted category/stage groups. Concurrent workers
  cannot pair one worker's category with another's stage; multiple signals stay
  grouped, and unobserved stages stay null. Return-only failures without a recorded
category use `generation_error`; escaping cancellation exceptions are recorded
  as `interrupted`, not errors.
  Different private
  errors can deliberately share this fingerprint; it is not root-cause identity.
- Known base-model cache presence and historical load-capture availability, not
  model-content verification or proof of current loaded state.

The export projects fixed keys, enums, bounded numbers and constrained version/
revision tokens **before** generic redaction. No exception message, nested cause,
raw log, source text, prompt, transcript, audio, reference name/path, book title,
username, branch, private endpoint or arbitrary metadata is exported. Redaction
alone cannot make arbitrary logs safe to publish. Review the preview nonetheless:
even aggregate usage and environment details may be information you do not want
to share. OS releases keep the numeric version, not host-specific suffixes;
dependency versions omit unrecognised local-build suffixes. Collection status
`collected` means the observer returned, not that all optional values are known.
Record projection runs outside the writer lock. A 48 KiB record budget inside
the 64 KiB bundle limit drops oldest records with an explicit `records_omitted`
count, rather than losing the entire report. No donor implementation was copied.

## Evidence and limits

Synthetic tests inject secrets, Windows/Unix paths, book/prompt text and nested
exceptions into both allowed and extra fields; verify category fingerprints,
original exception identity, unavailable collectors/dependencies, bounded output,
and exact preview/download parity with stale-response and failed-refresh controls.
These are automated synthetic checks, not native model, browser accessibility,
listening, or privacy qualification of arbitrary user-provided logs. When reporting
a problem, distinguish configured from observed state and executed from unrun
validation, as the bug-report template asks.
