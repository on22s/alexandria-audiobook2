# Findings brief: nine papers, play scripts, and the usual-suspect prior

Dates: 2026-09-13 and 2026-09-14. Everything below is a measured number with
its artifact, or is marked as an inference. Per-goal detail lives in
`GOALS.md` (1.2 attribution selection, 2.7 voice fidelity) and settings that
worked or failed in `RECIPES.md`; this is the one-page record of what the
batch taught.

Instrument, unless stated: the product harness `lora_serving_eval.py` at
batch 25, four clean-gold books (grimgar03, index18, mushoku16,
owarimonogatari3, 768 rows), paired base/LoRA on one server, temperature 0.
Voice fidelity: `library_voice_fidelity.py`, ECAPA, 20 val lines.

## What changed a decision

| # | finding | numbers | artifact / recipe |
|---|---|---|---|
| 1 | **Curated-small beats big-mixed** for attribution adapters (Maekawa et al., LREC 2026) | hardcases-only 61.7 → 66.4, **+4.7** (p=0.007): 7% of the rows give 60% of the 29-file mixed set's +7.8 | `lora_serving_eval__qwen3-14b-hardcases-a6000-product-batch25-20260913.json`; RECIPES row |
| 2 | **The usual-suspect prior is real and dose-dependent** (cue probe in the spirit of Li et al.) | minor-speaker rows 57.2 → 63.7 with the top-2 speakers removed from the cast, 59.8 → 69.4 with top-5; 52% of full-cast errors name a lead | `two_stage_attribution__usual_suspects_k{2,5}_*_20260913.json`; GOALS 1.2 |
| 2a | A one-line prompt hint ("do not assume the lead") recovers part of it, **book-dependent** | P&P minor rows 62.8 → 66.2 (removal ceiling 68.1); Sign of the Four 43.1 → 43.1 (ceiling 52.2); Awakening 54.5 vs control (see GOALS 1.2 once the all-rows pass lands) | `two_stage_attribution__usual_suspects_hint_20260914.json` |
| 3 | **Muse-Glimmer 30B serves and helps at the product window** once llama.cpp `b6b003d2c` fixed the PEG-native 500s | task4k-multin tplfix seed 2: **+4.4**, 768/768 answered | `lora_serving_eval__muse-glimmer-30b-task4k-multin-tplfix-seed2-tnr0-product-batch25-q3-jsonschema-reasoningnone-20260913.json`; RECIPES serving row with build hash |
| 4 | **Prosodic pruning is a cleaner, not a repair** (Chalamandaris et al., LREC 2014) | +0.06–0.10 ECAPA on the two mid-range REBUILD datasets, nothing or harm on the three different-people mixtures; two control retrains agree within 0.04 | `prune_retrain__<adapter>__fidelity.json` ×5; GOALS 2.7 |
| 5 | **Quotes-vs-narration training data is not a lever** (Piits et al. 2022; LibriQuote 2026) | reader 4992: narration wins both held-out columns; reader 2033: quotes wins both — the sign flips by reader | `run_chains/libriquote_{4992,2033}_20260913.sh` artifacts; GOALS 2.7 |
| 6 | **ReadAlong forced alignment** finds boundaries whisper.cpp misses in Japanese (SIGUL 2022) | 10/10 boundaries in EN/JA/ZH (whisper.cpp base: 5/10 JA); consistent 0.28 s early bias, RTF 0.03 | `readalong_probe.py` output in the ASR bench dir; not yet in the preparer |
| 7 | **novelshare hashes can publish the light-novel gold** (Amalvy et al., ACL 2026) | identical edition 100%; 3.5% edition drift → 76–86% of quote spans realign (index18 66%); index18's real 2026-08-19 damage → 30–40% | `novelshare_probe.py` output; memory `papers_20260913_review` |
| 8 | **Play scripts (DraCor, CC0) teach nothing the product window uses** — first clean evidence the losing books need *frame* training | English plays 61.7 → 57.8, **−3.9** (p=0.034); mixed 6-language 61.7 → 62.2, +0.5 (p=0.82); only mushoku16 (bare dialogue) gains, +7/+12 of 133 | `lora_serving_eval__qwen3-14b-dracor-{en,mixed}-a6000-product-batch25-20260913.json`; GOALS 1.2 registered prediction + result; PR #558 |
| 9 | **Qwen3.8-27B author-balanced adapter hurts at batch 25** and it is mostly the output contract | −2.9, 54 windows blank; JSON-schema rerun in flight on tnr-0 | `lora_serving_eval__qwen38-author-heldout-balanced-tnr0-product-batch25-*.json`; RECIPES row |

## Nulls worth not repeating

- **Garble floor is not the shipped-vs-retrain gap.** `GARBLE_FLOOR = 4.1`
  discards epochs below 4.1 in target-loss mode; retraining with no floor
  for 4 epochs gives ECAPA 0.315/0.319 (grad-accum 4) and 0.335/0.337
  (grad-accum 8), inside the 0.29–0.34 band of floored retrains, far from
  the shipped 0.40/0.50. `floor_test__nofloor_4ep{,_ga4}__fidelity.json`.
  The gap is still in the original runs' settings — undiagnosed.
- **Chapter-aware chunking (issue #522 §10) can barely reach the product's
  attribution window.** In the clean-gold segmentation most chapter
  headings sit inside a narration entry (owarimonogatari3: 1 of 11 starts
  an entry), so window cuts touch 28 of 768 gold rows. A/B (fixed vs
  chapter cuts vs matched random cuts) running on tnr-1; implementing #522
  §10 properly means splitting headings at segmentation, not at windowing.
  `app/experiments/chapter_cuts.py`, `chapter_cuts_cleangold_20260914.json`.
- **Epistolary novels as a labelled source**: dropped before building. A
  letter header labels the narrator of a passage; the product never
  attributes narration, so the rows would not be this task.
- Boeffard boundary-conflict audit: built and unit-tested
  (`boundary_conflict_audit.py`), not yet run on a preparer output.
- Sini et al. (PPG voice conversion), Li et al. (Luxembourgish grammar
  probing) directly: no use here beyond the cue-probe idea.

## Queued from these findings (2026-09-14)

- **Play rows with template narrative frames** (`dracor_prose.py`): speaker
  from the TEI, frame shapes copied from the product's own segmentation
  (bare / post-frame / pre-beat, plus the neighbour's frame as the
  confusable case). Training on tnr-2, same recipe and harness as #8.
- **RiQuA adapter** (`riqua_trainset.py`): 2,180 rows whose speaker is a
  hand-annotated span, on public-domain novels *with* frames; Emma excluded
  (evaluation fixture). Queued behind the prose arm on tnr-2.
- Chapter-cut A/B (tnr-1), Qwen3.8 JSON-schema rerun (tnr-0), hint
  all-rows pass and reasoning-on pass (local card).

## Rules this batch paid for

- A chain's `pgrep -f <pattern>` wait matched the session's own
  `tail -F <pattern>.log` monitor and idled the card (Rule 22, detection
  variant). Wait on a completion line in the log, not a process name.
- The window-level test of a chunking idea is only as wide as the
  segmentation lets it be: count the rows a change can touch before running
  the A/B (28 of 768 here), or the pooled number measures phase shift.
