# Legacy attribution audit — 2026-08-05

All 195 legacy-metadata artifacts are listed exactly once. Classification describes whether the recorded measurement can be used with today's fixtures; it does not turn accuracy into a product or perceptual conclusion.

## Counts

- `exploratory`: 113
- `historical_only`: 30
- `provisional`: 13
- `supported_measurement`: 39

`historical_only` means current-gold rescoring changes at least one judgment or cannot map at least one row. Original files remain preserved; their saved summaries were not rewritten.

## Per-artifact audit

| artifact | family | class | rows | changed scores | unmapped | dirty | problems |
|---|---|---|---:|---:|---:|---|---|
| `batch_contiguity__index18__qwen__qwen3-14b__local-rocm-contig.json` | batch_contiguity | exploratory | 184 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_contiguity__mushoku16__qwen__qwen3-14b__local-rocm-contig.json` | batch_contiguity | exploratory | 266 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_contiguity__owarimonogatari3__qwen__qwen3-14b__local-rocm-contig.json` | batch_contiguity | exploratory | 324 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_size__grimgar03__qwen__qwen3-14b__local-llamacpp-wide.json` | batch_size | exploratory | 1200 | 41 | 12 | True | saved summary differs from row recomputation |
| `batch_size__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 1600 | 27 | 16 | False | saved summary differs from row recomputation |
| `batch_size__index18__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 396 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_size__mushoku16__qwen__qwen3-14b__local-llamacpp-wide.json` | batch_size | exploratory | 417 | 0 | 0 | True | saved summary differs from row recomputation |
| `batch_size__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 556 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_size__owarimonogatari3__qwen__qwen3-14b__local-llamacpp-rep1.json` | batch_size | exploratory | 486 | 0 | 0 | True | saved summary differs from row recomputation |
| `batch_size__owarimonogatari3__qwen__qwen3-14b__local-llamacpp-rep2.json` | batch_size | exploratory | 486 | 0 | 0 | True | saved summary differs from row recomputation |
| `batch_size__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 648 | 0 | 0 | False | saved summary differs from row recomputation |
| `because_production__grimgar03__qwen__qwen3-14b__local.json` | because_production | exploratory | 800 | 33 | 8 | False | saved summary differs from row recomputation |
| `because_production__mushoku16__qwen__qwen3-14b__local.json` | because_production | exploratory | 278 | 0 | 0 | False | saved summary differs from row recomputation |
| `because_production__qwen__qwen3-14b.json` | because_production | exploratory | 417 | 0 | 0 | True | saved summary differs from row recomputation |
| `candidate_id__qwen__qwen3-14b.json` | candidate_id | historical_only | 294 | 1 | 0 | False |  |
| `closed_set.json` | closed_set | exploratory | 441 | 2 | 0 | True | artifact validation is not ok; environment is missing context_length; environment is missing parallel; no LM Studio load state recorded; no harness fingerprint: the code that ran is unidentified |
| `closed_set__gemma-4-e4b-uncensored-hauhaucs-aggressive.json` | closed_set | exploratory | 441 | 0 | 0 | False | recorded commit is unavailable from current history |
| `closed_set__grimgar03__gemma-4-e4b-uncensored-hauhaucs-aggressive.json` | closed_set | historical_only | 1200 | 50 | 12 | False |  |
| `closed_set__grimgar03__google__gemma-3-27b__thunder-a6000.json` | closed_set | historical_only | 1200 | 54 | 12 | False |  |
| `closed_set__grimgar03__ministral-3-14b-instruct-2512.json` | closed_set | historical_only | 1200 | 52 | 12 | False |  |
| `closed_set__grimgar03__mistralai__magistral-small__local-llamacpp.json` | closed_set | supported_measurement | 1188 | 0 | 0 | False |  |
| `closed_set__grimgar03__mistralai__magistral-small__thunder-a6000.json` | closed_set | historical_only | 1200 | 58 | 12 | False |  |
| `closed_set__grimgar03__qwen__qwen3-14b.CLOUD-a6000.json` | closed_set | historical_only | 1200 | 53 | 12 | False |  |
| `closed_set__grimgar03__qwen__qwen3-14b.LOCAL-9070xt.json` | closed_set | historical_only | 1200 | 53 | 12 | False |  |
| `closed_set__grimgar03__qwen__qwen3-14b.json` | closed_set | historical_only | 1200 | 53 | 12 | False |  |
| `closed_set__grimgar03__qwen__qwen3-14b__local-llamacpp-regold.json` | closed_set | supported_measurement | 1188 | 0 | 0 | False |  |
| `closed_set__grimgar03__qwen__qwen3-32b__thunder-a6000.json` | closed_set | historical_only | 1200 | 59 | 12 | False |  |
| `closed_set__index18__mistralai__magistral-small__local-llamacpp.json` | closed_set | supported_measurement | 297 | 0 | 0 | False |  |
| `closed_set__index18__qwen__qwen3-14b__local-llamacpp-regold.json` | closed_set | supported_measurement | 297 | 0 | 0 | False |  |
| `closed_set__microsoft__phi-4.json` | closed_set | exploratory | 441 | 0 | 0 | False | recorded commit is unavailable from current history |
| `closed_set__ministral-3-14b-instruct-2512-absolute-heresy-i1.json` | closed_set | exploratory | 441 | 2 | 0 | False | recorded commit is unavailable from current history |
| `closed_set__ministral-3-14b-instruct-2512.json` | closed_set | exploratory | 441 | 3 | 0 | False | recorded commit is unavailable from current history |
| `closed_set__mushoku16__google__gemma-3-27b__thunder-a6000.json` | closed_set | historical_only | 417 | 3 | 0 | False |  |
| `closed_set__mushoku16__mistralai__magistral-small__local-llamacpp.json` | closed_set | historical_only | 417 | 1 | 0 | False |  |
| `closed_set__mushoku16__mistralai__magistral-small__thunder-a6000.json` | closed_set | historical_only | 417 | 1 | 0 | False |  |
| `closed_set__mushoku16__qwen__qwen3-14b.json` | closed_set | historical_only | 417 | 2 | 0 | False |  |
| `closed_set__mushoku16__qwen__qwen3-14b__local-llamacpp-regold.json` | closed_set | supported_measurement | 408 | 0 | 0 | False |  |
| `closed_set__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | closed_set | historical_only | 417 | 2 | 0 | False |  |
| `closed_set__mushoku16__qwen__qwen3-14b__local-lmstudio.json` | closed_set | historical_only | 417 | 2 | 0 | False |  |
| `closed_set__mushoku16__qwen__qwen3-14b__local-vulkan.json` | closed_set | historical_only | 417 | 2 | 0 | False |  |
| `closed_set__mushoku16__qwen__qwen3-32b__thunder-a6000.json` | closed_set | historical_only | 417 | 3 | 0 | False |  |
| `closed_set__owarimonogatari3__mistralai__magistral-small__local-llamacpp.json` | closed_set | supported_measurement | 486 | 0 | 0 | False |  |
| `closed_set__owarimonogatari3__qwen__qwen3-14b__local-llamacpp-regold.json` | closed_set | supported_measurement | 486 | 0 | 0 | False |  |
| `closed_set__qwen3.5-9b-uncensored-hauhaucs-aggressive.json` | closed_set | exploratory | 441 | 2 | 0 | False | recorded commit is unavailable from current history |
| `closed_set__qwen__qwen3-14b.json` | closed_set | exploratory | 441 | 2 | 0 | False | recorded commit is unavailable from current history |
| `committed_history__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | committed_history | historical_only | 1200 | 43 | 12 | False |  |
| `committed_history__index18__qwen__qwen3-14b__local-llamacpp.json` | committed_history | supported_measurement | 297 | 0 | 0 | False |  |
| `committed_history__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | committed_history | supported_measurement | 408 | 0 | 0 | False |  |
| `committed_history__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | committed_history | supported_measurement | 486 | 0 | 0 | False |  |
| `context_width__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | context_width | historical_only | 1600 | 49 | 16 | False |  |
| `context_width__index18__qwen__qwen3-14b__local-llamacpp.json` | context_width | supported_measurement | 297 | 0 | 0 | False |  |
| `context_width__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | context_width | supported_measurement | 408 | 0 | 0 | False |  |
| `context_width__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | context_width | supported_measurement | 486 | 0 | 0 | False |  |
| `context_width_production__grimgar03__qwen__qwen3-14b__local-llamacpp-rep1.json` | context_width_production | exploratory | 800 | 26 | 8 | False | saved summary differs from row recomputation |
| `context_width_production__grimgar03__qwen__qwen3-14b__local-llamacpp-rep2.json` | context_width_production | exploratory | 800 | 23 | 8 | False | saved summary differs from row recomputation |
| `context_width_production__grimgar03__qwen__qwen3-14b__local-llamacpp-rep3.json` | context_width_production | exploratory | 800 | 23 | 8 | False | saved summary differs from row recomputation |
| `context_width_production__grimgar03__qwen__qwen3-14b__local.json` | context_width_production | exploratory | 800 | 26 | 8 | False | saved summary differs from row recomputation |
| `context_width_production__index18__qwen__qwen3-14b__local-llamacpp.json` | context_width_production | exploratory | 198 | 0 | 0 | False | saved summary differs from row recomputation |
| `context_width_production__mushoku16__qwen__qwen3-14b__local.json` | context_width_production | exploratory | 278 | 0 | 0 | False | saved summary differs from row recomputation |
| `context_width_production__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | context_width_production | exploratory | 324 | 0 | 0 | False | saved summary differs from row recomputation |
| `crossover__grimgar03__local.json` | segmentation_crossover | historical_only | 7980 | 320 | 60 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r64-mushoku16-tnr2-20260904.json` | distill_eval | supported_measurement | 266 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r64-owarimonogatari3-tnr2-20260904.json` | distill_eval | supported_measurement | 324 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r8-mushoku16-tnr2-20260904.json` | distill_eval | supported_measurement | 266 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r8-owarimonogatari3-tnr2-20260904.json` | distill_eval | supported_measurement | 324 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr5e5_r16-mushoku16-tnr2-20260904.json` | distill_eval | supported_measurement | 266 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr5e5_r16-owarimonogatari3-tnr2-20260904.json` | distill_eval | supported_measurement | 324 | 0 | 0 | False |  |
| `distill_eval__qwen35-27b-lightnovel-teacher-production-off-diagnostic-full-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35-27b-lightnovel-teacher-production-off-diagnostic-n10-a6000-20260831.json` | distill_eval | exploratory | 60 | 0 | 40 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35-9b-apos-damaged-a6000-20260904.json` | distill_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `distill_eval__qwen35-9b-apos-repaired-a6000-20260904.json` | distill_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `distill_eval__qwen35_9b_bf16_author_heldout_balanced-corrected-gold-production-off-diagnostic-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35_9b_bf16_speaker_hardcases_split_nonmajor-corrected-gold-production-off-diagnostic-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35_9b_bf16_speaker_longcontext_tophalf_5epoch-corrected-gold-production-off-diagnostic-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-multientry-thinking-low-full-a100-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-multientry-thinking-low-n10-a100-20260831.json` | distill_eval | exploratory | 60 | 0 | 40 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-multientry-thinking-medium-n10-a100-20260831.json` | distill_eval | exploratory | 60 | 0 | 40 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-multientry-thinking-off-full-a100-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-multientry-thinking-off-n10-a100-20260831.json` | distill_eval | exploratory | 60 | 0 | 40 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-multientry-thinking-xhigh-n10-a100-20260831.json` | distill_eval | exploratory | 60 | 0 | 40 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-nf4-heldout-one_pass-index18-tnr2-20260904.json` | distill_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `distill_eval__qwen38-nf4-heldout-stage1_only-index18-tnr2-20260904.json` | distill_eval | exploratory | 176 | 0 | 0 | False | base: every one of 88 predictions is 'UNKNOWN'; a constant predictor carries no information and is usually a parse failure; tuned: every one of 88 predictions is 'UNKNOWN'; a constant predictor carries no information and is usually a parse failure |
| `distill_eval__qwen38-ratio-mix75-tnr0-20260904.json` | distill_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `distill_eval__qwen38-ratio-task4k-tnr0-20260904.json` | distill_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `distill_eval__qwen38_27b_bf16_origin_author_balanced_lr2e5_1epoch-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38_27b_bf16_origin_hybrid_targets_lr2e5_1epoch-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38_27b_fp8_author_heldout_balanced-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | base: every prediction is empty; this can be an inference failure, not a measured null result; recorded commit is unavailable from current history; tuned: every prediction is empty; this can be an inference failure, not a measured null result |
| `distill_eval__qwen38_27b_fp8_author_heldout_balanced_lr2e5_1epoch-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | base: every prediction is empty; this can be an inference failure, not a measured null result; recorded commit is unavailable from current history; tuned: every prediction is empty; this can be an inference failure, not a measured null result |
| `distill_eval__qwen38_27b_nf4_speaker_hardcases_split_nonmajor-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38_27b_nf4_speaker_longcontext_tophalf_5epoch-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r8-seed20260904-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r8-seed20260905-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `grammar_constraint__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | grammar_constraint | supported_measurement | 1584 | 0 | 0 | False |  |
| `grammar_constraint__index18__qwen__qwen3-14b__local-llamacpp.json` | grammar_constraint | supported_measurement | 396 | 0 | 0 | False |  |
| `grammar_constraint__mushoku16__mistralai__magistral-small__local-llamacpp.json` | grammar_constraint | supported_measurement | 556 | 0 | 0 | False |  |
| `grammar_constraint__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | grammar_constraint | supported_measurement | 648 | 0 | 0 | False |  |
| `joint_scene__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | historical_only | 1200 | 30 | 12 | False |  |
| `joint_scene__index18__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | supported_measurement | 297 | 0 | 0 | False |  |
| `joint_scene__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | historical_only | 417 | 2 | 0 | False |  |
| `joint_scene__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | supported_measurement | 486 | 0 | 0 | False |  |
| `lora_serving_eval__local-balanced-q4-20260825.json` | lora_serving_eval | exploratory | 1360 | 0 | 590 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-hardcases-q4-20260825.json` | lora_serving_eval | exploratory | 1360 | 0 | 590 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-longcontext-q4-20260825.json` | lora_serving_eval | exploratory | 1360 | 0 | 590 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-rocm-lora-b2.json` | lora_serving_eval | exploratory | 450 | 0 | 266 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-rocm-lora.json` | lora_serving_eval | exploratory | 1094 | 0 | 324 | True | saved summary differs from row recomputation |
| `lora_serving_eval__new-author_heldout_balanced-grimgar03.json` | lora_serving_eval | exploratory | 770 | 0 | 0 | True | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__new-speaker_hardcases_split_nonmajor-grimgar03.json` | lora_serving_eval | exploratory | 770 | 0 | 0 | True | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__new-speaker_longcontext_tophalf_5epoch-grimgar03.json` | lora_serving_eval | exploratory | 770 | 0 | 0 | True | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q4km-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 383 | 0 | 295 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q4km-hardcases-clean-gold-repeat-20260828.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q6k-baseline-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 383 | 0 | 295 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q6k-qwen35_9b_bf16_author_heldout_balanced-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q6k-qwen35_9b_bf16_speaker_hardcases_split_nonmajor-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q6k-qwen35_9b_bf16_speaker_longcontext_tophalf_5epoch-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q8-baseline-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 383 | 0 | 295 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q8-hardcases-clean-gold-repeat-20260828.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q8-qwen35_9b_bf16_author_heldout_balanced-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q8-qwen35_9b_bf16_speaker_hardcases_split_nonmajor-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q8-qwen35_9b_bf16_speaker_longcontext_tophalf_5epoch-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35_9b_bf16_author_heldout_balanced-q4km-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35_9b_bf16_speaker_hardcases_split_nonmajor-q4km-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35_9b_bf16_speaker_longcontext_tophalf_5epoch-q4km-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `narrator_prior__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | narrator_prior | exploratory | 800 | 29 | 8 | False | saved summary differs from row recomputation |
| `narrator_prior__index18__qwen__qwen3-14b__local-llamacpp.json` | narrator_prior | exploratory | 198 | 0 | 0 | False | saved summary differs from row recomputation |
| `narrator_prior__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | narrator_prior | exploratory | 278 | 0 | 0 | False | saved summary differs from row recomputation |
| `narrator_prior__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | narrator_prior | exploratory | 324 | 0 | 0 | False | saved summary differs from row recomputation |
| `pdnc_context_evidence__pilot__local-llamacpp.json` | pdnc_context_evidence | supported_measurement | 1200 | None | None | False |  |
| `pdnc_evidence__pilot__local-llamacpp.json` | pdnc_evidence | supported_measurement | 1200 | None | None | False |  |
| `pdnc_narrator_prior__clean-3book.json` | pdnc_narrator_prior | supported_measurement | 720 | None | None | False |  |
| `pdnc_narrator_prior__local-llamacpp-generic.json` | pdnc_narrator_prior | provisional | 240 | None | None | True |  |
| `pdnc_narrator_prior__local-llamacpp.json` | pdnc_narrator_prior | provisional | 480 | None | None | True |  |
| `pdnc_sequence__pilot__local-llamacpp.json` | pdnc_sequence | supported_measurement | 1200 | None | None | False |  |
| `pdnc_sequence__pilot__repeat2.json` | pdnc_sequence | supported_measurement | 1200 | None | None | False |  |
| `pdnc_targeted_sequence__pilot__local-llamacpp.json` | pdnc_targeted_sequence | supported_measurement | 1800 | None | None | False |  |
| `reasoning_arms__grimgar03__google__gemma-3-27b__thunder-a6000.json` | reasoning_arms | exploratory | 2000 | 111 | 20 | False | saved summary differs from row recomputation |
| `reasoning_arms__grimgar03__qwen__qwen3-14b.json` | reasoning_arms | exploratory | 2000 | 79 | 20 | True | saved summary differs from row recomputation |
| `reasoning_arms__index18__qwen__qwen3-14b__local-llamacpp.json` | reasoning_arms | exploratory | 495 | 0 | 0 | False | saved summary differs from row recomputation |
| `reasoning_arms__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | reasoning_arms | exploratory | 810 | 0 | 0 | False | saved summary differs from row recomputation |
| `reasoning_arms__qwen__qwen3-14b.json` | reasoning_arms | exploratory | 695 | 4 | 0 | True | saved summary differs from row recomputation |
| `reasoning_check__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 792 | 1 | 0 | False | saved summary differs from row recomputation |
| `reasoning_check__index18__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 198 | 0 | 0 | False | saved summary differs from row recomputation |
| `reasoning_check__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 278 | 0 | 0 | False | saved summary differs from row recomputation |
| `reasoning_check__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 324 | 0 | 0 | False | saved summary differs from row recomputation |
| `reexamine__qwen__qwen3-14b.json` | reexamine | exploratory | 695 | 0 | 0 | True | saved summary differs from row recomputation |
| `roster_quality__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | provisional | 1540 | 0 | 0 | True |  |
| `roster_quality__index18__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | provisional | 368 | 0 | 0 | True |  |
| `roster_quality__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | provisional | 532 | 0 | 0 | True |  |
| `roster_quality__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | provisional | 648 | 0 | 0 | True |  |
| `roster_warmup.json` | roster_warmup | exploratory | 417 | 0 | 0 | False | recorded commit is unavailable from current history |
| `roster_warmup__ministral-3-14b-instruct-2512.json` | roster_warmup | supported_measurement | 417 | 0 | 0 | False |  |
| `scene_cast__grimgar03__qwen__qwen3-14b__local-llamacpp-look1.json` | scene_cast | provisional | 1155 | 0 | 0 | True |  |
| `scene_cast__grimgar03__qwen__qwen3-14b__local-llamacpp-look6.json` | scene_cast | provisional | 1155 | 0 | 0 | True |  |
| `scene_cast__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | provisional | 1155 | 0 | 0 | True |  |
| `scene_cast__index18__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | provisional | 276 | 0 | 0 | True |  |
| `scene_cast__mushoku16__qwen__qwen3-14b__local-llamacpp-look6.json` | scene_cast | provisional | 399 | 0 | 0 | True |  |
| `scene_cast__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | provisional | 399 | 0 | 0 | True |  |
| `scene_cast__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | provisional | 486 | 0 | 0 | True |  |
| `tag_priority__grimgar03__mistralai__magistral-small__local-llamacpp.json` | tag_priority | exploratory | 792 | 0 | 0 | True | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp-rep1.json` | tag_priority | exploratory | 792 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp-rep2.json` | tag_priority | exploratory | 792 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp-rep3.json` | tag_priority | exploratory | 792 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 800 | 22 | 8 | True | saved summary differs from row recomputation |
| `tag_priority__index18__mistralai__magistral-small__local-llamacpp.json` | tag_priority | exploratory | 198 | 0 | 0 | True | saved summary differs from row recomputation |
| `tag_priority__index18__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 198 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 278 | 0 | 0 | True | saved summary differs from row recomputation |
| `tag_priority__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 324 | 0 | 0 | False | saved summary differs from row recomputation |
| `two_by_two.json` | two_by_two | exploratory | 556 | 0 | 556 | True | artifact validation is not ok; environment is missing context_length; environment is missing parallel; no LM Studio load state recorded; no harness fingerprint: the code that ran is unidentified; saved summary differs from row recomputation |
| `two_stage_attribution__explicit_control.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_explicit_hint.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_inner_narration.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_shuffled_roster.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_speaker_not_addressee.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__smoke.json` | two_stage_attribution | historical_only | 90 | 0 | 60 | False |  |
| `two_stage_attribution_full.json` | two_stage_attribution | historical_only | 2494 | 0 | 1224 | False |  |
| `two_stage_attribution_narrator_w3200.json` | two_stage_attribution | supported_measurement | 200 | 0 | 0 | False |  |
| `two_stage_attribution_riqua.json` | two_stage_attribution | supported_measurement | 1287 | None | None | False |  |
| `two_stage_attribution_w16000.json` | two_stage_attribution | exploratory | 600 | None | None | False | recorded commit is unavailable from current history |
| `two_stage_attribution_w3200.json` | two_stage_attribution | historical_only | 2494 | 0 | 1224 | False |  |
| `two_stage_attribution_w8000.json` | two_stage_attribution | exploratory | 600 | None | None | False | recorded commit is unavailable from current history |
| `two_stage_attribution_wp2021.json` | two_stage_attribution | exploratory | 380 | None | None | False | recorded commit is unavailable from current history |
| `two_stage_attribution_wp2021_train.json` | two_stage_attribution | exploratory | 200 | None | None | False | recorded commit is unavailable from current history |
| `voting__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | voting | exploratory | 1200 | 30 | 12 | False | saved summary differs from row recomputation |
| `voting__index18__qwen__qwen3-14b__local-llamacpp.json` | voting | exploratory | 198 | 0 | 0 | False | saved summary differs from row recomputation |
| `voting__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | voting | exploratory | 417 | 0 | 0 | False | saved summary differs from row recomputation |
| `voting__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | voting | exploratory | 324 | 0 | 0 | False | saved summary differs from row recomputation |
| `xc_riqua__control.json` | two_stage_attribution | exploratory | 1537 | None | None | False | recorded commit is unavailable from current history |
| `xc_riqua__explicit_hint.json` | two_stage_attribution | exploratory | 1537 | None | None | False | recorded commit is unavailable from current history |
| `xc_riqua__inner_narration.json` | two_stage_attribution | exploratory | 1537 | None | None | False | recorded commit is unavailable from current history |
| `xc_riqua__shuffled_roster.json` | two_stage_attribution | exploratory | 1537 | None | None | False | recorded commit is unavailable from current history |
| `xc_riqua__speaker_not_addressee.json` | two_stage_attribution | exploratory | 1537 | None | None | False | recorded commit is unavailable from current history |
| `xc_wp2021__control.json` | two_stage_attribution | exploratory | 1835 | None | None | False | recorded commit is unavailable from current history |
| `xc_wp2021__explicit_hint.json` | two_stage_attribution | exploratory | 1835 | None | None | False | recorded commit is unavailable from current history |
| `xc_wp2021__inner_narration.json` | two_stage_attribution | exploratory | 1835 | None | None | False | recorded commit is unavailable from current history |
| `xc_wp2021__shuffled_roster.json` | two_stage_attribution | exploratory | 1835 | None | None | False | recorded commit is unavailable from current history |
| `xc_wp2021__speaker_not_addressee.json` | two_stage_attribution | exploratory | 1835 | None | None | False | recorded commit is unavailable from current history |

## Family-level interpretation limits

- `batch_alignment`: One book. Where a batch is CUT is not where a batch is SIZED; this cannot be read as a batching policy.
- `batch_contiguity`: Isolates companion ordering, not end-to-end production quality.
- `batch_size`: Accuracy and throughput must be considered together; books differ.
- `because_production`: A justification field test; explanations are not confidence estimates.
- `booknlp_baseline`: An external baseline on one PDNC book, recorded with no notes field. Its provenance is thinner than every other family here; treat it as a reference point, not a measurement of ours.
- `candidate_id`: One model/corpus comparison; opaque IDs do not prove general naming gains.
- `cascade`: Live end-to-end routing whose decisions come from answers produced in the SAME run, so routing and outcome are not independent. Offline pricing is a prediction, not a control arm.
- `closed_set`: Oracle candidate arms are invalid for current claims because their lists used superseded labels.
- `committed_history`: Oracle history is an upper bound and is not shippable state.
- `context_width`: A harness diagnostic; production-path confirmation is separate.
- `context_width_production`: Book-specific repeats; report each book/repeat rather than pooling.
- `distill_eval`: LoRA adapter arms that share one loaded model and differ only by peft disable_adapter(), scored on Japanese light-novel gold books. Three limits travel with every number here. (1) Light novels in translation only - nothing in this family speaks to PDNC or Chinese. (2) A single-book arm is 88 scoreable rows on index18, where a five-point gap is about five rows; the 14B strength ladder's +14.8 at r32 against +9.1 at the shipped r16 is exactly that size. (3) Measured 2026-09-03: every PDNC-trained adapter in this family learned from a corpus where 18.1% of rows carried apostrophes stripped to spaces upstream, which caps the TUNED arm and not the base one, so the deltas are a floor rather than the adapter's ceiling.
- `grammar_constraint`: Roster-valid output does not establish correct speaker identity.
- `joint_scene`: Joint and shuffled controls answer ordering only within the tested fixtures.
- `lora_serving_eval`: Two gold books and one serving stack; not a universal adapter claim.
- `narrator_prior`: A predeclared book-contrast test, not a general narrator rule.
- `pdnc_context_evidence`: A five-book English PDNC pilot at 120 lines per book whose arms differ by 5 correct lines in 600 (57.7% vs 58.5%); sized to decide whether the confirmatory run is worth doing, not to establish an effect, and no confirmatory run exists.
- `pdnc_evidence`: A five-book English PDNC pilot, 120 lines per book, run 2026-08-18: baseline 58.5% against evidence 59.5% overall (351 vs 357 correct of 600), conditional 59.0% vs 61.1%. Six lines apart on a pre-declared gate the arm did NOT clear, so the twenty-book confirmatory set stayed sealed - which is the pilot working, not a result. Nothing here supports a claim that supplying evidence spans helps attribution; it is the reason not to spend the confirmatory run.
- `pdnc_narrator_prior`: Two books and 120 rows per book with an explicitly supplied narrator identity; not a general held-out attribution result.
- `pdnc_sequence`: A five-book English PDNC pilot at 120 lines per book; sequence-aware resolution beats baseline by 14 correct lines in 600 (57.7% vs 60.0%), which is a reason to run the confirmatory arm, not a result.
- `pdnc_targeted_sequence`: A pilot on five newly-opened PDNC books, 120 lines each; the three arms span 8 correct lines in 600 (73.5% / 74.5% / 74.8%), inside noise, and the books were previously sealed so this is also their first exposure.
- `qwen38_forced_choice`: Sixteen rows SELECTED for being tuned-empty and base-answered - conditioned on the outcome it is asked about. It can show whether the answer was recoverable on those rows; it cannot estimate any rate, because the denominator was chosen after seeing the result.
- `reasoning_arms`: Reasoning/justification settings are model- and serving-stack-specific.
- `reasoning_check`: Justification disagreement is a routing signal, not calibrated confidence.
- `reexamine`: Selected previously negative results; selection prevents broad inference.
- `roster_quality`: Gold-roster arms are upper bounds and not deployable inputs.
- `roster_warmup`: Book-quartile diagnostic; oracle roster is not deployable.
- `scene_cast`: Scene-cast extraction and attribution effects cannot be conflated.
- `segmentation_crossover`: Factorial diagnostic on one book; retain repeat-level uncertainty.
- `tag_priority`: Prompt rule effects vary by book/model and require per-book reporting.
- `two_by_two`: The two factors are not independent; this prices context, not batching.
- `two_stage_attribution`: Three English PDNC books (Pride and Prejudice, The Awakening, The Sign of the Four), 2,494 quotes, one request per quote with the cast supplied, qwen3-14b, 2026-08-19: 54.5% against the one-pass baseline of 83.6% on these SAME books. The arm is 29 points WORSE, so this is evidence against this design, not for it - and it is one form (cast-supplied, single request) of one model, not two-stage attribution in general. The internal split is the interesting part and is also the reason not to read the headline alone: Explicit quotes, where the text names the speaker, score 52.9% - LOWER than Anaphoric at 61.7%. A method that misses half the cases the text answers outright is not a weaker method, it is a broken one, and the number to quote is that contrast rather than the 54.5%.
- `voting`: Voting cost and routing coverage accompany accuracy; no pooled policy claim.
