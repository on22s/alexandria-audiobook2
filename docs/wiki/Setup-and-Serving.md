# Setup and serving

The application uses a local or remote OpenAI-compatible LLM endpoint. Muse
requires the DeepSeek reasoning format when reasoning is enabled.

## Muse baseline

```bash
llama-server \
  -m Muse-Glimmer-30B-UD-Q3_K_XL.gguf \
  --reasoning on \
  --reasoning-format deepseek \
  --chat-template-kwargs '{"reasoning_strength":"low"}' \
  --parallel 1
```

Use request-level JSON schema and do not use `--skip-chat-parsing` for Muse
reasoning evaluations.

Before a full adapter evaluation, run the served-contract preflight. It must
parse one `michel2_full` request at adapter scale 1.0 and base scale 0.0.
