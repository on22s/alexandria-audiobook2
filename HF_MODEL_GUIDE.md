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
| `Om22s/alexandria-qwen3-14b-rightsclean-speaker-attribution-attrv1` | one adapter, byte-identical to `adapters/qwen3-14b-rightsclean-attrv1` | superseded (`new_version` → collection) |
| `Om22s/alexandria-qwen3-14b-speaker-attribution-attrv1` | the earlier experimental r8 adapter | superseded (`new_version` → collection) |

Everything under `Om22s/alexandria-attribution-adapters` and the
`*-evaluation-archive` / `*-arms` repos is **private and stays private**: the
voice LoRAs are trained on audiobook narrators, and the Muse/Qwen3.8/Gemma
archives hold adapters trained on the mixed light-novel sets. Only adapters
trained on PDNC + RiQuA + DraCor prose go public — that rule, not the base
model, decides; Muse-Glimmer-30B itself is Apache-2.0.

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

## Cards owed as cells land (2026-09-19)

Uploaded unscored or partly scored, each card says so in bold; when the
cell lands, put the number on the adapter's card **and** in the collection's
list and `model-index`, then tag `v1.1.0`:

| adapter | waiting on | where it runs |
|---|---|---|
| `qwen3.6-35b-a3b-rightsclean-michel2v1` | quant ladder (IQ1_M off/on, IQ2_XXS, IQ3_XXS, Q4_K_XL), four-book + Emma; nine-book on IQ3_XXS | tnr-0 `a3b_adapter_ladder_tnr0_20260917c.sh`; tnr-2 `pdnc9_tnr2_20260919b.sh` |
| `qwen3.8-27b-rightsclean-michel2v1` | Q4_K_M reasoning on, Q3_K_XL, IQ2_XXS; Emma; nine-book on Q4_K_M | tnr-4 `qwen38_adapter_tnr4_20260917d.sh` → `qwen38_pdnc2_tnr4_20260918.sh` → `pdnc9_tnr4_20260919c.sh` |
| `qwen3-14b-rightsclean-attrv1` (+ `-seed2-attrv2`) | nine-book paired, default and michel2_full | tnr-2 `pdnc9_tnr2_20260919b.sh` |
| Muse-Glimmer-30B rights-clean, gen 3 (**not uploaded**) | training now: rejection-sampled traces then the loss-fixed trainer; upload only if it serves in the JSON contract and scores paired against the base. The only rights-clean Muse adapter so far (`rightsclean-lossfix`) failed the contract (4.6%, 555 of 606 unanswered) and is not release material. Base is Apache-2.0, so licence is not the obstacle. | tnr-1 `muse_gen3_rightsclean_tnr1_20260917b.sh` |

Nine-book aggregates go on the cards as **eight held-out novels**, with The
Sun Also Rises reported separately as a training book.

**What the adapters are for (2026-09-19).** They were built to lift the
base models; the `michel2_full` prompt turned out to be most of that lift.
Their job now is to find how small a quant can ship: for each base, the
smallest rung at which base+adapter under `michel2_full` holds the goal-4.2
band, and whether the adapter buys a rung the base cannot hold alone. A card
therefore leads with "smallest quant within band" and shows every rung
against the same model's Q4 `michel2_full` base.

**Release criterion: does it help at the quant it ships on.** The Qwen3-14B
adapters ship at Q4_K_M, so their four-book Q4 row is the headline. The A3B
and Muse adapters exist to close the low-quant gap (goal 4.2): A3B ships at
IQ2_XXS/IQ3_XXS on a 16 GB card, Muse at UD-Q3_K_XL. For those, the card
carries **one row per rung**, and an adapter that is flat at Q4_K_XL but
positive at IQ2_XXS is a release with a positive headline — the IQ2 row —
not a negative with a footnote. "Negative" means negative at every quant it
would ship on, measured paired at that quant.

A negative row still goes on the card, with "served correctly" stated first
(adapter loaded, scale toggled, rows answered) — a card that only carries the
wins is the kind of card the Hub page warns about — but it never decides the
release on its own if a shipping-quant row is positive.

## Adapter names carry their training prompt (2026-09-27)

Every released adapter is named **model-training-prompt**: the existing name plus the prompt it
was trained on (`michel2` in a name becomes `michel2v1`/`michel2v2`; otherwise the label is appended).
An adapter measured under a different prompt is a different measurement, and this was invisible
before: the window25/KL adapters were served off-prompt on tnr-0/tnr-4, the 2026-09-17 michel2
adapters off-prompt everywhere else, and the Qwen3-14B `rightsclean` seed pair trained on two prompts.

| label | text | sha256 |
|---|---|---|
| `attrv1` | `app/default_prompts_attribute.txt` at `b02d5950` | file `e5fd22ad…` |
| `attrv2` | the same file at `5ec2ae07` (#565) | file `f39ea0eb…` |
| `michel2v1` | `MICHEL2_SYSTEM` before #619/#623, 1,825 chars | system text `8447565f…` |
| `michel2v2` | `MICHEL2_SYSTEM` after #619/#623, 2,071 chars | system text `a63e2124…` |

Collection `v1.6.0` (2026-09-28) adds `qwen3.6-35b-a3b-rightsclean-window25-michel2v2`: on the nine
held-out novels +2.7 (IQ1_M), +1.3 (IQ2_XXS, p=0.03), +1.6 (IQ3_XXS), −0.8 (Q4_K_XL, p=0.12) —
the release criterion (positive at IQ2, flat at Q4) met, every rung on its training prompt.
**Rescored 2026-09-28** (PDNC silent short names, RECIPES): +1.5 (IQ1_M, p=0.01), +0.7 (IQ2_XXS,
p=0.28), +0.4 (IQ3_XXS, p=0.50), **−2.1 (Q4_K_XL, p=0.00015)** — the criterion is no longer met;
the card says so.

Label only from a record: the training data's `system` field, or the manifest's clean git commit, or
the chain that set `PYTHONPATH`. Where none exists the adapter stays unlabelled (the Qwen3.8 and Gemma
evaluation archives). Collection `v1.5.0`; the two single-adapter repos were renamed (old URLs redirect);
the private archive keeps its box-mirror paths and maps them in `ADAPTER_NAMES.md`. Artifact tags are
evidence and keep the names they were recorded under.

## Freeing private storage: weights purged, lessons kept (2026-09-27)

The Hub bills every LFS object ever pushed; deleting a file in a commit frees nothing.
`tools/hf_purge_batch.py <spec.json>` is the only sanctioned way to reclaim space. It
refuses before its first write if a purge object is still referenced by another path in
the repo (`list_lfs_files` names objects by their *first* push path) or sits at HEAD of
any other Om22s repo, commits a `LESSONS.md` per folder and reads it back, then purges
exactly the targets. `app/tests/test_hf_purge_guards.py` pins each refusal.

A folder is purged only when its lesson can state the result (artifact names) and how to
redo it. Folders with no recorded result stay.

| batch | what | freed | archive commits |
|---|---|---:|---|
| step 1 | Muse tplfix + lossfix, window25 prompt-mismatch | 2.94 GB | `alexandria-adapters-archive` history, 2026-09-27 |
| A | Gemma `adapters_gold_20260911` duplicates + mixed-multin | 1.47 GB | 〃 |
| B | Gemma 2026-09-09..12 (author-rank, mixed, QAT-base, product-eval) | 6.69 GB | `5d064ce3` lessons, `17e850c9` delete |
| C | Aug–Sep attribution adapters: Qwen3-14B/3.5/3.8, rank and LR ladders, seed controls, reasoning replication (72 folders) | 14.77 GB | `e6f12c98` lessons, `eb8f0b37` delete |

Archive went 56.13 → 30.64 GB. Redo inputs live in the same repo under `redo_inputs/`:
per-box trainers, data, and `recipe_bundle_tier3_20260927.tar.gz` (every script, training
log and manifest on each box, 228 MB).

Two things a lesson's "how to redo" does not yet say, found by testing one (2026-09-27):
- **The training prompt is not in the bundle.** The trainers import
  `app/default_prompts.py`, whose attribute prompt changed on 2026-09-14 (#565). Adapters
  trained before then used the file at `b02d5950` (every box's copy hashes to it); today's
  file builds different examples.
- **Package versions were not recorded.** The August runs used transformers 5.0/5.1 (the
  logs warn that `warmup_ratio` goes in 5.2); the boxes now carry 5.14.1.

**Tested 2026-09-28:** a 60-step retrain of `qwen35_9b_bf16_author_heldout_balanced` from its
lesson, the bundle and the `b02d5950` prompt tracks the original loss within 0.0018 on average;
with today's prompt it departs 2.6× further (0.0047). Not exact — gradient norms differ in both
arms, most likely package drift. Full table: `redo_inputs/README.md` in the archive.

Kept on purpose: anything published elsewhere (the guard found three), folders with no
recorded result, and anything backing a live adapter.
