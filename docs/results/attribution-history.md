# Attribution results: the README's earlier detail

This page holds the long results tables that used to sit at the top of the
README. They were moved here on 2026-09-28 when the README was rewritten for
new readers. The text below is unchanged from that README.

> **Read the current verdicts first.** Several rows below were overtaken on
> 2026-09-28:
>
> - PDNC lists some names that never speak (The Sign of the Four's "Small"
>   beside the Wooden-Legged Man). Counting those short answers as correct
>   changed ten adapter verdicts — see RECIPES.md, "Rescored 2026-09-28".
> - The A3B rows marked "unmeasured" belong to the older `michel2v1` adapter.
>   Its window25 successor is scored in
>   [Which model for your card](../wiki/Which-Model-For-Your-Card.md).
>
> The current per-card recommendations and adapter verdicts live in that wiki
> page and in [RECIPES.md](../../RECIPES.md).

### Which model for your card

Same measurement as above — nine novels, 2,655 rows, `michel2_full`, no adapter.
Full ladder and the reasoning: [Which model for your card](../wiki/Which-Model-For-Your-Card.md).

| your card | typical cards | run this | file | base | adapter? |
|---|---|---|---:|---:|---|
| 32 / 24 GB | RTX 5090, 4090, 3090 | Qwen3.8-27B UD-Q3_K_XL | 12.5 GB | **95.2%** | **yes — 96.0%** |
| **16 GB** | RTX 5080, 5070 Ti, RX 9070 XT | Qwen3.8-27B UD-Q3_K_XL | 12.5 GB | **95.2%** | **yes — 96.0%** |
| 12 GB | RTX 5070, Arc B580 | Qwen3.8-27B **Q2_K_XL** | 9.4 GB | **93.7%** | untested at this rung |
| 8 GB | RX 9060 XT 8 GB, RTX 5060 | Qwen3.8-27B UD-IQ2_XXS | 6.9 GB | 88.7% | **+1.0** (p=0.059) |
| 6 GB | GTX 1660, RTX 2060, laptops | Qwen3-8B Q4_K_M | 4.7 GB | **77.0%** *(four-book)* | **yes — +5.3** *(four-book, p=0.0006)* |
| no usable GPU | — | hosted model, or the manual transport | — | **94.9–95.4%** | n/a |

**16 GB is the sweet spot and nothing above it helps** — a 5090 runs the same
12.5 GB file to the same 95.2%. At 8 GB you can still run a 27B: UD-IQ2_XXS is
6.9 GB and beats a 14B at Q4_K_M by 4.4 points.

**An adapter's value depends on how degraded the base is** (2026-09-25, four
model families). Below ~35% base it is worth +57 to +62 points (Muse IQ2_XXS, on two
vendors' GPUs) — it restores the ability to answer at all. *(Corrected 2026-09-27: the
Gemma E2B/E4B "+34 / +51" once quoted here came from base files missing their chat
template; see RECIPES §"A fourth family".)* Between 85 and 90% it is worth +1 to +4, rarely
significant. **Above ~90% it costs you 1–3 points.** So: load one where the base
is visibly failing — blank replies, malformed JSON, wrong entry counts — not to
chase a few points on a base that already works.

The earlier reading here ("adapters usually lose; A3B and Muse cost 3.2 to 27.5
points") is withdrawn. Those A3B and Muse gen-3 numbers came from adapters
trained on **single-entry** examples and served with a 25-entry contract; they
measure that mismatch, not the recipe. See RECIPES §"The A3B adapter was trained
on a different task".

Three measured surprises: **Q3_K_XL beats Q4_K_M** on Qwen3.8 (95.2 vs 94.9) for
3 GB less, so don't pay for Q4; a **small quant of a big model beats a big quant
of a small model** (27B at IQ2_XXS, 6.9 GB, 88.7% vs 14B at Q4_K_M, 9.0 GB,
84.3%); and **context length, not file size, decides whether a model fits** — the
same 5.4 GB model needs 5.1 GB at `-c 4096` and 11.3 GB at `-c 32768`, while the
product prompt's longest window is 5,966 tokens, so `-c 8192` is enough.

### Adapters — trained on rights-clean data only

LoRA adapters for the attribution task, trained on 20 public-domain PDNC
novels, RiQuA and CC0 play scripts rendered as prose (no light-novel text,
nothing you could not redistribute). Public on the Hub:
[Om22s/alexandria-qwen3-attribution](https://huggingface.co/Om22s/alexandria-qwen3-attribution).

| adapter | base GGUF / size | reasoning | nine PDNC novels (2,655 rows) | four-book fixture | Emma (held out, 318 rows) |
|---|---|---|---:|---:|---:|
| Qwen3.8-27B `michel2` | IQ2_XXS 7.3 GB | on | queued | 83.1 → **85.7 (+2.6)** | 91.2 → 92.1 |
| Qwen3.8-27B `michel2` | Q3_K_XL 13.1 GB | on | queued | 87.5 → **88.8 (+1.3)** | 99.4 → 99.7 |
| Qwen3.8-27B `michel2` | Q4_K_M 16.5 GB | on | 94.9 → **95.9 (+1.0)**; strict paired p=0.057 | 89.8 → 89.8 | 99.7 → 99.4 |
| Qwen3.8-27B `michel2` | Q4_K_M 16.5 GB | off | 93.5 → **95.8 (+2.4)**; held-out eight: 94.9 → **97.1** | 87.4 → **88.3 (+0.9)** | 99.1 → 99.1 |
| ⚠️ A3B `michel2` **(unmeasured)** | IQ1_M 10.0 GB | off | 89.4 → 85.7 (−3.7); held-out eight: 91.4 → 88.9 | 82.9 → **85.9 (+3.0)** | 97.8 → 96.2  |
| ⚠️ A3B `michel2` **(unmeasured)** | IQ1_M 10.0 GB | on | queued | 87.5 → 85.0 (−2.5) | 96.2 → 98.1  |
| ⚠️ A3B `michel2` **(unmeasured)** | IQ2_XXS 10.8 GB | on | 91.6 → 88.3 raw; 91.3 offline echo-strip | 88.3 → 87.9 (−0.4) | 95.6 → 95.3  |
| ⚠️ A3B `michel2` **(unmeasured)** | IQ3_XXS 13.2 GB | on | 91.6 → 71.3 raw / **90.9 app-read** | 87.5 → 86.6 (−0.9) | 95.6 → 82.7 raw / 93.1 app-read  |
| ⚠️ A3B `michel2` **(unmeasured)** | Q4_K_XL 22.4 GB | on | 92.1 → 64.6 raw; 91.9 offline echo-strip | 90.8 → 88.5 (−2.2) | 96.9 → 95.3 raw / 98.1 app-read  |
| Qwen3-14B rights-clean, seed 1, `default` | Q4_K_M 9.0 GB | low, budget 1024 | 67.6 → **72.6 (+5.0)**; held-out-eight replication 72.2 → **74.9 (+2.7)** | 66.1 → **74.7 (+8.6)** | 68.9 → 75.8 |
| Qwen3-14B rights-clean, seed 2, `default` | Q4_K_M 9.0 GB | low, budget 1024 | queued | 66.1 → **74.9 (+8.8)** | 68.9 → 69.5 |
| Qwen3-14B rights-clean, seed 1, `michel2_full` | Q4_K_M 9.0 GB | low, budget 1024 | 84.3 → 84.3 (+0.1) | — | 89.6 → 95.9; P&P 88.9 → 77.0 |
| **Muse window25 `michel2_full`** | IQ2_XXS 7.4 GB | low | — | **15.9 → 77.8 (+61.9)** | — |
| **Muse window25 `michel2_full`** | IQ3_XXS 10.4 GB | low | 93.0 → 93.2 (+0.2) | 85.8 → **90.3 (+4.5)** | — |
| **Muse window25 `michel2_full`** | IQ3_M 11.9 GB | low | 91.7 → 92.1 (+0.4) | 85.2 → **89.2 (+4.0)** | — |
| **Muse window25 `michel2_full`** | Q3_K_XL 12.4 GB | low | — | 88.6 → 90.3 (+1.7) | — |
| **Muse window25 `michel2_full`** | Q4_K_M 15.6 GB | low | **94.6 → 93.6 (−1.0, p=0.037)** | 90.9 → 88.1 (−2.8) | — |
| ⚠️ Muse Gen 3 RFT **(unmeasured)** | Q3_K_XL 13.4 GB | low | eight-novel pilot, 631 rows: scale 0.25 **92.9 → 96.0**; 0.5 null; 1.0 92.9 → 89.4 | — | —  |

Every nine-novel cell uses the same fixed panel, 40 windows per novel and
2,655 labelled rows. `queued` means that exact paired measurement is running,
not that a four-book result was silently substituted. The Sun Also Rises is
one of the 20 training novels, so the nine-novel column is a consistent stress
test rather than a purely held-out estimate; Emma remains the clean held-out
check. Four-book rows are 768 paired rows on one server with the adapter scale
toggled (RECIPES §"Attribution adapters", 2026-09-20). The adapters were built
to lift the base models — the prompt did most of that — so their job now is
**how small a quant can ship**, and the answer so far: the Qwen3.8 adapter
earns its place at IQ2_XXS and Q3_K_XL, where the gain grows as the quant
shrinks; Qwen3.8 Q4 also gains 2.4 points with reasoning off on the nine-novel
panel. **Every A3B row above, and the Muse Gen 3 row, is UNMEASURED.** Both adapters
trained on single-entry examples and were served with a 25-entry contract, so
those numbers describe the mismatch rather than the recipe — including the
roster-line echo on 21% of nine-novel rows, which is most likely a symptom of
the same cause: a model trained only on one-element arrays never learned rule
6's main-form convention. A3B is retraining on the corrected window shape. The
Qwen3-14B adapter's +8.7 under the `default` prompt is a null under the product
prompt. The A3B IQ2_XXS and Q4_K_XL nine-novel raw scores fall mainly because
the adapter copies roster `NAME (also: …)` entries. For Q4_K_XL, stripping
that tail recovers 727 exact-name rows (64.6% raw → 91.9% offline), nearly
matching the 92.1% base. The run used source commit `4d337725`, before the
app's roster-echo fix; this offline rescore is not a fresh current-app run.
The Qwen3.8 Q4_K_M reasoning-on gain is one paired
run and does not clear the strict shared-row p<0.05 threshold. Raw artifacts
and paired counts are in [RECIPES.md](../../RECIPES.md#september-23-nine-novel-paired-follow-up).
The Muse **window25** rows are the ones to read: that adapter is the rebuild
that fixed the gen-3 contract defect, and it is the best-evidenced adapter here
— seven paired cells across two fixtures, three GPUs and two vendors. Its shape
is the dose-response above: **+61.9 at IQ2_XXS** where the base cannot hold the
contract, **+4.0 to +4.5** on four-book IQ3, and **−1.0 (p=0.037)** at Q4_K_M
where there is nothing to repair. Note the four-book IQ3 gain (+4.3 pooled,
p=0.014) does **not** replicate on nine novels (+0.3, p=0.489) — quote the
fixture every time.
[`ATTRIBUTION_ADAPTER_SETUP.md`](../guides/ATTRIBUTION_ADAPTER_SETUP.md) says how to load one.

The separate eight-book PDNC pilot for Qwen3.8 IQ2_XXS measured 89.6% base
vs 91.1% with the rights-clean adapter (+1.5 points; 2,310 rows). It is not
the nine-book panel above and remains pilot evidence, not a release-wide
claim; see [RECIPES.md](../../RECIPES.md#september-22-follow-up-low-quant-adapter-and-nemotron-api-baselines).
