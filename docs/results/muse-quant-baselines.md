# Muse-Glimmer base quant comparison

This page compares two completed base-only Muse-Glimmer-30B runs. They use the
same nine-novel PDNC fixture, prompt variant, decode settings, and probe
harness. No adapter was loaded in either run.

## Results

| quant | model file size | correct / scored rows | accuracy | conditional accuracy |
|---|---:|---:|---:|---:|
| UD-Q3_K_XL | 13.4 GB | 2,478 / 2,655 | **93.33%** | 95.21% (1,869 / 1,963 available) |
| Q4 K-quant | about 17 GB | 2,512 / 2,655 | **94.61%** | 95.42% (1,873 / 1,963 available) |

Both use `michel2_full`, reasoning low, temperature 0, JSON schema, and 40
evenly spaced windows from each of nine novels. The headline accuracy counts
all 2,655 rows; conditional accuracy counts only rows where the model returned
a parseable answer. The Q4 run scored 34 more rows correctly (+1.28 percentage
points), but this is one paired run per quant, not a repeated-seed estimate.

## Data and provenance

- [Q3 raw result](../../ab_test_runtime/experiments/lora_serving_eval__muse-q3-michel2_full-tnr1-pdnc9lite-low-schema-20260917.json)
- [Q4 raw result](../../ab_test_runtime/experiments/lora_serving_eval__muse-q4-kquant17-michel2_full-tnr4-pdnc9lite-low-schema-20260921.json)
- [Evaluation recipes](../../RECIPES.md#september-22-follow-up-low-quant-adapter-and-nemotron-api-baselines)
- [Complete artifact index](../../RESULTS_INDEX.md)

Both artifacts report validation `ok`, the same gold-file hashes, and the same
probe-harness hash. They identify source commit `4d337725` and also record that
the checkout was dirty because `app/default_prompts_attribute.txt` was
modified. The runs explicitly selected `michel2_full`; retaining the dirty
state in this note avoids implying a perfectly clean checkout. The two
measurements were collected on different Thunder hosts/dates, so this is a
matched-instrument comparison, not a same-session hardware-speed comparison.

The smaller Muse IQ3_XXS and IQ3_M base runs are queued separately. They are
not included above until their artifacts are complete and validated.
