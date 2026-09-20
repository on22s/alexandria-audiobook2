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
