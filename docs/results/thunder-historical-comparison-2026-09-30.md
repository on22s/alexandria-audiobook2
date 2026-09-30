# Historical Thunder reports recovered on 2026-09-30

Twenty-eight older private reports were compared with the published JSON row artifacts in fresh `origin/main` and the draft results branch. This is historical coverage, not a new cloud campaign or a production recommendation. Raw replies, quotations, transcripts, audio paths and source records remain private.

## Comparison with published evidence

The comparison inspected 1,684 nonempty row-bearing JSON artifact instances across the two trees (the same file in both trees counts twice). Attribution equivalence compares sorted `(arm, id, expected, predicted, correct)` tuples, ignoring runtime metadata, raw replies and ordering; boundary equivalence compares complete saved rows. This does not search every GitHub branch or historical commit.

One report is attribution-row equivalent to the already published [Muse four-book Q3_K_XL result](../../ab_test_runtime/experiments/lora_serving_eval__muse-window25b-q3kxl-gold-paired-michel2_full-tnr-4-20260924.json), despite its cloud filename identifying tnr-0 rather than tnr-4. Its saved base/adapter counts are 156/176 and 159/176. It adds no independent replication evidence. The other 27 have no row-equivalent match in these checked trees; that does not establish that they were never summarized elsewhere. Exact run identifiers were also searched in tracked documentation.

Current published interpretation takes precedence: [RECIPES](../../RECIPES.md) documents serving and training-contract failures and the September 28 silent-alias correction; [attribution history](attribution-history.md) explicitly withdraws earlier interpretations. Old smoke tests, failed serving runs and prompt mismatches cannot establish adapter quality.

## Recounted attribution reports

All 23 attribution reports' saved arm totals and accuracies match counts recomputed from their stored `correct` flags. This validates their arithmetic, not the old judgement or the server's behavior. The table retains exact counts rather than pooling different fixtures or configurations.

Current `experiments.scoring.alias_groups` / `same_speaker` were additionally applied to every recovered PDNC row. The preserved private *The Sun Also Rises* fixture completes the previously uncovered 345 rows in each arm of the affected nine-book reports. All **2,655/2,655** rows per arm are now covered in those reports; expected labels and quotation text match the answer keys. The correction remains **25 flags**: 10 base flags in the September 21 Muse gen-3 Q4 run, 3 in the September 17 Muse Q3 run, and 12 in the September 24 Muse window25b nine-book run. No additional flag changes appear in the recovered fixture. Non-PDNC rows retain stored flags, explicitly identified in the [CPU replay aggregate](../../ab_test_runtime/experiments/thunder_cpu_followups_verification__20260930.json).

This completes the PDNC alias checks, not validation of old serving behavior, raw replies, training separation or current product performance. The source JSONs remain unchanged privately. The [portable CPU verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_cpu_followups.py) records all fixture and source hashes and recounts the 23 attribution reports.

| Historical run (filename without JSON suffix) | Stored correct / scored rows by arm | After available current-alias checks |
|---|---|---|
| `gemma-schema-smoke-20260910` | base: 2/3; lora: 2/3 | unchanged |
| `qwen38-author-heldout-balanced-tnr0-product-batch10-latest-20260913` | base: 566/768; lora: 576/768 | unchanged |
| `gemma4-12b-task4k-a6000-gold-reasoning0-20260909` | base: 530/772; lora: 14/772 | unchanged |
| `muse-glimmer-30b-mixed-tplfix-seed2-a6000-gold-schema-checked-20260911` | base: 416/768; lora: 454/768 | unchanged |
| `muse-glimmer-30b-task4k-multin-tplfix-seed2-promptgate-q3-canary-20260912` | base: 1/2; lora: 0/2 | unchanged |
| `gemma4-12b-qatbase-multin-tnr0-gold-schema-batch1b-20260910` | base: 407/768; lora: 417/768 | unchanged |
| `muse-glimmer-30b-mixed-lossfix-michel-tnr0-none-20260915b` | base: 291/768; lora: 288/768 | unchanged |
| `qwen38-repl-mix75full-s20260905-tnr0split-20260905` | base: 604/768; tuned: 0/768 | unchanged |
| `qwen38-repl-mix75match-s20260905-tnr0split-20260905` | base: 604/768; tuned: 621/768 | unchanged |
| `muse-window25b-q3kxl-gold-paired-michel2_full-tnr-0-20260924` | base: 156/176; lora: 159/176 | unchanged |
| `qwen36-35b-a3b-nonthinking-michel2_full-tnr0-cleangold-none-temp07-schema-20260917` | base: 660/768 | unchanged |
| `qwen36-35b-a3b-nonthinking-default-tnr0-cleangold-none-temp07-schema-20260917` | base: 594/768 | unchanged |
| `muse-q4-kquant17-gen3-rightsclean-michel2_full-tnr0-pdnc9lite-low-schema-20260921` | base: 2498/2655; lora: 2407/2655 | base: 2508/2655; lora: 2407/2655 |
| `muse-glimmer-30b-gen3-rightsclean-rft-michel2-full-tnr0-product-batch25-20260921` | base: 690/768; lora: 671/768 | unchanged |
| `muse-q3-michel2-tnr1-pdnc9lite-low-schema-20260917` | base: 2437/2655 | base: 2440/2655 |
| `muse-window25b-q3kxl-paired-michel2_full-tnr-1d-pdnc9-20260924` | base: 2356/2655; lora: 2278/2655 | base: 2368/2655; lora: 2278/2655 |
| `gemma4-12b-hardcases-a100-gold-20260909` | base: 541/768; lora: 0/768 | unchanged |
| `gemma4-task4k-hf-adapter-smoke-20260909` | base: 14/18; tuned: 13/18 | unchanged |
| `gemma4-12b-qatbase-multin-a100-gold-batch1-20260910` | base: 377/768; lora: 384/768 | unchanged |
| `gemma4-12b-qatbase-multin-a100-gold-verified-20260910` | base: 417/768; lora: 438/768 | unchanged |
| `qwen3-14b-base-tnr1-cleangold-windows-fixed-20260914` | base: 477/768 | unchanged |
| `qwen3-14b-base-tnr1-cleangold-windows-control-20260914` | base: 479/768 | unchanged |
| `qwen3-14b-base-tnr1-cleangold-windows-chapter-20260914` | base: 474/768 | unchanged |

## Boundary-audit coverage

The five saved audits each sample 200 chunks. Their row status counts exactly reproduce the saved totals and alert rates. Together they flag **93/1,000 sampled chunks (9.3%)**, with per-source rates **4.5–14.0%**. This is a descriptive total over these five samples, not a corpus-wide error rate. The instrument flags ASR/source disagreement at chunk edges; it does not establish a real cut error. Source text and ASR/audio inputs were not replayed, and these alerts have no new listening adjudication.

| Private source identifier (report SHA-256 prefix) | Alerts / chunks | First only | Last only | Both |
|---|---:|---:|---:|---:|
| `0b67a8317e58` | 9/200 | 6 | 3 | 0 |
| `ba08e6d2b0cf` | 28/200 | 9 | 18 | 1 |
| `87453606295e` | 25/200 | 17 | 8 | 0 |
| `f8d764621b36` | 11/200 | 7 | 3 | 1 |
| `9e96683114e8` | 20/200 | 13 | 5 | 2 |

## Frozen report hashes

SHA-256 hashes identify the unchanged private inputs. Boundary source titles are omitted. No raw report is included in this PR.

| Report | SHA-256 |
|---|---|
| `lora_serving_eval__gemma-schema-smoke-20260910.json` | `b3cc95eb41a812eb677d7015b79ec87e364fea1ae84ec7fbd4550afb23ee5649` |
| `lora_serving_eval__qwen38-author-heldout-balanced-tnr0-product-batch10-latest-20260913.json` | `4816685563e6e10b2c9d369cfb591265058117f86c9d6338fec898682d66078c` |
| `lora_serving_eval__gemma4-12b-task4k-a6000-gold-reasoning0-20260909.json` | `07a5491feec5e0cf188bf06e65de1eedf0d55b8e7528cab7e91d842e5c97aeca` |
| `lora_serving_eval__muse-glimmer-30b-mixed-tplfix-seed2-a6000-gold-schema-checked-20260911.json` | `8dee01bcf5c854c392222bd992cfc215a897f8913af62d1ba85ea45d05b18dfc` |
| `lora_serving_eval__muse-glimmer-30b-task4k-multin-tplfix-seed2-promptgate-q3-canary-20260912.json` | `6bb1a27f6da9ebbaa4d3067daf6a848f4821b4c5b53b38041150f0f5110c05b4` |
| `lora_serving_eval__gemma4-12b-qatbase-multin-tnr0-gold-schema-batch1b-20260910.json` | `809e3927a1314c7f3835502123925d3565ff143a45742f9333fe10a38af984f3` |
| `lora_serving_eval__muse-glimmer-30b-mixed-lossfix-michel-tnr0-none-20260915b.json` | `562246856597d2e2715f7c96e50db78660e254916cde356002e9111620b74754` |
| `distill_eval__qwen38-repl-mix75full-s20260905-tnr0split-20260905.json` | `819f9d29facbf125f4f07e4b737f4b6ffd3023ba867dbc8807f75a1090fa1a3d` |
| `distill_eval__qwen38-repl-mix75match-s20260905-tnr0split-20260905.json` | `e054df2e572f8bf710642166df03b80ad9d2441b84989e672a940e759f5936f0` |
| `lora_serving_eval__muse-window25b-q3kxl-gold-paired-michel2_full-tnr-0-20260924.json` | `9e3fc5b75042d04ec8c060c44666df89fbcc273c46c3eaa244662399fd50d4af` |
| `lora_serving_eval__qwen36-35b-a3b-nonthinking-michel2_full-tnr0-cleangold-none-temp07-schema-20260917.json` | `5a897b105fd4c7b2d198ffc79861ca6358f4028beddc6713a689d5c1382cf3eb` |
| `lora_serving_eval__qwen36-35b-a3b-nonthinking-default-tnr0-cleangold-none-temp07-schema-20260917.json` | `2cccacf302449bdbc977090eaa6e003048a6fce4ce1848a7787c6b1b1b2415f6` |
| `lora_serving_eval__muse-q4-kquant17-gen3-rightsclean-michel2_full-tnr0-pdnc9lite-low-schema-20260921.json` | `c8f6bf8ae1f349c6fab5009a519f87573bc6a1638b2f065df8d0ecfc572748ea` |
| `lora_serving_eval__muse-glimmer-30b-gen3-rightsclean-rft-michel2-full-tnr0-product-batch25-20260921.json` | `e2d537fcf5a6faacd3d35f1627a773b180b24899443c6d0f2f4f7af151c5485c` |
| `lora_serving_eval__muse-q3-michel2-tnr1-pdnc9lite-low-schema-20260917.json` | `15ba9c292c3c2427779033a4f380b38adbfb496725c3e64aa13adc502b59e3ed` |
| `lora_serving_eval__muse-window25b-q3kxl-paired-michel2_full-tnr-1d-pdnc9-20260924.json` | `1aba3c1c38a1b003a3e14e3254741a33579150f6002c9255cbf4ed176d459b4a` |
| `private boundary source 1` | `0b67a8317e582b17ac88e577fe2e166b7ad99a48e946c9b6f9289c9663b550e6` |
| `private boundary source 2` | `ba08e6d2b0cfb4079f2150260a825fa5dbd46b528c31175bd3aa975511a555c1` |
| `private boundary source 3` | `87453606295ef36679578cc586694f7b3db1a946d98924b934e51328129fa0b7` |
| `private boundary source 4` | `f8d764621b3665027d64217856b315b918f2014bd4a8876a4a75e9993c31707c` |
| `private boundary source 5` | `9e96683114e87b157f39867856c245eb1586d5140099437e388fff34f6e209b7` |
| `lora_serving_eval__gemma4-12b-hardcases-a100-gold-20260909.json` | `d3a2f063309a9ddb14171715769f198165624eada90efdd3ea3533204b802804` |
| `distill_eval__gemma4-task4k-hf-adapter-smoke-20260909.json` | `17c3e83df3d4f8a3c85affef39641e8e8de32c8f0635da16672f107376088b51` |
| `lora_serving_eval__gemma4-12b-qatbase-multin-a100-gold-batch1-20260910.json` | `caa5ffbe6f413931fc59388d1f10a253baa7c25ea5875badc5f9d6758db0a49d` |
| `lora_serving_eval__gemma4-12b-qatbase-multin-a100-gold-verified-20260910.json` | `56accd6e93eff5e7dd25e616d588aaabc253f0320bcb5d7a3fb768bea9b3ed0b` |
| `lora_serving_eval__qwen3-14b-base-tnr1-cleangold-windows-fixed-20260914.json` | `7e585ef1c4465da1d8960d1eecd2b80a4ad21f58730f98a57e0c24c3f59772f4` |
| `lora_serving_eval__qwen3-14b-base-tnr1-cleangold-windows-control-20260914.json` | `ef56960179fd9a773a6be330b88e476c32baea4fbd3bc23457ff67125cb6ec62` |
| `lora_serving_eval__qwen3-14b-base-tnr1-cleangold-windows-chapter-20260914.json` | `23ccc2da8c8ce9aac93437cd166d483a48a9ae0346cadf7b9ab75cc1ed937558` |

Comparison refs: main `51edf6a62e500aac65bd509a8fb17098097465cf`; draft before this note `698aa7f4c71c005bd5a61dd40c5e23c22e9f2632`. Counts are measured from retained reports; interpretations are limited above. No goal closed, no weights promoted, and no claim that deleted cloud archives were fully preserved.
