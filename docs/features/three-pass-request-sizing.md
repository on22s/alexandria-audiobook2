# Three-pass request sizing

Step 1's source target and Step 2's extra context allow up to 1,000,000 characters. Defaults remain 3,000 and 2,000 respectively. This allowance does not enlarge the loaded model context or promise that a million-character request fits.

Step 2 and Step 3 have independent source-character targets. Zero preserves existing line-based behavior (Step 2 uses its saved line count; Step 3 uses 25). Positive values opt in to character batching. Targets count the source entries being processed, excluding surrounding context and prompt wrappers. Whole entries retain their source indices; repeated text is distributed across duplicate-free calls. A single entry may exceed the soft character target.

The existing shared conservative token estimate budgets the complete prompt, reserved response space and configured reasoning allowance. Character-mode batches subdivide when they do not fit; a singleton that still cannot fit is refused before model dispatch. Reduce extra context, reduce a custom prompt or select a capable endpoint. Large Step 1 targets use the existing source splitter at a smaller effective size when needed. Model contexts are estimates/configured limits, not exact tokenizer measurements.

CLI overrides: `--attribute-target-chars`, `--instruct-target-chars`; zero retains legacy batching. Configuration keys: `generation.three_pass_attribute_target_chars` and `generation.three_pass_instruct_target_chars`.

Changing a positive target invalidates incompatible checkpoints. Explicit zero retains legacy checkpoint identity. Context-dependent sizing reports are invalidated when context changes. Runtime still checks actual serving parameters; roster growth, fallback profiles and retries may require more calls than preliminary estimates. Caches, bounded retries, validation, GPU locks and local concurrency policy remain in place.

Validation uses synthetic fixtures and a fake SDK transport through the actual pipeline. It establishes batching, source preservation, budget refusal and resume behavior, not real-book attribution quality or a universal speedup. Live comparisons remain required before recommending larger targets.
