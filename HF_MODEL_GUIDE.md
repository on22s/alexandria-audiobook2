# Publishing adapters on the Hugging Face Hub

How this project's public model repos are laid out and documented, so the
next upload matches. Written 2026-09-19 after bringing the three public
attribution repos in line with the Hub's
[model-cards](https://huggingface.co/docs/hub/model-cards) page and the
[sharing best-practices thread](https://discuss.huggingface.co/t/best-practices-for-sharing-and-documenting-models-on-the-hugging-face-hub/174526/2).

Public repos as of that date:

| repo | what | state |
|---|---|---|
| `Om22s/alexandria-qwen3-attribution` | the collection: six attribution adapters under `adapters/<name>/` | current, `v1.0.0` |
| `Om22s/alexandria-qwen3-14b-rightsclean-speaker-attribution` | one adapter, byte-identical to `adapters/qwen3-14b-rightsclean` | superseded (`new_version` → collection) |
| `Om22s/alexandria-qwen3-14b-speaker-attribution` | the earlier experimental r8 adapter | superseded (`new_version` → collection) |

Everything under `Om22s/alexandria-attribution-adapters` and the
`*-evaluation-archive` / `*-arms` repos is **private and stays private**: the
voice LoRAs are trained on audiobook narrators and are not rights-clean. Only
adapters trained on PDNC + RiQuA + DraCor prose go public.

## What goes in the repo

```
README.md                      the model card (frontmatter + text)
LICENSE                        Apache-2.0 text - weights are ours to license
default_prompts_attribute.txt  exact copy of the prompt the scores used
examples/infer.py              transformers+PEFT, loads the prompt file, checks the contract
examples/serve_llamacpp.sh     the llama-server line the scores were produced with
adapters/<name>/
  adapter_config.json          PEFT config (target modules, r, alpha)
  adapter_model.safetensors    the weights - safetensors, never pickle
  <name>.f16.gguf              the same weights for `llama-server --lora`
  tokenizer_config.json
  training_manifest.json       recipe, seed, data hashes, trainer commit
  README.md                    per-adapter card (frontmatter with its own base_model)
```

Not included, ever: training rows, row-level evaluation files (they contain
source text), `__pycache__` (py_compile before uploading leaves one — delete
it, it happened once), checkpoints.

## Frontmatter, and why each field

```yaml
license: apache-2.0            # a valid identifier; `other` needs license_name + license_link
language:
- en                           # ISO list form, not a bare string
library_name: peft             # explicit; the Hub no longer guesses transformers from config.json
pipeline_tag: text-generation
base_model:
- Qwen/Qwen3-14B               # Hub IDs, never local paths (the trainer writes /home/ubuntu/...)
- Qwen/Qwen3.6-35B-A3B
- Qwen/Qwen3.8-27B
base_model_relation: adapter   # REQUIRED with a list: a list alone means "merge"
tags: [lora, peft, speaker-attribution, audiobook, qwen3, rights-reviewed]
new_version: Om22s/...         # on a superseded repo only; the Hub follows the chain to the newest
model-index:                   # structured eval results; rendered in the results widget
- name: <repo or adapter>
  results:
  - task: {type: text-generation, name: speaker attribution (structured JSON, roster given)}
    dataset: {name: <fixture, serving condition>, type: alexandria-attribution-fixture}
    metrics:
    - {type: accuracy, name: "accuracy, adapter (base 61.7)", value: 73.4, verified: false}
    source: {name: <how it was served>, url: <RECIPES.md>}
```

Deliberately absent: `datasets` (must be Hub dataset IDs; PDNC/RiQuA/DraCor
are not used from the Hub — name them in the text instead),
`co2_eq_emissions` (nothing was tracked; do not invent it), `widget`
(adapters cannot run on the Hub without the base).

## Card sections, in order

1. **Title** naming base + task, one paragraph on what it is and is not
   (adapter only, base not included).
2. **Adapter list** with one measured number each, or **"not yet scored"**
   in bold — never a placeholder number. A card is updated when the cell
   lands, not written in anticipation of it.
3. **Quickstart** — copy-paste PEFT load with `subfolder=` and
   `revision="v1.0.0"`; pointers to `examples/`.
4. **Intended use / Not for** — the structured attribution prompt and the
   JSON contract to validate; not chat, not other languages, not decisions
   about real people.
5. **Bias, risks and limitations** — English-only, roster-selection bias,
   fixture-specific gains, unscored rows are unknown not small.
6. **Requirements** — per base: weights size in bf16 and the GGUF quant
   tested, context, slots, reasoning setting; library versions the loader
   was *written against* (say "written against", not "tested with", unless
   it was run).
7. **Training data** — sources and their terms in one sentence each, the
   exact held-out split (see below), manifests for hashes.
8. **Versioning** — `main` is current; tags `vX.Y.Z`; superseded adapters
   stay in place with their card marked.
9. **Evaluation** — fixture, rows, serving condition, base vs adapter,
   paired; then failure modes.
10. **Prompt and serving recipe** — the prompt file, and that changing
    prompt/reasoning/batch/schema is a new measurement.
11. **Usage / License / Links** — license high enough to find; repo link.

## Numbers on a card

- Every number is paired base vs adapter on one server with the scale
  toggled; say the serving condition next to it. A number from a different
  condition is a different row, not a footnote.
- **Held out means held out of *this* adapter's training.** The rights-clean
  set is 20 PDNC novels *including The Sun Also Rises*; the held-out eight
  are Emma, Pride and Prejudice, Mansfield Park, Sense and Sensibility,
  Northanger Abbey, Persuasion, The Awakening, The Sign of the Four. A
  "two held-out novels" row that was half training data went on a card for
  an hour on 2026-09-19 before the training-data section caught it. List
  the training novels before writing "held out".
- Seeds: report both when two exist; disagreement (+6.9 vs +0.6 on Emma)
  is the finding, not something to average away.

## Release mechanics

```python
from huggingface_hub import HfApi
api = HfApi()
api.upload_folder(folder_path=local, path_in_repo=f"adapters/{name}", repo_id=rid, commit_message=...)
api.upload_file(path_or_fileobj=readme, path_in_repo="README.md", repo_id=rid, commit_message=...)
api.create_tag(rid, tag="v1.0.0", tag_message=...)       # after the last card edit, not before
api.model_info(rid).card_data.eval_results                # confirm the Hub parsed model-index
```

Before tagging: `yaml.safe_load` the frontmatter, confirm `base_model` IDs
exist on the Hub (`api.model_info(id)`), sha-compare weights against any
repo you are calling identical (`model_info(rid, files_metadata=True)` →
`siblings[].lfs.sha256`), and list the repo to check nothing stray went up.
A tag that was created minutes ago can be moved (`delete_tag` +
`create_tag`); one that has been public for a day should not.
