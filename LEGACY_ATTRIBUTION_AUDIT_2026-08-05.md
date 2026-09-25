# Legacy attribution audit — 2026-08-05

All 358 legacy-metadata artifacts are listed exactly once. Classification describes whether the recorded measurement can be used with today's fixtures; it does not turn accuracy into a product or perceptual conclusion.

## Counts

- `exploratory`: 246
- `historical_only`: 58
- `provisional`: 11
- `supported_measurement`: 43

`historical_only` means current-gold rescoring changes at least one judgment or cannot map at least one row. Original files remain preserved; their saved summaries were not rewritten.

## Per-artifact audit

| artifact | family | class | rows | changed scores | unmapped | dirty | problems |
|---|---|---|---:|---:|---:|---|---|
| `batch_contiguity__index18__qwen__qwen3-14b__local-rocm-contig.json` | batch_contiguity | exploratory | 184 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_contiguity__mushoku16__qwen__qwen3-14b__local-rocm-contig.json` | batch_contiguity | exploratory | 266 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_contiguity__owarimonogatari3__qwen__qwen3-14b__local-rocm-contig.json` | batch_contiguity | exploratory | 324 | 3 | 0 | False | saved summary differs from row recomputation |
| `batch_size__grimgar03__qwen__qwen3-14b__local-llamacpp-wide.json` | batch_size | exploratory | 1200 | 41 | 12 | True | saved summary differs from row recomputation |
| `batch_size__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 1600 | 27 | 16 | False | saved summary differs from row recomputation |
| `batch_size__index18__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 396 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_size__mushoku16__qwen__qwen3-14b__local-llamacpp-wide.json` | batch_size | exploratory | 417 | 0 | 0 | True | saved summary differs from row recomputation |
| `batch_size__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 556 | 0 | 0 | False | saved summary differs from row recomputation |
| `batch_size__owarimonogatari3__qwen__qwen3-14b__local-llamacpp-rep1.json` | batch_size | exploratory | 486 | 4 | 0 | True | saved summary differs from row recomputation |
| `batch_size__owarimonogatari3__qwen__qwen3-14b__local-llamacpp-rep2.json` | batch_size | exploratory | 486 | 4 | 0 | True | saved summary differs from row recomputation |
| `batch_size__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | batch_size | exploratory | 648 | 1 | 0 | False | saved summary differs from row recomputation |
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
| `context_width_production__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | context_width_production | exploratory | 324 | 1 | 0 | False | saved summary differs from row recomputation |
| `crossover__grimgar03__local.json` | segmentation_crossover | historical_only | 7980 | 320 | 60 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r64-mushoku16-tnr2-20260904.json` | distill_eval | supported_measurement | 266 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r64-owarimonogatari3-tnr2-20260904.json` | distill_eval | historical_only | 324 | 10 | 0 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r8-mushoku16-tnr2-20260904.json` | distill_eval | supported_measurement | 266 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr2e5_r8-owarimonogatari3-tnr2-20260904.json` | distill_eval | historical_only | 324 | 7 | 0 | False |  |
| `distill_eval__qwen3-14b-lr5e5_r16-mushoku16-tnr2-20260904.json` | distill_eval | supported_measurement | 266 | 0 | 0 | False |  |
| `distill_eval__qwen3-14b-lr5e5_r16-owarimonogatari3-tnr2-20260904.json` | distill_eval | historical_only | 324 | 10 | 0 | False |  |
| `distill_eval__qwen35-27b-lightnovel-teacher-production-off-diagnostic-full-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35-27b-lightnovel-teacher-production-off-diagnostic-n10-a6000-20260831.json` | distill_eval | exploratory | 60 | 0 | 40 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35-9b-apos-damaged-a6000-20260904.json` | distill_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `distill_eval__qwen35-9b-apos-repaired-a6000-20260904.json` | distill_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `distill_eval__qwen35_9b_bf16_author_heldout_balanced-corrected-gold-production-off-diagnostic-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35_9b_bf16_speaker_hardcases_split_nonmajor-corrected-gold-production-off-diagnostic-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen35_9b_bf16_speaker_longcontext_tophalf_5epoch-corrected-gold-production-off-diagnostic-a6000-20260901.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38-effort-low-a6000-tnr4-20260906.json` | distill_eval | historical_only | 778 | 0 | 578 | False |  |
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
| `distill_eval__qwen38_27b_fp8_author_heldout_balanced-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | base: 383 rows but not one prediction - the model produced no output. This is a failed run, not a score of zero, and must not be written as a result; base: every prediction is empty; this can be an inference failure, not a measured null result; recorded commit is unavailable from current history; saved summary differs from row recomputation; tuned: 383 rows but not one prediction - the model produced no output. This is a failed run, not a score of zero, and must not be written as a result; tuned: every prediction is empty; this can be an inference failure, not a measured null result |
| `distill_eval__qwen38_27b_fp8_author_heldout_balanced_lr2e5_1epoch-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | base: 383 rows but not one prediction - the model produced no output. This is a failed run, not a score of zero, and must not be written as a result; base: every prediction is empty; this can be an inference failure, not a measured null result; recorded commit is unavailable from current history; saved summary differs from row recomputation; tuned: 383 rows but not one prediction - the model produced no output. This is a failed run, not a score of zero, and must not be written as a result; tuned: every prediction is empty; this can be an inference failure, not a measured null result |
| `distill_eval__qwen38_27b_nf4_speaker_hardcases_split_nonmajor-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__qwen38_27b_nf4_speaker_longcontext_tophalf_5epoch-diagnostic-off-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r16-seed20260904-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r16-seed20260905-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r16-seed20260906-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r32-seed20260904-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r8-seed20260904-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r8-seed20260905-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `distill_eval__rank-seed-control-r8-seed20260906-h100-20260904.json` | distill_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history |
| `grammar_constraint__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | grammar_constraint | supported_measurement | 1584 | 0 | 0 | False |  |
| `grammar_constraint__index18__qwen__qwen3-14b__local-llamacpp.json` | grammar_constraint | supported_measurement | 396 | 0 | 0 | False |  |
| `grammar_constraint__mushoku16__mistralai__magistral-small__local-llamacpp.json` | grammar_constraint | supported_measurement | 556 | 0 | 0 | False |  |
| `grammar_constraint__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | grammar_constraint | supported_measurement | 648 | 0 | 0 | False |  |
| `joint_scene__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | historical_only | 1200 | 30 | 12 | False |  |
| `joint_scene__index18__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | supported_measurement | 297 | 0 | 0 | False |  |
| `joint_scene__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | historical_only | 417 | 2 | 0 | False |  |
| `joint_scene__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | joint_scene | historical_only | 486 | 1 | 0 | False |  |
| `lora_serving_eval__a3b-iq1m-michel2_full-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq1m-off-michel2_full-pdnc9-tnr0-20260922.json` | lora_serving_eval | exploratory | 5310 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq2xxs-default-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq2xxs-michel-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq2xxs-michel2-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq2xxs-michel2_full-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq2xxs-michel2_shot-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq2xxs-on-michel2_full-pdnc9-tnr0-20260922.json` | lora_serving_eval | exploratory | 5310 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq3xxs-default-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq3xxs-michel2-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq3xxs-michel2_full-local-9070xt-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq3xxs-on-michel2_full-pdnc9-tnr0-20260923.json` | lora_serving_eval | exploratory | 5310 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-iq3xxs-rightsclean-michel2-adapter-michel2_full-tnr2-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 5310 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__a3b-q4kxl-on-michel2_full-pdnc9-tnr0-20260922.json` | lora_serving_eval | exploratory | 5310 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-cleangold-batch25-thinking-low-8k-michel2_full-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-cleangold-batch25-thinking-off-michel-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-cleangold-batch25-thinking-off-michel2-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-cleangold-batch25-thinking-off-michel2_full-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-cleangold-batch25-thinking-off-michel2_shot-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-pdnc_emma-batch25-thinking-off-default-20260917.json` | lora_serving_eval | exploratory | 998 | 0 | 0 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-pdnc_emma-batch25-thinking-off-michel2_full-20260917.json` | lora_serving_eval | exploratory | 998 | 0 | 0 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-pdnc_thesunalsorises-batch25-thinking-off-default-20260917.json` | lora_serving_eval | exploratory | 1759 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__deepseek-v4-pro-api-pdnc_thesunalsorises-batch25-thinking-off-michel2_full-20260917.json` | lora_serving_eval | exploratory | 1759 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__gemma4-e2b-w25-earlyread-w20-tnr1-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__gemma4-e4b-w25-earlyread-w20-tnr1-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__local-12h-muse-base-20260916.json` | lora_serving_eval | historical_only | 768 | 0 | 383 | False |  |
| `lora_serving_eval__local-4book-base-20260907.json` | lora_serving_eval | historical_only | 768 | 0 | 383 | False |  |
| `lora_serving_eval__local-balanced-q4-20260825.json` | lora_serving_eval | exploratory | 1360 | 0 | 590 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-hardcases-q4-20260825.json` | lora_serving_eval | exploratory | 1360 | 0 | 590 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-longcontext-q4-20260825.json` | lora_serving_eval | exploratory | 1360 | 0 | 590 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-owarimonogatari3-diagnostic-20260907.json` | lora_serving_eval | historical_only | 162 | 5 | 0 | False |  |
| `lora_serving_eval__local-rocm-lora-b2.json` | lora_serving_eval | exploratory | 450 | 0 | 266 | False | saved summary differs from row recomputation |
| `lora_serving_eval__local-rocm-lora.json` | lora_serving_eval | exploratory | 1094 | 0 | 324 | True | saved summary differs from row recomputation |
| `lora_serving_eval__muse-gen3-scale0.25-pdnc8-pilot-tnr0-20260922.json` | lora_serving_eval | exploratory | 1262 | 0 | 1064 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-gen3-scale0.5-pdnc8-pilot-tnr0-20260922.json` | lora_serving_eval | exploratory | 1262 | 0 | 1064 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-gen3-scale1.0-pdnc8-pilot-tnr0-20260922.json` | lora_serving_eval | exploratory | 1262 | 0 | 1064 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-glimmer-30b-base-local-9070xt-product-batch25-q3-jsonschema-reasoninglow-grimgar03-20260914.json` | lora_serving_eval | supported_measurement | 385 | 0 | 0 | False |  |
| `lora_serving_eval__muse-glimmer-30b-base-local-9070xt-product-batch25-q3-jsonschema-reasoninglow-index18-20260914.json` | lora_serving_eval | supported_measurement | 88 | 0 | 0 | False |  |
| `lora_serving_eval__muse-glimmer-30b-base-local-9070xt-product-batch25-q3-jsonschema-reasoninglow-mushoku16-20260914.json` | lora_serving_eval | supported_measurement | 133 | 0 | 0 | False |  |
| `lora_serving_eval__muse-glimmer-30b-base-local-9070xt-product-batch25-q3-jsonschema-reasoninglow-owarimonogatari3-20260914.json` | lora_serving_eval | supported_measurement | 162 | 0 | 0 | False |  |
| `lora_serving_eval__muse-glimmer-30b-base-tnr0-product-batch25-q3-jsonschema-reasoningmedium-20260914.json` | lora_serving_eval | historical_only | 768 | 0 | 383 | False |  |
| `lora_serving_eval__muse-glimmer-30b-base-tnr4-product-batch25-q3-jsonschema-reasoninglow-mentioned-20260914.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-glimmer-30b-mixed-lossfix-michel-tnr0-low-20260916_working.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-glimmer-30b-task4k-multin-lossfix-onebook-owari-workingrecipe-20260916.json` | lora_serving_eval | exploratory | 324 | 0 | 0 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-glimmer-30b-task4k-multin-lossfix-tnr0-product-batch25-q3-jsonschema-reasoninglow-hint-20260914.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-glimmer-30b-task4k-multin-tplfix-seed2-tnr0-product-batch25-q3-jsonschema-reasoninglow-baseonly-hint-20260914.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-iq2xxs-nani-base-michel2_full-tnr2-pdnc9-20260924.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-iq3m-base-michel2_full-tnr4-pdnc9-20260924.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-iq3xxs-gen3-rightsclean-michel2_full-tnr2-pdnc9-20260923.json` | lora_serving_eval | exploratory | 5310 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel-none-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel2-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel2-none-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel2full-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel2full-low-tnr1-cleangold-reworded-20260920.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel2shot-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-michel2shot-low-tnr1-cleangold-reworded-20260920.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-q3-michel2_full-tnr1-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-q4-kquant17-michel2_full-tnr4-pdnc9lite-low-schema-20260921.json` | lora_serving_eval | exploratory | 2655 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-q4-kquant17-rightsclean-lossfix-michel2_full-tnr4-pdnc9-rerun-b8-clean-20260922.json` | lora_serving_eval | exploratory | 1988 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-iq2xxs-nani-gold-paired-michel2_full-tnr-2-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-iq3m-gold-paired-michel2_full-tnr-4-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-iq3xxs-gold-paired-michel2_full-local9070xt-20260925.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-iq3xxs-gold-paired-michel2_full-tnr-2-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-q3kxl-gold-paired-michel2_full-tnr-1-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-q3kxl-gold-paired-michel2_full-tnr-4-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-q4km-gold-paired-michel2_full-tnr-4-20260924.json` | lora_serving_eval | exploratory | 352 | 0 | 220 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__muse-window25b-q4km-paired-michel2_full-tnr-0-pdnc9-20260924.json` | lora_serving_eval | exploratory | 5310 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__nemotron3-ultra-paid-clean-mansfieldpark-michel2full-b8-low-20260922.json` | lora_serving_eval | exploratory | 734 | 0 | 0 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__nemotron3-ultra-paid-clean-northangerabbey-michel2full-b8-low-20260922.json` | lora_serving_eval | exploratory | 672 | 0 | 0 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__nemotron3-ultra-paid-clean-persuasion-michel2full-b8-low-20260922.json` | lora_serving_eval | exploratory | 309 | 0 | 0 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__nemotron3-ultra-paid-clean-thesignofthefour-michel2full-b8-low-20260922.json` | lora_serving_eval | exploratory | 353 | 0 | 0 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__nemotron3-ultra-paid-clean-thesunalsorises-michel2full-b8-low-20260922.json` | lora_serving_eval | exploratory | 1759 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__new-author_heldout_balanced-grimgar03.json` | lora_serving_eval | exploratory | 770 | 0 | 0 | True | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__new-speaker_hardcases_split_nonmajor-grimgar03.json` | lora_serving_eval | exploratory | 770 | 0 | 0 | True | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__new-speaker_longcontext_tophalf_5epoch-grimgar03.json` | lora_serving_eval | exploratory | 770 | 0 | 0 | True | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen14-rightsclean-default-pdnc8-replication-tnr2-20260922.json` | lora_serving_eval | exploratory | 4620 | 0 | 3984 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-base-local-9070xt-inrepo-batch25-20260912.json` | lora_serving_eval | historical_only | 768 | 0 | 383 | False |  |
| `lora_serving_eval__qwen3-14b-base-tnr1-cleangold-minorhint-20260914.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-base-tnr4-cleangold-prompt-current-20260914.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-base-tnr4-cleangold-prompt-v2-20260914.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-dracor-en-a6000-product-batch25-20260913.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | False |  |
| `lora_serving_eval__qwen3-14b-dracor-en-prose-a6000-product-batch25-20260914.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | False |  |
| `lora_serving_eval__qwen3-14b-dracor-mixed-a6000-product-batch25-20260913.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | False |  |
| `lora_serving_eval__qwen3-14b-michel-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-michel-none-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-michel2-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-michel2-none-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-michel2full-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-michel2shot-low-tnr1-cleangold-replication-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-mixed-local-9070xt-inrepo-batch1-20260912.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | True |  |
| `lora_serving_eval__qwen3-14b-mixed-local-9070xt-inrepo-batch25-20260912.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | True |  |
| `lora_serving_eval__qwen3-14b-rightsclean-a6000-product-batch25-20260914.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | False |  |
| `lora_serving_eval__qwen3-14b-rightsclean-default-tnr2-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 5310 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-rightsclean-local-9070xt-product-batch25-budget1024-schema-grimgar03-20260917.json` | lora_serving_eval | supported_measurement | 770 | 0 | 0 | False |  |
| `lora_serving_eval__qwen3-14b-rightsclean-local-9070xt-product-batch25-budget1024-schema-index18-20260917.json` | lora_serving_eval | supported_measurement | 176 | 0 | 0 | False |  |
| `lora_serving_eval__qwen3-14b-rightsclean-local-9070xt-product-batch25-budget1024-schema-mushoku16-20260917.json` | lora_serving_eval | supported_measurement | 266 | 0 | 0 | False |  |
| `lora_serving_eval__qwen3-14b-rightsclean-local-9070xt-product-batch25-budget1024-schema-owarimonogatari3-20260917.json` | lora_serving_eval | supported_measurement | 324 | 0 | 0 | False |  |
| `lora_serving_eval__qwen3-14b-rightsclean-michel2_full-tnr2-pdnc9lite-low-schema-20260917.json` | lora_serving_eval | exploratory | 5310 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-rightsclean-tnr2-cleangold-budget2048-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-rightsclean-tnr2-cleangold-budget512-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-14b-riqua-a6000-product-batch25-20260914.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | False |  |
| `lora_serving_eval__qwen3-14b-riqua3ep-a6000-product-batch25-20260914.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | False |  |
| `lora_serving_eval__qwen3-14b-riquax2-retry-20260915c.json` | lora_serving_eval | historical_only | 1536 | 0 | 766 | False |  |
| `lora_serving_eval__qwen3-30b-a3b-instruct-2507-tnr0-cleangold-product-batch25-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-30b-a3b-thinking-2507-michel2_full-tnr4-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-30b-a3b-thinking-2507-tnr0-cleangold-product-batch25-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-8b-q4km-default-local-9070xt-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-8b-q4km-michel2-post616-local-9070xt-cleangold-low-budget1024-schema-20260924.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-8b-q4km-michel2_full-local-9070xt-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-8b-q4km-michel2_full-post616-local-9070xt-cleangold-low-budget1024-schema-20260924.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen3-8b-q4km-michel2_shot-post616-local-9070xt-cleangold-low-budget1024-schema-20260924.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen35-9b-iq4xs-small6-tnr0-20260923.json` | lora_serving_eval | exploratory | 6685 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen35-9b-q4km-clean-gold-local-20260827.json` | lora_serving_eval | exploratory | 383 | 0 | 295 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q4km-default-local-9070xt-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen35-9b-q4km-hardcases-clean-gold-repeat-20260828.json` | lora_serving_eval | exploratory | 766 | 0 | 590 | False | recorded commit is unavailable from current history; saved summary differs from row recomputation |
| `lora_serving_eval__qwen35-9b-q4km-michel2-post616-local-9070xt-cleangold-low-budget1024-schema-20260924.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen35-9b-q4km-michel2_full-local-9070xt-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen35-9b-q4km-michel2_full-post616-local-9070xt-cleangold-low-budget1024-schema-20260924.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen35-9b-q4km-michel2_shot-post616-local-9070xt-cleangold-low-budget1024-schema-20260924.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | False | recorded commit is unavailable from current history |
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
| `lora_serving_eval__qwen36-35b-a3b-iq1m-rightsclean-michel2-adapter-tnr0-cleangold-michel2_full-reasoningoff-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq1m-rightsclean-michel2-adapter-tnr0-cleangold-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq1m-rightsclean-michel2-adapter-tnr0-pdnc2lite-michel2_full-reasoningoff-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq1m-rightsclean-michel2-adapter-tnr0-pdnc2lite-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq1m-thinking-michel2_full-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq2xxs-rightsclean-michel2-adapter-tnr0-cleangold-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq2xxs-rightsclean-michel2-adapter-tnr0-pdnc2lite-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq2xxs-thinking-michel2_full-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq3xxs-rightsclean-michel2-adapter-tnr0-cleangold-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq3xxs-rightsclean-michel2-adapter-tnr0-pdnc2lite-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-iq3xxs-thinking-michel2_full-tnr2-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-q4kxl-rightsclean-michel2-adapter-tnr0-cleangold-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-q4kxl-rightsclean-michel2-adapter-tnr0-pdnc2lite-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-thinking-michel-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-thinking-michel2-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-thinking-michel2_full-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen36-35b-a3b-thinking-michel2_shot-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-iq2-xxs-rightsclean-michel2-adapter-tnr4-cleangold-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-iq2-xxs-rightsclean-michel2-adapter-tnr4-pdnc2lite-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-iq2xxs-michel2_full-tnr2-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q3-k-xl-rightsclean-michel2-adapter-tnr4-cleangold-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q3-k-xl-rightsclean-michel2-adapter-tnr4-pdnc2lite-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q3kxl-michel2_full-tnr2-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4-k-m-rightsclean-michel2-adapter-tnr4-cleangold-michel2_full-reasoningoff-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4-k-m-rightsclean-michel2-adapter-tnr4-cleangold-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1536 | 0 | 766 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4-k-m-rightsclean-michel2-adapter-tnr4-pdnc2lite-michel2_full-reasoningoff-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4-k-m-rightsclean-michel2-adapter-tnr4-pdnc2lite-michel2_full-reasoningon-schema-20260917.json` | lora_serving_eval | exploratory | 1326 | 0 | 690 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4km-michel-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4km-michel2-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4km-michel2_full-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-27b-q4km-michel2_shot-tnr0-cleangold-low-budget1024-schema-20260917.json` | lora_serving_eval | exploratory | 768 | 0 | 383 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-iq2-rightsclean-pdnc8-tnr2-20260922.json` | lora_serving_eval | exploratory | 4620 | 0 | 3984 | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-q3kxl-on-michel2_full-pdnc9-tnr2-20260922.json` | lora_serving_eval | exploratory | 5310 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-q4km-off-michel2_full-pdnc9-tnr4-20260922.json` | lora_serving_eval | exploratory | 5310 | None | None | True | recorded commit is unavailable from current history |
| `lora_serving_eval__qwen38-q4km-on-michel2_full-pdnc9-tnr4-20260922.json` | lora_serving_eval | exploratory | 5310 | None | None | False | recorded commit is unavailable from current history |
| `lora_serving_eval__specdecode-ngram-20260924.json` | lora_serving_eval | exploratory | 66 | 0 | 0 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__workerscheck-w1-20260924.json` | lora_serving_eval | exploratory | 318 | 0 | 0 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__workerscheck2-w1-20260924.json` | lora_serving_eval | exploratory | 318 | 0 | 0 | True | recorded commit is unavailable from current history |
| `lora_serving_eval__workerscheck2-w3-20260924.json` | lora_serving_eval | exploratory | 318 | 0 | 0 | True | recorded commit is unavailable from current history |
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
| `reasoning_arms__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | reasoning_arms | exploratory | 810 | 6 | 0 | False | saved summary differs from row recomputation |
| `reasoning_arms__qwen__qwen3-14b.json` | reasoning_arms | exploratory | 695 | 4 | 0 | True | saved summary differs from row recomputation |
| `reasoning_check__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 792 | 1 | 0 | False | saved summary differs from row recomputation |
| `reasoning_check__index18__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 198 | 0 | 0 | False | saved summary differs from row recomputation |
| `reasoning_check__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 278 | 0 | 0 | False | saved summary differs from row recomputation |
| `reasoning_check__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | reasoning_check | exploratory | 324 | 0 | 0 | False | saved summary differs from row recomputation |
| `reexamine__qwen__qwen3-14b.json` | reexamine | exploratory | 695 | 0 | 0 | True | saved summary differs from row recomputation |
| `roster_quality__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | provisional | 1540 | 0 | 0 | True |  |
| `roster_quality__index18__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | provisional | 368 | 0 | 0 | True |  |
| `roster_quality__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | provisional | 532 | 0 | 0 | True |  |
| `roster_quality__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | roster_quality | historical_only | 648 | 3 | 0 | True |  |
| `roster_warmup.json` | roster_warmup | exploratory | 417 | 0 | 0 | False | recorded commit is unavailable from current history |
| `roster_warmup__ministral-3-14b-instruct-2512.json` | roster_warmup | supported_measurement | 417 | 0 | 0 | False |  |
| `scene_cast__grimgar03__qwen__qwen3-14b__local-llamacpp-look1.json` | scene_cast | provisional | 1155 | 0 | 0 | True |  |
| `scene_cast__grimgar03__qwen__qwen3-14b__local-llamacpp-look6.json` | scene_cast | provisional | 1155 | 0 | 0 | True |  |
| `scene_cast__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | provisional | 1155 | 0 | 0 | True |  |
| `scene_cast__index18__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | provisional | 276 | 0 | 0 | True |  |
| `scene_cast__mushoku16__qwen__qwen3-14b__local-llamacpp-look6.json` | scene_cast | provisional | 399 | 0 | 0 | True |  |
| `scene_cast__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | provisional | 399 | 0 | 0 | True |  |
| `scene_cast__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | scene_cast | historical_only | 486 | 2 | 0 | True |  |
| `tag_priority__grimgar03__mistralai__magistral-small__local-llamacpp.json` | tag_priority | exploratory | 792 | 0 | 0 | True | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp-rep1.json` | tag_priority | exploratory | 792 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp-rep2.json` | tag_priority | exploratory | 792 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp-rep3.json` | tag_priority | exploratory | 792 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__grimgar03__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 800 | 22 | 8 | True | saved summary differs from row recomputation |
| `tag_priority__index18__mistralai__magistral-small__local-llamacpp.json` | tag_priority | exploratory | 198 | 0 | 0 | True | saved summary differs from row recomputation |
| `tag_priority__index18__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 198 | 0 | 0 | False | saved summary differs from row recomputation |
| `tag_priority__mushoku16__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 278 | 0 | 0 | True | saved summary differs from row recomputation |
| `tag_priority__owarimonogatari3__qwen__qwen3-14b__local-llamacpp.json` | tag_priority | exploratory | 324 | 1 | 0 | False | saved summary differs from row recomputation |
| `two_by_two.json` | two_by_two | exploratory | 556 | 0 | 556 | True | artifact validation is not ok; environment is missing context_length; environment is missing parallel; no LM Studio load state recorded; no harness fingerprint: the code that ran is unidentified; saved summary differs from row recomputation |
| `two_stage_attribution__all_rows_hint_20260914.json` | two_stage_attribution | historical_only | 2494 | 0 | 1224 | True |  |
| `two_stage_attribution__explicit_control.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_explicit_hint.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_inner_narration.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_shuffled_roster.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__explicit_speaker_not_addressee.json` | two_stage_attribution | historical_only | 1823 | 0 | 1414 | False |  |
| `two_stage_attribution__smoke.json` | two_stage_attribution | historical_only | 90 | 0 | 60 | False |  |
| `two_stage_attribution__usual_suspects_control_20260913.json` | two_stage_attribution | historical_only | 1213 | 0 | 488 | True |  |
| `two_stage_attribution__usual_suspects_dropped_20260913.json` | two_stage_attribution | historical_only | 1213 | 0 | 488 | True |  |
| `two_stage_attribution__usual_suspects_hint_20260914.json` | two_stage_attribution | historical_only | 1213 | 0 | 488 | True |  |
| `two_stage_attribution__usual_suspects_muse_reasoninglow_mt4096_20260914.json` | two_stage_attribution | historical_only | 1213 | 0 | 488 | False |  |
| `two_stage_attribution__usual_suspects_reasoning_20260914.json` | two_stage_attribution | exploratory | 1213 | 0 | 488 | False | recorded commit is unavailable from current history |
| `two_stage_attribution_full.json` | two_stage_attribution | historical_only | 2494 | 0 | 1224 | False |  |
| `two_stage_attribution_narrator_w3200.json` | two_stage_attribution | supported_measurement | 200 | 0 | 0 | False |  |
| `two_stage_attribution_restricted.json` | two_stage_attribution | historical_only | 2494 | 0 | 1224 | False |  |
| `two_stage_attribution_riqua.json` | two_stage_attribution | supported_measurement | 1287 | None | None | False |  |
| `two_stage_attribution_w16000.json` | two_stage_attribution | exploratory | 600 | None | None | False | recorded commit is unavailable from current history |
| `two_stage_attribution_w3200.json` | two_stage_attribution | historical_only | 2494 | 0 | 1224 | False |  |
| `two_stage_attribution_w3200_eight.json` | two_stage_attribution | historical_only | 7643 | 0 | 6373 | False |  |
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
