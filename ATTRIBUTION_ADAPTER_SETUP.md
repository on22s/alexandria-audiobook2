# Turning on an attribution adapter

The adapters exist for one reason now: **to let a smaller quant ship.** The
prompt (`michel2_full`) did the lifting on the base models; an adapter is
worth loading only at the rung where it is measured to help. All rows are the
four-book product fixture (768 rows, paired base → adapter on one server with
the adapter scale toggled, temperature 0, JSON schema, `michel2_full`), from
the public collection
[Om22s/alexandria-qwen3-attribution](https://huggingface.co/Om22s/alexandria-qwen3-attribution)
(RECIPES §"Attribution adapters", 2026-09-20):

| base | rung (file) | reasoning | base → adapter | load it? |
| --- | --- | --- | ---: | --- |
| Qwen3.8-27B | IQ2_XXS (7.3 GB) | on | 83.1 → **85.7 (+2.6)** | **yes** — the rung this adapter is for |
| Qwen3.8-27B | Q3_K_XL (13.1 GB) | on | 87.5 → **88.8 (+1.3)** | yes |
| Qwen3.8-27B | Q4_K_M (16.5 GB) | on / off | 89.8 → 89.8 (0) / 87.4 → 88.3 (+0.9) | no with reasoning; marginal without |
| Qwen3.6-35B-A3B | IQ1_M (10.0 GB) | off | 82.9 → **85.9 (+3.0)** | **yes, reasoning off only** |
| Qwen3.6-35B-A3B | IQ1_M | on | 87.5 → 85.0 (−2.5) | no |
| Qwen3.6-35B-A3B | IQ2_XXS (10.8 GB) | on | 88.3 → 87.9 (−0.4) | no |
| Qwen3.6-35B-A3B | IQ3_XXS (13.2 GB) | on | 87.5 → 86.6 (−0.9) | no |
| Qwen3.6-35B-A3B | Q4_K_XL (22.4 GB) | on | Q4KXL_ROW | Q4KXL_VERDICT |
| Qwen3-14B | Q4_K_M (9.0 GB) | low, budget 1024 | 66.1 → **74.7 / 74.9 (+8.6 / +8.8)** under `default`; **84.3 → 84.3 (0)** under `michel2_full` on nine PDNC novels | only with the `default` prompt; under `michel2_full` use the base |

Two things the table does not say on its own. The A3B adapter on PDNC text
(rosters with aliases) copies the roster line back — `EMMA (also: EMMA
WOODHOUSE, …)` — on up to 12% of rows; the app reads that as `EMMA` since
#625, but the harness scores it wrong, so the adapter's own cards carry both
numbers. And the Qwen3-14B gain is prompt-specific: it was trained on the
`default` shape and is a null under the product prompt.

## It is a serving change, not an application change

The adapter is applied by `llama-server`. Nothing in `app/` computes with it.

```bash
llama-server \
  -m /path/to/Qwen3.8-27B-UD-IQ2_XXS.gguf \
  --lora /path/to/qwen38-rightsclean-michel2.f16.gguf \
  --host 127.0.0.1 --port 8090 -c 32768 --parallel 1
```

The `.f16.gguf` under each `adapters/<name>/` directory of the collection is
the file that was measured (`examples/serve_llamacpp.sh` there does the
above); the light-novel-trained `adapter_mixed.gguf` that earlier versions of
this page described is not distributable and stays in the private archive.

Then point the app at it, in `app/config.json`, inside the block for the mode
you are running:

```json
"llm_local": {
  "base_url": "http://127.0.0.1:8090/v1",
  "model_name": "qwen3.8-27b",
  "attribution_adapter": {
    "path": "models/qwen38-rightsclean-michel2.f16.gguf",
    "scale": 1.0,
    "require": false
  }
}
```

The adapter lives in the same block as `base_url` on purpose: it belongs to
that endpoint, and a second location could disagree with it about which server
is meant.

## What the check does

`generate_script.py` prints, at startup:

```
LLM adapter: attribution_adapter=qwen38-rightsclean-michel2.f16.gguf scale=1.0 require=False
```

and warns if the server is not actually serving it. **This is the point of the
whole thing.** Without it, a server started without `--lora`, or with the scale
toggled to zero, answers every request perfectly happily at base quality.
Nothing fails. The book generates. It is simply the base number and no
artifact records why - the same shape as the seed bug, which cost six
contaminated comparisons before somebody noticed by ear.

`require: true` turns the warning into a refusal. The default warns, because a
book half-generated overnight should not die when a server is restarted - but
the run must carry the fact.

## Three honest limits

**The endpoint must be llama.cpp.** LM Studio and Ollama do not implement
`/lora-adapters`, so the check reports "cannot verify" rather than pretending
the adapter is absent. That is deliberate: a warning that fires on every setup
is one nobody reads.

**Scale is not tuned.** 1.0 is what the eval used. `POST /lora-adapters` can
set it anywhere, and nothing here has measured whether 0.7 or 1.3 is better.

**The gain is measured on light novels, plus two PDNC novels.** The four
gold books are Japanese light novels in English translation; the two-book
PDNC cells (Emma, held out; The Sun Also Rises, a training novel) are on the
cards. The training data is English public-domain prose, so the light-novel
gain is transfer, and it is small.
