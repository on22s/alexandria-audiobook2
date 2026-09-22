# Evaluation recipes

Use the in-repository evaluator with one server and paired adapter scales:

- base arm: adapter scale 0.0;
- LoRA arm: the adapter's declared candidate scale (1.0 unless a recorded
  scale sweep selected another value);
- temperature 0;
- batch size 25;
- request-level JSON schema;
- the same prompt variant for both arms.

Report accuracy, unanswered rows, parser failures, per-book scores, adapter
hash, base hash, prompt variant, and runtime details.

Do not promote an adapter from a batch-1 smoke test. A valid promotion result
needs the product-window evaluation and a clean served-contract preflight.
If a pilot selects a non-default scale, replicate that exact scale over the
full panel and report every tested scale; do not silently relabel a scale-1
failure as an adapter success.

## Paid API baselines

Keep API base-only results separate from adapter evaluations. The September
22 Nemotron 3 Ultra 550B A55B OpenRouter runs used `michel2_full`, low
reasoning, temperature 0, batch 8, and no structured output. Per-book scores
were Mansfield Park 96.7%, Northanger Abbey 97.6%, Persuasion 97.1%, The Sign
of the Four 85.3%, and The Sun Also Rises 87.3%. Do not pool these independent
fixtures into one model score or infer adapter effects. OpenRouter reported
the requested model; routing preferred Baseten then Venice with fallbacks,
and the provider hardware was not observed. The repository's
[`RECIPES.md`](../../RECIPES.md#september-22-follow-up-low-quant-adapter-and-nemotron-api-baselines)
has denominators and the artifacts.
