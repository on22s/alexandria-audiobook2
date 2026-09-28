# Which model for your card

Every accuracy figure here is the **same measurement**: nine public-domain
novels, 2,655 speaker-labelled rows, the `michel2_full` prompt, batch 25,
request-level JSON schema, temperature 0, reasoning effort low, no adapter.
Nothing is extrapolated between rows. Where a number does not exist yet the row
says so rather than guessing.

**One exception, marked where it appears:** the 6 GB row quotes the four-book
fixture, because **no 6 GB model has yet been measured on the nine-novel panel
with this instrument**. The one nine-novel figure that exists for a small model
(Qwen3-8B, 33.7%) was taken at `batch 1` with the `default` prompt — two
variables away from every other row here, and `default` alone costs this base
16 points. It is not comparable and is not quoted. Mixing fixtures silently is
how a +4.3 became a +0.3 elsewhere in this project, so that row is labelled
rather than blended in. The matching nine-novel cell is queued.

## Read this first: context length decides whether a model fits

File size is not the memory you need. The KV cache is, and it scales with the
context you ask for. Measured on the same 5.4 GB model:

| context | total VRAM |
|---|---:|
| `-c 4096` | **5.1 GB** |
| `-c 32768` | **11.3 GB** |

The same file needs twice its own size at 32k and barely more than its own size
at 4k. The product prompt's longest window measures **5,966 tokens**, so
**`-c 8192` is enough** — and choosing 8192 over 32768 is worth several
gigabytes, which is frequently the difference between fitting on your card and
not. Set it deliberately.

## The recommendations

| your card | typical cards | run this | file | base | adapter? |
|---|---|---|---:|---:|---|
| **32 GB** | RTX 5090 | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** | **yes — 96.0%** |
| **24 GB** | RTX 4090, 3090 | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** | **yes — 96.0%** |
| **16 GB** | RTX 5080, 5070 Ti, RX 9070 XT / 9070, 9060 XT 16 GB | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** | **yes — 96.0%** |
| **12 GB** | RTX 5070, Intel Arc B580 | Qwen3.8-27B **Q2_K_XL** | 9.4 GB | **93.7%** | **+0.2** (93.7 → 93.9, p=0.711) |
| **8 GB** | RX 9060 XT 8 GB, RTX 5060 | Qwen3.8-27B **UD-IQ2_XXS** | 6.9 GB | 88.7% | **+1.0** (89.0 → 90.0, p=0.059) |
| **6 GB** | GTX 1660, RTX 2060, laptop cards | Qwen3-8B **Q4_K_M** | 4.7 GB | **77.0%** *(four-book, see note)* | **yes — +5.3** (77.0 → 82.3, p=0.0006, four-book) |
| **no usable GPU** | — | a hosted model, or the manual transport | — | **94.9–95.4%** (DeepSeek v4-pro) | n/a |

## If speed matters more than the last few points: Qwen3.6-35B-A3B

The table picks the most accurate file for each card. It does not pick for speed, and the
speed gap is large. A3B is a mixture-of-experts model (35B weights, about 3B active per
token), so it runs at small-model speed with near-large-model accuracy. Measured
2026-09-27 on one RX 9070 XT, same instrument for every row (28 PDNC novels, 12 windows
each, `michel2_full`, reasoning low, base only), end-to-end time per 25-entry window:

| base | file | accuracy | time per window | whole 28-book run |
|---|---:|---:|---:|---:|
| Qwen3-8B Q4_K_M | 5.0 GB | 77.4% | 18.6 s | 1.7 h |
| Qwen3.5-9B Q4_K_M | 5.7 GB | 77.4% | 23.3 s | 2.2 h |
| **Qwen3.6-35B-A3B UD-IQ1_M** | 10.0 GB | **88.8%** | **19.7 s** | **1.8 h** |
| **Qwen3.6-35B-A3B UD-IQ2_XXS** | 10.8 GB | **87.5%** | **20.2 s** | **1.9 h** |
| Qwen3.8-27B UD-Q3_K_XL | 13.1 GB | 93.7% | 49.0 s | 4.6 h |

A3B runs as fast as the 8B models while scoring about 11 points higher, and **about 2.4×
faster than Qwen3.8** for about 6 points less. Pick it when throughput matters (a whole
library, re-running books, a card shared with other work) and the 16 GB recommendation
when accuracy does. Its window25 adapter (`qwen3.6-35b-a3b-rightsclean-window25-michel2v2`)
adds +1.5 at IQ1_M on nine held-out novels and is flat to negative above it (rescored 2026-09-28, see RECIPES). These are one card and one
run each; Qwen3.8's smaller files (IQ2_XXS, Q2_K_XL) were not timed on this instrument, and a
dense 27B will not reach MoE speed at any quant — but that last point is expected, not measured.

**16 GB is the tier most people are on** — it covers the RTX 5080 and 5070 Ti and
AMD's entire RX 9070 line — and it is comfortably the best value on this page.
Qwen3.8-27B UD-Q3_K_XL is 12.5 GB, so at `-c 8192` it lands near 13 GB with room
to spare, scores **95.2%**, and takes the adapter to **96.0%**. Nothing above
16 GB buys a better number; a 5090 runs the same file to the same score.

**12 GB got better.** The Qwen3.8 **Q2_K_XL** cell that was running when this
page was written has landed: **93.7%** at **9.4 GB** — a gigabyte smaller than
the Muse IQ3_XXS previously recommended here *and* 1.1 points ahead of it. It is
now the answer for this tier, with real headroom at `-c 8192`.

On adapters at this tier: the earlier advice cited a **−3.6** cost, but that
figure came from the gen-3 Muse adapter, which was trained on single-entry
examples and served with a 25-entry contract — its rows measure that mismatch,
not the recipe (RECIPES §"The A3B adapter was trained on a different task"
records the same defect). The corrected window25 adapter measured on this
fixture is **+0.2 (p = 0.838)** at IQ3_XXS, **+0.4 (p = 0.474)** at IQ3_M, and
**+0.2 (p = 0.711)** at the Q2_K_XL now recommended here — six rows out of 2,655,
94 improved against 88 regressed. Dropping *The Sun Also Rises*, which is in the
adapter's own training set, turns that into **−0.6 (p = 0.268)** across the eight
genuinely held-out novels. So the advice stands — *don't bother with an adapter
here* — but because it does nothing, not because it costs you 3.6 points.

One caveat on those three figures, because this project has been bitten by it
before. They were not all measured with the same pass-2 prompt: the IQ3_XXS cell
ran on the A100, which carries the post-#616 `michel2_full` text (2,071 chars),
while the IQ3_M and Q2_K_XL cells ran on A6000s still carrying the pre-#616 text
(1,825 chars). Across this fleet prompt and GPU architecture are perfectly
confounded, which is exactly why the earlier cross-GPU claim was retracted, so
read the three as three separate small nulls that happen to agree rather than as
one trend measured on one instrument. The recommendation does not turn on it —
every arm says the same thing — but the rungs are not strictly comparable and
this page should not imply they are.

**8 GB is better served than you would expect.** Qwen3.8-27B at UD-IQ2_XXS is
6.9 GB and **88.7%** — a 27B model on an entry-level card, and 4.4 points ahead
of a 14B at Q4_K_M that would not fit anyway.

**6 GB: pick a small model that works, not a shrunken large one.** Qwen3-8B
Q4_K_M is 4.7 GB and scores **77.0%** on the four-book fixture — the best of the
small models, and it fits with headroom at `-c 8192`. Shrinking a big model
instead does not work: Qwen3.8 `Q1_L` is **larger** at 5.7 GB and scores
**30.1%** on nine novels, and Muse `Q1_0` (4.5 GB) could not finish a cell.

**Load the adapter at this tier.** The Qwen3-8B window25 adapter takes it from
**77.0% to 82.3% (+5.3, p=0.0006)** on the same four-book rows — it fixes 90 of
the base's 177 errors and breaks 49 of its 591 correct rows. The breakage rate
(8.3%) is the highest measured for any adapter, so the gain is real but not
free; see RECIPES §"The adapter does two separable things".

What is *not* yet known is how Qwen3-8B does on the harder nine-novel panel at
this instrument — that cell (base and adapter, paired) is running now. Until it
lands, treat both numbers as four-book and do not assume they transfer.

### Three things that surprised us, all measured

**Do not pay for Q4.** Qwen3.8-27B at Q3_K_XL (12.5 GB) scored **95.2%** against
Q4_K_M (15.7 GB) at **94.9%**. Four gigabytes bought nothing; the difference is
inside the noise. The same holds for Muse: Q4_K_M 94.6% against Q3_K_XL 93.3%
for 3 GB more.

**A small quant of a big model beats a big quant of a small model.** Qwen3.8-27B
at IQ2_XXS is **6.9 GB and 88.7%**. Qwen3-14B at Q4_K_M is **9.0 GB and 84.3%**.
The smaller file of the larger model wins by 4.4 points. If you are choosing
between "a 14B I can run comfortably" and "a 27B I can just barely squeeze in",
squeeze in the 27B.

**The cliff is between Q3 and IQ2, not at Q4.** Qwen3.8 holds 95.2% at Q3_K_XL
and falls to 88.7% at IQ2_XXS — one step, 6.5 points. Everything above that step
is nearly flat. If you can reach 12.5 GB, reach it; below that, expect a real
cost rather than a gentle slope.

### 6 GB

This tier matters — plenty of working cards are 6 GB, and it is where the app's
users actually hit problems first.

Two local candidates were measured on the nine-novel fixture to see whether a
shrunken large model beats a small one here. Qwen3.8 **Q1_L** (5.7 GB) has
landed at **30.1%** — it is both *larger* than the Qwen3-8B Q4_K_M recommended
above and less than half as accurate, so squeezing the 27B down to 1 bit is not
the move at this tier. Muse **Q1_0** (4.5 GB) produced no result: three attempts, about 37
card-hours on two boxes, and it never held the 25-entry JSON contract long
enough to finish a cell. Both fit a 6 GB card at `-c 8192`; fitting was never
the problem.

**Context length matters more at 6 GB than anywhere else.** The product prompt's
longest window is 5,966 tokens, so `-c 8192` is sufficient — and the measured
difference between 8192 and a default of 32768 was **about 6 GB of VRAM** on one
5.4 GB model. On a 6 GB card that is the entire budget. Set it explicitly.

### No usable GPU

Having no GPU does not block you, and this is not a consolation prize — it is
the highest-scoring option on this page.

- **A hosted model.** Set a remote LLM profile in Setup and point it at any
  OpenAI-compatible endpoint. Measured on the four-book fixture with
  `michel2_full`: DeepSeek v4-pro scores **94.9%** with thinking off and
  **95.4%** with thinking low at an 8k budget, at roughly $0.50–0.75 per run of
  that fixture. That beats every local option here. A hosted model also leaves
  your GPU free to render audio while it annotates.
- **The manual transport**, where *you* are the model. The app writes each
  request to a file and waits for your reply, so you paste it into any chat
  model you already have open and paste the answer back. No API key, no VRAM, no
  cost — slower and hands-on, but it completes whole books. The issues behind
  #609–#616 were found by driving a book end to end this way.

Those two numbers are four-book, not the nine-novel fixture the table above
uses, so they are not strictly comparable to the rows there. They are reported
as what they are.

## Should you load an adapter?

**It depends on the adapter generation.** The older adapters (trained on the `michel2v1` or
`attrv1` prompt text) mostly lose, often badly — only Qwen3.8's gains. The **window25**
adapters (trained on `michel2v2`, the text the app serves today) gain on every family measured
so far, most where the base is weakest. Every row below is paired on the nine PDNC novels; the
window25 and KL adapters never trained on any of the nine. The *served its training prompt?*
column was checked cell by cell (2026-09-28): every older row ran on the old `8447565f` text
its adapter was trained on, so their losses are real, not a prompt mismatch.

| base | adapter trained on | quant | prompt variant | served its training prompt? | base → adapter | verdict |
|---|---|---|---|---|---:|---|
| Qwen3.8-27B | `michel2v1` | Q4_K_M, reasoning off | `michel2_full` | yes | 94.4 → **95.9** (+1.5, p=9e−5) | **load it** |
| Qwen3.8-27B | `michel2v1` | Q4_K_M, reasoning low | `michel2_full` | yes | 95.8 → 96.4 (+0.6, p=0.17) | flat |
| Qwen3.8-27B | `michel2v1` | Q3_K_XL | `michel2_full` | yes | 95.3 → 96.1 (+0.8, p=0.06) | marginal |
| Qwen3-14B | `attrv1` | Q4_K_M | `default` | unverified (attribute-prompt version on that tree not recorded) | 67.6 → 72.6 (+5.0) | pointless — see below |
| Qwen3-14B | `attrv1` | Q4_K_M | `michel2_full` | no — transfer | 84.3 → 84.3 (+0.1) | no, null |
| Qwen3.6-35B-A3B | `michel2v1` | IQ2_XXS | `michel2_full` | yes | 91.6 → 88.3 (−3.2) | **no** |
| Muse-Glimmer-30B | `michel2v1` (gen 3) | IQ3_XXS | `michel2_full` | yes | 92.6 → 89.0 (−3.6) | **no** |
| Qwen3.6-35B-A3B | `michel2v1` | IQ1_M | `michel2_full` | yes | 89.3 → 85.3 (−4.0) | **no** |
| Muse-Glimmer-30B | `michel2v1` (gen 3) | Q4_K_M | `michel2_full` | yes | 94.6 → 87.4 (−7.2) | **no** |
| Qwen3.6-35B-A3B | `michel2v1` | IQ3_XXS | `michel2_full` | yes | 91.6 → **71.3** (−20.3) | **no** |
| Qwen3.6-35B-A3B | `michel2v1` | Q4_K_XL | `michel2_full` | yes | 92.1 → **64.6** (−27.5) | **no** |
| Qwen3-8B | `michel2v2` (window25) | Q4_K_M | `michel2_full` | yes | 75.5 → **82.8** (+7.3) | **load it** |
| Qwen3.6-35B-A3B | `michel2v2` (window25) | UD-IQ1_M | `michel2_full` | yes | 90.7 → **92.1** (+1.5, p=0.01) | **load it** |
| Qwen3.6-35B-A3B | `michel2v2` (window25) | UD-IQ3_XXS | `michel2_full` | yes | 92.5 → 92.8 (+0.4, p=0.50) | flat |
| Qwen3.6-35B-A3B | `michel2v2` (window25) | UD-IQ2_XXS | `michel2_full` | yes | 90.9 → 91.6 (+0.7, p=0.28) | flat |
| Qwen3.6-35B-A3B | `michel2v2` (window25) | UD-Q4_K_XL | `michel2_full` | yes | 93.2 → 91.1 (**−2.1**, p=0.0002) | **no** |
| Muse-Glimmer-30B | `michel2v2` (KL, seeds 1 / 2) | UD-Q3_K_XL | `michel2_full` | yes | 94.5 → 95.1 / 95.3 (+0.7 / +0.8) | marginal |
| Muse-Glimmer-30B | `michel2v2` (KL, seeds 1 / 2) | IQ3_M | `michel2_full` | **no** (old text) | 92.3 → 93.3 / 93.2 (+1.0 / +0.9, p=0.08) | on-prompt rerun queued |

**Among the older adapters only Qwen3.8's earns its place**, and after the 2026-09-28
rescore only clearly with reasoning off (+1.5); with reasoning low it is flat. **Among the
window25 adapters, Qwen3-8B's does** — most of all (+7.3) on the card tier where the base is
weakest. A3B's helps only at IQ1_M and **costs 2.1 points at Q4_K_XL**; the Muse KL adapter
is marginal. (Rescored rows count PDNC's silent short names such as SMALL for the Wooden-Legged
Man; RECIPES §"Rescored 2026-09-28" has the ten verdicts that moved.) That is convenient, because Qwen3.8 is also the recommendation
for most card sizes.

**The Qwen3-14B row is the trap worth understanding.** Under the `default`
prompt the adapter looks like the best result on this page: +5.0 points. But
72.6% with the adapter is still far below the **84.3%** the same model reaches
with `michel2_full` and no adapter at all. The adapter was recovering ground the
prompt gives away for free. Under `michel2_full` it adds nothing. If a result
looks like a large adapter win, check what prompt the base arm used.

**Adapters are prompt-specific and quant-specific.** One trained for `michel2`
served under `default` is a transfer test, not a matched result. A number from a
different rung does not carry: the same A3B adapter is −20.3 at IQ3_XXS and
−3.2 at IQ2_XXS.

**Check that it loaded, and check the output.** One Muse adapter in this table
scored **4.7%** — the run was serving, answering, and almost entirely wrong.
A paired base/LoRA run on one server with the scale toggled is the only way to
see that; a LoRA-only run would have looked like a bad model.

## The complete measured ladder

| model | quant | file | nine-novel accuracy |
|---|---|---:|---:|
| Qwen3.8-27B | UD-Q3_K_XL | 12.5 GB | **95.2%** |
| Qwen3.8-27B | UD-Q4_K_M | 15.7 GB | 94.9% |
| Muse-Glimmer-30B | KQuant Q4_K_M | 16.0 GB | 94.6% |
| Muse-Glimmer-30B | UD-Q3_K_XL | 12.75 GB | 93.3% |
| Muse-Glimmer-30B | IQ3_XXS | 10.6 GB | 92.6% |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | 22.4 GB | 92.1% |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | 12.6 GB | 91.6% |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | 10.3 GB | 90.5–91.6% |
| Qwen3.6-35B-A3B | UD-IQ1_M | 9.6 GB | 89.3% |
| Qwen3.8-27B | UD-IQ2_XXS | 6.9 GB | 88.7% |
| Qwen3-14B | Q4_K_M | 9.0 GB | 84.3% |

The A3B IQ2_XXS row is a range because two runs of the same cell on different
machines returned 91.6% and 90.5%. That 1.1-point spread is a useful scale for
reading every other row: differences smaller than about a point are not
differences.

## The prompt matters more than any of this

On the four-book fixture, switching the prompt from `default` to `michel2_full`
moved Qwen3.8-27B from 82.9% to 89.8% and Qwen3-14B from 65.9% to 82.0%. That is
larger than the gap between most adjacent rows above, and it costs nothing.

A concrete illustration of getting this wrong: a nine-novel panel run with the
`default` prompt, one entry per request, scored Qwen3-8B at **33.7%** — against
71.7% for the same model on the four-book fixture with `michel2_full` at batch
25. Three things differed at once, and the prompt was probably the largest. Pick
`michel2_full` (the shipped default) before you spend money on a bigger card.

## Not yet measured

- **6 GB cards.** Qwen3.8 `Q1_L` (5.7 GB) has landed and is not the answer:
  **30.1%** on nine novels, larger than the Q4_K_M 8B that beats it. Muse
  `Q1_0` (4.5 GB) is still being measured. The tier's recommendation remains
  Qwen3-8B Q4_K_M.
- **Muse below IQ3_XXS.** `IQ2_XXS` (7.4 GB) has landed and is below the JSON
  contract floor: **1.1%** on nine novels, with 2,613 of 2,655 predictions the
  empty string. It is not a weak score, it is an absent one — do not run it
  base-only. The adapter rescues it to 77.8% on gold (+61.9), which is the one
  rung where an adapter is load-bearing rather than optional.
- **Adapters.** Every row above is base-only. Adapters are measured separately
  and are only worth loading at the rungs where they are measured to help; see
  [Prompts and adapters](Prompts-and-Adapters).
