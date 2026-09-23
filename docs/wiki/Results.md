# Results

This is the compact results index for the attribution evaluations. The raw
JSON artifacts remain the source of truth; the table is a navigation aid for
comparing runs without mixing fixtures.

## How to read this page

- Accuracy counts unanswered or invalid rows as incorrect unless the column is
  explicitly labelled **strict shared**.
- `base → adapter` compares the same served model and fixture with adapter
  scale 0 versus the recorded adapter arm.
- A result is comparable only when the fixture, prompt variant, decoding
  settings, and output contract match. The prompt is shown because adapters
  are prompt-specific.
- The complete machine-generated ledger is [RESULTS_INDEX.md](../../RESULTS_INDEX.md).

Unless a row says otherwise, the product-window runs used temperature `0`,
batch size `25`, maximum `4,096` output tokens, request-level JSON schema,
and 40 evenly spaced windows per book. `reasoning off` means no reasoning;
`reasoning low` means the provider's low-effort setting with the server budget
set to `1,024` tokens where that setting is recorded. No high-reasoning result
is included in these tables. The raw artifact remains authoritative when an
older run does not expose every setting in its summary metadata.

## Base-only results

These rows measure the model/quant without an adapter. They are useful for
choosing a base, not for judging adapter quality.

| Model | Quant | Adapter | Prompt | Settings | Fixture | Rows | Accuracy | Conditional accuracy | Artifact / notes |
|---|---|---|---|---|---|---:|---:|---:|---|
| Muse-Glimmer-30B | UD-Q3_K_XL | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels | 2,655 | **93.33%** | 95.21% (1,869/1,963) | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__muse-q3-michel2_full-tnr1-pdnc9lite-low-schema-20260917.json) |
| Muse-Glimmer-30B | Q4 K-quant | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels | 2,655 | **94.61%** | 95.42% (1,873/1,963) | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__muse-q4-kquant17-michel2_full-tnr4-pdnc9lite-low-schema-20260921.json) |
| Qwen3.8-27B | Q4_K_M | none | `michel2_full` | temp 0; reasoning off; batch 25; schema | 9 PDNC novels | 2,655 | 93.5% | — | See the paired row below |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | none | `michel2_full` | temp 0.6; low / 1,024; batch 25; schema | 4-book product fixture | 768 | **89.6%** | 89.6% | Quant ladder in [RECIPES.md](../../RECIPES.md) |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 4-book product fixture | 768 | 88.0% | 88.0% | Quant ladder in [RECIPES.md](../../RECIPES.md) |
| Qwen3.8-27B | UD-Q4_K_M | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 4-book product fixture | 768 | 89.8% | 89.8% | Quant ladder in [RECIPES.md](../../RECIPES.md) |

## Paired adapter results

| Model | Quant | Adapter | Prompt | Settings | Fixture | Rows | Base accuracy | Adapter accuracy | Delta | Strict shared | Verdict / artifact |
|---|---|---|---|---|---|---:|---:|---:|---:|---|---|
| Qwen3.8-27B | Q4_K_M | rights-clean | `michel2_full` | temp 0; reasoning off; batch 25; schema | 9 PDNC novels | 2,655 | 93.5% | **95.8%** | **+2.4 pp** | +92/−29, p=8e-9 | Positive; `qwen38-q4km-off-michel2_full-pdnc9-tnr4-20260922` |
| Qwen3.8-27B | UD-Q3_K_XL | rights-clean | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels* | 2,655 | 95.2% | **96.0%** | **+0.8 pp** | +61/−45, p=0.145 | Positive point estimate; [raw result](../../ab_test_runtime/experiments/lora_serving_eval__qwen38-q3kxl-on-michel2_full-pdnc9-tnr2-20260922.json) |
| Qwen3.6-35B-A3B | IQ3_XXS | rights-clean | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels | 2,655 | 91.6% | 71.3% | **−20.3 pp** | base 92.3% → adapter 72.8%; +74/−582, p=9.05e-99 | **Negative repeat; do not promote.** [raw artifact](../../ab_test_runtime/experiments/lora_serving_eval__a3b-iq3xxs-on-michel2_full-pdnc9-tnr0-20260923.json) |
| Qwen3.6-35B-A3B | IQ1_M | rights-clean | `michel2_full` | temp 0; reasoning off; batch 25; schema | 9 PDNC novels | 2,655 | 89.4% | 85.7% | −3.7 pp | +130/−228, p=2.5e-7 | Negative; `a3b-iq1m-off-michel2_full-pdnc9-tnr0-20260922` |
| Qwen3-14B | Q4_K_M | rights-clean | `default` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels | 2,655 | 67.6% | **72.6%** | **+5.0 pp** | — | Positive; `qwen3-14b-rightsclean-default-tnr2-pdnc9lite` |
| Qwen3-14B | Q4_K_M | rights-clean | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels | 2,655 | 84.3% | 84.3% | 0.0 pp | — | Null; `qwen3-14b-rightsclean-michel2_full-tnr2-pdnc9lite` |
| Muse-Glimmer-30B | — | mixed-lossfix | Michel prompt | temp 0; low / 1,024; batch 25; schema | 4-book product fixture | 768 | 84.0% | 76.3% | −7.7 pp | +51/−110, p=3.83e-6 | Negative; do not promote |

\* The Qwen3.8 UD-Q3_K_XL run includes Sun Also Rises, which is marked as
training-overlap in the artifact metadata; treat the pooled result as a
paired measurement with that caveat rather than as a clean held-out estimate.

## API and non-adapter baselines

Paid/API results are base-only and must not be compared to adapter deltas as
though they were the same experiment. The OpenRouter Nemotron run used
`michel2_full`, low reasoning, temperature 0, batch 8, and no structured
output; see [Evaluation recipes](Evaluation-Recipes.md) for its per-book
scores and limitations.

### Nemotron 3 Ultra 550B A55B — paid OpenRouter

These are independent per-book API baselines. OpenRouter reported the
requested model, but provider routing and hardware are not observed. Do not
pool these percentages into a model-wide score or interpret them as adapter
gains.

| Book | Rows | Correct | Accuracy | Raw artifact |
|---|---:|---:|---:|---|
| Mansfield Park | 734 | 710 | **96.7%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-mansfieldpark-michel2full-b8-low-20260922.json) |
| Northanger Abbey | 672 | 656 | **97.6%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-northangerabbey-michel2full-b8-low-20260922.json) |
| Persuasion | 309 | 300 | **97.1%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-persuasion-michel2full-b8-low-20260922.json) |
| The Sign of the Four | 353 | 301 | **85.3%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesignofthefour-michel2full-b8-low-20260922.json) |
| The Sun Also Rises | 1,759 | 1,536 | **87.3%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesunalsorises-michel2full-b8-low-20260922.json) |

Settings: temperature 0, low reasoning, 1,024-token reasoning budget,
batch size 8, maximum 4,096 output tokens, `michel2_full`, structured output
off.

## Provenance

Every published artifact records its prompt variant, model, quant/runtime,
adapter path and hash, gold-file hashes, decoding settings, validation state,
and row-level outcomes. When a row is repeated, keep both artifacts and label
the repeat rather than replacing the earlier measurement.
