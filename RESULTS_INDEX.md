# Results index

Generated 2026-09-04 from `ab_test_runtime/experiments/` — 1026 artifacts, 1249 arms.

Regenerate with `python3 collect_results.py`. Machine-readable copy in `results_index.csv`.

`resolution_tier` says how confidently an artifact's origin is known: `provenance` records the script and its arguments and can be re-run exactly (`replayable=yes`); `git+naming` means the producing script and the commit that added the file are known but the arguments are not, so it can be read but not reproduced; `none` is neither. 108 of 883 rows are replayable.

`cited_by_goal` is populated on 10 rows of 883, and that is the finding rather than a gap in this column: only goals 2.4 and 5.4 name their evidence by filename. Every other claim in GOALS.md quotes a number no reader can follow back to a file.

`evidence_status` comes from the committed audit snapshots. `supported_structure` validates provenance shape only; `supported_measurement` is the strongest attribution classification. `not_audited` is explicit and must not be treated as support.

`dirty=True` means tracked files were modified when the artifact was written: the numbers are inspectable but the run is not reproducible from its recorded commit.

**`closed-oracle` arms are invalidated.** Their candidate sets were built from the pre-gold labels, so the arm was shown shortlists derived from answers that have since changed. `valid=ok` on those rows means internally consistent, NOT trustworthy — do not read them as results.

**`qwen35_35b_a3b_bf16_speaker_longcontext_tophalf_5epoch` is invalidated.** Its tuned arm returned an empty prediction on 266 of 383 rows (69%), against 3-10% for every sibling run in the same batch. The 25.6% it reports measures a generation failure, not attribution accuracy — see its `.INVALID.json` sidecar. Do not read it as a result.

`closed_set.json` and `two_by_two.json` predate the environment contract and captured no `context_length` or `parallel`, which cannot be reconstructed. Their rows and summaries were recomputed and are internally consistent, but the runs are not comparable to artifacts that record an environment: inspectable, not citable.


## batch_alignment

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | aligned | 385 | 70.9% | exploratory | ok | False | 1295.5s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | fixed | 385 | 71.4% | exploratory | ok | False | 1295.5s |

## batch_contiguity

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | contiguous | 385 | 74.5% | exploratory | ok | False | 5518.1s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | scattered | 385 | 57.9% | exploratory | ok | False | 5518.1s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | contiguous | 92 | 75.0% | supported_measurement | ok | False | 10801.7s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scattered | 92 | 67.4% | supported_measurement | ok | False | 10801.7s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | contiguous | 133 | 68.4% | supported_measurement | ok | False | 15992.9s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scattered | 133 | 31.6% | supported_measurement | ok | False | 15992.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | contiguous | 162 | 53.7% | supported_measurement | ok | False | 19624.4s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scattered | 162 | 35.2% | supported_measurement | ok | False | 19624.4s |

## batch_size

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | b1 | 400 | 60.5% | exploratory | ok | False | 3853.3s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | b25 | 400 | 79.2% | exploratory | ok | False | 3853.3s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | b5 | 400 | 69.0% | exploratory | ok | False | 3853.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b1 | 400 | 54.0% | historical_only | ok | False | 2298.0s |
| grimgar03 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b1 | 400 | 52.8% | exploratory | ok | False | 2647.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b10 | 400 | 54.0% | historical_only | ok | False | 2298.0s |
| grimgar03 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b10 | 400 | 52.0% | exploratory | ok | False | 2647.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b100 | 400 | 62.3% | historical_only | ok | True | 2990.1s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b25 | 400 | 59.8% | historical_only | ok | True | 2990.1s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b25 | 400 | 55.5% | historical_only | ok | False | 2298.0s |
| grimgar03 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b25 | 400 | 55.8% | exploratory | ok | False | 2647.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b5 | 400 | 54.0% | historical_only | ok | False | 2298.0s |
| grimgar03 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b5 | 400 | 52.0% | exploratory | ok | False | 2647.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b50 | 400 | 63.7% | historical_only | ok | True | 2990.1s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b10 | 99 | 68.7% | supported_measurement | ok | False | 1851.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b25 | 99 | 66.7% | supported_measurement | ok | False | 1851.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b5 | 99 | 54.5% | supported_measurement | ok | False | 1851.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b50 | 99 | 54.5% | supported_measurement | ok | False | 1851.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b1 | 139 | 30.2% | supported_measurement | ok | False | 952.5s |
| mushoku16 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b1 | 139 | 28.8% | exploratory | ok | False | 1396.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b10 | 139 | 46.0% | supported_measurement | ok | False | 952.5s |
| mushoku16 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b10 | 139 | 38.1% | exploratory | ok | False | 1396.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b100 | 139 | 49.6% | provisional | ok | True | 3425.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b25 | 139 | 51.8% | provisional | ok | True | 3425.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b25 | 139 | 49.6% | supported_measurement | ok | False | 952.5s |
| mushoku16 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b25 | 139 | 46.8% | exploratory | ok | False | 1396.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | b5 | 139 | 40.3% | supported_measurement | ok | False | 952.5s |
| mushoku16 | qwen3-14b | cloud-a6000-LM | LM Studio loopback | 16384 | b5 | 139 | 37.4% | exploratory | ok | False | 1396.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b50 | 139 | 48.9% | provisional | ok | True | 3425.6s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b10 | 162 | 36.4% | supported_measurement | ok | False | 2716.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b100 | 162 | 46.3% | provisional | ok | True | 8526.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b100 | 162 | 46.3% | provisional | ok | True | 8230.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b25 | 162 | 40.7% | provisional | ok | True | 8526.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b25 | 162 | 40.7% | provisional | ok | True | 8230.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b25 | 162 | 39.5% | supported_measurement | ok | False | 2716.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b5 | 162 | 26.5% | supported_measurement | ok | False | 2716.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b50 | 162 | 47.5% | provisional | ok | True | 8526.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b50 | 162 | 47.5% | provisional | ok | True | 8230.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | b50 | 162 | 46.3% | supported_measurement | ok | False | 2716.3s |

## because_production

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | baseline | 400 | 55.5% | historical_only | ok | False | 4766.2s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | thinking | 400 | 63.7% | historical_only | ok | False | 4766.2s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | baseline | 139 | 49.6% | supported_measurement | ok | False | 2668.6s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | baseline | 139 | 49.6% | provisional | ok | True | 4564.3s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | because | 139 | 42.4% | provisional | ok | True | 4564.3s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | scaffold_thinking | 139 | 43.2% | provisional | ok | True | 4564.3s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | thinking | 139 | 52.5% | supported_measurement | ok | False | 2668.6s |

## booknlp_baseline

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| ? | booknlp-big | local-lmstudio | lmstudio |  | booknlp | 1226 | 54.2% | exploratory | None | None | s |

## candidate_id

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | id | 147 | 35.4% | historical_only | ok | False | 68.3s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | name | 147 | 49.0% | historical_only | ok | False | 68.3s |

## cascade

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 396 | 73.7% | exploratory | ok | False | 0.1s |
| grimgar03 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 400 | 77.8% | exploratory | ok | False | 0.1s |
| grimgar03 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cascade | 396 | 84.6% | exploratory | ok | False | 0.2s |
| grimgar03 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 396 | 57.3% | exploratory | ok | False | 0.1s |
| grimgar03 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 400 | 55.8% | exploratory | ok | False | 0.1s |
| grimgar03 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cheap-w1 | 396 | 82.3% | exploratory | ok | False | 0.2s |
| index18 | gemma-3-27b | cloud-a6000-LM | LM Studio loopback | 16384 | cascade | 99 | 65.7% | exploratory | ok | False | 0.1s |
| index18 | gemma-3-27b | cloud-a6000-LM | LM Studio loopback | 16384 | cheap-w1 | 99 | 62.6% | exploratory | ok | False | 0.1s |
| index18 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 99 | 69.7% | exploratory | ok | False | 0.1s |
| index18 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 99 | 73.7% | exploratory | ok | False | 0.1s |
| index18 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cascade | 99 | 63.6% | exploratory | ok | False | 0.1s |
| index18 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 99 | 62.6% | exploratory | ok | False | 0.1s |
| index18 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 99 | 62.6% | exploratory | ok | False | 0.1s |
| index18 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cheap-w1 | 99 | 57.6% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 136 | 64.0% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 139 | 64.0% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cascade | 136 | 61.8% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 136 | 50.0% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 139 | 47.5% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cheap-w1 | 136 | 55.1% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-32b | cloud-a6000-LM | LM Studio loopback | 16384 | cascade | 139 | 45.3% | exploratory | ok | False | 0.1s |
| mushoku16 | qwen3-32b | cloud-a6000-LM | LM Studio loopback | 16384 | cheap-w1 | 139 | 47.5% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | gemma-3-27b | cloud-a6000-LM | LM Studio loopback | 16384 | cascade | 162 | 46.3% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | gemma-3-27b | cloud-a6000-LM | LM Studio loopback | 16384 | cheap-w1 | 162 | 42.6% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 162 | 54.9% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cascade | 162 | 56.2% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cascade | 162 | 59.9% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 162 | 40.1% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | cheap-w1 | 162 | 42.0% | exploratory | ok | False | 0.1s |
| owarimonogatari3 | qwen3-14b + llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 16384 | cheap-w1 | 162 | 54.3% | exploratory | ok | False | 0.1s |

## closed_set

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | gemma-4-e4b-uncensored-hau | local-lmstudio | lmstudio | 32768 | closed-6 | 400 | 59.8% | historical_only | ok | False | 198.1s |
| grimgar03 | gemma-4-e4b-uncensored-hau | local-lmstudio | lmstudio | 32768 | closed-oracle | 400 | 65.5% | historical_only | ok | False | 198.1s |
| grimgar03 | gemma-4-e4b-uncensored-hau | local-lmstudio | lmstudio | 32768 | open | 400 | 57.2% | historical_only | ok | False | 198.1s |
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-6 | 400 | 62.3% | historical_only | ok | False | 1206.5s |
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-oracle | 400 | 70.5% | historical_only | ok | False | 1206.5s |
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | open | 400 | 61.5% | historical_only | ok | False | 1206.5s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda b10 | 8192 | closed-6 | 400 | 76.5% | exploratory | ok | False | 1489.6s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda b10 | 8192 | closed-oracle | 400 | 83.0% | exploratory | ok | False | 1489.6s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda b10 | 8192 | open | 400 | 75.8% | exploratory | ok | False | 1489.6s |
| grimgar03 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | closed-6 | 400 | 52.5% | historical_only | ok | False | 285.2s |
| grimgar03 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | closed-oracle | 400 | 58.2% | historical_only | ok | False | 285.2s |
| grimgar03 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | open | 400 | 51.5% | historical_only | ok | False | 285.2s |
| grimgar03 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | closed-6 | 396 | 64.4% | supported_measurement | ok | False | 470.2s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | closed-6 | 400 | 58.0% | historical_only | ok | False | 854.3s |
| grimgar03 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | closed-oracle | 396 | 77.3% | supported_measurement | ok | False | 470.2s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | closed-oracle | 400 | 70.8% | historical_only | ok | False | 854.3s |
| grimgar03 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | open | 396 | 64.1% | supported_measurement | ok | False | 470.2s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | open | 400 | 61.3% | historical_only | ok | False | 854.3s |
| grimgar03 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | closed-6 | 400 | 60.8% | historical_only | ok | False | 915.3s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | closed-6 | 400 | 60.5% | historical_only | ok | False | 291.8s |
| grimgar03 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | closed-6 | 400 | 60.8% | historical_only | ok | False | 915.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-6 | 396 | 64.4% | supported_measurement | ok | False | 261.7s |
| grimgar03 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | closed-oracle | 400 | 72.8% | historical_only | ok | False | 915.3s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | closed-oracle | 400 | 72.5% | historical_only | ok | False | 291.8s |
| grimgar03 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | closed-oracle | 400 | 72.8% | historical_only | ok | False | 915.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-oracle | 396 | 73.7% | supported_measurement | ok | False | 261.7s |
| grimgar03 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | open | 400 | 61.3% | historical_only | ok | False | 915.3s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | open | 400 | 60.8% | historical_only | ok | False | 291.8s |
| grimgar03 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | open | 400 | 61.3% | historical_only | ok | False | 915.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | open | 396 | 62.6% | supported_measurement | ok | False | 261.7s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-6 | 400 | 61.5% | historical_only | ok | False | 1430.8s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-oracle | 400 | 76.2% | historical_only | ok | False | 1430.8s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | open | 400 | 61.8% | historical_only | ok | False | 1430.8s |
| index18 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | closed-6 | 99 | 70.7% | supported_measurement | ok | False | 97.5s |
| index18 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | closed-oracle | 99 | 78.8% | supported_measurement | ok | False | 97.5s |
| index18 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | open | 99 | 65.7% | supported_measurement | ok | False | 97.5s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-6 | 99 | 60.6% | supported_measurement | ok | False | 62.6s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-oracle | 99 | 72.7% | supported_measurement | ok | False | 62.6s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | open | 99 | 66.7% | supported_measurement | ok | False | 62.6s |
| mushoku16 | gemma-4-e4b-uncensored-hau | local-lmstudio | lmstudio | 32768 | closed-6 | 147 | 38.8% | exploratory | ok | False | 81.9s |
| mushoku16 | gemma-4-e4b-uncensored-hau | local-lmstudio | lmstudio | 32768 | closed-oracle | 147 | 49.7% | exploratory | ok | False | 81.9s |
| mushoku16 | gemma-4-e4b-uncensored-hau | local-lmstudio | lmstudio | 32768 | open | 147 | 39.5% | exploratory | ok | False | 81.9s |
| mushoku16 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-6 | 139 | 44.6% | historical_only | ok | False | 421.3s |
| mushoku16 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-oracle | 139 | 59.0% | historical_only | ok | False | 421.3s |
| mushoku16 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | open | 139 | 55.4% | historical_only | ok | False | 421.3s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda b10 | 8192 | closed-6 | 139 | 60.4% | exploratory | ok | False | 445.8s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda b10 | 8192 | closed-oracle | 139 | 74.8% | exploratory | ok | False | 445.8s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda b10 | 8192 | open | 139 | 59.7% | exploratory | ok | False | 445.8s |
| mushoku16 | phi-4 | local-lmstudio | lmstudio | 16384 | closed-6 | 147 | 32.7% | exploratory | ok | False | 112.6s |
| mushoku16 | phi-4 | local-lmstudio | lmstudio | 16384 | closed-oracle | 147 | 59.2% | exploratory | ok | False | 112.6s |
| mushoku16 | phi-4 | local-lmstudio | lmstudio | 16384 | open | 147 | 45.6% | exploratory | ok | False | 112.6s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | closed-6 | 147 | 41.5% | exploratory | ok | False | 64.0s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | closed-oracle | 147 | 61.2% | exploratory | ok | False | 64.0s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | open | 147 | 47.6% | exploratory | ok | False | 64.0s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | closed-6 | 147 | 40.8% | exploratory | ok | False | 84.9s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | closed-oracle | 147 | 59.2% | exploratory | ok | False | 84.9s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | open | 147 | 46.9% | exploratory | ok | False | 84.9s |
| mushoku16 | magistral-small | local-llamacpp-hip | llama.cpp-hip b101 | 8192 | closed-6 | 139 | 45.3% | historical_only | ok | False | 100.5s |
| mushoku16 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | closed-6 | 139 | 45.3% | historical_only | ok | False | 254.3s |
| mushoku16 | magistral-small | local-llamacpp-hip | llama.cpp-hip b101 | 8192 | closed-oracle | 139 | 57.6% | historical_only | ok | False | 100.5s |
| mushoku16 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | closed-oracle | 139 | 56.8% | historical_only | ok | False | 254.3s |
| mushoku16 | magistral-small | local-llamacpp-hip | llama.cpp-hip b101 | 8192 | open | 139 | 53.2% | historical_only | ok | False | 100.5s |
| mushoku16 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | open | 139 | 52.5% | historical_only | ok | False | 254.3s |
| mushoku16 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | closed-6 | 139 | 38.8% | historical_only | ok | False | 317.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-6 | 136 | 40.4% | supported_measurement | ok | False | 65.2s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | closed-6 | 139 | 38.8% | historical_only | ok | False | 81.7s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | closed-6 | 139 | 39.6% | historical_only | ok | False | 90.6s |
| mushoku16 | qwen3-14b | local-llamacpp-vulkan | llama.cpp-vulkan b | 16384 | closed-6 | 139 | 39.6% | historical_only | ok | False | 87.6s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | closed-6 | 147 | 36.7% | exploratory | ok | False | 92.7s |
| mushoku16 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | closed-oracle | 139 | 66.9% | historical_only | ok | False | 317.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-oracle | 136 | 66.2% | supported_measurement | ok | False | 65.2s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | closed-oracle | 139 | 66.9% | historical_only | ok | False | 81.7s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | closed-oracle | 139 | 66.2% | historical_only | ok | False | 90.6s |
| mushoku16 | qwen3-14b | local-llamacpp-vulkan | llama.cpp-vulkan b | 16384 | closed-oracle | 139 | 66.9% | historical_only | ok | False | 87.6s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | closed-oracle | 147 | 66.0% | exploratory | ok | False | 92.7s |
| mushoku16 | qwen3-14b | cloud-a6000-lmstudio | lmstudio | 98304 | open | 139 | 50.4% | historical_only | ok | False | 317.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | open | 136 | 52.9% | supported_measurement | ok | False | 65.2s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | open | 139 | 48.2% | historical_only | ok | False | 81.7s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | open | 139 | 49.6% | historical_only | ok | False | 90.6s |
| mushoku16 | qwen3-14b | local-llamacpp-vulkan | llama.cpp-vulkan b | 16384 | open | 139 | 49.6% | historical_only | ok | False | 87.6s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | open | 147 | 48.3% | exploratory | ok | False | 92.7s |
| mushoku16 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-6 | 139 | 46.8% | historical_only | ok | False | 496.9s |
| mushoku16 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | closed-oracle | 139 | 64.0% | historical_only | ok | False | 496.9s |
| mushoku16 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | open | 139 | 52.5% | historical_only | ok | False | 496.9s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio |  | closed-6 | 147 | 34.7% | exploratory | ['no LM Studio load state recorded', 'en | True | 138.3s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio | 32768 | closed-6 | 147 | 34.7% | exploratory | ok | False | 150.0s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio |  | closed-oracle | 147 | 49.0% | exploratory | ['no LM Studio load state recorded', 'en | True | 138.3s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio | 32768 | closed-oracle | 147 | 49.0% | exploratory | ok | False | 150.0s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio |  | open | 147 | 35.4% | exploratory | ['no LM Studio load state recorded', 'en | True | 138.3s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio | 32768 | open | 147 | 35.4% | exploratory | ok | False | 150.0s |
| owarimonogatari3 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | closed-6 | 162 | 41.4% | supported_measurement | ok | False | 165.9s |
| owarimonogatari3 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | closed-oracle | 162 | 57.4% | supported_measurement | ok | False | 165.9s |
| owarimonogatari3 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | open | 162 | 46.9% | supported_measurement | ok | False | 165.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-6 | 162 | 42.6% | supported_measurement | ok | False | 110.4s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | closed-oracle | 162 | 51.9% | supported_measurement | ok | False | 110.4s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | open | 162 | 47.5% | supported_measurement | ok | False | 110.4s |

## committed_history

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | none | 400 | 63.5% | historical_only | ok | False | 337.5s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | oracle | 400 | 63.5% | historical_only | ok | False | 337.5s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | predicted | 400 | 62.3% | historical_only | ok | False | 337.5s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | none | 99 | 63.6% | supported_measurement | ok | False | 80.8s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle | 99 | 60.6% | supported_measurement | ok | False | 80.8s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | predicted | 99 | 63.6% | supported_measurement | ok | False | 80.8s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | none | 136 | 50.7% | supported_measurement | ok | False | 77.8s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle | 136 | 54.4% | supported_measurement | ok | False | 77.8s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | predicted | 136 | 47.8% | supported_measurement | ok | False | 77.8s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | gated | 162 | 48.8% | exploratory | ok | False | 417.6s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | gated | 162 | 48.8% | exploratory | ok | False | 579.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | none | 162 | 50.0% | supported_measurement | ok | False | 116.4s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | none | 162 | 50.0% | exploratory | ok | False | 417.6s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | none | 162 | 50.0% | exploratory | ok | False | 579.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle | 162 | 59.3% | supported_measurement | ok | False | 116.4s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | oracle | 162 | 59.3% | exploratory | ok | False | 417.6s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | oracle | 162 | 59.3% | exploratory | ok | False | 579.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | predicted | 162 | 46.9% | supported_measurement | ok | False | 116.4s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | predicted | 162 | 48.1% | exploratory | ok | False | 417.6s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | predicted | 162 | 48.1% | exploratory | ok | False | 579.9s |

## context_width

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | w1 | 400 | 55.8% | historical_only | ok | False | 1003.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | w15 | 400 | 61.5% | historical_only | ok | False | 1003.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | w4 | 400 | 62.0% | historical_only | ok | False | 1003.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip b101 | 16384 | w40 | 400 | 60.5% | historical_only | ok | False | 1003.4s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w1 | 99 | 48.5% | supported_measurement | ok | False | 146.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w15 | 99 | 63.6% | supported_measurement | ok | False | 146.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w4 | 99 | 65.7% | supported_measurement | ok | False | 146.2s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w1 | 136 | 38.2% | supported_measurement | ok | False | 140.1s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w15 | 136 | 45.6% | supported_measurement | ok | False | 140.1s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w4 | 136 | 55.9% | supported_measurement | ok | False | 140.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w1 | 162 | 41.4% | supported_measurement | ok | False | 238.7s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w15 | 162 | 46.3% | supported_measurement | ok | False | 238.7s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w4 | 162 | 46.9% | supported_measurement | ok | False | 238.7s |

## context_width_production

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w1 | 400 | 79.2% | exploratory | ok | False | 4704.6s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w4 | 400 | 76.8% | exploratory | ok | False | 4704.6s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w1 | 400 | 59.8% | historical_only | ok | False | 1914.8s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w1 | 400 | 58.0% | historical_only | ok | False | 1897.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w1 | 400 | 58.0% | historical_only | ok | False | 1882.6s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | w1 | 400 | 57.0% | historical_only | ok | False | 1426.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w4 | 400 | 69.8% | historical_only | ok | False | 1914.8s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w4 | 400 | 69.8% | historical_only | ok | False | 1897.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | w4 | 400 | 69.8% | historical_only | ok | False | 1882.6s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | w4 | 400 | 67.5% | historical_only | ok | False | 1426.2s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w1 | 99 | 70.7% | exploratory | ok | False | 4411.0s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w4 | 99 | 72.7% | exploratory | ok | False | 4411.0s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | w1 | 99 | 65.7% | supported_measurement | ok | False | 1317.4s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | w4 | 99 | 68.7% | supported_measurement | ok | False | 1317.4s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w1 | 139 | 66.9% | exploratory | ok | False | 2904.1s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w4 | 139 | 59.0% | exploratory | ok | False | 2904.1s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | w1 | 139 | 49.6% | supported_measurement | ok | False | 826.8s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | w4 | 139 | 44.6% | supported_measurement | ok | False | 826.8s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w1 | 162 | 59.3% | exploratory | ok | False | 5596.8s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | w4 | 162 | 62.3% | exploratory | ok | False | 5596.8s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | w1 | 162 | 40.1% | supported_measurement | ok | False | 2955.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | w4 | 162 | 40.1% | supported_measurement | ok | False | 2955.3s |

## distill_eval

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 385 | 68.8% | exploratory | ok | False | 40304.5s |
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 385 | 68.8% | exploratory | ok | False | 40991.3s |
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 385 | 68.8% | exploratory | ok | False | 23636.2s |
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 385 | 68.8% | exploratory | ok | False | 50138.1s |
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 385 | 84.4% | exploratory | ok | False | 40304.5s |
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 385 | 78.7% | exploratory | ok | False | 40991.3s |
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 385 | 68.3% | exploratory | ok | False | 23636.2s |
| grimgar03 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 385 | 78.4% | exploratory | ok | False | 50138.1s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 61.4% | exploratory | ok | False | 6678.2s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 61.4% | exploratory | ok | False | 6705.1s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 61.4% | exploratory | ok | False | 6666.3s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 62.5% | exploratory | ok | False | 6539.6s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 60.2% | exploratory | ok | False | 4222.4s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 61.4% | exploratory | ok | False | 4123.4s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 62.5% | exploratory | ok | False | 4339.3s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 61.4% | exploratory | ok | False | 4217.4s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 60.2% | exploratory | ok | False | 6468.8s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 62.5% | exploratory | ok | False | 6254.9s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 88 | 61.4% | exploratory | ok | False | 6444.9s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 70.5% | exploratory | ok | False | 6678.2s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 67.0% | exploratory | ok | False | 6705.1s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 68.2% | exploratory | ok | False | 6666.3s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 71.6% | exploratory | ok | False | 6539.6s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 75.0% | exploratory | ok | False | 4222.4s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 76.1% | exploratory | ok | False | 4123.4s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 75.0% | exploratory | ok | False | 4339.3s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 73.9% | exploratory | ok | False | 4217.4s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 68.2% | exploratory | ok | False | 6468.8s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 77.3% | exploratory | ok | False | 6254.9s |
| index18 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 88 | 63.6% | exploratory | ok | False | 6444.9s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 78.4% | exploratory | ok | False | 41016.0s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 10 | 90.0% | exploratory | ok | False | 3116.6s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 78.4% | exploratory | ok | False | 46972.6s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 78.4% | exploratory | ok | False | 42125.5s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 78.4% | exploratory | ok | False | 44817.8s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 78.4% | exploratory | ok | False | 43536.4s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 76.1% | exploratory | ok | False | 41016.0s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 10 | 80.0% | exploratory | ok | False | 3116.6s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 78.4% | exploratory | ok | False | 46972.6s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 76.1% | exploratory | ok | False | 42125.5s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 77.3% | exploratory | ok | False | 44817.8s |
| index18 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 76.1% | exploratory | ok | False | 43536.4s |
| index18 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 13881.0s |
| index18 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 14009.3s |
| index18 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 27294.0s |
| index18 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 64.8% | exploratory | ok | False | 13881.0s |
| index18 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 71.6% | exploratory | ok | False | 14009.3s |
| index18 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 25.0% | exploratory | ok | False | 27294.0s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 88 | 64.8% | supported_measurement | ok | False | 4420.8s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 88 | 64.8% | supported_measurement | ok | False | 4272.4s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 88 | 64.8% | exploratory | ok | False | 19058.8s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 88 | 64.8% | exploratory | ok | False | 18449.6s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 88 | 64.8% | exploratory | ok | False | 16187.8s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 62.5% | supported_measurement | ok | False | 4420.8s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 59.1% | supported_measurement | ok | False | 4272.4s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 60.2% | exploratory | ok | False | 19058.8s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 59.1% | exploratory | ok | False | 18449.6s |
| index18 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 58.0% | exploratory | ok | False | 16187.8s |
| index18 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 72.7% | exploratory | ok | False | 47025.9s |
| index18 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 72.7% | exploratory | ok | False | 44789.2s |
| index18 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 88 | 72.7% | exploratory | ok | False | 46101.9s |
| index18 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 77.3% | exploratory | ok | False | 47025.9s |
| index18 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 70.5% | exploratory | ok | False | 44789.2s |
| index18 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 88 | 77.3% | exploratory | ok | False | 46101.9s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 88 | 75.0% | exploratory | ok | False | 19197.8s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 88 | 73.9% | exploratory | ok | False | 15856.9s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 88 | 73.9% | exploratory | ok | False | 15987.6s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 88 | 73.9% | exploratory | ok | False | 18069.2s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 88 | 73.9% | exploratory | ok | False | 18656.4s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 72.7% | exploratory | ok | False | 19197.8s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 69.3% | exploratory | ok | False | 15856.9s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 70.5% | exploratory | ok | False | 15987.6s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 71.6% | exploratory | ok | False | 18069.2s |
| index18 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 76.1% | exploratory | ok | False | 18656.4s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 90.0% | exploratory | ok | False | 5016.9s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 90.0% | exploratory | ok | False | 5928.3s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 11010.4s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 90.0% | exploratory | ok | False | 970.0s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 10.0% | exploratory | ok | False | 8963.0s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 15616.2s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 18920.4s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 17394.5s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 16723.5s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 18279.0s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 100.0% | exploratory | ok | False | 5016.9s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 100.0% | exploratory | ok | False | 5928.3s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 61.4% | exploratory | ok | False | 11010.4s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 90.0% | exploratory | ok | False | 970.0s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 10.0% | exploratory | ok | False | 8963.0s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 61.4% | exploratory | ok | False | 15616.2s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 75.0% | exploratory | ok | False | 18920.4s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 75.0% | exploratory | ok | False | 17394.5s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 71.6% | exploratory | ok | False | 16723.5s |
| index18 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 88 | 77.3% | exploratory | ok | False | 18279.0s |
| index18 | Qwen3.8-27B-unsloth-bnb-4b | local-lmstudio | lmstudio | 32768 | base | 88 | 73.9% | exploratory | ok | False | 18131.2s |
| index18 | Qwen3.8-27B-unsloth-bnb-4b | local-lmstudio | lmstudio | 32768 | tuned | 88 | 67.0% | exploratory | ok | False | 18131.2s |
| index18 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 92 | 71.7% | exploratory | ok | False | 40304.5s |
| index18 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 92 | 71.7% | exploratory | ok | False | 40991.3s |
| index18 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 92 | 71.7% | exploratory | ok | False | 50138.1s |
| index18 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 92 | 75.0% | exploratory | ok | False | 40304.5s |
| index18 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 92 | 75.0% | exploratory | ok | False | 40991.3s |
| index18 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 92 | 75.0% | exploratory | ok | False | 50138.1s |
| mushoku16 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 133 | 51.1% | exploratory | ok | False | 7319.4s |
| mushoku16 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 133 | 52.6% | exploratory | ok | False | 14029.3s |
| mushoku16 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 133 | 59.4% | exploratory | ok | False | 7319.4s |
| mushoku16 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 133 | 58.6% | exploratory | ok | False | 14029.3s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 41016.0s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 10 | 80.0% | exploratory | ok | False | 3116.6s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 46972.6s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 42125.5s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 44817.8s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 43536.4s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 75.2% | exploratory | ok | False | 41016.0s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 10 | 100.0% | exploratory | ok | False | 3116.6s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 72.2% | exploratory | ok | False | 46972.6s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 75.2% | exploratory | ok | False | 42125.5s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 78.2% | exploratory | ok | False | 44817.8s |
| mushoku16 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 78.2% | exploratory | ok | False | 43536.4s |
| mushoku16 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 133 | 61.7% | exploratory | ok | False | 13881.0s |
| mushoku16 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 133 | 61.7% | exploratory | ok | False | 14009.3s |
| mushoku16 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 133 | 61.7% | exploratory | ok | False | 27294.0s |
| mushoku16 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 73.7% | exploratory | ok | False | 13881.0s |
| mushoku16 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 69.9% | exploratory | ok | False | 14009.3s |
| mushoku16 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 22.6% | exploratory | ok | False | 27294.0s |
| mushoku16 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 133 | 45.1% | exploratory | ok | False | 19058.8s |
| mushoku16 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 133 | 45.1% | exploratory | ok | False | 18449.6s |
| mushoku16 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 133 | 45.1% | exploratory | ok | False | 16187.8s |
| mushoku16 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 52.6% | exploratory | ok | False | 19058.8s |
| mushoku16 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 54.9% | exploratory | ok | False | 18449.6s |
| mushoku16 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 51.1% | exploratory | ok | False | 16187.8s |
| mushoku16 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 47025.9s |
| mushoku16 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 44789.2s |
| mushoku16 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 46101.9s |
| mushoku16 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 79.7% | exploratory | ok | False | 47025.9s |
| mushoku16 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 71.4% | exploratory | ok | False | 44789.2s |
| mushoku16 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 69.9% | exploratory | ok | False | 46101.9s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 19197.8s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 15856.9s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 15987.6s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 18069.2s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 133 | 71.4% | exploratory | ok | False | 18656.4s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 75.9% | exploratory | ok | False | 19197.8s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 77.4% | exploratory | ok | False | 15856.9s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 76.7% | exploratory | ok | False | 15987.6s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 76.7% | exploratory | ok | False | 18069.2s |
| mushoku16 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 72.9% | exploratory | ok | False | 18656.4s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 100.0% | exploratory | ok | False | 5016.9s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 100.0% | exploratory | ok | False | 5928.3s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 11010.4s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 100.0% | exploratory | ok | False | 970.0s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 30.0% | exploratory | ok | False | 8963.0s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 15616.2s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 18920.4s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 17394.5s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 16723.5s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 18279.0s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 80.0% | exploratory | ok | False | 5016.9s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 80.0% | exploratory | ok | False | 5928.3s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 66.2% | exploratory | ok | False | 11010.4s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 100.0% | exploratory | ok | False | 970.0s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 0.0% | exploratory | ok | False | 8963.0s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 66.2% | exploratory | ok | False | 15616.2s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 72.2% | exploratory | ok | False | 18920.4s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 75.9% | exploratory | ok | False | 17394.5s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 72.2% | exploratory | ok | False | 16723.5s |
| mushoku16 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 133 | 75.2% | exploratory | ok | False | 18279.0s |
| mushoku16 | Qwen3.8-27B-unsloth-bnb-4b | local-lmstudio | lmstudio | 32768 | base | 133 | 70.7% | exploratory | ok | False | 18131.2s |
| mushoku16 | Qwen3.8-27B-unsloth-bnb-4b | local-lmstudio | lmstudio | 32768 | tuned | 133 | 73.7% | exploratory | ok | False | 18131.2s |
| mushoku16 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 133 | 50.4% | exploratory | ok | False | 40304.5s |
| mushoku16 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 133 | 50.4% | exploratory | ok | False | 40991.3s |
| mushoku16 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 133 | 50.4% | exploratory | ok | False | 50138.1s |
| mushoku16 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 72.9% | exploratory | ok | False | 40304.5s |
| mushoku16 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 62.4% | exploratory | ok | False | 40991.3s |
| mushoku16 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 133 | 62.4% | exploratory | ok | False | 50138.1s |
| owarimonogatari3 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 162 | 39.5% | exploratory | ok | False | 26607.3s |
| owarimonogatari3 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | base | 162 | 38.9% | exploratory | ok | False | 33318.2s |
| owarimonogatari3 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 162 | 42.0% | exploratory | ok | False | 26607.3s |
| owarimonogatari3 | 40c069824f4251a91eefaf281e | local-lmstudio | lmstudio | 32768 | tuned | 162 | 51.2% | exploratory | ok | False | 33318.2s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 70.4% | exploratory | ok | False | 41016.0s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 10 | 90.0% | exploratory | ok | False | 3116.6s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 70.4% | exploratory | ok | False | 46972.6s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 70.4% | exploratory | ok | False | 42125.5s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 70.4% | exploratory | ok | False | 44817.8s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 70.4% | exploratory | ok | False | 43536.4s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 70.4% | exploratory | ok | False | 41016.0s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 10 | 90.0% | exploratory | ok | False | 3116.6s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 67.3% | exploratory | ok | False | 46972.6s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 70.4% | exploratory | ok | False | 42125.5s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 66.0% | exploratory | ok | False | 44817.8s |
| owarimonogatari3 | Qwen3.5-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 72.8% | exploratory | ok | False | 43536.4s |
| owarimonogatari3 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 162 | 61.1% | exploratory | ok | False | 13881.0s |
| owarimonogatari3 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 162 | 61.1% | exploratory | ok | False | 14009.3s |
| owarimonogatari3 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | base | 162 | 61.1% | exploratory | ok | False | 27294.0s |
| owarimonogatari3 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 67.9% | exploratory | ok | False | 13881.0s |
| owarimonogatari3 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 64.8% | exploratory | ok | False | 14009.3s |
| owarimonogatari3 | Qwen3.5-35B-A3B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 28.4% | exploratory | ok | False | 27294.0s |
| owarimonogatari3 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 162 | 30.2% | exploratory | ok | False | 19058.8s |
| owarimonogatari3 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 162 | 30.2% | exploratory | ok | False | 18449.6s |
| owarimonogatari3 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | base | 162 | 30.2% | exploratory | ok | False | 16187.8s |
| owarimonogatari3 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 33.3% | exploratory | ok | False | 19058.8s |
| owarimonogatari3 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 37.7% | exploratory | ok | False | 18449.6s |
| owarimonogatari3 | Qwen3.5-9B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 32.7% | exploratory | ok | False | 16187.8s |
| owarimonogatari3 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 62.3% | exploratory | ok | False | 47025.9s |
| owarimonogatari3 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 62.3% | exploratory | ok | False | 44789.2s |
| owarimonogatari3 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | base | 162 | 62.3% | exploratory | ok | False | 46101.9s |
| owarimonogatari3 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 63.0% | exploratory | ok | False | 47025.9s |
| owarimonogatari3 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 57.4% | exploratory | ok | False | 44789.2s |
| owarimonogatari3 | Qwen3.8-27B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 67.9% | exploratory | ok | False | 46101.9s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.9% | exploratory | ok | False | 19197.8s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 15856.9s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 15987.6s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 18069.2s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 18656.4s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 63.6% | exploratory | ok | False | 19197.8s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 67.3% | exploratory | ok | False | 15856.9s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 64.8% | exploratory | ok | False | 15987.6s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 66.7% | exploratory | ok | False | 18069.2s |
| owarimonogatari3 | Qwen3.8-27B-BF16 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 66.0% | exploratory | ok | False | 18656.4s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 80.0% | exploratory | ok | False | 5016.9s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 30.0% | exploratory | ok | False | 5928.3s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 11010.4s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 80.0% | exploratory | ok | False | 970.0s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 10 | 0.0% | exploratory | ok | False | 8963.0s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 15616.2s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 18920.4s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 17394.5s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 16723.5s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | base | 162 | 67.3% | exploratory | ok | False | 18279.0s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 40.0% | exploratory | ok | False | 5016.9s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 10.0% | exploratory | ok | False | 5928.3s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 46.9% | exploratory | ok | False | 11010.4s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 0.0% | exploratory | ok | False | 970.0s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 10 | 0.0% | exploratory | ok | False | 8963.0s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 46.9% | exploratory | ok | False | 15616.2s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 56.2% | exploratory | ok | False | 18920.4s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 64.8% | exploratory | ok | False | 17394.5s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 64.2% | exploratory | ok | False | 16723.5s |
| owarimonogatari3 | Qwen3.8-27B-FP8 | local-lmstudio | lmstudio | 32768 | tuned | 162 | 63.6% | exploratory | ok | False | 18279.0s |
| owarimonogatari3 | Qwen3.8-27B-unsloth-bnb-4b | local-lmstudio | lmstudio | 32768 | base | 162 | 61.1% | exploratory | ok | False | 18131.2s |
| owarimonogatari3 | Qwen3.8-27B-unsloth-bnb-4b | local-lmstudio | lmstudio | 32768 | tuned | 162 | 58.6% | exploratory | ok | False | 18131.2s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 162 | 40.1% | exploratory | ok | False | 40304.5s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 162 | 40.1% | exploratory | ok | False | 40991.3s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 162 | 40.1% | exploratory | ok | False | 23636.2s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | base | 162 | 40.1% | exploratory | ok | False | 50138.1s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 69.1% | exploratory | ok | False | 40304.5s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 51.9% | exploratory | ok | False | 40991.3s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 45.7% | exploratory | ok | False | 23636.2s |
| owarimonogatari3 | Qwen3-14B | local-lmstudio | lmstudio | 32768 | tuned | 162 | 61.1% | exploratory | ok | False | 50138.1s |

## grammar_constraint

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | open-free | 396 | 62.6% | supported_measurement | ok | False | 360.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | open-grammar | 396 | 62.6% | supported_measurement | ok | False | 360.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle-free | 396 | 74.2% | supported_measurement | ok | False | 360.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle-grammar | 396 | 76.8% | supported_measurement | ok | False | 360.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | open-free | 99 | 66.7% | supported_measurement | ok | False | 71.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | open-grammar | 99 | 65.7% | supported_measurement | ok | False | 71.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle-free | 99 | 73.7% | supported_measurement | ok | False | 71.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle-grammar | 99 | 74.7% | supported_measurement | ok | False | 71.3s |
| mushoku16 | magistral-small | local-llamacpp-hip | llama.cpp-hip b101 | 8192 | open-free | 139 | 53.2% | supported_measurement | ok | False | 114.0s |
| mushoku16 | magistral-small | local-llamacpp-hip | llama.cpp-hip b101 | 8192 | open-grammar | 139 | 51.8% | supported_measurement | ok | False | 114.0s |
| mushoku16 | magistral-small | local-llamacpp-hip | llama.cpp-hip b101 | 8192 | oracle-free | 139 | 58.3% | supported_measurement | ok | False | 114.0s |
| mushoku16 | magistral-small | local-llamacpp-hip | llama.cpp-hip b101 | 8192 | oracle-grammar | 139 | 66.2% | supported_measurement | ok | False | 114.0s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | open-free | 139 | 61.2% | exploratory | ok | False | 1382.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | open-grammar | 139 | 59.0% | exploratory | ok | False | 1382.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | oracle-free | 139 | 77.0% | exploratory | ok | False | 1382.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | oracle-grammar | 139 | 79.1% | exploratory | ok | False | 1382.5s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | open-free | 162 | 47.5% | supported_measurement | ok | False | 135.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | open-grammar | 162 | 46.3% | supported_measurement | ok | False | 135.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle-free | 162 | 51.9% | supported_measurement | ok | False | 135.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | oracle-grammar | 162 | 51.2% | supported_measurement | ok | False | 135.1s |

## joint_scene

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | independent | 400 | 71.5% | exploratory | ok | False | 2149.6s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-chrono | 400 | 63.2% | exploratory | ok | False | 2149.6s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-shuffled | 400 | 47.8% | exploratory | ok | False | 2149.6s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | independent | 400 | 60.5% | historical_only | ok | False | 498.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-chrono | 400 | 57.0% | historical_only | ok | False | 498.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-shuffled | 400 | 50.2% | historical_only | ok | False | 498.9s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | independent | 99 | 76.8% | exploratory | ok | False | 957.2s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-chrono | 99 | 66.7% | exploratory | ok | False | 957.2s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-shuffled | 99 | 58.6% | exploratory | ok | False | 957.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | independent | 99 | 61.6% | supported_measurement | ok | False | 199.5s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-chrono | 99 | 54.5% | supported_measurement | ok | False | 199.5s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-shuffled | 99 | 54.5% | supported_measurement | ok | False | 199.5s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | independent | 139 | 59.7% | exploratory | ok | False | 996.8s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-chrono | 139 | 58.3% | exploratory | ok | False | 996.8s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-shuffled | 139 | 43.9% | exploratory | ok | False | 996.8s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | independent | 139 | 51.8% | historical_only | ok | False | 213.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-chrono | 139 | 47.5% | historical_only | ok | False | 213.6s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-shuffled | 139 | 48.9% | historical_only | ok | False | 213.6s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | independent | 162 | 59.3% | exploratory | ok | False | 1996.3s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-chrono | 162 | 69.8% | exploratory | ok | False | 1996.3s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | joint-shuffled | 162 | 43.8% | exploratory | ok | False | 1996.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | independent | 162 | 48.8% | supported_measurement | ok | False | 538.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-chrono | 162 | 45.7% | supported_measurement | ok | False | 538.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | joint-shuffled | 162 | 45.7% | supported_measurement | ok | False | 538.3s |

## lora_serving_eval

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | base | 385 | 64.4% | exploratory | ok | False | 6206.6s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | base | 385 | 64.4% | exploratory | ok | False | 2253.6s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | base | 385 | 64.4% | exploratory | ok | False | 1984.6s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | base | 385 | 64.4% | exploratory | ok | False | 1929.7s |
| grimgar03 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 385 | 63.6% | historical_only | ok | False | 4494.5s |
| grimgar03 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 385 | 63.6% | historical_only | ok | False | 6000.3s |
| grimgar03 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 385 | 63.6% | historical_only | ok | False | 4544.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | base | 385 | 79.7% | historical_only | ok | True | 28194.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 32768 | base | 385 | 64.4% | exploratory | ok | False | 3920.5s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | lora | 385 | 75.8% | exploratory | ok | False | 6206.6s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | lora | 385 | 68.1% | exploratory | ok | False | 2253.6s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | lora | 385 | 72.7% | exploratory | ok | False | 1984.6s |
| grimgar03 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | lora | 385 | 76.6% | exploratory | ok | False | 1929.7s |
| grimgar03 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 385 | 78.4% | historical_only | ok | False | 4494.5s |
| grimgar03 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 385 | 72.5% | historical_only | ok | False | 6000.3s |
| grimgar03 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 385 | 77.9% | historical_only | ok | False | 4544.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | lora | 385 | 83.9% | historical_only | ok | True | 28194.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 32768 | lora | 385 | 80.3% | exploratory | ok | False | 3920.5s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | base | 385 | 63.6% | exploratory | ok | True | 1456.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | base | 385 | 63.6% | exploratory | ok | True | 1799.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | base | 385 | 63.6% | exploratory | ok | True | 1441.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | lora | 385 | 78.4% | exploratory | ok | True | 1456.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | lora | 385 | 72.5% | exploratory | ok | True | 1799.3s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | lora | 385 | 77.9% | exploratory | ok | True | 1441.2s |
| index18 | llama-3.3-70b | local-lmstudio | lmstudio | 32768 | base | 88 | 77.3% | exploratory | ok | False | 4021.8s |
| index18 | llama-4-scout | local-lmstudio | lmstudio | 32768 | base | 88 | 62.5% | exploratory | ok | False | 3282.9s |
| index18 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | base | 92 | 70.7% | exploratory | ok | False | 6206.6s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | base | 92 | 73.9% | historical_only | ok | False | 19529.9s |
| index18 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | lora | 92 | 67.4% | exploratory | ok | False | 6206.6s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | lora | 92 | 81.5% | historical_only | ok | False | 19529.9s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 75.0% | exploratory | ok | False | 22071.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 75.0% | exploratory | ok | False | 23705.1s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 71.6% | exploratory | ok | False | 13622.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 73.9% | exploratory | ok | False | 7329.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 72.7% | exploratory | ok | False | 18441.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 76.1% | exploratory | ok | False | 6147.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 71.6% | exploratory | ok | False | 4068.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 71.6% | exploratory | ok | False | 3350.4s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 69.3% | exploratory | ok | False | 20538.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 64.8% | exploratory | ok | False | 24066.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 88 | 72.7% | exploratory | ok | False | 23059.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 22071.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 23705.1s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 13622.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 18441.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 4068.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 3350.4s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 20538.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 88 | 0.0% | exploratory | ok | False | 24066.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 76.1% | exploratory | ok | False | 22071.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 72.7% | exploratory | ok | False | 23705.1s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 76.1% | exploratory | ok | False | 13622.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 72.7% | exploratory | ok | False | 7329.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 72.7% | exploratory | ok | False | 18441.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 73.9% | exploratory | ok | False | 6147.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 76.1% | exploratory | ok | False | 4068.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 76.1% | exploratory | ok | False | 3350.4s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 71.6% | exploratory | ok | False | 20538.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 67.0% | exploratory | ok | False | 24066.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 88 | 72.7% | exploratory | ok | False | 23059.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 22071.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 23705.1s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 13622.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 18441.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 4068.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 3350.4s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 20538.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 88 | 0.0% | exploratory | ok | False | 24066.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 75.0% | exploratory | ok | False | 22071.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 75.0% | exploratory | ok | False | 23705.1s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 72.7% | exploratory | ok | False | 13622.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 75.0% | exploratory | ok | False | 18441.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 72.7% | exploratory | ok | False | 4068.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 72.7% | exploratory | ok | False | 3350.4s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 70.5% | exploratory | ok | False | 20538.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 88 | 68.2% | exploratory | ok | False | 24066.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 75.0% | exploratory | ok | False | 22071.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 77.3% | exploratory | ok | False | 23705.1s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 72.7% | exploratory | ok | False | 13622.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 75.0% | exploratory | ok | False | 18441.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 72.7% | exploratory | ok | False | 4068.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 72.7% | exploratory | ok | False | 3350.4s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 72.7% | exploratory | ok | False | 20538.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 88 | 67.0% | exploratory | ok | False | 24066.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 77.3% | exploratory | ok | False | 22071.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 77.3% | exploratory | ok | False | 23705.1s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 75.0% | exploratory | ok | False | 13622.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 72.7% | exploratory | ok | False | 18441.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 75.0% | exploratory | ok | False | 4068.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 75.0% | exploratory | ok | False | 3350.4s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 72.7% | exploratory | ok | False | 20538.5s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 88 | 68.2% | exploratory | ok | False | 24066.6s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-gold-guaranteed | 88 | 75.0% | exploratory | ok | False | 23059.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-open | 88 | 43.2% | exploratory | ok | False | 23059.2s |
| index18 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-reversed | 88 | 73.9% | exploratory | ok | False | 23059.2s |
| index18 | qwen35-9b-q4km | local-Vulkan | Vulkan | 32768 | base | 88 | 59.1% | exploratory | ok | False | 1090.3s |
| index18 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 88 | 59.1% | exploratory | ok | False | 2352.4s |
| index18 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 88 | 60.2% | exploratory | ok | False | 2352.4s |
| index18 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 88 | 59.1% | exploratory | ok | False | 2251.0s |
| index18 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 88 | 58.0% | exploratory | ok | False | 2251.0s |
| index18 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 88 | 59.1% | exploratory | ok | False | 2348.9s |
| index18 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 88 | 53.4% | exploratory | ok | False | 2348.9s |
| index18 | qwen35-9b-q4km-hardcases-r | local-Vulkan | Vulkan | 32768 | base | 88 | 59.1% | exploratory | ok | False | 2306.9s |
| index18 | qwen35-9b-q4km-hardcases-r | local-Vulkan | Vulkan | 32768 | lora | 88 | 58.0% | exploratory | ok | False | 2306.9s |
| index18 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 88 | 64.8% | exploratory | ok | False | 23.5s |
| index18 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 88 | 64.8% | exploratory | ok | False | 2831.8s |
| index18 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 88 | 64.8% | exploratory | ok | False | 2758.0s |
| index18 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 88 | 64.8% | exploratory | ok | False | 2791.1s |
| index18 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 88 | 67.0% | exploratory | ok | False | 2831.8s |
| index18 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 88 | 59.1% | exploratory | ok | False | 2758.0s |
| index18 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 88 | 62.5% | exploratory | ok | False | 2791.1s |
| index18 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 88 | 65.9% | exploratory | ok | False | 1374.4s |
| index18 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 88 | 65.9% | exploratory | ok | False | 2992.4s |
| index18 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 88 | 65.9% | exploratory | ok | False | 2854.6s |
| index18 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 88 | 65.9% | exploratory | ok | False | 2957.1s |
| index18 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 88 | 59.1% | exploratory | ok | False | 2992.4s |
| index18 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 88 | 55.7% | exploratory | ok | False | 2854.6s |
| index18 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 88 | 60.2% | exploratory | ok | False | 2957.1s |
| index18 | qwen35-9b-q8-hardcases-rep | local-Vulkan | Vulkan | 32768 | base | 88 | 65.9% | exploratory | ok | False | 2890.6s |
| index18 | qwen35-9b-q8-hardcases-rep | local-Vulkan | Vulkan | 32768 | lora | 88 | 55.7% | exploratory | ok | False | 2890.6s |
| mushoku16 | llama-3.3-70b | local-lmstudio | lmstudio | 32768 | base | 133 | 73.7% | exploratory | ok | False | 4021.8s |
| mushoku16 | llama-4-scout | local-lmstudio | lmstudio | 32768 | base | 133 | 45.1% | exploratory | ok | False | 3282.9s |
| mushoku16 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | base | 133 | 49.6% | exploratory | ok | False | 6206.6s |
| mushoku16 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 133 | 48.1% | historical_only | ok | False | 4494.5s |
| mushoku16 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 133 | 48.1% | historical_only | ok | False | 6000.3s |
| mushoku16 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 133 | 48.1% | historical_only | ok | False | 4544.9s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | base | 133 | 51.9% | historical_only | ok | False | 19529.9s |
| mushoku16 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | lora | 133 | 64.7% | exploratory | ok | False | 6206.6s |
| mushoku16 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 133 | 59.4% | historical_only | ok | False | 4494.5s |
| mushoku16 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 133 | 62.4% | historical_only | ok | False | 6000.3s |
| mushoku16 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 133 | 62.4% | historical_only | ok | False | 4544.9s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | lora | 133 | 60.9% | historical_only | ok | False | 19529.9s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 72.9% | exploratory | ok | False | 22071.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 70.7% | exploratory | ok | False | 23705.1s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 13622.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 74.4% | exploratory | ok | False | 7329.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 18441.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 74.4% | exploratory | ok | False | 6147.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 72.9% | exploratory | ok | False | 20538.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 69.2% | exploratory | ok | False | 24066.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 133 | 72.2% | exploratory | ok | False | 23059.2s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 133 | 0.0% | exploratory | ok | False | 22071.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 133 | 0.0% | exploratory | ok | False | 23705.1s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 133 | 0.0% | exploratory | ok | False | 13622.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 133 | 0.0% | exploratory | ok | False | 18441.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 133 | 0.0% | exploratory | ok | False | 20538.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 133 | 0.0% | exploratory | ok | False | 24066.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 71.4% | exploratory | ok | False | 22071.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 72.2% | exploratory | ok | False | 23705.1s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 69.2% | exploratory | ok | False | 13622.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 72.2% | exploratory | ok | False | 7329.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 73.7% | exploratory | ok | False | 18441.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 71.4% | exploratory | ok | False | 6147.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 72.9% | exploratory | ok | False | 20538.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 67.7% | exploratory | ok | False | 24066.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 133 | 73.7% | exploratory | ok | False | 23059.2s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 133 | 0.0% | exploratory | ok | False | 22071.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 133 | 0.0% | exploratory | ok | False | 23705.1s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 133 | 0.0% | exploratory | ok | False | 13622.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 133 | 0.0% | exploratory | ok | False | 18441.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 133 | 0.0% | exploratory | ok | False | 20538.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 133 | 0.0% | exploratory | ok | False | 24066.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 133 | 75.9% | exploratory | ok | False | 22071.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 133 | 76.7% | exploratory | ok | False | 23705.1s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 133 | 71.4% | exploratory | ok | False | 13622.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 133 | 76.7% | exploratory | ok | False | 18441.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 133 | 75.9% | exploratory | ok | False | 20538.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 133 | 74.4% | exploratory | ok | False | 24066.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 133 | 77.4% | exploratory | ok | False | 22071.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 133 | 76.7% | exploratory | ok | False | 23705.1s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 133 | 72.2% | exploratory | ok | False | 13622.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 133 | 78.2% | exploratory | ok | False | 18441.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 133 | 77.4% | exploratory | ok | False | 20538.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 133 | 75.9% | exploratory | ok | False | 24066.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 133 | 75.9% | exploratory | ok | False | 22071.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 133 | 76.7% | exploratory | ok | False | 23705.1s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 133 | 72.2% | exploratory | ok | False | 13622.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 133 | 78.2% | exploratory | ok | False | 18441.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 133 | 78.9% | exploratory | ok | False | 20538.5s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 133 | 78.9% | exploratory | ok | False | 24066.6s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-gold-guaranteed | 133 | 74.4% | exploratory | ok | False | 23059.2s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-open | 133 | 47.4% | exploratory | ok | False | 23059.2s |
| mushoku16 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-reversed | 133 | 75.2% | exploratory | ok | False | 23059.2s |
| mushoku16 | qwen35-9b-q4km | local-Vulkan | Vulkan | 32768 | base | 133 | 34.6% | exploratory | ok | False | 1090.3s |
| mushoku16 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 133 | 34.6% | exploratory | ok | False | 2352.4s |
| mushoku16 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 133 | 52.6% | exploratory | ok | False | 2352.4s |
| mushoku16 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 133 | 34.6% | exploratory | ok | False | 2251.0s |
| mushoku16 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 133 | 49.6% | exploratory | ok | False | 2251.0s |
| mushoku16 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 133 | 34.6% | exploratory | ok | False | 2348.9s |
| mushoku16 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 133 | 51.1% | exploratory | ok | False | 2348.9s |
| mushoku16 | qwen35-9b-q4km-hardcases-r | local-Vulkan | Vulkan | 32768 | base | 133 | 34.6% | exploratory | ok | False | 2306.9s |
| mushoku16 | qwen35-9b-q4km-hardcases-r | local-Vulkan | Vulkan | 32768 | lora | 133 | 49.6% | exploratory | ok | False | 2306.9s |
| mushoku16 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 133 | 45.1% | exploratory | ok | False | 23.5s |
| mushoku16 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 133 | 45.1% | exploratory | ok | False | 2831.8s |
| mushoku16 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 133 | 45.1% | exploratory | ok | False | 2758.0s |
| mushoku16 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 133 | 45.1% | exploratory | ok | False | 2791.1s |
| mushoku16 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 133 | 48.9% | exploratory | ok | False | 2831.8s |
| mushoku16 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 133 | 48.9% | exploratory | ok | False | 2758.0s |
| mushoku16 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 133 | 48.9% | exploratory | ok | False | 2791.1s |
| mushoku16 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 133 | 41.4% | exploratory | ok | False | 1374.4s |
| mushoku16 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 133 | 41.4% | exploratory | ok | False | 2992.4s |
| mushoku16 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 133 | 41.4% | exploratory | ok | False | 2854.6s |
| mushoku16 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 133 | 41.4% | exploratory | ok | False | 2957.1s |
| mushoku16 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 133 | 53.4% | exploratory | ok | False | 2992.4s |
| mushoku16 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 133 | 47.4% | exploratory | ok | False | 2854.6s |
| mushoku16 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 133 | 51.9% | exploratory | ok | False | 2957.1s |
| mushoku16 | qwen35-9b-q8-hardcases-rep | local-Vulkan | Vulkan | 32768 | base | 133 | 41.4% | exploratory | ok | False | 2890.6s |
| mushoku16 | qwen35-9b-q8-hardcases-rep | local-Vulkan | Vulkan | 32768 | lora | 133 | 47.4% | exploratory | ok | False | 2890.6s |
| owarimonogatari3 | llama-3.3-70b | local-lmstudio | lmstudio | 32768 | base | 162 | 65.4% | exploratory | ok | False | 4021.8s |
| owarimonogatari3 | llama-4-scout | local-lmstudio | lmstudio | 32768 | base | 162 | 45.7% | exploratory | ok | False | 3282.9s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | base | 162 | 45.7% | exploratory | ok | False | 6206.6s |
| owarimonogatari3 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 162 | 43.8% | historical_only | ok | False | 4494.5s |
| owarimonogatari3 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 162 | 43.8% | historical_only | ok | False | 6000.3s |
| owarimonogatari3 | qwen3-14b | local-ROCm | ROCm | 32768 | base | 162 | 43.8% | historical_only | ok | False | 4544.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | base | 162 | 53.7% | historical_only | ok | True | 28194.5s |
| owarimonogatari3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | base | 162 | 45.7% | exploratory | ok | False | 3920.5s |
| owarimonogatari3 | qwen3-14b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda | 32768 | lora | 162 | 54.9% | exploratory | ok | False | 6206.6s |
| owarimonogatari3 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 162 | 42.0% | historical_only | ok | False | 4494.5s |
| owarimonogatari3 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 162 | 43.8% | historical_only | ok | False | 6000.3s |
| owarimonogatari3 | qwen3-14b | local-ROCm | ROCm | 32768 | lora | 162 | 43.8% | historical_only | ok | False | 4544.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | lora | 162 | 58.0% | historical_only | ok | True | 28194.5s |
| owarimonogatari3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | lora | 162 | 57.4% | exploratory | ok | False | 3920.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 69.1% | exploratory | ok | False | 22071.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 68.5% | exploratory | ok | False | 23705.1s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 56.2% | exploratory | ok | False | 13622.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 67.9% | exploratory | ok | False | 7329.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 66.0% | exploratory | ok | False | 18441.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 67.9% | exploratory | ok | False | 6147.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 63.0% | exploratory | ok | False | 20538.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 51.2% | exploratory | ok | False | 24066.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 162 | 66.0% | exploratory | ok | False | 23059.2s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 162 | 0.0% | exploratory | ok | False | 22071.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 162 | 0.0% | exploratory | ok | False | 23705.1s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 162 | 0.0% | exploratory | ok | False | 13622.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 162 | 0.0% | exploratory | ok | False | 18441.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 162 | 0.0% | exploratory | ok | False | 20538.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base-contract | 162 | 0.0% | exploratory | ok | False | 24066.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 53.7% | exploratory | ok | False | 22071.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 57.4% | exploratory | ok | False | 23705.1s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 55.6% | exploratory | ok | False | 13622.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 55.6% | exploratory | ok | False | 7329.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 55.6% | exploratory | ok | False | 18441.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 56.2% | exploratory | ok | False | 6147.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 54.3% | exploratory | ok | False | 20538.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 47.5% | exploratory | ok | False | 24066.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora | 162 | 55.6% | exploratory | ok | False | 23059.2s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 162 | 0.0% | exploratory | ok | False | 22071.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 162 | 0.0% | exploratory | ok | False | 23705.1s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 162 | 0.0% | exploratory | ok | False | 13622.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 162 | 0.0% | exploratory | ok | False | 18441.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 162 | 0.0% | exploratory | ok | False | 20538.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | lora-contract | 162 | 0.0% | exploratory | ok | False | 24066.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 162 | 68.5% | exploratory | ok | False | 22071.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 162 | 66.0% | exploratory | ok | False | 23705.1s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 162 | 59.3% | exploratory | ok | False | 13622.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 162 | 63.6% | exploratory | ok | False | 18441.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 162 | 59.9% | exploratory | ok | False | 20538.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale025 | 162 | 53.1% | exploratory | ok | False | 24066.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 162 | 63.6% | exploratory | ok | False | 22071.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 162 | 62.3% | exploratory | ok | False | 23705.1s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 162 | 55.6% | exploratory | ok | False | 13622.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 162 | 61.7% | exploratory | ok | False | 18441.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 162 | 53.7% | exploratory | ok | False | 20538.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale050 | 162 | 48.1% | exploratory | ok | False | 24066.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 162 | 58.6% | exploratory | ok | False | 22071.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 162 | 56.2% | exploratory | ok | False | 23705.1s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 162 | 56.8% | exploratory | ok | False | 13622.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 162 | 61.7% | exploratory | ok | False | 18441.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 162 | 55.6% | exploratory | ok | False | 20538.5s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | scale075 | 162 | 47.5% | exploratory | ok | False | 24066.6s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-gold-guaranteed | 162 | 58.6% | exploratory | ok | False | 23059.2s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-open | 162 | 41.4% | exploratory | ok | False | 23059.2s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned-reversed | 162 | 51.2% | exploratory | ok | False | 23059.2s |
| owarimonogatari3 | qwen35-9b-q4km | local-Vulkan | Vulkan | 32768 | base | 162 | 37.0% | exploratory | ok | False | 1090.3s |
| owarimonogatari3 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 162 | 37.0% | exploratory | ok | False | 2352.4s |
| owarimonogatari3 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 162 | 34.0% | exploratory | ok | False | 2352.4s |
| owarimonogatari3 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 162 | 37.0% | exploratory | ok | False | 2251.0s |
| owarimonogatari3 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 162 | 34.0% | exploratory | ok | False | 2251.0s |
| owarimonogatari3 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | base | 162 | 37.0% | exploratory | ok | False | 2348.9s |
| owarimonogatari3 | qwen35-9b-q4km+qwen35_9b_b | local-Vulkan | Vulkan | 32768 | lora | 162 | 35.2% | exploratory | ok | False | 2348.9s |
| owarimonogatari3 | qwen35-9b-q4km-hardcases-r | local-Vulkan | Vulkan | 32768 | base | 162 | 37.0% | exploratory | ok | False | 2306.9s |
| owarimonogatari3 | qwen35-9b-q4km-hardcases-r | local-Vulkan | Vulkan | 32768 | lora | 162 | 34.0% | exploratory | ok | False | 2306.9s |
| owarimonogatari3 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 162 | 30.9% | exploratory | ok | False | 23.5s |
| owarimonogatari3 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 162 | 30.9% | exploratory | ok | False | 2831.8s |
| owarimonogatari3 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 162 | 30.9% | exploratory | ok | False | 2758.0s |
| owarimonogatari3 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | base | 162 | 30.9% | exploratory | ok | False | 2791.1s |
| owarimonogatari3 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 162 | 34.0% | exploratory | ok | False | 2831.8s |
| owarimonogatari3 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 162 | 32.7% | exploratory | ok | False | 2758.0s |
| owarimonogatari3 | qwen35-9b-q6k | local-Vulkan | Vulkan | 32768 | lora | 162 | 34.6% | exploratory | ok | False | 2791.1s |
| owarimonogatari3 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 162 | 35.2% | exploratory | ok | False | 1374.4s |
| owarimonogatari3 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 162 | 35.2% | exploratory | ok | False | 2992.4s |
| owarimonogatari3 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 162 | 35.2% | exploratory | ok | False | 2854.6s |
| owarimonogatari3 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | base | 162 | 35.2% | exploratory | ok | False | 2957.1s |
| owarimonogatari3 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 162 | 34.0% | exploratory | ok | False | 2992.4s |
| owarimonogatari3 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 162 | 32.7% | exploratory | ok | False | 2854.6s |
| owarimonogatari3 | qwen35-9b-q8 | local-Vulkan | Vulkan | 32768 | lora | 162 | 33.3% | exploratory | ok | False | 2957.1s |
| owarimonogatari3 | qwen35-9b-q8-hardcases-rep | local-Vulkan | Vulkan | 32768 | base | 162 | 35.2% | exploratory | ok | False | 2890.6s |
| owarimonogatari3 | qwen35-9b-q8-hardcases-rep | local-Vulkan | Vulkan | 32768 | lora | 162 | 32.7% | exploratory | ok | False | 2890.6s |

## narrator_prior

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 400 | 57.2% | historical_only | ok | False | 2180.7s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | narrator | 400 | 60.2% | historical_only | ok | False | 2180.7s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 99 | 64.6% | supported_measurement | ok | False | 1021.5s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | narrator | 99 | 65.7% | supported_measurement | ok | False | 1021.5s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 139 | 51.8% | supported_measurement | ok | False | 842.4s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | narrator | 139 | 56.1% | supported_measurement | ok | False | 842.4s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | baseline | 162 | 38.3% | supported_measurement | ok | False | 1783.8s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | narrator | 162 | 38.9% | supported_measurement | ok | False | 1783.8s |

## pdnc_context_evidence

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 58.3% | supported_measurement | ok | False | 767.9s |
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 60.0% | supported_measurement | ok | False | 767.9s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 62.5% | supported_measurement | ok | False | 767.9s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 64.2% | supported_measurement | ok | False | 767.9s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 61.7% | supported_measurement | ok | False | 767.9s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 51.7% | supported_measurement | ok | False | 767.9s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 51.7% | supported_measurement | ok | False | 767.9s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 51.7% | supported_measurement | ok | False | 767.9s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 54.2% | supported_measurement | ok | False | 767.9s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 65.0% | supported_measurement | ok | False | 767.9s |

## pdnc_evidence

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 59.2% | supported_measurement | ok | False | 791.6s |
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 69.2% | supported_measurement | ok | False | 791.6s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 62.5% | supported_measurement | ok | False | 791.6s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 60.0% | supported_measurement | ok | False | 791.6s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 60.0% | supported_measurement | ok | False | 791.6s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 53.3% | supported_measurement | ok | False | 791.6s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 50.0% | supported_measurement | ok | False | 791.6s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 51.7% | supported_measurement | ok | False | 791.6s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 60.8% | supported_measurement | ok | False | 791.6s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | evidence | 120 | 63.3% | supported_measurement | ok | False | 791.6s |

## pdnc_narrator_prior

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 50.0% | supported_measurement | ok | False | 422.2s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 51.7% | provisional | ok | True | 294.0s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | generic | 120 | 51.7% | provisional | ok | True | 140.0s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | narrator | 120 | 77.5% | supported_measurement | ok | False | 422.2s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | narrator | 120 | 77.5% | provisional | ok | True | 294.0s |
| TheMysteriousAffairAtStyles | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 80.8% | supported_measurement | ok | False | 422.2s |
| TheMysteriousAffairAtStyles | qwen3-14b | local-lmstudio | lmstudio | 32768 | narrator | 120 | 85.0% | supported_measurement | ok | False | 422.2s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 54.2% | supported_measurement | ok | False | 422.2s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 54.2% | provisional | ok | True | 294.0s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | generic | 120 | 53.3% | provisional | ok | True | 140.0s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | narrator | 120 | 75.8% | supported_measurement | ok | False | 422.2s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | narrator | 120 | 75.8% | provisional | ok | True | 294.0s |

## pdnc_sequence

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 58.3% | supported_measurement | ok | False | 741.2s |
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 70.0% | supported_measurement | ok | False | 823.6s |
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 64.2% | supported_measurement | ok | False | 741.2s |
| AnneOfGreenGables | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 70.0% | supported_measurement | ok | False | 823.6s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 62.5% | supported_measurement | ok | False | 741.2s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 62.5% | supported_measurement | ok | False | 823.6s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 58.3% | supported_measurement | ok | False | 741.2s |
| MansfieldPark | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 60.0% | supported_measurement | ok | False | 823.6s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 61.7% | supported_measurement | ok | False | 741.2s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 55.0% | supported_measurement | ok | False | 823.6s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 62.5% | supported_measurement | ok | False | 741.2s |
| Persuasion | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 49.2% | supported_measurement | ok | False | 823.6s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 51.7% | supported_measurement | ok | False | 741.2s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 49.2% | supported_measurement | ok | False | 823.6s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 51.7% | supported_measurement | ok | False | 741.2s |
| TheGambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 51.7% | supported_measurement | ok | False | 823.6s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 54.2% | supported_measurement | ok | False | 741.2s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 62.5% | supported_measurement | ok | False | 823.6s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 63.3% | supported_measurement | ok | False | 741.2s |
| TheSunAlsoRises | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 62.5% | supported_measurement | ok | False | 823.6s |

## pdnc_targeted_sequence

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| AHandfulOfDust | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 77.5% | supported_measurement | ok | False | 679.2s |
| AHandfulOfDust | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 75.8% | supported_measurement | ok | False | 679.2s |
| AHandfulOfDust | qwen3-14b | local-lmstudio | lmstudio | 32768 | targeted_sequence | 120 | 78.3% | supported_measurement | ok | False | 679.2s |
| APassageToIndia | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 65.0% | supported_measurement | ok | False | 679.2s |
| APassageToIndia | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 61.7% | supported_measurement | ok | False | 679.2s |
| APassageToIndia | qwen3-14b | local-lmstudio | lmstudio | 32768 | targeted_sequence | 120 | 60.8% | supported_measurement | ok | False | 679.2s |
| ARoomWithAView | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 72.5% | supported_measurement | ok | False | 679.2s |
| ARoomWithAView | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 75.0% | supported_measurement | ok | False | 679.2s |
| ARoomWithAView | qwen3-14b | local-lmstudio | lmstudio | 32768 | targeted_sequence | 120 | 76.7% | supported_measurement | ok | False | 679.2s |
| AlicesAdventuresInWonderland | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 83.3% | supported_measurement | ok | False | 679.2s |
| AlicesAdventuresInWonderland | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 90.8% | supported_measurement | ok | False | 679.2s |
| AlicesAdventuresInWonderland | qwen3-14b | local-lmstudio | lmstudio | 32768 | targeted_sequence | 120 | 89.2% | supported_measurement | ok | False | 679.2s |
| DaisyMiller | qwen3-14b | local-lmstudio | lmstudio | 32768 | baseline | 120 | 69.2% | supported_measurement | ok | False | 679.2s |
| DaisyMiller | qwen3-14b | local-lmstudio | lmstudio | 32768 | sequence | 120 | 69.2% | supported_measurement | ok | False | 679.2s |
| DaisyMiller | qwen3-14b | local-lmstudio | lmstudio | 32768 | targeted_sequence | 120 | 69.2% | supported_measurement | ok | False | 679.2s |

## pipeline_repeat

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run1 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run2 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run3 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run4 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run5 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run6 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run7 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | run8 | 396 | 56.1% | not_audited | n/a (pipeline output, not an ExperimentRecord) |  | s |

## qwen38_forced_choice

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | base | 8 | 12.5% | exploratory | ok | False | 19.4s |
| owarimonogatari3 | qwen3.8-27b | local-lmstudio | lmstudio | 32768 | tuned | 8 | 12.5% | exploratory | ok | False | 19.4s |

## reasoning_arms

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | baseline | 400 | 71.5% | historical_only | ok | False | 9454.9s |
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | because | 400 | 72.2% | historical_only | ok | False | 9454.9s |
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | scaffold | 400 | 68.2% | historical_only | ok | False | 9454.9s |
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | scaffold_thinking | 400 | 68.2% | historical_only | ok | False | 9454.9s |
| grimgar03 | gemma-3-27b | cloud-a6000-lmstudio | lmstudio | 16384 | thinking | 400 | 71.2% | historical_only | ok | False | 9454.9s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | baseline | 400 | 64.0% | exploratory | ok | False | 8256.2s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | because | 400 | 61.8% | exploratory | ok | False | 8256.2s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | scaffold | 400 | 52.0% | exploratory | ok | False | 8256.2s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | scaffold_thinking | 400 | 52.0% | exploratory | ok | False | 8256.2s |
| grimgar03 | magistral-small | cloud-a6000-lmstudio | lmstudio | 16384 | thinking | 400 | 63.0% | exploratory | ok | False | 8256.2s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | baseline | 400 | 56.5% | historical_only | ok | True | 8588.7s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | because | 400 | 54.8% | historical_only | ok | True | 8588.7s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | scaffold | 400 | 52.5% | historical_only | ok | True | 8588.7s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | scaffold_thinking | 400 | 56.5% | historical_only | ok | True | 8588.7s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | thinking | 400 | 66.2% | historical_only | ok | True | 8588.7s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | baseline | 400 | 67.2% | exploratory | ok | False | 21565.9s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | because | 400 | 67.5% | exploratory | ok | False | 21565.9s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | scaffold | 400 | 57.8% | exploratory | ok | False | 21565.9s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | scaffold_thinking | 400 | 68.5% | exploratory | ok | False | 21565.9s |
| grimgar03 | qwen3-32b | cloud-a6000-lmstudio | lmstudio | 16384 | thinking | 400 | 72.2% | exploratory | ok | False | 21565.9s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 99 | 61.6% | supported_measurement | ok | False | 8280.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | because | 99 | 62.6% | supported_measurement | ok | False | 8280.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | scaffold | 99 | 56.6% | supported_measurement | ok | False | 8280.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | scaffold_thinking | 99 | 68.7% | supported_measurement | ok | False | 8280.2s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | thinking | 99 | 68.7% | supported_measurement | ok | False | 8280.2s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | baseline | 139 | 39.6% | historical_only | ok | True | 5021.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | because | 139 | 50.4% | historical_only | ok | True | 5021.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | scaffold | 139 | 41.0% | historical_only | ok | True | 5021.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | scaffold_thinking | 139 | 48.2% | historical_only | ok | True | 5021.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | thinking | 139 | 41.7% | historical_only | ok | True | 5021.5s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 162 | 43.2% | supported_measurement | ok | False | 12654.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | because | 162 | 47.5% | supported_measurement | ok | False | 12654.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | scaffold | 162 | 38.9% | supported_measurement | ok | False | 12654.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | scaffold_thinking | 162 | 43.2% | supported_measurement | ok | False | 12654.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | thinking | 162 | 38.9% | supported_measurement | ok | False | 12654.2s |

## reasoning_check

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | plain | 396 | 58.1% | historical_only | ok | False | 3748.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | reasoned | 396 | 59.3% | historical_only | ok | False | 3748.4s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | plain | 99 | 62.6% | supported_measurement | ok | False | 1966.4s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | reasoned | 99 | 58.6% | supported_measurement | ok | False | 1966.4s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | plain | 139 | 51.1% | supported_measurement | ok | False | 1970.5s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | reasoned | 139 | 48.2% | supported_measurement | ok | False | 1970.5s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | plain | 162 | 40.7% | supported_measurement | ok | False | 2902.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | reasoned | 162 | 41.4% | supported_measurement | ok | False | 2902.2s |

## reexamine

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | baseline | 139 | 49.6% | provisional | ok | True | 2737.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | narration | 139 | 34.5% | provisional | ok | True | 2737.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | narrator | 139 | 51.8% | provisional | ok | True | 2737.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | prose | 139 | 47.5% | provisional | ok | True | 2737.5s |
| mushoku16 | qwen3-14b | local-lmstudio | lmstudio | 16384 | voting | 139 | 49.6% | provisional | ok | True | 2737.5s |

## roster_quality

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | augmented | 385 | 85.7% | exploratory | ok | False | 8051.1s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | generated | 385 | 84.9% | exploratory | ok | False | 8051.1s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | gold | 385 | 83.9% | exploratory | ok | False | 8051.1s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | inflated | 385 | 83.6% | exploratory | ok | False | 8051.1s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | augmented | 385 | 63.6% | provisional | ok | True | 4837.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | generated | 385 | 59.7% | provisional | ok | True | 4837.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | gold | 385 | 61.6% | provisional | ok | True | 4837.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | inflated | 385 | 61.0% | provisional | ok | True | 4837.9s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | augmented | 92 | 79.3% | exploratory | ok | False | 7535.3s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | generated | 92 | 80.4% | exploratory | ok | False | 7535.3s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | gold | 92 | 82.6% | exploratory | ok | False | 7535.3s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | inflated | 92 | 79.3% | exploratory | ok | False | 7535.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | augmented | 92 | 71.7% | provisional | ok | True | 2056.7s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | generated | 92 | 67.4% | provisional | ok | True | 2056.7s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | gold | 92 | 69.6% | provisional | ok | True | 2056.7s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | inflated | 92 | 66.3% | provisional | ok | True | 2056.7s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | augmented | 133 | 69.2% | exploratory | ok | False | 4930.5s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | generated | 133 | 69.2% | exploratory | ok | False | 4930.5s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | gold | 133 | 69.9% | exploratory | ok | False | 4930.5s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | inflated | 133 | 68.4% | exploratory | ok | False | 4930.5s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | augmented | 133 | 51.9% | provisional | ok | True | 2800.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | generated | 133 | 48.9% | provisional | ok | True | 2800.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | gold | 133 | 48.9% | provisional | ok | True | 2800.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | inflated | 133 | 48.1% | provisional | ok | True | 2800.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | augmented | 162 | 45.1% | provisional | ok | True | 7321.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | generated | 162 | 40.7% | provisional | ok | True | 7321.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | gold | 162 | 42.6% | provisional | ok | True | 7321.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | inflated | 162 | 30.9% | provisional | ok | True | 7321.2s |

## roster_warmup

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | incremental | 139 | 41.0% | supported_measurement | ok | False | 8408.8s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | oracle | 139 | 46.8% | supported_measurement | ok | False | 8408.8s |
| mushoku16 | ministral-3-14b-instruct-2 | local-lmstudio | lmstudio | 16384 | warm | 139 | 44.6% | supported_measurement | ok | False | 8408.8s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio | 32768 | incremental | 139 | 27.3% | exploratory | ok | False | 1039.6s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio | 32768 | oracle | 139 | 35.3% | exploratory | ok | False | 1039.6s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio | 32768 | warm | 139 | 32.4% | exploratory | ok | False | 1039.6s |

## scene_cast

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | full | 385 | 85.7% | exploratory | ok | False | 6197.0s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene | 385 | 84.9% | exploratory | ok | False | 6197.0s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene+2 | 385 | 86.8% | exploratory | ok | False | 6197.0s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | full | 385 | 63.6% | provisional | ok | True | 2178.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | full | 385 | 63.6% | provisional | ok | True | 2284.5s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | full | 385 | 66.0% | provisional | ok | True | 2094.8s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene | 385 | 66.0% | provisional | ok | True | 2178.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene | 385 | 62.3% | provisional | ok | True | 2284.5s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene | 385 | 63.1% | provisional | ok | True | 2094.8s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene+2 | 385 | 65.5% | provisional | ok | True | 2178.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene+2 | 385 | 63.1% | provisional | ok | True | 2284.5s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene+2 | 385 | 63.6% | provisional | ok | True | 2094.8s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | full | 92 | 81.5% | exploratory | ok | False | 5653.6s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene | 92 | 80.4% | exploratory | ok | False | 5653.6s |
| index18 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene+2 | 92 | 80.4% | exploratory | ok | False | 5653.6s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | full | 92 | 71.7% | provisional | ok | True | 1518.9s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene | 92 | 68.5% | provisional | ok | True | 1518.9s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene+2 | 92 | 68.5% | provisional | ok | True | 1518.9s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | full | 133 | 69.2% | exploratory | ok | False | 4011.9s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene | 133 | 62.4% | exploratory | ok | False | 4011.9s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene+2 | 133 | 70.7% | exploratory | ok | False | 4011.9s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | full | 133 | 51.9% | provisional | ok | True | 1690.7s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | full | 133 | 51.9% | provisional | ok | True | 1714.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene | 133 | 51.9% | provisional | ok | True | 1690.7s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene | 133 | 52.6% | provisional | ok | True | 1714.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene+2 | 133 | 52.6% | provisional | ok | True | 1690.7s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene+2 | 133 | 51.9% | provisional | ok | True | 1714.3s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | full | 162 | 64.2% | exploratory | ok | False | 7587.4s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene | 162 | 54.9% | exploratory | ok | False | 7587.4s |
| owarimonogatari3 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | scene+2 | 162 | 62.3% | exploratory | ok | False | 7587.4s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | full | 162 | 45.1% | provisional | ok | True | 3728.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene | 162 | 38.9% | provisional | ok | True | 3728.1s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | scene+2 | 162 | 40.7% | provisional | ok | True | 3728.1s |

## segmentation_crossover

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=gemma,t=0.0,rep=1 | 399 | 58.6% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=gemma,t=0.0,rep=2 | 399 | 58.6% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=gemma,t=0.6,rep=1 | 399 | 57.1% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=gemma,t=0.6,rep=2 | 399 | 57.9% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=gemma,t=0.6,rep=3 | 399 | 59.4% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=qwen,t=0.0,rep=1 | 399 | 60.9% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=qwen,t=0.0,rep=2 | 399 | 60.9% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=qwen,t=0.6,rep=1 | 399 | 60.7% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=qwen,t=0.6,rep=2 | 399 | 60.7% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=gemma,attr=qwen,t=0.6,rep=3 | 399 | 59.9% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=gemma,t=0.0,rep=1 | 399 | 56.6% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=gemma,t=0.0,rep=2 | 399 | 56.6% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=gemma,t=0.6,rep=1 | 399 | 55.9% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=gemma,t=0.6,rep=2 | 399 | 56.6% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=gemma,t=0.6,rep=3 | 399 | 57.4% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=qwen,t=0.0,rep=1 | 399 | 58.4% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=qwen,t=0.0,rep=2 | 399 | 58.4% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=qwen,t=0.6,rep=1 | 399 | 58.6% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=qwen,t=0.6,rep=2 | 399 | 57.9% | historical_only | ok | False | 1832.5s |
| grimgar03 | qwen3-14b | local-lmstudio | lmstudio | 16384 | seg=qwen,attr=qwen,t=0.6,rep=3 | 399 | 58.1% | historical_only | ok | False | 1832.5s |

## tag_priority

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | baseline | 396 | 82.6% | exploratory | ok | False | 4007.5s |
| grimgar03 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | tagfirst | 396 | 82.8% | exploratory | ok | False | 4007.5s |
| grimgar03 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 396 | 70.7% | provisional | ok | True | 2667.2s |
| grimgar03 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | tagfirst | 396 | 68.9% | provisional | ok | True | 2667.2s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 396 | 63.4% | supported_measurement | ok | False | 1803.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 396 | 62.4% | supported_measurement | ok | False | 1782.1s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 396 | 62.4% | supported_measurement | ok | False | 1781.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 400 | 56.5% | historical_only | ok | True | 1887.0s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | tagfirst | 396 | 62.4% | supported_measurement | ok | False | 1803.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | tagfirst | 396 | 62.4% | supported_measurement | ok | False | 1782.1s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | tagfirst | 396 | 62.4% | supported_measurement | ok | False | 1781.9s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | tagfirst | 400 | 63.0% | historical_only | ok | True | 1887.0s |
| index18 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 99 | 68.7% | provisional | ok | True | 1642.5s |
| index18 | magistral-small | local-llamacpp-hip | llama.cpp-hip | 16384 | tagfirst | 99 | 64.6% | provisional | ok | True | 1642.5s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | baseline | 99 | 64.6% | supported_measurement | ok | False | 913.0s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | tagfirst | 99 | 65.7% | supported_measurement | ok | False | 913.0s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | baseline | 136 | 67.6% | exploratory | ok | False | 2362.9s |
| mushoku16 | llama-3.3-70b | cloud-a6000-llamacpp-cuda | llama.cpp-cuda on- | 16384 | tagfirst | 136 | 66.2% | exploratory | ok | False | 2362.9s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | baseline | 139 | 51.1% | provisional | ok | True | 1073.2s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | tagfirst | 139 | 45.3% | provisional | ok | True | 1073.2s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | baseline | 162 | 40.7% | supported_measurement | ok | False | 1891.9s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 32768 | tagfirst | 162 | 43.2% | supported_measurement | ok | False | 1891.9s |

## two_by_two

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio |  | A | 139 | 19.4% | exploratory | ['no LM Studio load state recorded', 'en | True | 698.2s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio |  | B | 139 | 2.2% | exploratory | ['no LM Studio load state recorded', 'en | True | 698.2s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio |  | C | 139 | 34.5% | exploratory | ['no LM Studio load state recorded', 'en | True | 698.2s |
| mushoku16 | qwen3.5-9b-uncensored-hauh | local-lmstudio | lmstudio |  | D | 139 | 18.7% | exploratory | ['no LM Studio load state recorded', 'en | True | 698.2s |

## two_stage_attribution

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| attribution_gold_pdnc_ahandfulofdust | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 409 | 56.0% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_ahandfulofdust | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 409 | 57.0% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_ahandfulofdust | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 409 | 56.0% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_ahandfulofdust | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 409 | 60.4% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_ahandfulofdust | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 409 | 56.2% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_prideandprejudice | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 47.4% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_prideandprejudice | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 47.7% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_prideandprejudice | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 47.4% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_prideandprejudice | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 48.3% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_prideandprejudice | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 46.8% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_prideandprejudice | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 30 | 50.0% | historical_only | ok | False | 25.9s |
| attribution_gold_pdnc_prideandprejudice | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 1270 | 53.8% | historical_only | ok | False | 718.1s |
| attribution_gold_pdnc_prideandprejudice_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 62.4% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_prideandprejudice_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 62.7% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_prideandprejudice_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 81.7% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_prideandprejudice_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 59.9% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_prideandprejudice_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 327 | 63.0% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_prideandprejudice_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 1270 | 68.2% | historical_only | ok | False | 2662.3s |
| attribution_gold_pdnc_theawakening | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 59.3% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_theawakening | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 62.2% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_theawakening | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 59.3% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_theawakening | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 62.2% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_theawakening | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 60.0% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_theawakening | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 30 | 70.0% | historical_only | ok | False | 25.9s |
| attribution_gold_pdnc_theawakening | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 584 | 60.4% | historical_only | ok | False | 718.1s |
| attribution_gold_pdnc_theawakening_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 74.8% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_theawakening_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 73.3% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_theawakening_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 78.5% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_theawakening_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 70.4% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_theawakening_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 135 | 69.6% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_theawakening_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 584 | 70.5% | historical_only | ok | False | 2662.3s |
| attribution_gold_pdnc_thegambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 89 | 42.7% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_thegambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 89 | 37.1% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_thegambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 89 | 42.7% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_thegambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 89 | 44.9% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_thegambler | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 89 | 42.7% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_themysteriousaffairatstyles | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 239 | 57.7% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_themysteriousaffairatstyles | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 239 | 56.5% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_themysteriousaffairatstyles | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 239 | 57.7% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_themysteriousaffairatstyles | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 239 | 56.9% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_themysteriousaffairatstyles | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 239 | 56.9% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_thesignofthefour | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 59.3% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_thesignofthefour | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 58.0% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_thesignofthefour | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 59.3% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_thesignofthefour | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 51.9% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_thesignofthefour | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 61.7% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_thesignofthefour | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 30 | 60.0% | historical_only | ok | False | 25.9s |
| attribution_gold_pdnc_thesignofthefour | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 640 | 50.6% | historical_only | ok | False | 718.1s |
| attribution_gold_pdnc_thesignofthefour_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 49.4% | historical_only | ok | False | 891.2s |
| attribution_gold_pdnc_thesignofthefour_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 58.0% | historical_only | ok | False | 917.3s |
| attribution_gold_pdnc_thesignofthefour_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 76.5% | historical_only | ok | False | 876.1s |
| attribution_gold_pdnc_thesignofthefour_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 54.3% | historical_only | ok | False | 1450.7s |
| attribution_gold_pdnc_thesignofthefour_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 81 | 59.3% | historical_only | ok | False | 940.4s |
| attribution_gold_pdnc_thesignofthefour_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 200 | 42.5% | supported_measurement | ok | False | 212.6s |
| attribution_gold_pdnc_thesignofthefour_w3200 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 640 | 56.1% | historical_only | ok | False | 2662.3s |
| attribution_gold_riqua_austen_emma_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 23 | 78.3% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_austen_emma_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 23 | 78.3% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_austen_emma_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 23 | 82.6% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_austen_emma_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 23 | 78.3% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_austen_emma_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 23 | 78.3% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_austen_emma_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 23 | 73.9% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_austen_emma_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 103 | 76.7% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_austen_emma_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 103 | 76.7% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_austen_emma_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 103 | 79.6% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_austen_emma_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 103 | 76.7% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_austen_emma_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 103 | 75.7% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_austen_emma_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 103 | 75.7% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_austen_emma_3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 189 | 77.2% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_austen_emma_3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 189 | 77.2% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_austen_emma_3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 189 | 73.5% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_austen_emma_3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 189 | 77.2% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_austen_emma_3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 189 | 74.6% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_austen_emma_3 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 189 | 75.1% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_chekhov_lady | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 6 | 16.7% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_chekhov_lady | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 6 | 16.7% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_chekhov_lady | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 6 | 16.7% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_chekhov_lady | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 6 | 16.7% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_chekhov_lady | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 6 | 16.7% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_chekhov_lady | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 6 | 33.3% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_chekhov_monk | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 20 | 85.0% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_chekhov_monk | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 20 | 85.0% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_chekhov_monk | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 20 | 90.0% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_chekhov_monk | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 20 | 85.0% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_chekhov_monk | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 20 | 85.0% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_chekhov_monk | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 20 | 90.0% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_chekhov_steppe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 200 | 70.0% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_chekhov_steppe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 252 | 69.8% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_chekhov_steppe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 252 | 72.2% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_chekhov_steppe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 252 | 69.8% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_chekhov_steppe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 252 | 68.7% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_chekhov_steppe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 252 | 71.8% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_dickens_xmas | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 200 | 63.5% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_dickens_xmas | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 320 | 61.3% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_dickens_xmas | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 320 | 65.6% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_dickens_xmas | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 320 | 61.3% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_dickens_xmas | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 320 | 65.3% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_dickens_xmas | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 320 | 65.0% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_doyle_boscombe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 55 | 38.2% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_doyle_boscombe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 55 | 38.2% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_doyle_boscombe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 55 | 49.1% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_doyle_boscombe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 55 | 38.2% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_doyle_boscombe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 55 | 45.5% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_doyle_boscombe | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 55 | 38.2% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_doyle_identity | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 7 | 71.4% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_doyle_identity | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 7 | 71.4% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_doyle_identity | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 7 | 71.4% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_doyle_identity | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 7 | 71.4% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_doyle_identity | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 7 | 71.4% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_doyle_identity | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 7 | 71.4% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_doyle_league | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 46 | 50.0% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_doyle_league | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 46 | 50.0% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_doyle_league | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 46 | 52.2% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_doyle_league | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 46 | 50.0% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_doyle_league | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 46 | 39.1% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_doyle_league | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 46 | 47.8% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_doyle_scandal | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 18 | 5.6% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_doyle_scandal | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 18 | 5.6% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_doyle_scandal | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 18 | 11.1% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_doyle_scandal | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 18 | 5.6% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_doyle_scandal | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 18 | 11.1% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_doyle_scandal | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 18 | 16.7% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_flaubert_bovary_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 95 | 65.3% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_flaubert_bovary_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 95 | 65.3% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_flaubert_bovary_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 95 | 69.5% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_flaubert_bovary_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 95 | 65.3% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_flaubert_bovary_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 95 | 64.2% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_flaubert_bovary_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 95 | 69.5% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_flaubert_bovary_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 68 | 66.2% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_flaubert_bovary_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 68 | 66.2% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_flaubert_bovary_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 68 | 75.0% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_flaubert_bovary_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 68 | 66.2% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_flaubert_bovary_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 68 | 66.2% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_flaubert_bovary_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 68 | 73.5% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_twain_sawyer_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 57 | 54.4% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_twain_sawyer_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 57 | 54.4% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_twain_sawyer_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 57 | 54.4% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_twain_sawyer_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 57 | 54.4% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_twain_sawyer_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 57 | 57.9% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_twain_sawyer_1 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 57 | 63.2% | exploratory | ok | False | 1552.0s |
| attribution_gold_riqua_twain_sawyer_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 200 | 62.5% | supported_measurement | ok | False | 1179.0s |
| attribution_gold_riqua_twain_sawyer_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 278 | 60.8% | exploratory | ok | False | 1399.2s |
| attribution_gold_riqua_twain_sawyer_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 278 | 61.9% | exploratory | ok | False | 1455.5s |
| attribution_gold_riqua_twain_sawyer_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 278 | 60.8% | exploratory | ok | False | 1496.5s |
| attribution_gold_riqua_twain_sawyer_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 278 | 60.1% | exploratory | ok | False | 1501.2s |
| attribution_gold_riqua_twain_sawyer_2 | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 278 | 62.6% | exploratory | ok | False | 1552.0s |
| attribution_gold_wp2021_dev | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 180 | 0.0% | exploratory | ok | False | 116.7s |
| attribution_gold_wp2021_dev | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 180 | 80.6% | exploratory | ok | False | 545.2s |
| attribution_gold_wp2021_dev | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 180 | 82.8% | exploratory | ok | False | 602.0s |
| attribution_gold_wp2021_dev | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 180 | 80.6% | exploratory | ok | False | 662.2s |
| attribution_gold_wp2021_dev | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 180 | 80.6% | exploratory | ok | False | 1378.1s |
| attribution_gold_wp2021_dev | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 180 | 82.8% | exploratory | ok | False | 613.5s |
| attribution_gold_wp2021_test | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 200 | 0.0% | exploratory | ok | False | 116.7s |
| attribution_gold_wp2021_test | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 215 | 84.2% | exploratory | ok | False | 545.2s |
| attribution_gold_wp2021_test | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 215 | 90.7% | exploratory | ok | False | 602.0s |
| attribution_gold_wp2021_test | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 215 | 84.2% | exploratory | ok | False | 662.2s |
| attribution_gold_wp2021_test | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 215 | 85.6% | exploratory | ok | False | 1378.1s |
| attribution_gold_wp2021_test | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 215 | 89.3% | exploratory | ok | False | 613.5s |
| attribution_gold_wp2021_train | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 200 | 0.0% | exploratory | ok | False | 59.2s |
| attribution_gold_wp2021_train | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 1440 | 83.1% | exploratory | ok | False | 545.2s |
| attribution_gold_wp2021_train | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 1440 | 84.7% | exploratory | ok | False | 602.0s |
| attribution_gold_wp2021_train | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 1440 | 83.1% | exploratory | ok | False | 662.2s |
| attribution_gold_wp2021_train | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 1440 | 83.2% | exploratory | ok | False | 1378.1s |
| attribution_gold_wp2021_train | qwen3-14b | local-lmstudio | lmstudio | 32768 | single | 1440 | 84.2% | exploratory | ok | False | 613.5s |

## voting

| book | model | env | backend | ctx | arm | n | acc | evidence | valid | dirty | elapsed |
|---|---|---|---|---:|---|---:|---:|---|---|---|---:|
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | greedy | 400 | 55.8% | historical_only | ok | False | 6517.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | vote3 | 400 | 58.0% | historical_only | ok | False | 6517.4s |
| grimgar03 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | vote5 | 400 | 57.8% | historical_only | ok | False | 6517.4s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | greedy | 99 | 64.6% | supported_measurement | ok | False | 1607.3s |
| index18 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | vote3 | 99 | 63.6% | supported_measurement | ok | False | 1607.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | greedy | 139 | 47.5% | supported_measurement | ok | False | 3221.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | vote3 | 139 | 48.9% | supported_measurement | ok | False | 3221.3s |
| mushoku16 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | vote5 | 139 | 48.9% | supported_measurement | ok | False | 3221.3s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | greedy | 162 | 40.7% | supported_measurement | ok | False | 2872.6s |
| owarimonogatari3 | qwen3-14b | local-llamacpp-hip | llama.cpp-hip | 16384 | vote3 | 162 | 38.3% | supported_measurement | ok | False | 2872.6s |

## Not indexed

These artifacts exist and hold real results; this table only represents per-arm attribution accuracy, so they cannot be rendered as rows. Read them directly.

| artifact | why |
|---|---|
| `adapter_stop_check_aishell3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `adapter_stop_check_kokoro.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `adapter_stop_check_ljspeech.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `addressee_confusion.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `addressee_confusion_riqua.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `aishell3_SSB0748_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `aishell3_SSB0748_score.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `aishell3_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `aishell3_score.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `alignment_diagnosis.json` | SKIPPED: 'rows' is not a list of scored arms |
| `alignment_diagnosis_trimmed.json` | SKIPPED: 'rows' is not a list of scored arms |
| `alignment_diagnosis_zh.json` | SKIPPED: 'rows' is not a list of scored arms |
| `anchor_length_audit.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `anchor_length_probe.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `annotator_evidence.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `aozora_quote_coverage.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr2_hybrid__kokoro.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr2_whisper_cpp__ggml-base.bin.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr2_whisper_cpp__ggml-large-v3.bin.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr2_whisper_cpp_hybrid__ggml-base.bin.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_backends__aishell3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_backends__kokoro.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_backends__ljspeech.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_backends_large__aishell3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_backends_large__kokoro.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_backends_large__ljspeech.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_energy_vad_ja.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_hybrid_zh.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_control_preexisting_kouyahijiri.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_cutting_control.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_hypotheses.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_largev3_hybrid.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_largev3_readings.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_reader__botchan-by-soseki-natsume-2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_reader__gan-by-ogai-mori.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_reader__kouyahijiri-by-kyoka-izumi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_reader__kusamakura-by-soseki-natsume.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_reading_per_reader.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_readings.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_ja_trimmed.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_silero_vad_ja.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_silero_vad_ja_holdout.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_silero_whisper_ja_confirmation.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_silero_whisper_ja_offset20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_zh_ggml-base.bin.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `asr_zh_ggml-large-v3.bin.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `attribution_hybrid__anne_pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `attribution_hybrid__evidence_direct_pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `attribution_hybrid__evidence_joint_pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `attribution_hybrid__evidence_joint_robustness2.json` | SKIPPED: 'rows' is not a list of scored arms |
| `attribution_hybrid__mixed_adapter_joint_pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `attribution_hybrid__open5_pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `attribution_hybrid__sequence_joint_pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `attribution_hybrid__sequence_repeat_joint_pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `audible_errors.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `audio_views.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `baseline_heldout__husky_baritone_40s_m_2.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `baseline_heldout__husky_baritone_40s_m_scifi.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `baseline_heldout__husky_tenor_30s_m_literary.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `baseline_heldout__silky_baritone_30s_m_fantasy.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `baseline_heldout__warm_baritone_30s_m_2.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `baseline_heldout__warm_baritone_30s_m_scifi.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `blinded_listening.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `blinded_listening_ratings.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `candidate_scoring_feasibility.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `candidate_scoring_feasibility__endpoint.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `cascade_state__grimgar03__a6000-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__grimgar03__a6000-contig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__grimgar03__local-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__grimgar03__local-bt-rep1.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__grimgar03__local-bt-rep2.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__grimgar03__thunder-a6000.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__grimgar03__tuned-cheap-arm.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__index18__a6000-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__index18__a6000-newbook.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__index18__local-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__index18__tuned-cheap-arm.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__mushoku16__a6000-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__mushoku16__a6000-contig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__mushoku16__local-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__mushoku16__thunder-a6000.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__mushoku16__tuned-cheap-arm.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__owarimonogatari3__a6000-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__owarimonogatari3__a6000-newbook.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__owarimonogatari3__local-batchtrig.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cascade_state__owarimonogatari3__tuned-cheap-arm.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chapter_manifest.json` | SKIPPED: not a result object (list) |
| `chapter_validation.json` | SKIPPED: 'rows' is not a list of scored arms |
| `character_distinctiveness.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `chinese_attribution_frame.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `chinese_attribution_jy.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chinese_attribution_jy_fixed.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chinese_attribution_wp.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chinese_attribution_wp_fixed.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chunk11_stability.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chunk_completion.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chunk_completion_goal31.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `chunk_completion_qwen3.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `clone_vs_lora_seeded.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `cluster_vs_name.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `constraint_refine.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `contextual_mandarin_tone_generated_n150.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `contextual_mandarin_tone_n150.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `corpus_hnr_baseline.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `cpu_chain_release.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `crossbook_normalization.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `crossbook_normalization_pilot.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `ctc_japanese_boundary_n10.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `ctc_japanese_boundary_n150.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `dataset_ref_audit.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `dataset_source_identification.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `dataset_speaker_consistency.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `dataset_speaker_consistency_n10.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `dataset_tone_spread.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `decontaminate_batch1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `decontaminate_batch2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `decontaminate_batch3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `decontaminate_batch4.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `decontaminate_batch5.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `decontaminate_batch6.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `descriptor_candidates.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `dialogue_attribution.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `dialogue_attribution_library.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `dialogue_map_compare.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `dialogue_map_compare_fresh.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `distill_eval__qwen35_35b_a3b_bf16_speaker_longcontext_tophalf_5epoch-corrected-gold-a100-loaderfix-20260828.INVALID.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `distill_eval__qwen38_27b_fp8_author_heldout_balanced-fp8-corrected-gold-a100-loaderfix-20260829.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `distill_eval__qwen38_27b_fp8_author_heldout_balanced_lr2e5_1epoch-fp8-corrected-gold-a100-20260830.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `distill_eval__qwen38_27b_nf4_author_heldout_balanced-corrected-gold-a6000-loaderfix-20260829.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `duration_length_intervention.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `duration_outlier_analysis.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `duration_probe.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `duration_probe_20260811_overnight.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `duration_probe_20260811_rerun.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `duration_probe_same_speaker_20260815.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `earcheck_separator_key.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `earcheck_separator_package.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `earcheck_separator_results.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `ecapa_duration_confound.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `elson_trigram__grimgar03.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `elson_trigram__index18.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `elson_trigram__mushoku16.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `elson_trigram__owarimonogatari3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `elson_trigram_w3200.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `epub_extraction_fidelity.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `epub_extractor_comparison.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `evidence_adjudication__pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `expected_prosody__ja_n150.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `expected_prosody__zh_n150.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `expected_prosody_fusion__ja_n150.json` | SKIPPED: 'rows' is not a list of scored arms |
| `expected_prosody_fusion__zh_n150.json` | SKIPPED: 'rows' is not a list of scored arms |
| `expected_prosody_ja.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `expected_prosody_ja_n200.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `expected_prosody_zh.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `expected_prosody_zh_n200.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `external_comparability.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `fallback_policy.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `final_release_after_remaining_gpu_research.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `fix_verification.json` | SKIPPED: not a result object (list) |
| `frontend_exposure.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `full_api_gpu_20260816.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `full_sequence_scoring__llamacpp_calibration.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_known_bad.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_known_good.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_postpromotion__breathy_alto_50s_f_fantasy_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_alto_50s_f_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_baritone_40s_m_literary.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_baritone_40s_m_military_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_baritone_40s_m_military_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_mezzo_20s_f_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_tenor_18s_m_supernatural.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_tenor_20s_m_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__breathy_tenor_50s_m_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__crisp_mezzo_30s_f.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__crisp_tenor_20s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__crisp_tenor_30s_m_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__gravelly_baritone_50s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_alto_40s_f.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_20s_m_anime.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_20s_m_supernatural.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_35s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_40s_m_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_40s_m_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_40s_m_military.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_40s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_50s_m_gothic.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_baritone_50s_m_military.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_soprano_20s_f.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_tenor_30s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_tenor_30s_m_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__husky_tenor_30s_m_literary.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_alto_40s_f.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_alto_40s_f_cyberpunk.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_alto_40s_f_literary_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_baritone_30s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_baritone_30s_m_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_baritone_40s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_baritone_45s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_mezzo_10s_f_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_mezzo_30s_f_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__silky_mezzo_30s_f_supernatural.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__velvety_mezzo_30s_f_gothic.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_alto_40s_f_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_alto_40s_f_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_alto_50s_f_anime.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_alto_50s_f_gothic.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_30s_m_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_30s_m_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_30s_m_3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_30s_m_fantasy_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_30s_m_fantasy_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_30s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_40s_m_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_40s_m_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_40s_m_4.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_40s_m_5.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_40s_m_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_40s_m_military.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_40s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_50s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_baritone_50s_m_gothic.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_bass_50s_m_fantasy.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_mezzo_20s_f_anime.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_mezzo_30s_f.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_mezzo_30s_f_fantasy_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_mezzo_30s_f_fantasy_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_tenor_20s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_tenor_20s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_tenor_25s_m_military.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_tenor_30s_m_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_tenor_30s_m_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_promote__warm_tenor_30s_m_gothic.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_recheck__silky_alto_40s_f_literary_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_recheck__silky_baritone_45s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_recheck__velvety_mezzo_30s_f_gothic.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `gate_reference_rank1__breathy_alto_50s_f_fantasy.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__husky_baritone_20s_m_supernatural.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__husky_baritone_40s_m_2.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__husky_baritone_40s_m_scifi.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__husky_soprano_20s_f.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__husky_tenor_30s_m_literary.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__silky_alto_40s_f_literary_1.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__silky_baritone_30s_m_fantasy.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__silky_baritone_45s_m.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__silky_mezzo_30s_f_supernatural.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__velvety_mezzo_30s_f_gothic.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_alto_50s_f_gothic.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_baritone_30s_m_2.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_baritone_30s_m_scifi.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_baritone_50s_m_gothic.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_bass_50s_m_fantasy.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_mezzo_20s_f_anime.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_mezzo_30s_f.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_tenor_20s_m.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_tenor_20s_m_scifi.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank1__warm_tenor_30s_m_gothic.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank2__breathy_alto_50s_f_fantasy.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank2__husky_baritone_20s_m_supernatural.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank2__silky_alto_40s_f_literary_1.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank2__silky_baritone_45s_m.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank2__velvety_mezzo_30s_f_gothic.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `gate_reference_rank2__warm_alto_50s_f_gothic.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `generation_realtime_rate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `hnr_length_probe.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `homograph_listening_ratings.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `homograph_probe.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__breathy_alto_50s_f_fantasy__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__husky_baritone_20s_m_supernatural__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__husky_tenor_30s_m__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__silky_baritone_45s_m__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__velvety_mezzo_30s_f_gothic__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__warm_baritone_30s_m_3__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__warm_baritone_40s_m_1__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `identity_recheck__warm_baritone_50s_m_gothic__seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `instruct_listening.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `instruct_listening_fixed.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `instruct_value.json` | SKIPPED: 'rows' is not a list of scored arms |
| `instruct_value_seeded.json` | SKIPPED: 'rows' is not a list of scored arms |
| `instrument_null_test.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `japanese_accent_calibration_key.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `japanese_quote_robustness.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `japanese_text_boundary_n50.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `kansai_otsuka_listening_key.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `kansai_otsuka_listening_package.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `kokoro_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `kokoro_same_speaker_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `kokoro_score.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `lexicon_attributed.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lexicon_attributed_v2.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lexicon_backmatter_probe.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lexicon_candidates.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `lexicon_corpus_candidates.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lexicon_pilot.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `library_fidelity_control_ryan_seed20260925.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260827_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260828_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260829_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260831_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260901_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260902_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260903_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260904_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260905_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260906_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260907_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260908_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260909_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260910_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260911_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260912_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260913_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260914_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260915_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260916_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260916_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260917_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260917_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260918_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260918_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260919_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260920_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260921_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260922_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260923_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260924_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_fidelity_seed_20260925_n20_full75.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity_n10.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity_postfix.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity_postpromotion_n10.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity_seed_20260824_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity_seed_20260825_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity_seed_20260826_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `library_voice_fidelity_seed_20260830_n20.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `listener_impact.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `listening_verdicts.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `ljspeech_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `ljspeech_score.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `longref__en_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `longref__en_score.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `longref__ja_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `longref__ja_score.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `longref__zh_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `longref__zh_score.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `lora_serving_eval__llama33-q4km-corrected-gold-a100-20260830.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lora_serving_eval__qwen35-9b-q4km-clean-gold-local-20260827.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lora_serving_eval__qwen35-9b-q6k-baseline-clean-gold-local-20260827.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lora_serving_eval__qwen35-9b-q8-baseline-clean-gold-local-20260827.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lora_serving_eval__qwen38-author-balanced-refusal-scale-contract-a6000-20260901d.ARM_INVALID.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `lora_serving_eval__scout-q4km-corrected-gold-a100-20260830.CORRECTION.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `medoid_counterexample.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `medoid_library_retrain.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `modernbooknlp_direct__Persuasion.json` | SKIPPED: 'rows' is not a list of scored arms |
| `modernbooknlp_direct__TheGambler.json` | SKIPPED: 'rows' is not a list of scored arms |
| `modernbooknlp_direct__TheSunAlsoRises.json` | SKIPPED: 'rows' is not a list of scored arms |
| `modernbooknlp_direct__anneofgreengables.json` | SKIPPED: 'rows' is not a list of scored arms |
| `modernbooknlp_joint__Persuasion.json` | SKIPPED: 'rows' is not a list of scored arms |
| `modernbooknlp_joint__TheGambler.json` | SKIPPED: 'rows' is not a list of scored arms |
| `modernbooknlp_joint__TheSunAlsoRises.json` | SKIPPED: 'rows' is not a list of scored arms |
| `modernbooknlp_joint__anneofgreengables.json` | SKIPPED: 'rows' is not a list of scored arms |
| `name_consistency.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `name_variant_triage.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `nonprose_category_expansion.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `nonprose_category_expansion_pilot.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `nonprose_gate.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `nonprose_mechanism.json` | SKIPPED: 'rows' is not a list of scored arms |
| `nonprose_replication.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `nonprose_replication_pilot.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `nonprose_split.json` | SKIPPED: 'rows' is not a list of scored arms |
| `nonprose_split_v2.json` | SKIPPED: 'rows' is not a list of scored arms |
| `offbyone_turns.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `overnight_release.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_character_style_selector__pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `pdnc_context_audit.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `pdnc_eval.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_development_ahandfulofdust.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_development_thegambler.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_development_themysteriousaffairatstyles.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_heldout_emma.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_heldout_mansfieldpark.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_heldout_northangerabbey.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_heldout_persuasion.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__goal13_heldout_senseandsensibility.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__mixed_adapter_clean_heldout_n900.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__mixed_adapter_clean_heldout_n900_b10.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval__qwen35_9b_author_five_new_heldout_20260828.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval_full.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval_full_summary.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_eval_mixed.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_failure_telemetry.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_generalisation.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_luar_character_selector__pilot.json` | SKIPPED: 'rows' is not a list of scored arms |
| `pdnc_new_adapter_adapter_author_heldout_balanced_full_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_new_adapter_adapter_speaker_hardcases_split_nonmajor_full_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_new_adapter_adapter_speaker_longcontext_tophalf_5epoch_full_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_adapter_ln_full_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_adapter_ln_n200_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_adapter_mixed_full_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_adapter_mixed_n200_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_attrib-lora-25_n200_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_attrib-lora-50_n200_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_attrib-lora-75_n200_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_attrib-lora-alldata_n200_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_attrib-lora-f16_full_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_old_adapter_attrib-lora-f16_n200_b5.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pdnc_pilots_paired.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `pitch_profile_matrix.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `pitch_profile_matrix_pilot.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `pitch_quality_SSB0748.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pitch_quality_longref.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pitch_quality_probe.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pitch_quality_probe_n200.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pitch_separation.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pitch_stability.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `pr308_narration_context__mushoku16_index18__current.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `production_trigram_audit.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `proper_noun_pronunciation.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prose_vs_nonprose.json` | SKIPPED: 'rows' is not a list of scored arms |
| `prose_vs_nonprose_v2.json` | SKIPPED: 'rows' is not a list of scored arms |
| `prose_vs_nonprose_v3.json` | SKIPPED: 'rows' is not a list of scored arms |
| `prosody_fidelity_en.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_en_n100.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_en_n150.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_en_n40.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_ja.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_ja_n100.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_ja_n150.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_ja_n40.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_zh.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_zh_n100.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_zh_n150.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_fidelity_zh_n40.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__husky_baritone_50s_m_military.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__husky_soprano_20s_f.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__silky_alto_40s_f.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__silky_baritone_30s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__silky_baritone_40s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__warm_baritone_30s_m_1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__warm_baritone_30s_m_scifi.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `prosody_second_english__warm_baritone_50s_m.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `realizable_router.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `ref_clip_match.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_audit.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_intervention__husky_baritone_20s_m_anime.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_intervention_sharp.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_rank1_all21.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_rank1_pilot.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_rank2_failed.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__21seed_aggregate_20260828.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `reference_spread__en.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm0_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm1_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm2_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_generate_arm3_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm0_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm1_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm2_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260825.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260826.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260827.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260828.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260829.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260830.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260831.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260901.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260902.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260903.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260904.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260905.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260906.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260907.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260908.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260909.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260910.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260911.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260912.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260913.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `reference_spread__en_score_arm3_seed20260914.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `repair_candidate_reference_text.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `repetition_scan.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `residual_errors.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `respelling_dot_allrows_n1600.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_e_row__ay.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_e_row__ay_n1200.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_e_row__ay_n1600.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_e_row__ay_n800.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_e_row__ay_sample2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_e_row__e.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_e_row__ei.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_earcheck.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_five_terms_ja.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_five_terms_unattributed.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_hyphen_allrows_n1600.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_measure.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `respelling_measure_rescored.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `respelling_none_allrows.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_none_allrows_n1600.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_none_allrows_n3200.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_pauses.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_pauses_allrows.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_pauses_allrows_4arm.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_pauses_separators.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_pauses_separators_3arm.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_rule_b.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `respelling_rule_b_rescored.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `respelling_selectivity.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_separator__dot.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_separator__none.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_separator__none_n400.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_separator__space.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `respelling_space_allrows_n1600.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `retrain_bad_refs.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `retrain_honest.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `retrain_rebuild_group.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `retrofit_dialogue_map.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `riqua_coverage.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `robotic_proxy_clips.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `roster_order_bias.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `run_lengths.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `satella_quality_probe.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `satella_quality_probe_n10.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `satella_quality_ratings.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `satella_quality_ratings_n10.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `scale_vs_register.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `scene_aware_casting.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `scene_narrowing_w3200.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `script_text_fidelity.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `script_text_fidelity_fresh.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__husky_baritone_50s_m_military_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__husky_soprano_20s_f_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__silky_alto_40s_f_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__silky_baritone_30s_m_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__silky_baritone_40s_m_scifi_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__warm_baritone_30s_m_1_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__warm_baritone_30s_m_scifi_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `second_english__warm_baritone_50s_m_generate.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `seed_instruction_controls.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `segmentation_classifier.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `selection_gap_recheck.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `shipped_book_lexicon_coverage.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `shipped_term_triage.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `shipping_readiness.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `simd_benchmark.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `source_coverage_single.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `source_encoding_audit.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `split_quote_evidence.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `stack_overlap.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `stage6_instruction_source.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `stage6_scene_aware_casting.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `statistic_discriminability.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `symbolization.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `symbolization_owari.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `three_pass_vs_single.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `three_pass_vs_single_fallback.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `three_pass_vs_single_index18.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `three_pass_vs_single_mapped.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `three_pass_vs_single_pdnc.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `three_pass_vs_single_pdnc_resumed.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `three_pass_vs_single_qwen3.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `training_composition.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `training_determinism.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `trivial_baselines.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `tts_boundary_audit.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `tts_output_validation_adapter_seed20260925.json` | SKIPPED: 'rows' is not a list of scored arms |
| `tts_output_validation_control_seed20260925.json` | SKIPPED: 'rows' is not a list of scored arms |
| `tuned_disagreement.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `two_stage_attribution.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `two_stage_diagnostic.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `two_stage_diagnostic2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `two_stage_selection_gap.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `two_stage_selection_gap_w3200.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__breathy_baritone_40s_m_military_2__clean.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__breathy_baritone_40s_m_military_2__shipped.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__breathy_tenor_50s_m_fantasy__clean.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__breathy_tenor_50s_m_fantasy__shipped.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__husky_baritone_40s_m_scifi__clean.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__husky_baritone_40s_m_scifi__shipped.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__husky_tenor_30s_m__clean.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__husky_tenor_30s_m__shipped.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__husky_tenor_30s_m_literary__clean.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__husky_tenor_30s_m_literary__shipped.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__silky_baritone_40s_m_scifi__clean.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `unseen_gate__silky_baritone_40s_m_scifi__shipped.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `validation_baseline.json` | SKIPPED: 'rows' is not a list of scored arms |
| `validation_manifest.json` | SKIPPED: not a result object (list) |
| `validation_smoke.json` | SKIPPED: 'rows' is not a list of scored arms |
| `voice_adapter_health.json` | SKIPPED: 'rows' is not a list of scored arms |
| `voice_blending.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `voice_data_saturation.json` | NOT INDEXED: no 'rows' list - this table only represents per-arm attribution results |
| `voice_data_saturation_seeded.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `voice_drift_2000__husky_tenor_30s_m_literary.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `voice_drift_2000__warm_baritone_40s_m_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `voice_drift_2000__warm_mezzo_30s_f_fantasy_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `voice_drift__husky_baritone_20s_m_anime.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `voice_drift__husky_tenor_30s_m_literary.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `voice_drift__warm_mezzo_30s_f_fantasy_2.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `weak_supervision.json` | SKIPPED: 'rows' is not a list of scored arms |
| `wider_tts_NARRATOR.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `wider_tts__test_voice.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
| `wp2021_coverage.json` | NOT INDEXED: TTS provenance artifact; read its per-book/category summary directly |
