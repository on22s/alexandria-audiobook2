# Alexandria Audiobook2 Wiki

Alexandria Audiobook2 turns a book into an audiobook with a different voice for every
character. A language model works out who says each line and how it should sound; a
text-to-speech model (Qwen3-TTS) reads it; and you get an MP3 or a chaptered M4B.

This fork measures every choice it makes. The research pages below say what was measured
and how; the user-guide pages say how to use the app.

## Using the app

- [Setup and serving](Setup-and-Serving.md) — connecting a language model, and serving
  settings that were measured to work.
- [Script generation](Script-Generation.md) — the three passes that turn text into a script,
  the review pass, and prompts.
- [Voice types](Voice-Types.md) — CustomVoice, Clone, LoRA and Voice Design, and when to use
  each.
- [Voice reference](Voice-Reference.md) — a vocabulary for voice descriptions and delivery
  instructions, with test results.
- [Batch generation](Batch-Generation.md) — how rendering is batched, and how to tune it.
- [Editor & export](Editor-&-Export.md) — editing lines, render modes, merging and export.
- [Troubleshooting](Troubleshooting.md) — common problems and fixes.

## Building voices

- [Dataset builder](Dataset-Builder.md) — build a voice-training dataset with a preview of
  every sample.
- [Training guide](Training-Guide.md) — train a LoRA voice, with settings that worked.

## Reference

- [API reference](API-Reference.md) — HTTP examples in curl, Python and JavaScript.

## Research

- [Which model for your card](Which-Model-For-Your-Card.md) — the best language model for
  each amount of GPU memory, with speeds.
- [Results](Results.md) — model, quant, prompt and adapter results in one place.
- [Prompts and adapters](Prompts-and-Adapters.md) — the rules adapters must follow, and when
  an adapter helps.
- [Evaluation recipes](Evaluation-Recipes.md) — how an adapter evaluation is run.
- [Thunder operations](Thunder-Operations.md) — running jobs on remote GPUs.
- [Hugging Face releases](Hugging-Face-Releases.md) — what a public release must include.

## The one rule for adapters

An adapter must be evaluated with the prompt it was trained on. A result under a different
prompt is a transfer test, not a result for that adapter.

## Checking an installation

The repository includes a live API test suite. The safe way to run it starts a throwaway copy of
the app with its own empty data folder, so your books and voices are never touched:

```bash
cd app
python run_isolated_api_tests.py          # quick tests — no TTS or LLM needed
python run_isolated_api_tests.py --full   # also tests generation — needs a GPU and an LLM
```

To test an app that is already running, use `python -m tests.test_api --url http://127.0.0.1:<port>`
from the `app` folder — but note that it creates and deletes uploads, scripts and voice settings
in that app's data. (Running `python tests/test_api.py` directly fails with
`ModuleNotFoundError: utils`; it must be run with `-m` from `app`.)

- **Quick mode** (82 checks; 70 run, 12 need a GPU and an LLM) covers settings round-trips,
  upload, the script library, voice settings, chunks, status polling, voice design, LoRA
  listings, the dataset builder and error handling, without loading any model.
- **Full mode** adds script generation, audio generation, batch rendering, voice design previews
  and LoRA testing.

If quick mode passes but generation fails, the problem is loading the TTS model or reaching
the language model, not the app itself.
