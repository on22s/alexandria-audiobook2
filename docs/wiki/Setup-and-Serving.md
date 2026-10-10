# Setup and serving

The app talks to a language model through any OpenAI-compatible endpoint, on your own machine
or on a remote one. The README's [first-launch section](../../README.md#first-launch-what-to-expect)
shows how to start llama.cpp, LM Studio, Ollama or a hosted API. This page covers the serving
settings that were measured to matter.

## General rules

- **Use reasoning low, not off, and do not ban `<think>`.** Reasoning low with a server-side
  budget beat reasoning off on every model that reasons.
- **Keep the JSON schema on.** Every request carries one.
- **Choose context for the request and available memory**, rather than copying a maximum.
  See the practical sizing steps below.

## Choosing a context size

**8,192 is a local starting point; 32,768 is not a universal requirement.** Context must
hold the entire prompt (instructions, cast roster and surrounding text), the generated
response and any reasoning tokens. Larger contexts also consume more KV-cache memory.

1. Start with a short text and one server slot (`--parallel 1`). In llama.cpp, try
   `-c 8192` to limit memory use for that first run, then check the request budget.
2. In Script, run **Test this book with the LLM** before generating the full script. It
   samples the beginning, middle and a dialogue-dense chunk; passing is useful evidence,
   not a guarantee for every later request.
3. If a request exceeds context, inspect the server error and token counts in
   `logs/review_responses.log`. Reduce **Step 1: text per request**, **Step 2: lines per
   request**, surrounding text or review window as appropriate. Leave room for the output:
   segmentation re-emits the text, so input length alone understates its needs.
4. Increase context only if the model supports it and it fits available VRAM. `-c 32768`
   is one possible larger setting, not an automatic fix. A smaller quantization or a
   remote server may be needed if the model and cache cannot fit together.

**What LM Studio optimization actually does.** In the current implementation, the local
fallback is 8,192 tokens and one slot. Known model profiles can select 16,384 or 32,768
(and sometimes two slots), subject to a live VRAM guard with a 2 GiB reserve. Unknown
models or unreadable metrics retain the fallback. The remote optimizer has a separate
98,304-token/two-slot target; do not assume that target suits an arbitrary remote machine.
Review its resources and settings before enabling remote optimization.

Sources: [load profiles and guards](../../app/lmstudio_settings.py) and
[book preflight and request budgeting](../../app/three_pass_generate.py). These describe
current code, not a fresh hardware benchmark. The earlier model-card guidance reports a
5,966-token attribution window, but no linked measurement establishes that as a maximum
for every book, tokenizer, pipeline pass or completion budget. Treat the
[model-card memory figures](Which-Model-For-Your-Card.md) as measurements for their stated
configuration, not a guarantee that all requests fit 8k.

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
