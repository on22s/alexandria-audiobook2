# Recipes that worked, and the ones that looked like they did

Every row here is a setting that produced a result the evidence tree still
stands on, next to the setting that produced a failure and *looked the same
from the training report*. The point of the file is the second column: a
`training_meta.json` records what a run used, not whether the run was any
good, and on 2026-09-13 a chain copied its recipe from the metadata of the one
LJSpeech adapter that never learned to stop — because that file looks
identical to the working one's. Nothing in the tree said which was which.

Rules for this file:

- A row goes in only with an evidence artifact that shows the result, not
  because the setting is the default.
- A failed recipe stays in, marked `runs away` / `collapses` / `memorises`,
  with the artifact that showed it. Deleting it is how it gets tried again.
- `app/tests/test_recipes.py` reads the **works** rows: a chain under
  `run_chains/` that trains a voice adapter with an explicit `--lr` must use
  a rate this file lists as working. Add the row before the chain.

## Muse-Glimmer disclosure (2026-09-16)

Today's Muse work does **not** support a single-cause story. We found three
separate layers that must be kept distinct when interpreting a score:

1. **The shipped base configuration is strong.** Muse base with low reasoning,
   the shipped attribution prompt, and request-level JSON schema scored 81.5%
   (626/768, zero blank rows) on the four-book product fixture. This is the
   current measured control; it is not evidence that every adapter or every
   prompt works with Muse.
2. **Reasoning changes the serving contract.** `--skip-chat-parsing` makes the
   non-reasoning JSON path reliable, but it also prevents separation of Muse's
   reasoning channel from its answer. Reasoning-on runs therefore require the
   parser and the correct Muse chat-template headers. The old multi-entry
   diagnostic failed 20/20 with `assistant=user`-style framing and one-object
   responses, while a 20-line smoke on the rebuilt A100 binary returned
   parser-valid output. The failure is consequently workload/build/configuration
   sensitive, not proof that Muse or llama.cpp categorically cannot parse the
   model.
3. **The template-fixed trainer had an independent loss-window bug.** The
   2026-09-11 trainer dropped the first supervised answer token (the leading
   JSON/header token in the reproduced tokenisation). The loss-fixed trainer
   corrects the slice; all adapters trained by the old trainer remain suspect
   until retrained and evaluated under the corrected serving recipe. Qwen and
   Gemma HF `labels=` trainers are not affected by this specific bug.
4. **Michel is not a Muse repair.** Michel and Michel-low both remained around
   35--37% on Muse in matched low/none runs, with no useful adapter separation.
   *(2026-09-17: those runs were setup failures; replicated on one harness Muse
   base + michel + reasoning low is 78.6 vs 81.5 on the default prompt - Michel
   still does not help Muse, but by 3 points, not 45.)*
5. **A smoke test is necessary but insufficient.** A passing 20-line parser
   smoke only establishes that the binary can frame a small response. It does
   not validate multi-entry batching, long outputs, adapter headers, or the
   four-book product score. Promotion requires the full frozen fixture, zero
   contract failures, and a matched base/adapter comparison on the same build.

Until the paused cloud rebuilds complete, the old full-run failures remain
evidence, while the small rebuilt-binary smoke remains a counterexample to an
absolute parser claim. Do not overwrite either artifact or merge them into a
single promotion score.

## Voice adapters (Qwen3-TTS LoRA, `app/train_lora.py`)

| use | recipe | status | evidence | the failure that taught it |
|---|---|---|---|---|
| library voice (one narrator, shipped) | `batch_train_lora.py` defaults: 6 epochs, lr 1e-6, r 64, alpha 128, 180–200 clips | works | 67 of the shipped `lora_models/*/training_meta.json` carry exactly this; goal 2.3 gate passes at 1.01x / 0.87x / 0.94x | see next row |
| eval-set adapter (LJSpeech, Hi-Fi TTS) | `train_lora.py --epochs 6 --lr 1e-6 --lora_r 32 --lora_alpha 128`, 200 clips, seed 1234 | works | `ab_test_runtime/ljspeech_eval/adapter_lr1e6/`, the adapter behind `ljspeech_generate.json` and every 2.9 English number | `ljspeech_eval/adapter/` (lr 5e-6): 163.8 s of audio for every held-out line, loss 2.95 looked normal. Reproduced 2026-09-13 on Hi-Fi 9017 (`hifitts_9017_eval/adapter_lr5e6_runaway/`, loss 3.12, also normal). |
| any voice adapter | lr 5e-6 | **runs away** | `app/tests/test_training_defaults.py`: 2 of 2 at 5e-6 never emitted end-of-speech; 3 of 3 at 1e-6 stopped | training loss cannot see it; only generated audio can — which is why the 2.3 stop gate (`verify_adapter_stops.py`, refuse above 3.0x) belongs in every chain before a generate stage |

## Attribution adapters (LLM LoRA, `app/experiments/distill_train.py` and the per-model cloud trainers)

**Every adapter row below was trained on the DEFAULT prompt shape** unless its
recipe says otherwise: `build_examples` renders each training row through
`load_attribute_prompts()` - the shipped system prompt plus the
`ESTABLISHED ROSTER: ... / ENTRIES: [{"n", "type", "text", previous_context,
next_context}]` user format - so the adapter learns that input shape along
with the task. Serve it under a different prompt and the gain does not carry:
the rights-clean adapter (+11.7 on its own prompt) under `--prompt-variant
michel` with reasoning low: **75.8 → 72.9, −2.9** (+73/−95, p=0.10, four books;
tnr-4 `stack_michel`, 2026-09-17) while the same adapter under its own prompt
on the same box, same day, is **66.1 → 74.7, +8.6** (+127/−61, p=2e-6;
`stack_default`, replicating the cloud +8.6 exactly). Read
every adapter score as "trained on prompt X, served on prompt X"; a
Michel-shape retrain of the same rows is queued on tnr-2
(`michelfmt_rightsclean_tnr2_20260917.sh`), and the michel2-shape adapters for
Qwen3.8-27B (tnr-4) and Qwen3.6-35B-A3B (A100) render their rows through the
product's own `build_variant_request`. Every one of those chains now runs a
**served-contract preflight** before the full training (40 steps → convert →
serve on the target quant → one michel2_full window must parse at adapter
scale 1.0 and 0.0; `adapter_preflight_20260917.py`) - the check that would have
caught the Muse gen-1 and gen-2 defects in ten minutes instead of a night.
The Qwen3.8 preflight **passed** on tnr-4 at 06:32Z 2026-09-18 (a 40-step
adapter served on the Q4_K_M base answered one michel2_full window in contract
at scale 1.0 and at scale 0.0; `qwen38_adapter_tnr4_20260917.log`), and the
full 2,540-step training started on that verdict.
The A3B adapter trains on the A100, not an A6000: Qwen3.6's routed experts are
fused `nn.Parameter`s that bitsandbytes cannot quantise, so a "4-bit" load
still holds ~62 GB of bf16 experts.

| model | recipe | status (product-window verdict where available) | evidence | notes |
|---|---|---|---|---|
| Qwen3-14B | "mixed": 2026-08-03 light-novel + PDNC set, 29 files, 2 epochs, 2048 ctx | works: 60.9 → 68.8, +7.8 (+126/−66, p=1.8e-5) | `lora_serving_eval__qwen3-14b-mixed-local-9070xt-inrepo-batch25-20260912.json` | the only adapter measured at the product window that helps there |
| Qwen3-14B | DraCor plays, English (`lacy`+`am`, 4,000 rows), same mixed recipe (2026-09-13) | **hurts at the product window**: 61.7 → 57.8, −3.9 (+79/−109, p=0.034) | `lora_serving_eval__qwen3-14b-dracor-en-a6000-product-batch25-20260913.json` | gains only on mushoku16 (+7); plays carry no narrative frame, see GOALS 1.2 |
| Qwen3-14B | DraCor plays, mixed 6 languages (4,000 rows), same recipe (2026-09-13) | null: 61.7 → 62.2, +0.5 (+91/−87, p=0.82) | `lora_serving_eval__qwen3-14b-dracor-mixed-a6000-product-batch25-20260913.json` | beats the English arm by 4.4 on the same rows; mushoku16 +12, the other three books down |
| Qwen3-14B | DraCor English plays rendered as prose frames (`dracor_prose.py`, 4,000 rows), same mixed recipe (2026-09-14) | works, small: +2.9 at the product window | `lora_serving_eval__qwen3-14b-dracor-en-prose-a6000-product-batch25-20260914.json` | the frame, not the play, was what the −3.9 English arm lacked |
| Qwen3-14B | RiQuA (`riqua_trainset.py`, 2,180 rows whose annotated speaker is a named span; public-domain novels, annotations published for "use, modification, and experimentation", no licence file), 2 epochs, 2048 ctx, seed 20260914 | works: 61.7 → 67.7, **+6.0** (+110/−64, p=0.0006), up on all four books | `lora_serving_eval__qwen3-14b-riqua-a6000-product-batch25-20260914.json` | best gain per row of any source; the 1,857 pronoun-speaker quotes it rejects cannot be recovered by a dialogue-turn rule (hand-checked 10/30 correct, 2026-09-14) — do not add them. Rights-clean stack (20 PDNC + RiQuA + prose plays): 61.7 → 73.4, **+11.7** (+142/−52, p=8e-11; `lora_serving_eval__qwen3-14b-rightsclean-a6000-product-batch25-20260914.json`) - the best Qwen adapter measured, from publishable sources only. RiQuA 3 epochs +8.0 and the stack with RiQuA ×2 +8.9 are both below it (2026-09-17 section) |
| Gemma4-12B | Sep-10 recipe: max_len 4096, no warmup, single-entry task4k | **batch-1 indication only**: 55.9 vs 50.8 for the Sep-9 recipe on identical data | `lora_serving_eval__gemma4-12b-task4k-*-schema-checked-20260911.json` | product-window result unverified; do not treat this as a release recipe |
| Gemma4-12B | "mixed-multin": task4k multi-entry + longcontext + hardcases + author-balanced, 1 epoch, r 16, 4096 ctx (2026-09-12) | **fails the product output contract**: 70.6 → 4.2, with 712 of 768 LoRA rows unanswered | `lora_serving_eval__gemma4-mixed-r16-seed2-a6000-product-batch25-mt4096-20260912.json` | emits one entry for a 12–25-entry window ("expected 13 entries, got 1"); its +9 batch-1 result does not reveal this |
| Muse-Glimmer-30B | task4k-multin template-fixed, seed 2 (2026-09-11): 1 epoch, max_len 4096, lr 2e-4, r16 α16, bf16 LoRA on the HF model (A100 80 GB; the 30B in bf16 does not fit a 48 GB card), `muse_tplfix_queue_20260911b.sh`; served on llama.cpp `b6b003d2c` with the row below | works at the product window: 72.4 → 76.8, **+4.4** (+112/−78, p=0.016), LoRA arm 768/768 answered | `lora_serving_eval__muse-glimmer-30b-task4k-multin-tplfix-seed2-tnr0-product-batch25-q3-jsonschema-reasoningnone-20260913.json` | first Muse adapter measurable at batch 25; the base arm still leaves 89 windows unanswered (46 PassExhausted), which is Muse reasoning past the schema, not the adapter. Earlier batch-1 numbers (54.2 → 61.6 task4k, → 59.9 mixed) stand as batch-1 signal only |
| Qwen3-14B | hardcases only: 1,271 curated rows (`train_hardcases_split_nonmajor.jsonl`), 2026-09-11 adapter | works at the product window: 61.7 → 66.4, **+4.7** (+104/−68, p=0.007) | `lora_serving_eval__qwen3-14b-hardcases-a6000-product-batch25-20260913.json` | 7% of the "mixed" set's rows for 60% of its +7.8 — a small curated set carries most of the skill (Maekawa et al., LREC 2026), not all of it |
| Qwen3.8-27B | author-held-out balanced PDNC adapter, served on llama.cpp `b6b003d2c`, free-form JSON | **superseded - an instrument result** (54 blank LoRA rows were the deficit; see "Negatives that were setup defects"): read as 79.8 → 77.0, −2.9 (+33/−55, p=0.025), 54 LoRA rows blank | `lora_serving_eval__qwen38-author-heldout-balanced-tnr0-product-latest-20260913.json` | the loss is the output contract, not the adapter — see the next row; do not ship on batch-1 evidence |
| Qwen3.8-27B | same adapter, same server, **request-level JSON schema** (2026-09-14) | null: 81.4 → 82.6, +1.2 (+37/−28, p=0.32), **0 blank rows** in either arm (was 14 base / 54 LoRA) | `lora_serving_eval__qwen38-author-heldout-balanced-tnr0-product-batch25-jsonschema-latest-20260914.json` | the schema recovers every blank window and moves the base arm +12 too; the "hurts" verdict above was an output-contract failure read as an adapter failure |
| Qwen3-14B | "mixed" adapter (the works row above) + JSON schema + minor-speaker rule, tnr-1 A100, clean gold (2026-09-14) | works, replicated: 63.5 → 72.0, **+8.5** (+122/−57, p=1.3e-6); per book grimgar 287→318, index18 61→65, mushoku16 75→95, owari 65→75 | `lora_serving_eval__qwen3-14b-mixed-tnr1-cleangold-hint-schema-reasoning-none-20260914.json` (checkpoint; the artifact write refused a partial EXPERIMENT_ENV and is being finalised) | second measurement of the only works row, on a different box and the corrected gold; reasoning-on arm pending |
| Muse-Glimmer-30B base, reasoning low | no adapter, UD-Q3_K_XL, `--reasoning on --reasoning-format deepseek --chat-template-kwargs '{"reasoning_strength":"low"}'` + request-level JSON schema + the shipped minor-speaker rule, llama.cpp `b6b003d2c`, max_tokens 4096 | **works, four books: 81.5%** (626/768, 0 blank rows) — grimgar03 86.0, index18 73.9, mushoku16 82.0, owarimonogatari3 74.7; Qwen3-14B base on the same rows 63.0 (71.9/67.0/56.4/45.1), its best adapter 68.8 | `lora_serving_eval__muse-glimmer-30b-task4k-multin-tplfix-seed2-tnr0-product-batch25-q3-jsonschema-reasoninglow-baseonly-hint-20260914.json` (`--base-only`; the tag names the adapter the server was launched for, which the base arm never touches) | the highest attribution number in the tree, from the base model alone; one seed, temperature 0, the four light novels only. ~44 s per 25-line window on an A6000. Serving on the product's own RX 9070 XT (16 GB): the same file offloads fully at 16k context with q8 KV, 28 tok/s (smoke 2026-09-14; the four-book local run is `run_chains/muse_local_reasoninglow_20260914.sh`). Replicated: 81.1 locally (623/768, per-book artifacts 2026-09-14), 81.4 locally on llama.cpp-hip 0.4.1-dev (`local-12h-muse-base-20260916`). Reasoning **medium** 80.6 (+31/−38, p=0.47 vs low) - no gain, slower. Out of genre: 86.9% on the 1,213 PDNC minor-speaker rows (`two_stage_attribution__usual_suspects_muse_reasoninglow_mt4096_20260914.json`; Qwen 57.2 on the same rows) |
| Qwen3-14B | same mixed-multin recipe (2026-09-12) | **PDNC generalisation concern**: held-out Austen −8.8…+1.0, development Gambler +33.6; product-window result unverified | `pdnc_eval__goal13mm_*.json` (#550) | do not infer Gemma's output-contract failure or Muse's serving failure from this model's held-out result |
| Qwen3-14B | same mixed-multin seed 2, product batch 25 | **quality retries exhausted** on some windows | tnr-2 `qwen14b_mixed_multin_seed2_20260912/eval/eval.log` | server stayed healthy, but some spoken lines returned no valid source-attested speaker; four retries then recorded `PassExhausted`. A batch-10 rerun is queued before changing the adapter. |

## Prompt (the product's own attribution prompt)

| setting | value | status | evidence |
|---|---|---|---|
| minor-speaker rule (rule 4 in `default_prompts_attribute.txt`) | "Do not default to the story's main characters…" | works, small: never negative on seven books; PDNC minor rows 57.2 → 59.1 pooled (p=0.035, one book carries it); product window 477 → 488 of 768 (p=0.26), blank windows 13 → 3 | `two_stage_attribution__usual_suspects_hint_20260914.json`, `two_stage_attribution__all_rows_hint_20260914.json`, `lora_serving_eval__qwen3-14b-base-tnr1-cleangold-minorhint-20260914.json` (GOALS 1.2) |
| reasoning on the base model | `reasoning_effort: medium`, **budget 1024** | works but under-read: minor rows 57.2 → 62.9 pooled — **21% of rows (252) spent the whole budget thinking and never answered**; on the rows that answered, 752/961 = 78.3% | `two_stage_attribution__usual_suspects_reasoning_20260914.json`, `reasoning_trace_probe__usual_suspects_20260914.json` (GOALS 1.2) | trace length predicts the failure: deciles 9–10 (traces at the cap) score 3–5%, the rest 76–88%. 4096-budget reruns in progress; `--max-tokens 1024` is the failure that looked like a model limit |
| Michel et al. prompt shape | `prompt_variant: michel`, product batch 25, max tokens 4096. **Adapters trained on the default shape lose under it** (rights-clean +29/−43 at 427 rows, tnr-4 2026-09-17) - a prompt-variant score with an adapter is only meaningful when the adapter was trained on that variant | **the "reasoning-sensitive" verdict is withdrawn (2026-09-17):** the reasoning-low readings of 25.3% (Qwen) and 34–37% (Muse) were broken runs; replicated on one harness on the A100 the same cells read Qwen 78.0 and Muse 78.6 (next row). Clean reasoning-off Qwen run: 68.6% (527/768), replicated 69.3. The matched Muse + mixed-lossfix run is even lower: base **34.4%** (264/768), LoRA **35.3%** (271/768), 0 blank rows; this is a valid four-book artifact but a negative result, not a promotion score. These low-reasoning results are retained as evidence of a prompt/reasoning interaction, not as a general prompt verdict. | `cloud_pull_20260915/lora_serving_eval__qwen3-14b-base-tnr4-cleangold-prompt-michel-ctxfix-20260914.json`; `cloud_pull_20260916/tnr0/lora_serving_eval__muse-glimmer-30b-mixed-lossfix-michel-tnr0-low-20260915b.json`; prior cloud reruns `qwen3-14b-production-michel-tnr2-20260915.json` and `muse-glimmer-30b-production-michel-tnr0-20260915.json` | use Michel only with reasoning disabled until a matched reasoning-on study is completed; do not attribute the Muse low result to the adapter because both base and LoRA are poor |
| Michel-low prompt | `prompt_variant: michel_low`: Michel aliases + marked-passage format + prior-window speakers, followed by a five-step compact decision order (cue → address → roster match → plausibility check → emit); no explanations; current-passage evidence overrides continuity | **fails to repair Muse low-reasoning collapse**: reasoning none = 35.8% base / 37.1% LoRA; reasoning low = 36.5% base / 36.7% LoRA. The low arm changes only +5/−0 base and −3/+0 LoRA correct rows versus none; both arms remain far below Muse's shipped prompt result. | `cloud_pull_20260916/tnr4/lora_serving_eval__muse-glimmer-30b-mixed-lossfix-michel-low-tnr4-none-20260915.json`; `cloud_pull_20260916/tnr4/lora_serving_eval__muse-glimmer-30b-mixed-lossfix-michel-low-tnr4-low-20260915.json` | do not promote Michel or Michel-low for Muse; the rewrite did not recover low-reasoning accuracy, so isolate aliases/passage/incremental components before further prompt work |
| **Prompt variants, base models, one harness (2026-09-17)** — `default` / `michel` / `michel2` / `michel2_full` / `michel2_shot` (`app/attribution_prompt_variants.py`, selectable in Setup since #581) | same 768 rows, batch 25, JSON schema, temperature 0, base only | **DeepSeek v4-pro, thinking off:** default 91.1 → michel 91.8 → michel2 93.2 → **michel2_full 94.9** → michel2_shot 93.8 (each ~$0.50–0.75). **Qwen3-14B, reasoning low (server budget 1024):** default 65.9/66.1 (two runs) → **michel 78.0** (599/768, 4 blank; grimgar 82.3, index18 77.3, mushoku 79.7, owari 66.7 — every book up, the hard books most). The 2026-09-15 "Qwen michel + low = 25.3" and "Muse michel + low = 34–37" readings were broken runs (see the row above), not the prompt. **Qwen3-14B, reasoning off:** default 63.0 → michel 69.3 (532/768, 21 blank). **Muse-30B, reasoning low:** default 81.5 → michel **78.6** (604/768, 0 blank; grimgar 81.8, index18 73.9, mushoku 79.7, owari 72.8) - the one base Michel does not help; its michel2 / full cells follow. **Qwen3.8-27B Q4_K_M, reasoning low:** default 82.9 → **michel2_full 89.8** (690/768, 0 blank; grimgar 92.2, index18 87.5, mushoku 91.7, owari 84.0). **Qwen3.6-35B-A3B Q4_K_XL, reasoning low, temperature 0.6:** michel2_full **89.6** (688/768, 0 blank; 91.7 / 81.8 / 89.5 / **88.9** - the best owari of any local model); its default-prompt control is queued. **Qwen3.5-9B Q4_K_M on the RX 9070 XT, reasoning low:** michel2_full 71.9 (552/768, 10 blank; 82.6 / 69.3 / 72.9 / 46.9) - a 5.7 GB file at the level of Qwen3-14B's default-prompt base, collapsing only on owari; its default control is running. The complete cells as of 2026-09-18 are in the "Prompt variants × bases" table below; Muse michel2 / full / shot and the michel2_shot rounds for Qwen3-14B, Qwen3.8 and A3B are still in flight | `cloud_pull_20260917/deepseek/lora_serving_eval__deepseek-v4-pro-api-cleangold-batch25-thinking-off-{michel,michel2,michel2_full,michel2_shot}-20260917.json`; `cloud_pull_20260917/tnr1/lora_serving_eval__qwen3-14b-michel-low-tnr1-cleangold-replication-20260917.json`; tnr-1 `*-michel-none-*` and `muse-michel-low-*`; tnr-0 `lora_serving_eval__qwen38-27b-q4km-michel2_full-*` and `qwen36-35b-a3b-thinking-michel2_full-*`; local `qwen35-9b-q4km-michel2_full-*` (dev11) | on Qwen3-14B the prompt alone (+12) equals the best adapter (mixed + reasoning, 78.0) with no adapter; on DeepSeek the surrounding text is the biggest single step (+1.7 over michel2) and the worked example adds nothing over it. On every reasoning model but Muse the prompt is worth +7 to +12; on Muse `michel` costs 3. Product default stays `default` until Muse's michel2 / michel2_full cells land (tnr-1, tonight) |

## Pass 1: dialogue detection (`generation.three_pass_segmentation`, 2026-09-18)

| mode | what pass 1 does | evidence |
|---|---|---|
| `auto` (default; every three-pass result above was measured with it) | quote marks where they split the chunk into more than one region and the segment gate passes; the model for the rest - in practice ~7% of chunks, almost all narration-only | manifests `pipeline_repeats/run*.json.threepass_manifest.json`: 90/99 and 79/90 chunks `quote_presegmented` |
| `quotes` (issue #588) | never the model: quoted text is dialogue, everything else narration, `quote_forced` in the manifest for chunks the gate did not vouch for; a run on a book with no quote marks in more than half its chunks is refused before it starts | a plain quote segmenter finds **99.84%** of PDNC's 44,812 hand-labelled quotation spans across all 28 novels (`quote_segmenter_pdnc_20260918.json`); 12k chars of Emma through DeepSeek: pass 1 in 0.06 s, four model calls total (`--segmentation quotes`) |
| `llm` | always the model | the old `three_pass_presegment_quotes: false` |

Not yet measured: pass-2 accuracy under `quotes` against `auto` on the same
rows (expected within noise, since `auto` already uses the marks on 93% of
chunks). A book with em-dash or unmarked dialogue must stay on `auto`.

## Voice: dead air, per-line instructs, identity anchor (2026-09-18)

Three measurements behind PR #603, all on this machine's card (RX 9070 XT).

**Dead air at the joins** (`dead_air_scan_20260918.json`, 6,593 generated
lines, −45 dBFS over 20 ms frames): the LoRA path emits a median **310–340 ms
of leading silence** per line (p90 540–600 ms) and 80 ms trailing - 4–5% of a
book, ~2.5–2.9 min per hour - on top of the configured pause, so a 250 ms
same-speaker gap was really ~670 ms. The CustomVoice path emits ~80 ms. Fix:
trim at join time to 40 ms head / 80 ms tail (`tts.trim_edge_silence`);
on the 150 real chapter lines 991.8 s → 948.6 s, 40 ms lead left, no onset
level jump.

**Per-line instructs and the identity anchor on the CustomVoice path**
(`custom_voice_instruct_drift__ryan_arc1_20260918.json`, 120 real narrator
lines with the instructs pass 3 actually wrote, same text, same seed, Ryan):

| arm | ECAPA to own opening (mean / p10) | pitch spread | VTL spread | s/line |
|---|---|---|---|---|
| as_written | 0.737 / 0.604 | 3.49 st | 0.738 | 10.26 |
| no_timbre (lexicon rule) | 0.737 / 0.604 | 3.48 st | 0.744 | 10.25 |
| anchored (constant identity + line) | 0.771 / 0.643 | **2.53 st** | 0.712 | 10.25 |
| anchor_only | **0.825 / 0.740** | **2.42 st** | 0.741 | 9.90 |

The buddies fork's strip-timbre-words rule does nothing (only 1–4% of our
instructs carry such words: `instruct_audit_20260918.json`, 129,560
instructs). A constant identity in front of each line's emotion cuts the
run's pitch wander from 3.5 to 2.5 semitones and raises self-similarity;
the anchor alone is the ceiling but loses the emotion. Shipped as the
anchor-first CustomVoice instruct (`tts.anchored_instruct`) with
change points (`style_timeline`) so an aged character or a time skip is a
point, not a second speaker. Not yet listened to (6.5).

## Serving and measuring (the instrument, not the model)

### Qwen reasoning and output references

- [Qwen3 English concepts and thinking format](https://qwen.readthedocs.io/en/latest/getting_started/concepts.html) — thinking is separated from final content with `<think>` blocks.
- [Qwen3 Chinese quickstart](https://qwen.readthedocs.io/zh-cn/stable/getting_started/quickstart.html) — documents `/think` and `/no_think`, recommends non-greedy thinking-mode sampling, and notes that open-source frameworks do not directly provide the hosted thinking-budget control.
- [QwenCloud structured-output FAQ](https://docs.qwencloud.com/resources/faq-text-generation) — warns that truncating a JSON response makes it invalid and recommends retaining full thinking output before parsing.
- [Japanese NLP proceedings: Qwen3 structured-output evaluation](https://www.anlp.jp/proceedings/annual_meeting/2026/pdf_dir/B2-3.pdf) — a Japanese-language evaluation that disables thinking for its strict JSON output setting.

| setting | value | status | evidence |
|---|---|---|---|
| product harness completion budget | `lora_serving_eval.py --max-tokens` = the product's `LLMGenParams.max_tokens` (4096) | works | #549. The harness pinned 2000 — half the product's — and Muse overflowed 70% of windows |
| temperature-0 retry guard | keyed on (prompt, budget), not prompt alone | works | #549: keyed on the prompt, it cancelled every budget escalation, so a truncated window at temp 0 could never recover |
| Muse-Glimmer on llama-server | `--reasoning off --skip-chat-parsing --chat-template-kwargs '{"reasoning_strength":"none"}'` + request-level JSON schema, llama.cpp **`b6b003d2c`** (built 2026-09-13; CTest 59/60, the one failure a missing LFS vocab fixture) | works: full batch-25 run, LoRA arm 768/768 answered, zero PEG-native 500s | `muse_task4k_tnr0_product_20260913/server.log` (0 "output does not match"), the Muse row above | on the 2026-08-23 build the same flags passed a 12-entry smoke and then failed every LoRA request in a full run — the build is part of the recipe. The base arm still emits raw non-JSON on ~12% of windows; that is Muse, not serving |
| Muse-Glimmer on llama-server | `--reasoning off` alone | **leaks reasoning** | same logs, the earlier runs |
| Muse-Glimmer adapter + reasoning on | `--reasoning on --reasoning-format deepseek --chat-template-kwargs '{"reasoning_strength":"low"}' --lora <any 2026-09-11 tplfix adapter>` (no `--skip-chat-parsing`, which reasoning separation cannot use) | **failed all 20/20 multi-entry windows in the 2026-09-14 diagnostic**; resolved by the loss-window fix - the 2026-09-14 `task4k-multin-lossfix` adapter served 768/768 windows with reasoning low and 0 parser failures (row in the 2026-09-17 section) | tnr-0 `diag_muse_server.log` (`--verbose`): `full peg-native output triggering error: <|start|>assistant=user<|message|>[{"n": 0, …}]<|eot|>`; tnr-1 `muse_parser_20line_smoke_tnr1_20260916c/adapter_parser.response.json` is the successful small-output counterexample | The failure is **configuration- and workload-sensitive**. The adapter/header diagnosis remains the leading explanation for the old multi-entry run, but the new build's small smoke means parser support is not categorically absent. Compare the same adapter, prompt, and batch size across builds before calling it a trainer-only bug. `--skip-chat-parsing` bypasses the parser but then reasoning cannot be split from content. Reported upstream on ggml-org/llama.cpp#27025 (closed; different trigger) |
| Muse-Glimmer trainer loss window | `distill_train_muse_multin_templatefix_20260911.py` compute_loss: `keep = (labels != -100).sum().max()`, then `shifted[:, -keep:]` with `logits_to_keep=keep` | **off by one: the first answer token is never supervised** (batch 1, so exactly one token per example). Tokenised check on tnr-1: answer `[' to','=user','<|message|>','[{"',…,'<|eot|>']`, old window supervises from `'=user'` on. Same bug dropped the leading `[` in the pre-templatefix adapters (the memory's "missing leading `[`") — the first answer token then | the reproduction is the 12-line snippet in the 2026-09-14 session (tokenizer only, no GPU); fixed copy `distill_train_muse_multin_lossfix_20260914.py` keeps every position from the earliest supervised one (`keep = L − first_supervised_index`) and slices `outputs.logits[:, -keep:]` | every `*-tplfix` Muse adapter (task4k-multin, longcontext, hardcases, author-balanced, mixed, both seed-2s) has this defect; they still score +4.4 with `--skip-chat-parsing` because the header is never parsed there. Do not copy the 09-11 trainer's loss into another trainer; the Qwen/Gemma trainers use the HF `labels=` path and are not affected — verify by the same tokenised check before assuming |
| structured attribution responses | request-level `response_format` JSON schema requiring `n` and `speaker` | **works, on two models**: Muse LoRA arm 768/768 answered; Qwen3.8 blank rows 14/54 → 0/0 and the adapter verdict from −2.9 to +1.2 | tnr-1 `muse_product_mixed-tplfix-seed2_jsonschema_20260913/eval.log`; the Qwen3.8 JSON-schema row above | the product default since 2026-09-14 (`structured_output: auto` per LLM profile, issue #522 §9.1; a server that rejects `response_format` is remembered for the run); schema validity still does not guarantee source-attested speakers, so the semantic gates stay |
| Muse JSON contract and serving path | newer llama.cpp build with Muse parsing/schema path versus the older A100 build using `--skip-chat-parsing` | **configuration implicated; adapter contribution unisolated**: tnr-1 still emits `expected N entries, got 1`/malformed JSON under the older path, while the known-good newer-build Muse evaluation answered 768/768; the rightsclean-lossfix adapter also showed the failure on tnr-0 | tnr-1 `muse_release_longcontext_recovery_20260916b/eval.log`; tnr-0 `muse_rightsclean_partial_20260916.md`; known-good `muse_product_mixed-tplfix-seed2_jsonschema_20260913/eval.log` | do not blame the model or llama.cpp alone until the same base/adapter, prompt, and book are compared across builds and batch sizes (including batch 1) |
| Qwen3-14B four-arm serving smoke | A100 llama.cpp `b6b003d2c`, 20-line request, base/adapter × parser/skip-parser, reasoning on | **mixed framing, all arms completed**: adapter+parser returned a JSON list of 20; base+parser returned 20 newline-delimited objects; both skip-parser arms returned `<think>` text plus content rather than a JSON list | `cloud_pull_20260916/tnr1/qwen_parser_20line_smoke_tnr1_20260916/` | confirms the rebuilt binary serves Qwen and loads the adapter; it is not a product accuracy result. Parser and response-shape handling must accept the base newline-object form or enforce the schema before promotion |
| product-window repair checklist (Gemma, Muse, Qwen) | preserve four-book gold evaluation; use 4096 completion budget; enforce JSON structure where the server supports it; use batch 10 for Qwen speaker attribution; wait for every same-GPU predecessor to exit | **queued for confirmation** | tnr-1 Muse JSON-schema rerun; tnr-2 Qwen batch-10 rerun; Gemma contract/canary queue on tnr-4 | Gemma needs one response object per frozen entry, Muse needs parser/reasoning containment, and Qwen needs smaller attribution batches when source-attestation retries exhaust |
| Qwen reasoning-control diagnostic | single-book `owarimonogatari3` (the hardest measured Qwen book), base-only; explicit `/think` and `/no_think`, recommended thinking sampling (`temperature 0.6`, `top_p 0.95`, `top_k 20`), batch sizes 10/25/5, and 4k/8k/16k completion ceilings | **queued; no result yet** | tnr-2 `/home/ubuntu/qwen_reasoning_matrix_tnr2_owari_20260916b.sh` and its four versioned tags | replaces the six queued four-book arms; the active four-book Michel-low run is unchanged. This isolates budget exhaustion from prompt, sampler, and batch-size effects. A true two-generation thinking-budget implementation remains untested |
| Qwen reasoning four-arm smoke | A100 llama.cpp `b6b003d2c`, 20-line fixture, Qwen sampling (`temperature 0.6`, `top_p 0.95`, `top_k 20`), `/no_think` plus `/think` budgets 4096/8192/16384 | **all four completed with `finish_reason=stop` and 20 objects**; reasoning traces were 0, 4,807, 2,703, and 2,533 characters respectively. This smoke did not truncate at any tested ceiling, so it does not reproduce the long-book budget failure | `cloud_pull_20260916/tnr1/qwen_reasoning_research_4arm_tnr1_20260916/` | confirms the Qwen-specific control path and sampler. It is not an accuracy result; the 4k/8k/16k arms need a hard multi-entry/book fixture to test budget exhaustion |
| Qwen reasoning four-arm schema smoke | Same A100/Qwen fixture and research controls, with strict array JSON schema in `response_format` | **all four completed with `finish_reason=stop`, valid JSON arrays, and exactly 20 objects**; reasoning chars were 0, 2,047, 2,947, and 1,307 for no-think/4k/8k/16k. The schema removed the newline-delimited shape seen in the unconstrained run | `cloud_pull_20260916/tnr1/qwen_reasoning_schema_4arm_tnr1_20260916/` | this is the preferred Qwen request recipe, but it is still a smoke result; validate the schema path on a hard multi-entry book before promotion |
| Qwen reasoning production recipe | First pass: Qwen `/think` with `--reasoning-format deepseek`, a measured budget (start at 4096), `temperature 0.6`, `top_p 0.95`, `top_k 20`; second pass: a separate `/no_think` request containing the original lines, roster, and draft, with `temperature 0` and strict array `response_format` schema | **JSON contract works with the adapter and reasoning**: the real-fixture adapter+think arm returned 20/20 schema-valid objects and stopped normally | `cloud_pull_20260916/tnr1/qwen_real_fixture_two_stage_4arm_tnr1_20260916/` | do not rely on schema enforcement during a thinking generation (llama.cpp #20345 reports grammar can be inactive then); keep reasoning and formatting separate. Before scoring, canonicalize aliases, require exact contiguous indices, roster membership or `UNKNOWN`, unchanged source text, and reject leftovers. The real 20-row diagnostic scored only 0--5/20 because it omitted surrounding passage context; that is a context/attribution failure, not a JSON failure |
| Qwen Michel-low small-context | single-book `owarimonogatari3`, base-only, `prompt_variant: michel_low_smallctx`, reasoning low, batch 25, max_tokens 4096 | **stopped; invalid as a promotion measurement** | tnr-2 artifact `qwen3-14b-michel-low-smallctx-tnr2-base-20260916.json` | repeated missing-speaker/JSON failures and PassExhausted windows; stop released the GPU for the controlled sampler/budget matrix. Preserve the artifact as failure evidence, not a score |
| Muse rightsclean-lossfix partial diagnostic | Muse base/LoRA, reasoning low, product batch 25; stopped after three books when the LoRA repeatedly returned one object for multi-entry windows | **partial negative; not promotion-grade**: base 505/606 (83.3%), LoRA 28/606 (4.6%), LoRA unanswered 555; `owarimonogatari3` not run | `ab_test_runtime/muse_rightsclean_partial_20260916.md` plus retained tnr-0 checkpoint/log | the base was healthy; the loss-fixed adapter still has a severe output-contract failure, so the GPU was released rather than spending another book on the same failure |
| LoRA voice serving | `merge_and_unload()` into the talker (#546) | works | goal 4.1: 1.25x → 0.828x realtime on 479 clips, 25 adapters; ECAPA inside seed noise |

## Runtime update and queue recovery (2026-09-16)

| item | recipe/result | status | evidence and guard |
|---|---|---|---|
| local llama.cpp-hip | upstream `0.4.1-dev`, commit `4bc272f`; `GGML_HIP=ON`, graphs and no-VMM enabled; RX 9070 XT | **works** | plain generation and strict JSON-schema smoke both returned HTTP 200; schema output contained required `speaker` and `why` fields. The previous `0.4.0-dev` binary is backed up outside the tree. |
| local 12-hour validation batch | one model at a time: Muse base four-book low-reasoning control, Muse task4k adapter comparison, then Qwen3-14B hard-book control | **running** | `ab_test_runtime/local_12h_20260916/`; first six Muse windows all stopped normally at 25–38 s/window. Do not run a second local llama server concurrently. |
| same-GPU evaluator dispatch | never run two evaluators against one llama-server; adapter scale toggles are shared server state | **required safety rule** | duplicate older PIDs on tnr-0, tnr-1, and tnr-4 were stopped on 2026-09-16. Any artifacts produced while two evaluators overlapped are diagnostic only and must not be used for promotion. |
| Muse Michel-low adapter arm | Muse `pdnc_verified_additions` adapter, reasoning low, multi-entry product batch | **collapses** | tnr-4 `muse_michel_low_low_20260916_working/eval.log`: repeated two-token responses, missing JSON, and `PassExhausted` from window 42 onward. The adapter was stopped, not scored. |
| Muse task4k-multin-lossfix recovery | same Muse base with `task4k-multin-lossfix`, one-book `owarimonogatari3`, reasoning low, schema auto | **serves correctly; the completed book and the four-book run are nulls** (2026-09-17 section) | tnr-4 `/home/ubuntu/muse_failed_onebook_task4k-multin-lossfix_20260916/eval.log`: server healthy and first window returned a valid 1,071-token response. Complete the book before interpreting accuracy. |
| tnr-2 / tnr-1 Qwen overnight queues (`/tmp/qwen14_overnight_*`) | restart the server before the evaluator and use the four-book checkpointed queue | **repaired, then found to be the wrong instrument** (see the 2026-09-17 section: the schema evaluator pins reasoning to none while the server reasons with a 1024 budget) | `/tmp/qwen14_overnight_queue_20260916f_rerun.log`; the prior attempt died with connection-refused/503 because its server exited. The restarted run reached the final `grimgar03` window; one row used the documented last-attempt fallback. |
| DeepSeek Re:Zero judges | judges A–E, three books, retain agreement plus each judge's `why` | **complete, not live** | `attribution_gold_rezero_v1/v2/v3.json` and review files are complete. No active process is expected; current API balance measured at $16.07. |

These recovery results separate serving failures from model/prompt failures. A two-token response or a concurrent evaluator is an instrument failure, not an adapter score. Re-run the affected arm with one evaluator, one server, a fresh tag, and the updated binary before promotion.

## Negatives that were setup defects, not results (2026-09-17)

Three adapter verdicts in this file and in GOALS were written as "the adapter
does not help" when what had been measured was a broken instrument. They are
listed here so the pattern is visible: in every case the score was real, the
sentence around it was wrong, and the defect was found by reading the raw
artifact (server output, tokenised label window, trace file) rather than the
loss curve or the accuracy number. Rule 19, again.

| verdict as first written | what was actually wrong | how it was found | status |
|---|---|---|---|
| Muse gen 1 (09-09) adapters "0/768, unusable" | trainer built the assistant turn without the template's ` to=user<|message|>` header and ended it with the wrong token; the adapter dropped the leading `[` of every answer | reading the raw completions against the official template render | the zero was real, the adapter was never measurable; "tplfix" retrain |
| Muse gen 2 (09-11, tplfix) "adapter + reasoning fails every window; harmony parser / channel envelope" | the loss window sliced one token short, so the first answer token (` to`) was never supervised and the adapter emitted `assistant=user`; the parser was right to reject it | `--verbose` server log + a 12-line tokenisation check of the label window | "lossfix" retrain; the tplfix +4.4/+8.5 stand only for the reasoning-off path |
| Muse gen 3 (09-14/15, lossfix) "adapters do not help Muse: null / negative with reasoning on" | training turns carried no reasoning channel, so the adapter taught the model to answer without thinking and was served in a mode where thinking is the accuracy (reasoning-trace collapse, arXiv 2605.21127; Unsloth's Muse guide: "mix reasoning-style examples with direct answers"). 98-100% of Muse's own correct traces are grounded in the row's context, so the reasoning itself was never the problem | the trace-quality filter (`filter_traces.py`) and the literature | gen-3 RFT retrain in flight (k=4 at T=0.7, own traces as `reasoning_content`); the first Muse adapter measured with its training shaped like inference |
| Qwen3.8-27B "hurts at the product window, −2.9" | free-form JSON with no schema: 54 LoRA rows blank; the blanks were the whole deficit | the schema rerun, 0 blank rows, +1.2 | null, not negative; and the base with reasoning is 82.9 - the second-strongest local-class base measured |
| Qwen3-14B reasoning-on chains "wrong instrument (reasoning pinned to none against a 1024 server budget)" | my inference from the code; the checkpoints showed 76% bases with no blanks - the runs were fine | reading the artifact instead of the mechanism | withdrawn the same day; those chains produced the best Qwen3-14B number (mixed + reasoning, 78.0) |
| Muse 2026-09-11 artifacts at "0%" (`author-balanced-tplfix`, `hardcases`, `task4k`, `longcontext-tplfix`, and the lossfix pair) | a bad CUDA library path: the server never answered, the harness scored 768 blanks | the one-book Owari reruns with the corrected path and a fail-loud health check (tnr-0, 2026-09-17, reasoning low, schema): base **121/162 = 74.7%**, 0 blanks; mixed-lossfix 69.1% (+19/−28), task4k-multin-lossfix 70.4% (+15/−22), both 0 blanks - **served correctly, mildly negative on the hardest book**, in line with their four-book −7.7 / −0.8. longcontext-tplfix 45.1% with 56 blanks: a gen-2 (loss-window bug) adapter, still broken as expected | `cloud_pull_20260917/tnr0/lora_serving_eval__muse-glimmer-30b-*-onebook-owari-workingrecipe-20260917.json` | the zeros were the instrument; the adapters' real verdicts are the lossfix rows above. author-balanced-tplfix's rerun was stopped at window 87 for the prompt grid and resumes after it |

**Rule for this file:** a negative adapter row needs the sentence "served correctly: N/N windows answered, 0 parser failures, base arm matches the standalone base" before it can say the adapter is the cause. None of the rows above had it.

## Adapter and roster results checked against the artifacts (2026-09-17)

Every completed artifact on the four boxes was pulled and rescored with the
paired row-level scorer (`cloud_pull_20260917/`, `cloud_pull_20260916/`).
Accuracy counts unanswered rows in the denominator; `+/-` is the paired
transition count against the arm's own base on the same server.

| model/adapter | fixture | base | adapter | delta | paired | verdict |
|---|---|---:|---:|---:|---|---|
| Muse task4k-multin-lossfix, reasoning low, schema | four books, 768 | 81.5 | 80.7 | −0.8 | +53/−59, p=0.64 | **null** - and the first Muse adapter that served with reasoning on: 0 blank rows, 0 parser failures, so the loss-window fix holds |
| Muse mixed-lossfix, reasoning low, schema, shipped prompt | four books, 768 | 84.0 | 76.3 | −7.7 | +51/−110, p=4e-6 | **negative** (the tag says "michel"; the run used `--prompt-variant default`) |
| Muse task4k-multin-lossfix, one book | owarimonogatari3, 162 | 74.7 | 70.4 | −4.3 | +15/−22, p=0.32 | null on a slice of the row above |
| Muse base, reasoning low, roster-by-mention | four books, 768 | 81.5 (full roster) | 78.3 | −3.2 | - | **negative**: the serial-novel roster rule (names attested in the window + the previous window's cast) loses on Muse as it did on Qwen (63.4 vs 63.0, null) |
| Qwen RiQuA adapter, reasoning low (budget 1024), schema, default prompt (Codex tag "michel-hardness" = default prompt; its base per book equals the rights-clean run's base exactly) | four books, 768 | 66.1 | 71.4 | +5.3 | +100/−60 | works; below rights-clean + reasoning (74.7) and mixed + reasoning (78.0) |
| Qwen rights-clean adapter on the product's own RX 9070 XT, reasoning low (budget 1024), schema, default prompt (`run_chains/qwen_local_rightsclean_budget_20260917.sh`) | grimgar03 385 / index18 88 / mushoku16 133 / owarimonogatari3 162 | 70.4 / 68.2 / 64.7 / 48.8 (pooled 64.6) | 83.1 / 71.6 / 78.2 / 62.3 (pooled **76.6**) | **+12.7** / +3.4 / **+13.5** / **+13.6** (pooled **+11.9**) | +63/−14 / +8/−5 / +33/−15 / +33/−11 | works locally as it did on the A6000 (+11.7); **the local default for Qwen3-14B**. Artifacts `lora_serving_eval__qwen3-14b-rightsclean-local-9070xt-product-batch25-budget1024-schema-{grimgar03,index18,mushoku16,owarimonogatari3}-20260917.json` |
| Qwen RiQuA, 3 epochs | four books, 768 | 61.7 | 69.7 | +8.0 | +112/−51, p=2e-6 | works; below the rights-clean stack |
| Qwen rights-clean stack with RiQuA ×2 | four books, 768 | 61.7 | 70.6 | +8.9 | +131/−63, p=1e-6 | works; below the plain stack's **+11.7** (73.4, the best Qwen adapter measured) - more RiQuA does not help |
| Qwen rights-clean, **reasoning budget 512 / 1024 / 2048** (tnr-2, 2026-09-17/18) | four books, 768 | 66.7 / 66.1 / 67.1 | 74.7 / 74.7 / 75.9 | **+8.1 / +8.6 / +8.8** | +119/−57 / +127/−61 / +124/−56 | the budget dial is flat: thinking longer buys the adapter nothing, and 512 is the cheap setting. `lora_serving_eval__qwen3-14b-rightsclean-tnr2-cleangold-budget{512,2048}-schema-20260917.json` |

**On Muse the base with reasoning low is the recipe.** Three loss-fixed
adapters have now been served correctly with reasoning on: task4k-multin is a
null, mixed is negative, rightsclean returned one object per multi-entry
window (partial diagnostic above). None beats 81.5.

**Numbers withdrawn from the first draft of this section, and why.** The tnr-1
`/tmp/qwen14_overnight_*` chains (2026-09-16) drive `lora_serving_eval_muse_jsonschema_20260913.py`,
which pins `reasoning_strength: none` in the request body, against a server
started with `--reasoning on --reasoning-budget 1024`. Qwen thinks, the
1024-token budget cuts it off, and the score is the instrument, not the
adapter: the DraCor-English arm read "27.9 → 64.5" (its checkpoint shows the
base collapsing to 47.5% before the run died) and the RiQuA arm read "72.4 →
72.8" with a base far above the shipped prompt's 63.0. The committed results
stand: DraCor-English −3.9, RiQuA +6.0. Any run of that evaluator with
`--reasoning on` on the server is diagnostic only. The 2026-09-11 Muse
artifacts at 0% (`author-balanced-tplfix`, `hardcases`, `task4k`) are invalid
parser/instrument runs, not quality measurements. They are queued for a
one-book Owari rerun with the corrected CUDA library path and fail-loud server
health check; do not interpret the old zero as an adapter verdict.

## Downloaded scores and Muse one-book reruns (2026-09-17)

Completed cloud artifacts were pulled to `cloud_pull_20260917/` and rescored
locally with the paired row-level scorer. Accuracy includes unanswered rows.

| model/adapter | fixture | base | adapter | delta | paired p | verdict |
|---|---|---:|---:|---:|---:|---|
| Muse mixed-lossfix + Michel-low | four books, 768 rows | 645/768 (84.0%) | 586/768 (76.3%) | −7.7 pp | 3.83e-6 (`+51/-110`) | negative; do not promote |
| Qwen RiQuA | four books, 768 rows | 556/768 (72.4%) | 559/768 (72.8%) | +0.4 pp | 0.864 (`+70/-67`) | null |
| Qwen DraCor-English | four books, 768 rows | 214/768 (27.9%) | 495/768 (64.5%) | +36.6 pp | 1.87e-54 (`+324/-43`) | audit first: anomalous Grimgar base collapse |
| Muse task4k-multin-lossfix | Owari, 162 rows | 121/162 (74.7%) | 114/162 (70.4%) | −4.3 pp | 0.324 (`+15/-22`) | negative; do not promote |

The valid under-base Muse adapters (`longcontext-tplfix`, `mixed-lossfix`, and
`task4k-multin-lossfix`) run first on tnr-0. The three historical 0% cases
(`author-balanced-tplfix`, `hardcases`, and `task4k`) run afterward. The new
launcher is `ab_test_runtime/muse_failed_onebook_queue_20260917b.sh`; it
exports the CUDA library directories, checks that `llama-server` remains alive
during health polling, and runs one evaluator per server. The first attempt
failed before evaluation because `libcudart.so.13` was not discoverable; no
score was taken from that attempt.

## Small quants of the two best local bases (2026-09-17, in flight)

All cells: michel2_full, reasoning low (server `--reasoning-budget 1024`), JSON
schema, base only, the same 768 rows. The question is how small a file still
holds the score - for a 16 GB card, and for the 6 GB / 8 GB cards users write
in with. The MoE runs with experts in system RAM (`--n-cpu-moe`), so its file
size is a RAM requirement, not a VRAM one.

| model | quant | file | score | evidence |
|---|---|---|---:|---|
| Qwen3.6-35B-A3B (temp 0.6) | UD-Q4_K_XL | 22.4 GB | **89.6** (0 blank) | tnr-0 `qwen36-35b-a3b-thinking-michel2_full-*` |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | 13.2 GB | **88.0** (0 blank; 91.4 / 83.0 / 88.7 / 82.1) | tnr-2 `*-iq3xxs-*` |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | 10.8 GB | **87.8** (0 blank; 91.7 / 81.8 / 88.7 / 80.9) | tnr-0 `*-iq2xxs-*` |
| Qwen3.6-35B-A3B | UD-IQ1_M | 10.0 GB | **85.0** (0 blank; 87.8 / 81.8 / 83.5 / 81.5) | tnr-0 `*-iq1m-*` |
| Qwen3.8-27B (dense) | UD-Q4_K_M | 16.5 GB | **89.8** (0 blank) | tnr-0 |
| Qwen3.8-27B | UD-Q3_K_XL | 13.1 GB | **87.5** (0 blank; 88.8 / 87.5 / 91.0 / 81.5) | tnr-2 `*-q3kxl-*` |
| Qwen3.8-27B | UD-IQ2_XXS | 7.3 GB | **83.1** (5 blank; 85.5 / 78.4 / 84.2 / 79.0) | tnr-2 `*-iq2xxs-*` |
| Qwen3-30B-A3B-Thinking-2507 | UD-Q4_K_XL | 17.7 GB | 78.5 (**41 blank**; 86.2 / 75.0 / 68.4 / 70.4) | tnr-4 - the older MoE; blanks are windows that spent the budget thinking |
| Qwen3.5-9B (dense) | Q4_K_M | 5.7 GB | 71.9 (10 blank; default prompt 62.6) | local |
| Qwen3-8B (dense) | Q4_K_M | 5.0 GB | 71.7 (4 blank; default prompt 60.8) | local |

Read across: the A3B loses 1.6 points from 22.4 GB to 13.2 GB, 1.8 to 10.8 GB
and 4.6 to 10.0 GB; the dense Qwen3.8 loses 2.3 to 13.1 GB and 6.7 to 7.3 GB.
Every A3B rung beats every Qwen3.8 rung of similar file size, and the 10 GB
IQ1_M (85.0) beats Muse's 13.4 GB Q3 (81.5) and Qwen3-14B's best prompt
(82.0). Speed on the RX 9070 XT, measured 2026-09-17: IQ1_M fully on the card
82 tok/s; with experts in system RAM (`--n-cpu-moe`) IQ1_M 22, IQ2_XXS 20,
IQ3_XXS 17 tok/s - so a 6–8 GB card runs the 88-point model at 17 tok/s
provided it has ~16 GB of RAM for the experts.

Then the same ladder with a michel2-shape rights-clean adapter on each base
(A3B on the A100, Qwen3.8 on tnr-4), paired, to see whether an adapter closes
the low-quant gap. Not in this table until measured.

## Prompt variants × bases, complete cells (2026-09-18)

Same 768 rows, batch 25, JSON schema, base only, reasoning low with the server
`--reasoning-budget 1024` unless the column says otherwise; DeepSeek is the API
with thinking off (its thinking-low cell is separate). A blank cell is not yet
measured. Every artifact is
`ab_test_runtime/experiments/lora_serving_eval__<tag>-20260917.json`; the
structural audit classes them **provisional** (probe-branch harness
`probe/attribute-prompt-v2` at 7a781e4…4d33772, run from a worktree it reports
as dirty), so they are cited here as measured numbers, not as release evidence.

| base | file | default | michel | michel2 | michel2_full | michel2_shot | notes |
|---|---|---:|---:|---:|---:|---:|---|
| DeepSeek v4-pro (API, thinking off) | - | 91.1 | 91.8 | 93.2 | **94.9** | 93.8 | thinking **low, 8k**: michel2_full **95.4** (1 blank) - the ceiling; ~$0.50–0.75 per cell |
| Qwen3.8-27B UD-Q4_K_M | 16.5 GB | 82.9 | 84.8 | **90.9** | 89.8 | 90.1 | the best local number on record; the three michel2 variants are within a point (michel2 94.3 / 88.6 / 88.7 / 85.8; shot owari **94.4**, the best owari of any local base); the one base where the worked example does not hurt |
| Qwen3.6-35B-A3B UD-Q4_K_XL (temp 0.6) | 22.4 GB | queued | 86.6 | 87.5 | **89.6** | 86.6 | shot = michel (86.6 both); the surround block is the whole gain |
| Muse-Glimmer-30B UD-Q3_K_XL | 13.4 GB | 81.5 | 78.6 (none: 75.7, 83 blank) | **86.6** (665/768, 0 blank; 90.4 / 81.8 / 84.2 / 82.1) | queued | queued | `michel` cost 3, `michel2` gains 5 - paired on the same rows against default low: +68/-29; against michel low: +76/-15. The shipped base's first owari over 80. full / shot land after the A3B adapter on tnr-1 |
| Qwen3-14B Q4_K_M | 9.0 GB | 65.9 / 66.1 | 78.0 (none 69.3) | 77.5 (none 74.5) | **82.0** (3 blank; 87.3 / 81.8 / 85.7 / 66.7) | 75.7 (27 blank) | +16 from the prompt alone, the largest gain of any base; the worked example hurts here (-6 vs full, 27 blank); the rights-clean adapter under `default` reaches 74.7–76.6 |
| Qwen3-30B-A3B-Thinking-2507 UD-Q4_K_XL | 17.7 GB | - | - | - | 78.5 (41 blank) | - | not pursued further |
| Qwen3.5-9B Q4_K_M (RX 9070 XT) | 5.7 GB | 62.6 | - | - | 71.9 (10 blank) | - | |
| Qwen3-8B Q4_K_M (RX 9070 XT) | 5.0 GB | 60.8 | - | - | 71.7 (4 blank) | - | Qwen3.5-9B and Qwen3-8B are within noise of each other; both collapse on owari (46.9 / 62.3) |

What holds across bases (2026-09-18, every cell but Muse full/shot in):
michel2_full ≥ michel2 ≥ michel ≥ default on DeepSeek, A3B and Qwen3-14B;
on Qwen3.8 michel2 edges michel2_full; on Muse `michel` costs 3 and `michel2`
gains 5. The surrounding-text block (`--surround-chars 2000`,
`three_pass_attribute_context_chars` in the product) is the single biggest
step on DeepSeek (+1.7), Qwen3-14B (+4.5) and A3B (+2.1). The worked example
(`michel2_shot`) never helps: DeepSeek −1.1, Qwen3-14B −6.3, A3B −3.0,
Qwen3.8 +0.3 vs full. **The michel2 family has now won on every base
measured, including the shipped one.** Product default moves to
`michel2_full` once Muse's own michel2_full cell confirms it beats michel2
there too (tnr-1, tonight); until then `michel2` is the measured best for
Muse.

## Independence check on novels this project never tuned on (2026-09-17/18)

Every number above is on the same four light novels (768 rows) that every
prompt and adapter decision was made against. The check: DeepSeek v4-pro
(thinking off), `default` against `michel2_full`, on two PDNC novels whose
gold is the published `quotation_info.csv` with the corpus cast as the roster
(`pdnc_fixture.py`; the attestation gate is off for corpus-cast fixtures).

| novel | rows | default | michel2_full | delta | evidence |
|---|---:|---:|---:|---:|---|
| *Emma* (Austen) | 998 | 96.9 | **99.4** | +2.5 | `lora_serving_eval__deepseek-v4-pro-api-pdnc_emma-batch25-thinking-off-{default,michel2_full}-20260917.json` |
| *The Sun Also Rises* (Hemingway) | 1,759 | 87.0 | **92.1** | +5.1 | `…pdnc_thesunalsorises-…` |

The prompt's gain replicates on both, and is largest on the harder book -
the same shape as on the four light novels, on text that shares nothing with
them. Rule adopted: **a product default needs both fixtures; owari is the
tie-breaker.** The nine-book version (Emma, Mansfield Park, Northanger Abbey,
Persuasion, Pride and Prejudice, Sense and Sensibility, The Awakening, The Sign
of the Four, The Sun Also Rises; ~2,300 evenly spaced rows via
`--window-limit`) is running on the RX 9070 XT for A3B IQ3_XXS / IQ2_XXS /
IQ1_M × five prompts, then Qwen3.5-9B, Qwen3-8B, Qwen3.5-9B-Uncensored and
Gemma-E4B (`local_matrix_20260917d.sh`); on tnr-4 Muse and Qwen3-14B × six
prompts follow the Qwen3.8 adapter ladder (`pdnc9_tnr4_20260917.sh`). First
cell is final:

| model | fixture | score | per book |
|---|---|---:|---|
| A3B UD-IQ3_XXS 13.2 GB, michel2_full, reasoning low, experts in RAM (RX 9070 XT) | nine PDNC novels, 2,655 rows | **91.6** (0 blank) | Emma 97.8, Northanger 97.3, S&S 96.0, Persuasion 94.5, Awakening 93.4, P&P 93.2, Mansfield 89.3, Sign of the Four 82.0, Sun Also Rises 81.2 |
| same, `default` prompt | same, partial 1,200 rows (cell interrupted, resumes in the make-up chain) | 73.9 | Sun Also Rises 66.4, Emma 78.3, P&P 80.4, Mansfield 71.0 |

`lora_serving_eval__a3b-iq3xxs-michel2_full-local-9070xt-pdnc9lite-low-schema-20260917.json`.
The two hard books are Doyle and Hemingway - terse, sparsely attributed
dialogue - the same shape DeepSeek showed (Sun Also Rises its low book at
92.1). Higher than its four-book 88.0: the light novels are the harder
fixture. Against its own `default` control on the same rows the prompt is
worth ~18 points on books this project never tuned on.

