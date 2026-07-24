# Reasoning-Aware Generation and Damaged-Source Gating

_Design, 2026-07-24. Branch `agent/reasoning-aware-generation`, based on
`origin/main` @ 589c0c2._

## Problem

The eight-book local-model A/B (`ab_test_runtime/results/collect_all_20260723-040555`)
was paused mid-way through its 14th of 48 model/book pairs. Analysis of the 14
completed pairs shows the run was measuring an artifact of our own token-budget
policy rather than model quality.

### Root cause

`qwen3.5-9b-uncensored-hauhaucs-aggressive` is a reasoning model. LM Studio
bills its thinking tokens to `usage.completion_tokens` but returns them in
`message.reasoning_content`, not `message.content`. Verified by live probe:

```
usage: completion_tokens=218, completion_tokens_details.reasoning_tokens=197
message keys: [role, content, reasoning_content, tool_calls]
content: '```json\n[{"n":0,"speaker":"Araragi"}]\n```'   (41 chars)
```

90% of the billed completion was invisible to any logic reading `content`.

`three_pass_generate.py:246` caps the segmentation budget at
`max(512, source_words * segment_output_ratio)`. That ceiling was calibrated on
gemma, a non-reasoning model whose visible output approximates its completion
tokens. Applied to a reasoning model the budget is consumed by thinking, the
response is truncated mid-thought, `content` comes back empty, and the caller
records `missing_json_array`. Token escalation then reports `cannot grow beyond
N` because `hard_max_tokens` was already clamped to the same ceiling, so the
batch is subdivided and the cycle repeats.

Evidence from the run logs (`*/*/run.log`, all 14 pairs):

| model | length-calls | wasted | productive | waste share |
|---|---:|---:|---:|---:|
| gemma | 14 | 0.10 h | 7.78 h | 1.3% |
| qwen3.5 | 735 | 10.54 h | 14.28 h | 42.5% |

`index18` is the extreme case: 503 truncations, zero successful calls, 2.1 h
burned. A representative pair from the response log — same prompt, two attempts:

```
ATTRIBUTE | attempt 1 | finish_reason=length | prompt=2328 completion=10000  -> content empty
ATTRIBUTE | attempt 2 | finish_reason=stop   | prompt=2328 completion=3085   -> valid 14-entry array
```

The prior conclusion that "qwen is several times slower per book" is therefore
substantially an artifact of a budget policy calibrated for a non-reasoning
model. The two ministral models and qwen3.6-27b would have hit the same wall.

### Secondary cause: damaged source

`index18.txt` contains 6,662 literal U+FFFD characters (1.4% of the file). The
damage is baked into the source — the file is valid UTF-8 containing EF BF BD
byte sequences, not a read-time artifact. Production rejects such a source
(`generate_script.py:1161-1167`), but `three_pass_generate.py` has no equivalent
gate, so it spent 2.1 h per model attempting a book that can never succeed.

`three_pass_generate.py:1013` also reads with `errors="replace"`, which can
manufacture the exact damage the gate exists to catch.

## Scope

Four work items, ordered. Items 1-3 are code and tests with no long compute.
Item 4 is the only one that spends GPU time, and costs one book across two arms
rather than the five days the unmodified matrix would have taken.

### 1. Unicode gate and repair

Add `repair_lossy_replacements(text) -> (text, repairs)` to
`source_normalization.py`, alongside the existing `normalize_homoglyph_words`
and `strip_known_front_matter`. This is distinct from `generate_script.py`'s
`fix_mojibake`, which handles the recoverable byte form (`â€™`); this handles
the lossy form where the original bytes are gone.

Context rules, derived from the 6,319 U+FFFD runs in `index18.txt`:

| context | observed | inferred character |
|---|---:|---|
| letter + FFFD + letter (`don�t`, `author�s`) | ~1,400 | `’` U+2019 |
| `.!?,` + FFFD + newline-or-space | ~1,090 | `”` U+201D |
| newline + FFFD + capital (`\n�I`) | ~600 | `“` U+201C |
| word + FFFD + word (`Magic�Fiction`) | many | `—` U+2014 |
| FFFD + year (`� 2019 by Yen Press`) | few | `©` U+00A9 |

The function returns a repair ledger for logging and mutates nothing on disk.

Wire into `three_pass_generate.py` at input load (currently lines 1013-1018),
matching production's order in `generate_script.py:1150-1167`:

```
read -> normalize_homoglyph_words -> strip_known_front_matter
     -> repair_lossy_replacements -> audit_unicode_text -> hard-fail on any
        remaining U+FFFD or unsafe control characters
```

Change `errors="replace"` to strict decoding with an explicit error message.

The gate runs before any LLM call, so a damaged source costs zero seconds
instead of 2.1 hours.

**Success criteria.** One unit test per context rule. `index18.txt` repairs to
zero U+FFFD. Every other A/B input book is byte-identical after the pass
(verified clean today: `arc4_volume10wn`, `grimgar03`, `grimgar06`,
`mushoku16`, `mushoku18`, `mushoku23`, `owarimonogatari3`). A source that
remains damaged after repair exits non-zero before any LLM call.

### 2. Telemetry

Prerequisite for item 4: the probe cannot be measured without it. Today
`diagnostic_failures` records only `pass`, `entry`, `text_sha256`, and
`text_preview` — every causal finding in this analysis came from grepping run
logs by hand.

Extend each failure record with `reason`, `finish_reason`, `prompt_tokens`,
`completion_tokens`, `reasoning_tokens`, `effective_max_tokens`, and `attempt`.
Add a per-run manifest carrying per-pass timing, truncation / subdivision /
near-miss / context-rescue counts, model name, and thinking mode.

The plumbing point already exists: `attempt_observer`
(`generate_script.py:672-685`) already receives most of these fields and
currently drops them.

**Success criteria.** A run that hits a truncation produces a failure record
naming `reasoning_overflow` with non-zero `reasoning_tokens`, and the manifest
reports per-pass timing without any log grepping.

### 3. Reasoning-aware token budget

Two defects, both evidenced above.

**Ceiling is blind to reasoning.** Measure `reasoning_tokens` per model from
observed calls and add a reasoning allowance to the ceiling at
`three_pass_generate.py:246`, instead of assuming completion approximates
visible output. The allowance is the running p95 of that model's observed
`reasoning_tokens`, with a floor of 1,024 for the cold-start case where no
observations exist yet; a model that never reports `reasoning_tokens` gets an
allowance of zero and so keeps today's behaviour exactly.

**Subdivision is the wrong response to thought overflow.** 105 attribution and
10 instruction subdivisions fired on truncations. Halving the input does not
shrink the reasoning preamble, which is why `index18` recorded 503 truncations
and zero successes. When `finish_reason == "length"` with empty `content` and
non-zero `reasoning_tokens`, classify the attempt as `reasoning_overflow`.

Per Rule 10, that classification carries exactly one retry policy, applied
identically on every attempt: raise the budget once to the reasoning-aware
ceiling and retry; if the retry truncates again at that ceiling, fail the batch
fast and record `reasoning_overflow`. Never subdivide, and never escalate more
than once — repeated escalation is what produced the 42.5% waste.

Per Rule 9, this adds a circuit-break; it does not weaken the existing retry or
subdivision paths for the non-reasoning failure modes they were built for.

**Success criteria.** A reasoning model completes a batch that previously
truncated, without subdividing. A non-reasoning model's behaviour on the same
input is unchanged (regression test against gemma's existing outputs).

### 4. Probe harness: thinking on vs off

Plumb `reasoning_effort` through `LLMGenParams` into the existing `extra_body`
at `generate_script.py:635`. Probe results establishing the mechanism:

| method | completion | reasoning | works |
|---|---:|---:|---|
| baseline | 108 | 87 | — |
| `chat_template_kwargs.enable_thinking=false` | 94 | 73 | no, silently ignored |
| `/no_think` prompt suffix | 239 | 218 | no, silently ignored |
| `reasoning_effort: "none"` | **19** | **0** | **yes** |

Only `reasoning_effort` is honoured by this LM Studio build.

Run two arms on **mushoku16** — the smallest qwen book at 1,764 entries, which
previously cost 8 failures and 45 truncations, so it exercises the bug without
costing a day.

Automated measurements per arm: wall time, truncation count, failure count, and
`script_preflight`'s existing `audit_script` and
`is_possible_misattributed_narration` findings.

Manual measurement: a sampler emits the entries where the two arms assign
different speakers and draws ~50 at random for hand-scoring, so the manual pass
touches only genuine disputes rather than the whole book.

**Success criteria.** A speed and structural-metric comparison across both
arms, plus a ~50-entry disagreement sample ready to score. The decision the
probe informs: whether thinking earns its cost, and therefore whether the
remaining matrix runs with `reasoning_effort: "none"` (all models under one
output contract) or with a reasoning allowance (each model as it behaves).

## Out of scope

**Segmentation divergence between arms.** gemma and qwen produce different
entry counts for the same book (grimgar03: 2,677 vs 2,443; mushoku16: 2,066 vs
1,764), so per-book failure counts were never strictly comparable across arms.
This is a real threat to the A/B's validity but is a separate problem from the
budget and gating defects above. Recorded here so it is not lost.

**Resuming the paused matrix.** The `owarimonogatari3` checkpoint is preserved
at stage `attribute`, 1,632/3,901 entries, and remains resumable. Whether to
resume it or restart the matrix under the fixed policy is a decision for after
the probe reports.

## Notes

- Structural metrics do not replace human scoring of attribution correctness
  and emotional-instruction quality. Item 4's design reflects this by pairing
  automated metrics with a sampled manual pass.
- Failed chunks from the paused run remain reconstructable from the copied
  inputs, deterministic chunk indices, source hashes, and checkpoints. A
  targeted regression corpus can be built from them rather than repeating full
  books.
