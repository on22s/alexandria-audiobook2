# Incremental speaker-label review (experimental, issue #744)

This CLI compares a saved attribution checkpoint with a provisional labelled
reference. It produces suggestions for human review; it never writes aliases or
changes the app's nickname workflow. Model agreement is not measured accuracy.

Use private files and an output directory outside the repository (enforced by the CLI). Source and
reference JSON contain an `entries` list (or are lists), with `text`, `type`
(`SPOKEN` or `NARRATOR`), and reference `speaker`. Prediction is newline-terminated
JSONL with those fields plus unique `entry_index` values covering a contiguous
prefix of the source. Every predicted text/type must match its indexed source.
Out-of-order rows are accepted. Only uniquely matching reference text/type is
compared; ambiguous/unmatched passages are reported rather than guessed.

```sh
app/env/bin/python app/experiments/incremental_alias_review.py \
  --source /private/source.json --prediction /private/attribution.jsonl \
  --reference /private/reference.json --config /private/config.json \
  --output /private/alias-review-run --allow-network --allow-partial
```

Repeat this command as checkpoints grow. Drop `--allow-partial` for the final
pass: incomplete coverage or an unterminated last line then fails before model
requests. Partial mode ignores only an unterminated final line, never malformed
complete lines. This is a snapshot runner, not a daemon or automatic queue task.

Each snapshot selects the first 20 distinct disagreeing label pairs in checkpoint
file order and reviews each with requested `reasoning_effort` values `none` and
`low`. Both allow 512 visible completion tokens; `low` additionally reserves
512 reasoning tokens (1,024 total requested), subject to the shared context
budget. The completion-budget policy participates in cache identity so verdicts
from the old 512-total policy are not reused. Use an endpoint that supports and honours these options; the mode names
record requests, not a measurement of actual server reasoning. It uses two entries
of surrounding source/reference context; unavailable predicted neighbours are
explicitly unknown. Keep checkpoints append-only for stable ordering. Cache keys
include evidence, labels, context, prompt, mode and the entire active profile
hash. Changed inputs/settings cause new reviews. Final reports contain only
current candidates and are marked reconciled; this means complete input, not
validated truth. Unchanged evidence can be reused during final reconciliation.

The cache persists an attempt reservation before each model call and a validated
verdict immediately afterward. Interrupted attempts consume budget. A run directory
allows at most 40 review invocations across all restarts, including failures and
invalidations. The SDK's existing transport retries remain enabled, so this is
not a ceiling of 40 HTTP requests. Validation retries are disabled for this small
probe; exhausted/failed reviews remain explicit errors and exit nonzero. Use a
new private directory deliberately if additional reviews are needed.

Configure one explicit API profile with `model_name` and the serving model's
actual `context_length`. Automatic failover and manual transport are refused so
cache provenance cannot silently switch models. The shared request pipeline
retains context/token budgeting and provider settings. Local endpoints (and
profiles marked `on_this_gpu`) hold the existing experiment GPU lock. Neither
server configuration nor production retries/locks are changed. The runner cannot
verify a remote server's claimed model/context; keep config current when changing
it and review the output.

`cache.json` and `report.json` contain private verdicts/reasons; the report also
contains candidate labels and source/reference context for human scrutiny. The shared LLM
pipeline also writes response text to the repository's ignored
`logs/persona_responses.log` (or its `ALEXANDRIA_RUN_ID` subdirectory). Keep these
files private; do not attach them to public issues/PRs. CLI output should also be
kept private because existing provider diagnostics may include response details.
Credential/profile values are hashed, not written into review artifacts. The CLI
sets a private umask for new files; use a private parent directory and do not
reuse a shared/public output directory.
