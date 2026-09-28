# Prompts and adapters

The rules every attribution adapter must follow, and what the measurements say about when an
adapter is worth loading.

## Rule 1: train on the window the product actually sends

**This has wasted more adapter runs than anything else in the project.** A training set can
carry the right prompt *name* and still present a completely different task. Nothing
downstream notices: the adapter trains, converts, serves and produces plausible numbers.

It has happened twice, to two different model families:

| adapter | trained on | served with | cost |
|---|---|---|---|
| Muse gen-3 | **1 line** per example | 25 lines plus 2,000 characters of surrounding text | caught before training, but 61 hours of sampling were unusable |
| A3B rights-clean (2026-09-17) | **1 line** per example, `max_length 2048` | the same | published; **every result it produced measured the mismatch, not the recipe** |

The A3B case is the warning. It passed its own preflight, served cleanly in every arm, and its
card reported careful, honest-looking numbers — flat to negative at every quant. For eight days
those were read as a verdict on the recipe. They were a verdict on the mismatch.

**Check the window, not the label.** One comparison catches it:

```
lines per training example  vs  lines the serving prompt asks for
```

If they differ, stop. The fix needs no new data: the window builder is deterministic and works
for any base model, so the same books re-render into the right shape in seconds.

## Rule 2: an adapter belongs to its prompt

Train on `default`, serve with `default`; train on `michel2_full`, serve with `michel2_full`.
Serving under another prompt makes the run a **transfer test**, and it must be labelled as one.

Every published adapter's name ends with the prompt it was trained on (`attrv1`, `attrv2`,
`michel2v1`, `michel2v2`) — see [Hugging Face releases](Hugging-Face-Releases.md).

## The prompt variants

| variant | what it sends | notes |
|---|---|---|
| `default` | the plain batch request | no surrounding text |
| `michel` | the cast list and the passage | does not use `MICHEL2_SYSTEM` |
| `michel2` | the cast list and a marked passage | |
| **`michel2_full`** | **a marked passage plus 2,000 characters of surrounding text** | **the product prompt** |
| `michel2_shot` | `michel2` plus a worked example | |

`michel2_full` is the only variant that marks narration lines `[n]`. That matters: rewording
`MICHEL2_SYSTEM` in #616 moved `michel2_full` by **+5.2** (Qwen3-8B, p<0.001) and **+3.0**
(Qwen3.5-9B, p=0.025), and made no measurable difference to `michel2` or `michel2_shot` on
either model — nor to Muse at all (695 of 768 lines both before and after).

## When an adapter helps, and when it costs you

Across four model families, an adapter's gain tracks **how degraded the base model is**, not
how good the adapter is:

| base score on this task | what the adapter does |
|---|---|
| **below about 35%** (the output format is collapsing) | **+57 to +62 points** — it restores the ability to answer at all |
| 85–90% | +1 to +4, often not significant |
| **above about 90%** | **can cost 1–3 points** |

Examples from each end: Muse IQ2_XXS **15.9 → 77.8** (A6000) and **22.2 → 79.0** (RX 9070 XT);
Muse Q4_K_M **90.9 → 88.1**.

*Corrected 2026-09-27 and 2026-09-28:*

- The Gemma E2B example once given here (7.4 → 58.5) came from a base file with no chat
  template, so it measured a packaging fault, not Gemma.
- Gemma 12B's four-book 86.9 → 85.2 was 3 lines fixed against 6 broken (p=0.51) — noise. On the
  nine novels the same adapter is +1.1 (p=0.09) once PDNC's silent short names are scored (it
  read +2.1 before).

**How it works**, measured line by line on 2,655 lines: the adapter recovers about **half** of
what quantisation breaks, keeps **97%** of what was already right, and breaks about **3.4%** of
those correct lines at every quant. Where there is nothing to recover, only the breakage shows.

**Practical rule:** if your base model already scores above about 90% on this task, an adapter
will probably cost you. Load one where the base is visibly failing — blank replies, malformed
JSON, the wrong number of lines — not to chase a few points on a model that already works.

## The test set changes the answer

The same adapter, the same quants, two test sets:

| | four-book set (352 lines) | nine-novel PDNC set (5,310 lines) |
|---|---|---|
| pooled IQ3 | **+4.3, p = 0.014** | **+0.3, p = 0.489** |

With fifteen times the lines the effect disappears — because the nine-novel base scores 92.4
and the four-book base 85.5, on either side of the point where the adapter stops paying.
**Never promote on one test set**, and always say which one a number comes from.

The scorer matters too. On 2026-09-28, counting correct short names such as SMALL for The Sign
of the Four's Wooden-Legged Man changed ten adapter verdicts. Some adapters had been credited for
learning PDNC's labelling habit; one had been hiding real mistakes (RECIPES §"Rescored
2026-09-28").

Hardware matters less, but not zero: the same cell on an A6000 and an RX 9070 XT moved the
measured change from +4.5 to +1.1, while two A6000s with the same build agreed exactly (0 of 352
predictions differed).

## Where the open questions stand (updated 2026-09-28)

- **A3B retrained on the right window shape — done.** The window25 adapter helps only at IQ1_M
  (+1.5, p=0.01); it is flat at IQ2_XXS and IQ3_XXS, and costs 2.1 points at Q4_K_XL (p=0.00015).
  Load it at IQ1_M only.
- **Training in 4-bit (QLoRA) — done.** It cut the breakage by 18% (105 → 86 lines), but not
  significantly (p=0.099), and repair fell by about as much, so the net did not move.
- **Holding the adapter close to the base (KL penalty) — in progress.** Two seeds agree at IQ3_M
  and Q3_K_XL, but disagree at IQ3_XXS (seed 1 −1.5 on an A6000, seed 2 +1.5 on an A100). A
  run of seed 2 on the A6000, to separate seed from GPU, is queued.
- **Roster length.** The adapter's gain ran +2.4 with 26–40 candidate names and −4.1 with 71 or
  more, and the training data under-represents the 56–70 band by 4.7×. A test capping the
  offered cast list at 30 was started; its result is not yet written up in RECIPES.

Recipes, artifacts and per-book splits: [RECIPES.md](../../RECIPES.md). Current results:
[Results](Results.md).
