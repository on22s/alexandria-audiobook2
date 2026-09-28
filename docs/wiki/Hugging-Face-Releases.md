# Hugging Face releases

The public adapters are in
[Om22s/alexandria-qwen3-attribution](https://huggingface.co/Om22s/alexandria-qwen3-attribution).
The full release checklist is [HF_MODEL_GUIDE.md](../../HF_MODEL_GUIDE.md).

## What may be published

Only files whose redistribution rights have been verified.

## What every public release must include

- the hash of the exact file that was tested;
- the base model and its licence;
- the training data sources and their rights;
- the prompt it was trained on, and the serving command;
- complete evaluation results, including the limitations.

## Adapter names

Every adapter name ends with the prompt it was trained on, because an adapter only works
under that prompt:

| label | prompt |
|---|---|
| `attrv1` | the attribution prompt before 2026-09-14 |
| `attrv2` | the attribution prompt from 2026-09-14 |
| `michel2v1` | `MICHEL2_SYSTEM`, 1,825 characters (before #619/#623) |
| `michel2v2` | `MICHEL2_SYSTEM`, 2,071 characters (after #619/#623) |

The prompt texts themselves are published in the collection's `prompts/` folder.

## Negative results

- A negative evaluation can be published without uploading restricted adapter weights or
  source text.
- Failed adapters, or adapters with a setup defect, must be labelled clearly and must never
  be promoted as quality models.
- When a published result is later corrected, the card says so and says what changed — for
  example the 2026-09-28 rescore, released as collection v1.6.1.
