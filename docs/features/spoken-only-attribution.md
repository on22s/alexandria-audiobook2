# Experimental spoken-only attribution

Issue #1171 reports a model assigning a quote's speaker to its following narration tag. The `michel2_spoken` attribution preset asks only for the marked spoken indexes. Narration remains visible as evidence but is not an output target. Code fills those rows with NARRATOR and validates the reconstructed full batch through the existing source and speaker gates.

Select the experimental preset in Setup's attribution presets, or pass `--prompt-variant michel2_spoken` to the three-pass CLI. The default remains `michel2_full`. Saved custom prompts are separate presets.

Every spoken index is required exactly once. Indexes may have gaps; duplicate, missing, narration and out-of-range indexes are rejected. Original text is supplied by the frozen batch. Truncation retries, task limits and provider retry policy stay active.

## Measured (2026-10-13)

On Qwen3-14B without an adapter, at the product's settings (temperature 0, default windows, the full three-pass pipeline, #1178 and #1179 on both arms), a local gold set of three books (679 scorable rows, not in the repo) scored **70.1%** with `michel2_spoken` against **63.6%** with `michel2_full`. Spoken-only won or tied on every book; on the rows the arms disagreed, it was right 109 times to 65 (sign test p = 0.001). Step 2 took 48 minutes of model time instead of 2 h 16 min, with 540 calls instead of 2,044. A small PDNC pilot pointed the same way (51.9% vs 40.3%). Details: #1171, #1176.

**Scope.** That is one base model. `michel2_full` became the default because it won on every base measured in September, and attribution adapters were trained on it: if you serve an adapter, keep the prompt it was trained on. The default stays `michel2_full` until a second base has been checked.

Unit tests verify binding and failure handling, not model accuracy.

The request schema fixes the count to the spoken targets and limits `n` to their sparse indexes, using the shared helper from #1178. Validators still enforce unique complete indexes. If a provider ignores the schema, extra narration rows labelled exactly `NARRATOR` are ignored; character-labelled narration, duplicates and out-of-window indexes are rejected. This implementation requires #1178 on main before merging.
