# Evaluation recipes

Use the in-repository evaluator with one server and paired adapter scales:

- base arm: adapter scale 0.0;
- LoRA arm: adapter scale 1.0;
- temperature 0;
- batch size 25;
- request-level JSON schema;
- the same prompt variant for both arms.

Report accuracy, unanswered rows, parser failures, per-book scores, adapter
hash, base hash, prompt variant, and runtime details.

Do not promote an adapter from a batch-1 smoke test. A valid promotion result
needs the product-window evaluation and a clean served-contract preflight.
