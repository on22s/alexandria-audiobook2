# Evaluation recipes

How an adapter evaluation is run. The measured settings and their evidence are in
[RECIPES.md](../../RECIPES.md).

## The paired setup

Use the evaluator in the repository, with **one server** and two adapter scales:

- **base arm:** adapter scale 0.0;
- **adapter arm:** the adapter's declared scale — 1.0, unless a recorded scale sweep chose
  another value.

Both arms use:

- temperature 0;
- batch size 25;
- a JSON schema on each request;
- the same prompt variant.

## What to report

- accuracy, and the number of unanswered lines and parser failures;
- the score for each book;
- the adapter hash and the base-model hash;
- the prompt variant;
- runtime details (server build, GPU, context length).

## When an adapter may be promoted

- **Never from a batch-size-1 smoke test.** Promotion needs the product-window evaluation and
  a clean served-contract preflight (see [Setup and serving](Setup-and-Serving.md)).
- **If a pilot chooses a scale other than 1.0**, repeat that exact scale over the full test set
  and report every scale that was tried. Never relabel a scale-1 failure as a success.
- **Quote the test set every time.** The same adapter can gain on one test set and not on
  another (see [Prompts and adapters](Prompts-and-Adapters.md)).
- **Check the scorer as well as the model.** The 2026-09-28 rescore changed ten verdicts
  because correct short names were being scored wrong.

## Paid API baselines

Keep hosted-API results, which are base-only, separate from adapter evaluations.

The Nemotron 3 Ultra 550B A55B runs of 2026-09-22 went through OpenRouter with
`michel2_full`, low reasoning, temperature 0, batch size 8, and no structured output. Per-book
scores:

| book | accuracy |
|---|---:|
| Mansfield Park | 96.7% |
| Northanger Abbey | 97.6% |
| Persuasion | 97.1% |
| The Sign of the Four | 85.3% (96.6% after the 2026-09-28 rescore) |
| The Sun Also Rises | 87.3% |

- These are independent books: do not pool them into one model score, and do not read them
  as adapter effects.
- OpenRouter reported the requested model. Routing preferred Baseten, then Venice, with
  fallbacks; the provider's hardware was not observed.
- Denominators and artifacts:
  [RECIPES.md](../../RECIPES.md#september-22-follow-up-low-quant-adapter-and-nemotron-api-baselines).
