# Results

A compact index of the speaker-attribution results. The raw JSON artifacts are the source of
truth; this page is a map for comparing runs without mixing up test sets. Every artifact is
listed in [RESULTS_INDEX.md](../../RESULTS_INDEX.md).

## How to read these tables

- **Accuracy** counts unanswered or invalid lines as wrong.
- **base → adapter** compares the same served model on the same test set, with the adapter
  switched off (scale 0) and on.
- **Two results are comparable only if** the test set, prompt variant, decoding settings and
  output format all match. The prompt is always shown, because adapters are prompt-specific.
- **Default settings**, unless a row says otherwise:
  - temperature 0;
  - batch size 25 and at most 4,096 output tokens;
  - a JSON schema on every request;
  - 40 evenly spaced windows per book.
- **Reasoning:** "off" means no reasoning; "low" means the provider's low setting, with the
  server budget at 1,024 tokens where recorded. No high-reasoning results are included.

> **Rescored 2026-09-28.** PDNC lists some names that never speak — for example "Small"
> beside The Sign of the Four's Wooden-Legged Man, whose alias is Jonathan Small. A model
> answering SMALL named the right person but was scored wrong. Five reviewed name pairs now
> count as correct, and the paired table below uses that scoring. Ten earlier verdicts
> changed; RECIPES.md §"Rescored 2026-09-28" lists them.

## Paired adapter results — nine PDNC novels

All rows: nine PDNC novels, 2,655 lines, the `michel2_full` prompt, same server for both arms.
The p-value is an exact sign test on the lines where the two arms disagree (fixed / broken).
Adapter names follow the training-prompt labels in
[Hugging Face releases](Hugging-Face-Releases.md).

| model | quant | adapter | reasoning | lines | base | adapter | change | fixed / broken, p | artifact |
|---|---|---|---|---:|---:|---:|---:|---|---|
| Qwen3-8B | Q4_K_M | window25 `michel2v2` | low | 2,655 | 76.6% | 82.8% | **+6.2** | +372/−207, p=7e-12 | `qwen3-8b-q4km-window25-paired-michel2_full-post616-pdnc9-local9070xt-20260926` |
| Qwen3-8B | UD-Q3_K_XL | window25 `michel2v2` | low | 2,655 | 71.8% | 80.8% | **+9.0** | +456/−218, p=3e-20 | `qwen3-8b-ud-q3kxl-window25-michel2v2-paired-michel2_full-pdnc9-local9070xt-20260928` |
| Qwen3.8-27B | Q4_K_M | `michel2v1` | off | 2,655 | 94.4% | 95.9% | **+1.5** | +68/−29, p=9e-5 | `qwen38-q4km-off-michel2_full-pdnc9-tnr4-20260922` |
| Qwen3.8-27B | Q4_K_M | `michel2v1` | low | 2,655 | 95.8% | 96.4% | **+0.6** | +59/−44, p=0.17 | `qwen38-q4km-on-michel2_full-pdnc9-tnr4-20260922` |
| Qwen3.8-27B | UD-Q3_K_XL | `michel2v1` | low | 2,655 | 95.3% | 96.1% | **+0.8** | +66/−45, p=0.057 | `qwen38-q3kxl-on-michel2_full-pdnc9-tnr2-20260922` |
| Qwen3.8-27B | Q2_K_XL | `michel2v1` | low | 2,655 | 93.8% | 93.9% | **+0.1** | +91/−88, p=0.88 | `qwen38-rightsclean-michel2-q2kxl-paired-michel2_full-tnr-0q-pdnc9-20260924` |
| Qwen3.8-27B | UD-IQ2_XXS | `michel2v1` | low | 2,655 | 89.0% | 90.1% | **+1.0** | +126/−99, p=0.083 | `qwen38-rightsclean-michel2-iq2xxs-paired-michel2_full-tnr-0-pdnc9-20260924` |
| Qwen3.6-35B-A3B | UD-IQ1_M | window25 `michel2v2` | low | 2,655 | 90.7% | 92.1% | **+1.5** | +138/−99, p=0.013 | `a3b-window25-rightsclean-michel2-iq1m-paired-michel2_full-tnr-1-pdnc9-20260926` |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | window25 `michel2v2` | low | 2,655 | 90.9% | 91.6% | **+0.7** | +135/−117, p=0.28 | `a3b-window25-rightsclean-michel2-iq2xxs-paired-michel2_full-tnr-1-pdnc9-20260927` |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | window25 `michel2v2` | low | 2,655 | 92.5% | 92.8% | **+0.4** | +96/−86, p=0.5 | `a3b-window25-rightsclean-michel2-iq3xxs-paired-michel2_full-tnr-2-pdnc9-20260927` |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | window25 `michel2v2` | low | 2,655 | 93.2% | 91.1% | **−2.1** | +78/−134, p=1e-4 | [`a3b-window25-rightsclean-michel2-q4kxl-paired-michel2_full-tnr-1-pdnc9-20260927`](../../ab_test_runtime/cloud_pull_20260928/lora_serving_eval__a3b-window25-rightsclean-michel2-q4kxl-paired-michel2_full-tnr-1-pdnc9-20260927.json) |
| Gemma 4 12B | Q4_K_M | window25 | low | 2,655 | 84.4% | 87.0% | **+2.6** | +174/−104, p=3e-5 | [`gemma4-12b-w25-q4km-paired-michel2_full-tnr-0-pdnc9-20260927`](../../ab_test_runtime/cloud_pull_20260928/lora_serving_eval__gemma4-12b-w25-q4km-paired-michel2_full-tnr-0-pdnc9-20260927.json) |
| Gemma 4 E4B | Q4_K_M | window25 (on its training prompt) | low | 2,655 | 63.5% | 66.5% | **+3.0** | +267/−188, p=2e-4 | `gemma4-e4b-w25-newprompt-q4km-paired-michel2_full-tnr-4-pdnc9-20260927` |
| Gemma 4 E2B | Q4_K_M | window25 (on its training prompt) | low | 2,655 | 42.3% | 53.1% | **+10.8** | +460/−174, p=1e-30 | `gemma4-e2b-w25-newprompt-q4km-paired-michel2_full-tnr-0-pdnc9-20260927` |
| Muse-Glimmer-30B | IQ3_XXS | window25b | low | 2,655 | 93.6% | 93.5% | **−0.1** | +87/−89, p=0.94 | `muse-window25b-iq3xxs-paired-michel2_full-tnr-2-post616-pdnc9-20260926` |
| Muse-Glimmer-30B | Q3_K_XL | window25b | low | 2,655 | 94.1% | 94.2% | **+0.1** | +81/−79, p=0.94 | [`muse-window25b-q3kxl-paired-michel2_full-tnr-4-pdnc9-20260926`](../../ab_test_runtime/cloud_pull_20260927/lora_serving_eval__muse-window25b-q3kxl-paired-michel2_full-tnr-4-pdnc9-20260926.json) |
| Muse-Glimmer-30B | Q4_K_M | window25b | low | 2,655 | 94.7% | 93.6% | **−1.1** | +58/−86, p=0.024 | `muse-window25b-q4km-paired-michel2_full-tnr-0-pdnc9-20260924` |
| Muse-Glimmer-30B | Q3_K_XL | KL seed 1 / seed 2 (see RECIPES) | low | 2,655 | 94.6% | 95.1% | **+0.6** | +61/−46, p=0.18 | `muse-kl-window25-q3kxl-paired-michel2_full-tnr-2-pdnc9-20260926` |
| Muse-Glimmer-30B | IQ3_XXS | KL seed 2 | low | 2,655 | 93.1% | 94.6% | **+1.5** | +103/−63, p=0.0024 | `muse-kl-seed2-window25-iq3xxs-paired-michel2_full-tnr-1-pdnc9-20260927` |
| Qwen3-14B | Q4_K_M | rights-clean `attrv1`, `default` prompt | low | 2,655 | 68.4% | 73.2% | **+4.8** | +433/−306, p=3e-6 | `qwen3-14b-rightsclean-default-tnr2-pdnc9lite-low-schema-20260917` |
| Qwen3-14B | Q4_K_M | rights-clean `attrv1`, `michel2_full` prompt | low | 2,655 | 85.3% | 84.4% | **−1.0** | +193/−219, p=0.22 | `qwen3-14b-rightsclean-michel2_full-tnr2-pdnc9lite-low-schema-20260917` |

Notes:

- **The Sun Also Rises** is one of the training novels for the rights-clean `michel2v1`
  adapters (the Qwen3.8 rows). Their honest held-out figure is the other eight novels:
  +1.0 to +1.3 at Q4_K_M, UD-Q3_K_XL and UD-IQ2_XXS. The window25 and KL adapters never
  trained on any of the nine.
- **Muse KL seed 1 at IQ3_XXS** scored −1.5 (p=0.003) on an A6000, while seed 2 above scored
  +1.5 on an A100. A test of seed 2 on the A6000, to tell seed from GPU, is queued.
- Several artifacts live only in the experiment folders and are not committed; the name is
  given so they can be found in [RESULTS_INDEX.md](../../RESULTS_INDEX.md).

## Base-only results

These rows measure a model and quant without an adapter. Use them to choose a base, not to
judge an adapter.

| Model | Quant | Adapter | Prompt | Settings | Fixture | Rows | Accuracy | Conditional accuracy | Artifact / notes |
|---|---|---|---|---|---|---:|---:|---:|---|
| Muse-Glimmer-30B | UD-Q3_K_XL | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels | 2,655 | **93.33%** | 95.21% (1,869/1,963) | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__muse-q3-michel2_full-tnr1-pdnc9lite-low-schema-20260917.json) |
| Muse-Glimmer-30B | Q4 K-quant | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 PDNC novels | 2,655 | **94.61%** | 95.42% (1,873/1,963) | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__muse-q4-kquant17-michel2_full-tnr4-pdnc9lite-low-schema-20260921.json) |
| Qwen3.8-27B | Q4_K_M | none | `michel2_full` | temp 0; reasoning off; batch 25; schema | 9 PDNC novels | 2,655 | 93.5% | — | See the paired row below |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | none | `michel2_full` | temp 0.6; low / 1,024; batch 25; schema | 4-book product fixture | 768 | **89.6%** | 89.6% | Quant ladder in [RECIPES.md](../../RECIPES.md) |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 4-book product fixture | 768 | 88.0% | 88.0% | Quant ladder in [RECIPES.md](../../RECIPES.md) |
| Qwen3.8-27B | UD-Q4_K_M | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 4-book product fixture | 768 | 89.8% | 89.8% | Quant ladder in [RECIPES.md](../../RECIPES.md) |

More base-only ladders, including the 28-novel comparison and model speeds, are in
[Which model for your card](Which-Model-For-Your-Card.md).

## Hosted API baselines

API results are base-only. Do not compare them with adapter changes as if they were the same
experiment.

- **DeepSeek v4-pro** (thinking off), nine novels: **98.0%** after the rescore (96.7% before),
  answering every line.
- **Nemotron 3 Ultra 550B A55B** through paid OpenRouter, per book. These are independent
  single-book baselines: do not pool them into one score. OpenRouter reported the requested
  model, but the provider and hardware were not observed.

| Book | Rows | Correct | Accuracy | Raw artifact |
|---|---:|---:|---:|---|
| Mansfield Park | 734 | 710 | **96.7%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-mansfieldpark-michel2full-b8-low-20260922.json) |
| Northanger Abbey | 672 | 656 | **97.6%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-northangerabbey-michel2full-b8-low-20260922.json) |
| Persuasion | 309 | 300 | **97.1%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-persuasion-michel2full-b8-low-20260922.json) |
| The Sign of the Four | 353 | 301 | **85.3%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesignofthefour-michel2full-b8-low-20260922.json) |
| The Sun Also Rises | 1,759 | 1,536 | **87.3%** | [raw result](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesunalsorises-michel2full-b8-low-20260922.json) |

Nemotron settings: temperature 0, low reasoning with a 1,024-token budget, batch size 8, at
most 4,096 output tokens, `michel2_full`, structured output off. These scores predate the
rescore; The Sign of the Four rises from 85.3% to 96.6% once SMALL counts. See
[Evaluation recipes](Evaluation-Recipes.md).

## Provenance

Every published artifact records its prompt variant, model, quant and runtime, adapter path
and hash, gold-file hashes, decoding settings, validation state, and the outcome of every
line. When a run is repeated, both artifacts are kept and the repeat is labelled — the earlier
measurement is never replaced.
