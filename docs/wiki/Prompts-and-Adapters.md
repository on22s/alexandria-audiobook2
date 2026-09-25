# Prompts and adapters

## The one rule: train on the window the product actually serves

**This is the single largest source of wasted adapter runs in this project.** A
training set can carry the right prompt *variant name* and still present a
completely different task, and nothing downstream will notice — the adapter
trains, converts, serves, and produces plausible numbers.

It has now happened twice, to two different model families:

| adapter | trained on | served with | cost |
|---|---|---|---|
| Muse gen-3 | **1 entry** per row | 25 entries + 2,000-char surround | blocked pre-training; 61 h of sampling unusable |
| A3B rights-clean (2026-09-17) | **1 entry** per row, `max_length 2048` | same | shipped publicly; **every row it ever produced is unmeasured** |

The A3B case is the cautionary one. It passed its own preflight, served cleanly
in every arm, and its card reported careful, honest-looking numbers — flat to
negative at every rung — that were read for eight days as a verdict on the
recipe. They were a verdict on a contract mismatch.

**Check the window, not the label.** A one-line check catches it:

```
entries per training example  vs  entries the serving prompt asks for
```

If those differ, stop. The fix needs no new data collection — the window
builder is deterministic and base-agnostic, so the same source books re-render
into the right shape in seconds.

## The variants

| variant | what it sends | notes |
|---|---|---|
| `default` | the canonical batch request | no surround |
| `michel` | roster + passage | does not use `MICHEL2_SYSTEM` |
| `michel2` | roster + marked passage | |
| **`michel2_full`** | **marked passage + 2,000 chars of surround** | **the product prompt** |
| `michel2_shot` | `michel2` plus a worked example | |

`michel2_full` is the only variant that sends narration entries marked `[n]`.
That matters: #616's reframing of `MICHEL2_SYSTEM` moved `michel2_full` by
**+5.2** (Qwen3-8B, p<0.001) and **+3.0** (Qwen3.5-9B, p=0.025) and did nothing
measurable to `michel2` or `michel2_shot` on either base — and nothing at all to
Muse (695/768 both before and after).

**Adapters are prompt-specific.** Train on `default`, serve with `default`;
train on `michel2_full`, serve with `michel2_full`. Changing the prompt turns
the run into a transfer test, and it should be labelled as one.

## When an adapter helps, and when it costs you

Measured across four model families, the gain tracks **how degraded the base
is**, not how good the adapter is:

| base score on the fixture | what the adapter does |
|---|---|
| **below ~35%** (contract collapsing) | **+34 to +62 points** — it restores the ability to answer at all |
| 85–90% | +1 to +4, rarely significant |
| **above ~90%** | **negative** — it costs 1–3 points |

Examples at each end: Muse IQ2_XXS **15.9 → 77.8** and Gemma E2B **7.4 → 58.5**
against Muse Q4_K_M **90.9 → 88.1** and Gemma 12B **86.9 → 85.2**.

The mechanism, measured at row level on 2,655 rows: the adapter recovers about
**half** of what quantisation breaks, preserves **97%** of what was already
right, and charges a **3.4% collateral** on that preserved majority at *every*
rung. Where there is no damage to recover, only the collateral shows.

**Practical rule: if your base already scores above ~90 on this contract, an
adapter will cost you.** Load one where the base is visibly failing — blank
replies, malformed JSON, wrong entry counts — not to chase a few points on a
base that already works.

## The fixture changes the answer

This is not a footnote. The same adapter, the same rungs, two fixtures:

| | four-book gold (352 rows) | nine-novel PDNC (5,310 rows) |
|---|---|---|
| pooled IQ3 | **+4.3, p = 0.014** | **+0.3, p = 0.489** |

Fifteen times the rows and the effect disappears, because the nine-novel base
sits at 92.4 and the four-book base at 85.5 — above and below the point where
the adapter stops paying. **Never promote on a single fixture**, and quote which
one every time.

Hardware matters less but is not free: the same cell on an A6000 and an RX
9070 XT moved the *measured delta* from +4.5 to +1.1, while two A6000s of the
same build are bit-identical (0 of 352 predictions differ).

## Open, as of 2026-09-25

- **Roster length.** The adapter's gain runs +2.4 at 26–40 candidates and −4.1
  at 71+, monotone across two independently measured cells. The training data
  under-represents the 56–70 band by **4.7×**, which is where it does worst. A
  controlled test (capping the offered roster at 30) is running.
- **Quantisation-aware training.** Whether training against a 4-bit base reduces
  the 3.4% collateral. Running.
- **A3B retrained** on the correct window shape. Running.

Recipes, artifacts and per-book splits: [`RECIPES.md`](../../RECIPES.md).
