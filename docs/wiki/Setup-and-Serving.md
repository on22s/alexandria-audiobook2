# Setup and serving

The app talks to a language model through any OpenAI-compatible endpoint, on your own machine
or on a remote one. The README's [first-launch section](../../README.md#first-launch-what-to-expect)
shows how to start llama.cpp, LM Studio, Ollama or a hosted API. This page covers the serving
settings that were measured to matter.

## General rules

- **Use reasoning low, not off, and do not ban `<think>`.** Reasoning low with a server-side
  budget beat reasoning off on every model that reasons.
- **Keep the JSON schema on.** Every request carries one.
- **Choose the context length deliberately.** The longest prompt window is 5,966 tokens, so
  `-c 8192` is enough, and it saves several gigabytes compared with `-c 32768`. See
  [Which model for your card](Which-Model-For-Your-Card.md).

## Serving Muse-Glimmer-30B

Muse needs the DeepSeek reasoning format when reasoning is on:

```bash
llama-server \
  -m Muse-Glimmer-30B-UD-Q3_K_XL.gguf \
  --reasoning on \
  --reasoning-format deepseek \
  --chat-template-kwargs '{"reasoning_strength":"low"}' \
  --parallel 1
```

- Send the JSON schema with each request.
- **Never use `--skip-chat-parsing`** when evaluating Muse with reasoning.

## Before a full adapter evaluation

Run the served-contract preflight first. It must successfully parse one `michel2_full`
request at the adapter's candidate scale **and** at base scale 0.0.

Record the candidate scale in the artifact. Do not assume 1.0 when a measured scale sweep
chose a lower value.
