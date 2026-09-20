# Alexandria Audiobook Wiki

Alexandria Audiobook is a FastAPI application for multi-voice audiobook
generation and attribution experiments.

## Start here

- [Setup and serving](Setup-and-Serving.md)
- [Prompts and adapters](Prompts-and-Adapters.md)
- [Evaluation recipes](Evaluation-Recipes.md)
- [Thunder operations](Thunder-Operations.md)
- [Hugging Face releases](Hugging-Face-Releases.md)

## Core rule

An adapter must be evaluated with the prompt shape it was trained on. A result
under a different prompt is a transfer test, not a matched adapter result.
