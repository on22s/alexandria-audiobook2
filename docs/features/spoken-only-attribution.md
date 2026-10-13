# Experimental spoken-only attribution

Issue #1171 reports a model assigning a quote's speaker to its following narration tag. The `michel2_spoken` attribution preset asks only for the marked spoken indexes. Narration remains visible as evidence but is not an output target. Code fills those rows with NARRATOR and validates the reconstructed full batch through the existing source and speaker gates.

Select the experimental preset in Setup's attribution presets, or pass `--prompt-variant michel2_spoken` to the three-pass CLI. The default remains `michel2_full`. Saved custom prompts are separate presets.

Every spoken index is required exactly once. Indexes may have gaps; duplicate, missing, narration and out-of-range indexes are rejected. Original text is supplied by the frozen batch. Truncation retries, task limits and provider retry policy stay active.

Repeated paired PDNC evaluation is required before making this the default. Unit tests verify binding and failure handling, not model accuracy. Gold-span evaluation measures attribution, not segmentation.

The request schema fixes the count to the spoken targets and limits `n` to their sparse indexes, using the shared helper from #1178. Validators still enforce unique complete indexes. If a provider ignores the schema, extra narration rows labelled exactly `NARRATOR` are ignored; character-labelled narration, duplicates and out-of-window indexes are rejected. This implementation requires #1178 on main before merging.
