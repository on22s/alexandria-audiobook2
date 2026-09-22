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
