# Recipes that worked, and the ones that looked like they did

Every row here is a setting that produced a result the evidence tree still
stands on, next to the setting that produced a failure and *looked the same
from the training report*. The point of the file is the second column: a
`training_meta.json` records what a run used, not whether the run was any
good, and on 2026-09-13 a chain copied its recipe from the metadata of the one
LJSpeech adapter that never learned to stop — because that file looks
identical to the working one's. Nothing in the tree said which was which.

Rules for this file:

- A row goes in only with an evidence artifact that shows the result, not
  because the setting is the default.
- A failed recipe stays in, marked `runs away` / `collapses` / `memorises`,
  with the artifact that showed it. Deleting it is how it gets tried again.
- `app/tests/test_recipes.py` reads the **works** rows: a chain under
  `run_chains/` that trains a voice adapter with an explicit `--lr` must use
  a rate this file lists as working. Add the row before the chain.

## Voice adapters (Qwen3-TTS LoRA, `app/train_lora.py`)

| use | recipe | status | evidence | the failure that taught it |
|---|---|---|---|---|
| library voice (one narrator, shipped) | `batch_train_lora.py` defaults: 6 epochs, lr 1e-6, r 64, alpha 128, 180–200 clips | works | 67 of the shipped `lora_models/*/training_meta.json` carry exactly this; goal 2.3 gate passes at 1.01x / 0.87x / 0.94x | see next row |
| eval-set adapter (LJSpeech, Hi-Fi TTS) | `train_lora.py --epochs 6 --lr 1e-6 --lora_r 32 --lora_alpha 128`, 200 clips, seed 1234 | works | `ab_test_runtime/ljspeech_eval/adapter_lr1e6/`, the adapter behind `ljspeech_generate.json` and every 2.9 English number | `ljspeech_eval/adapter/` (lr 5e-6): 163.8 s of audio for every held-out line, loss 2.95 looked normal. Reproduced 2026-09-13 on Hi-Fi 9017 (`hifitts_9017_eval/adapter_lr5e6_runaway/`, loss 3.12, also normal). |
| any voice adapter | lr 5e-6 | **runs away** | `app/tests/test_training_defaults.py`: 2 of 2 at 5e-6 never emitted end-of-speech; 3 of 3 at 1e-6 stopped | training loss cannot see it; only generated audio can — which is why the 2.3 stop gate (`verify_adapter_stops.py`, refuse above 3.0x) belongs in every chain before a generate stage |

## Attribution adapters (LLM LoRA, `app/experiments/distill_train.py` and the per-model cloud trainers)

| model | recipe | status at the product window (batch 25, in-repo harness) | evidence | notes |
|---|---|---|---|---|
| Qwen3-14B | "mixed": 2026-08-03 light-novel + PDNC set, 29 files, 2 epochs, 2048 ctx | works: 60.9 → 68.8, +7.8 (+126/−66, p=1.8e-5) | `lora_serving_eval__qwen3-14b-mixed-local-9070xt-inrepo-batch25-20260912.json` | the only adapter measured at the product window that helps there |
| Gemma4-12B | Sep-10 recipe: max_len 4096, no warmup, single-entry task4k | works on the batch-1 harness: 55.9 vs 50.8 for the Sep-9 recipe on identical data | `lora_serving_eval__gemma4-12b-task4k-*-schema-checked-20260911.json` | not yet measured at batch 25 |
| Gemma4-12B, Muse, Qwen3-14B | "mixed-multin": task4k multi-entry + longcontext + hardcases + author-balanced, 1 epoch, r 16, 4096 ctx, same data and recipe on all three (2026-09-12) | **collapses** at batch 25: Gemma 70.6 → 4.2 (712 of 768 LoRA rows unanswered); Muse mixed-tplfix seed 2 grimgar03 85.2 → 25.7. **Memorises** on PDNC: Qwen held-out Austen −8.8…+1.0, development Gambler +33.6 | `lora_serving_eval__gemma4-mixed-r16-seed2-a6000-product-batch25-mt4096-20260912.json`, tnr-1 `muse_product_mixed-tplfix-seed2*/eval.log`, `pdnc_eval__goal13mm_*.json` (#550) | returns ONE entry for a 12–25-entry window ("expected 13 entries, got 1"); +9 points on the batch-1 schema-checked harness, which never shows it |

## Serving and measuring (the instrument, not the model)

| setting | value | status | evidence |
|---|---|---|---|
| product harness completion budget | `lora_serving_eval.py --max-tokens` = the product's `LLMGenParams.max_tokens` (4096) | works | #549. The harness pinned 2000 — half the product's — and Muse overflowed 70% of windows |
| temperature-0 retry guard | keyed on (prompt, budget), not prompt alone | works | #549: keyed on the prompt, it cancelled every budget escalation, so a truncated window at temp 0 could never recover |
| Muse-Glimmer on llama-server | `--reasoning off --skip-chat-parsing --chat-template-kwargs '{"reasoning_strength":"none"}'` | works | tnr-0 `muse-*-reasoningnone-run-mt4096/eval.log` (2026-09-13): completions 750–1,180 tokens and ~26 s per window, against 1,385–3,019 tokens and ~55 s with `--reasoning off` alone — reasoning was leaking into the content |
| Muse-Glimmer on llama-server | `--reasoning off` alone | **leaks reasoning** | same logs, the earlier runs |
| LoRA voice serving | `merge_and_unload()` into the talker (#546) | works | goal 4.1: 1.25x → 0.828x realtime on 479 clips, 25 adapters; ECAPA inside seed noise |
