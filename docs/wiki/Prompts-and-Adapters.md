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

The Gen 3 Muse recipe adds rejection-sampled reasoning traces, preserves the
loss-window fix, mixes direct-answer examples, runs a smoke training step, and
requires a served-contract preflight before full evaluation.

The first held-out eight-novel scale pilot (631 shared rows, 2026-09-22) found
that Gen 3 is scale-sensitive: 0.25 improved 92.9% to 96.0%, 0.5 was a null,
and 1.0 regressed to 89.4%. Treat 0.25 as the replication candidate, not a
shipping recommendation, until it passes the full 40-window evaluation.

The same campaign found that the Qwen3.8 Q4 `michel2` adapter with reasoning
off improved 94.9% to 97.1% on 2,305 held-out shared rows. In contrast, the
A3B IQ1_M reasoning-off adapter regressed 91.4% to 88.9%; its earlier
four-book gain did not generalise.
