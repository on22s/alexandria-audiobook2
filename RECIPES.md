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

| model | recipe | status (product-window verdict where available) | evidence | notes |
|---|---|---|---|---|
| Qwen3-14B | "mixed": 2026-08-03 light-novel + PDNC set, 29 files, 2 epochs, 2048 ctx | works: 60.9 → 68.8, +7.8 (+126/−66, p=1.8e-5) | `lora_serving_eval__qwen3-14b-mixed-local-9070xt-inrepo-batch25-20260912.json` | the only adapter measured at the product window that helps there |
| Qwen3-14B | DraCor plays, English (`lacy`+`am`, 4,000 rows), same mixed recipe (2026-09-13) | **hurts at the product window**: 61.7 → 57.8, −3.9 (+79/−109, p=0.034) | `lora_serving_eval__qwen3-14b-dracor-en-a6000-product-batch25-20260913.json` | gains only on mushoku16 (+7); plays carry no narrative frame, see GOALS 1.2 |
| Qwen3-14B | DraCor plays, mixed 6 languages (4,000 rows), same recipe (2026-09-13) | null: 61.7 → 62.2, +0.5 (+91/−87, p=0.82) | `lora_serving_eval__qwen3-14b-dracor-mixed-a6000-product-batch25-20260913.json` | beats the English arm by 4.4 on the same rows; mushoku16 +12, the other three books down |
| Gemma4-12B | Sep-10 recipe: max_len 4096, no warmup, single-entry task4k | **batch-1 indication only**: 55.9 vs 50.8 for the Sep-9 recipe on identical data | `lora_serving_eval__gemma4-12b-task4k-*-schema-checked-20260911.json` | product-window result unverified; do not treat this as a release recipe |
| Gemma4-12B | "mixed-multin": task4k multi-entry + longcontext + hardcases + author-balanced, 1 epoch, r 16, 4096 ctx (2026-09-12) | **fails the product output contract**: 70.6 → 4.2, with 712 of 768 LoRA rows unanswered | `lora_serving_eval__gemma4-mixed-r16-seed2-a6000-product-batch25-mt4096-20260912.json` | emits one entry for a 12–25-entry window ("expected 13 entries, got 1"); its +9 batch-1 result does not reveal this |
| Muse-Glimmer-30B | task4k-multin template-fixed, seed 2 (2026-09-11), served on llama.cpp `b6b003d2c` with the row below | works at the product window: 72.4 → 76.8, **+4.4** (+112/−78, p=0.016), LoRA arm 768/768 answered | `lora_serving_eval__muse-glimmer-30b-task4k-multin-tplfix-seed2-tnr0-product-batch25-q3-jsonschema-reasoningnone-20260913.json` | first Muse adapter measurable at batch 25; the base arm still leaves 89 windows unanswered (46 PassExhausted), which is Muse reasoning past the schema, not the adapter. Earlier batch-1 numbers (54.2 → 61.6 task4k, → 59.9 mixed) stand as batch-1 signal only |
| Qwen3-14B | hardcases only: 1,271 curated rows (`train_hardcases_split_nonmajor.jsonl`), 2026-09-11 adapter | works at the product window: 61.7 → 66.4, **+4.7** (+104/−68, p=0.007) | `lora_serving_eval__qwen3-14b-hardcases-a6000-product-batch25-20260913.json` | 7% of the "mixed" set's rows for 60% of its +7.8 — a small curated set carries most of the skill (Maekawa et al., LREC 2026), not all of it |
| Qwen3.8-27B | author-held-out balanced PDNC adapter, served on llama.cpp `b6b003d2c` | **hurts at the product window**: 79.8 → 77.0, −2.9 (+33/−55, p=0.025) | `lora_serving_eval__qwen38-author-heldout-balanced-tnr0-product-latest-20260913.json` | second adapter this week to lose at batch 25 after a clean batch-1 result; do not ship on batch-1 evidence |
| Qwen3-14B | same mixed-multin recipe (2026-09-12) | **PDNC generalisation concern**: held-out Austen −8.8…+1.0, development Gambler +33.6; product-window result unverified | `pdnc_eval__goal13mm_*.json` (#550) | do not infer Gemma's output-contract failure or Muse's serving failure from this model's held-out result |
| Qwen3-14B | same mixed-multin seed 2, product batch 25 | **quality retries exhausted** on some windows | tnr-2 `qwen14b_mixed_multin_seed2_20260912/eval/eval.log` | server stayed healthy, but some spoken lines returned no valid source-attested speaker; four retries then recorded `PassExhausted`. A batch-10 rerun is queued before changing the adapter. |

## Serving and measuring (the instrument, not the model)

| setting | value | status | evidence |
|---|---|---|---|
| product harness completion budget | `lora_serving_eval.py --max-tokens` = the product's `LLMGenParams.max_tokens` (4096) | works | #549. The harness pinned 2000 — half the product's — and Muse overflowed 70% of windows |
| temperature-0 retry guard | keyed on (prompt, budget), not prompt alone | works | #549: keyed on the prompt, it cancelled every budget escalation, so a truncated window at temp 0 could never recover |
| Muse-Glimmer on llama-server | `--reasoning off --skip-chat-parsing --chat-template-kwargs '{"reasoning_strength":"none"}'` + request-level JSON schema, llama.cpp **`b6b003d2c`** (built 2026-09-13; CTest 59/60, the one failure a missing LFS vocab fixture) | works: full batch-25 run, LoRA arm 768/768 answered, zero PEG-native 500s | `muse_task4k_tnr0_product_20260913/server.log` (0 "output does not match"), the Muse row above | on the 2026-08-23 build the same flags passed a 12-entry smoke and then failed every LoRA request in a full run — the build is part of the recipe. The base arm still emits raw non-JSON on ~12% of windows; that is Muse, not serving |
| Muse-Glimmer on llama-server | `--reasoning off` alone | **leaks reasoning** | same logs, the earlier runs |
| structured attribution responses | request-level `response_format` JSON schema requiring `n` and `speaker` | **works for syntax; semantic gates still required** | tnr-1 `muse_product_mixed-tplfix-seed2_jsonschema_20260913/eval.log` | first Muse windows returned valid JSON with no malformed-output failures; schema validity does not guarantee source-attested speakers |
| product-window repair checklist (Gemma, Muse, Qwen) | preserve four-book gold evaluation; use 4096 completion budget; enforce JSON structure where the server supports it; use batch 10 for Qwen speaker attribution; wait for every same-GPU predecessor to exit | **queued for confirmation** | tnr-1 Muse JSON-schema rerun; tnr-2 Qwen batch-10 rerun; Gemma contract/canary queue on tnr-4 | Gemma needs one response object per frozen entry, Muse needs parser/reasoning containment, and Qwen needs smaller attribution batches when source-attestation retries exhaust |
| LoRA voice serving | `merge_and_unload()` into the talker (#546) | works | goal 4.1: 1.25x → 0.828x realtime on 479 clips, 25 adapters; ECAPA inside seed noise |
