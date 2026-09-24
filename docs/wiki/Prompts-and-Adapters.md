# Prompts and adapters

Available attribution prompt variants include `default`, `michel`, `michel2`,
`michel2_full`, and `michel2_shot`.

`michel2_full` is the current product prompt for the strongest measured base
results. It includes the marked passage and surrounding evidence text.

Adapters are prompt-specific:

- adapters trained on `default` should be served with `default`;
- adapters trained on `michel2`/`michel2_full` should be served with the
  matching variant;
- changing the prompt turns the run into a transfer test.

Train an adapter on the window shape the product actually serves. This is the
single largest source of wasted adapter runs in this project: a training set can
carry the right prompt *variant name* and still present a different task. The
Gen 3 Muse `michel2_full` set passed its own `michel2_full` preflight while
building one entry per row, where production sends 25 segmented entries with
2,000 characters of surround; it was blocked before training, and 61 hours of
sampling could not be filtered into shape. Check the window, not the label.

The Gen 3 Muse recipe itself — rejection-sampled reasoning traces, loss-window
fix, mixed direct-answer examples, smoke step, served-contract preflight — is
**not** the recommended recipe on current evidence. It measured −2.0 on the
four-book fixture. The only Muse adapter that has won is the earlier
`task4k-multin` template-fixed run (+4.4), which was multi-entry and
answer-only, trained before the reasoning channel existed. Prefer the
multi-entry shape; treat the reasoning channel as an open question rather than
a fix.

The smoke training step and the served-contract preflight are worth keeping
from Gen 3 regardless of recipe.

The first held-out eight-novel scale pilot (631 shared rows, 2026-09-22) found
that Gen 3 is scale-sensitive: 0.25 improved 92.9% to 96.0%, 0.5 was a null,
and 1.0 regressed to 89.4%. Treat 0.25 as the replication candidate, not a
shipping recommendation, until it passes the full 40-window evaluation.

The same campaign found that the Qwen3.8 Q4 `michel2` adapter with reasoning
off improved 94.9% to 97.1% on 2,305 held-out shared rows. In contrast, the
A3B IQ1_M reasoning-off adapter regressed 91.4% to 88.9%; its earlier
four-book gain did not generalise.

A separate eight-book PDNC pilot measured the Qwen3.8 IQ2_XXS rights-clean
adapter at 89.6% base versus 91.1% with the adapter (+1.5 points across
2,310 rows; 2,292 strict shared rows, paired p=0.0486). This is pilot-scale
evidence, not the nine-book replication or a release-wide claim. See
[`RECIPES.md`](../../RECIPES.md#september-22-follow-up-low-quant-adapter-and-nemotron-api-baselines)
for the paired details and artifact.
