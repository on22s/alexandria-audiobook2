# Which model for your card

Every accuracy figure here is the **same measurement**: nine public-domain
novels, 2,655 speaker-labelled rows, the `michel2_full` prompt, batch 25,
request-level JSON schema, temperature 0, reasoning effort low, no adapter.
Nothing is extrapolated between rows, and nothing from a different fixture is
mixed in. Where a number does not exist yet the row says so rather than
guessing.

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
| **12 GB** | RTX 5070, Intel Arc B580 | Qwen3.8-27B **Q2_K_XL** | 9.4 GB | **93.7%** | *untested at this rung* |
| **8 GB** | RX 9060 XT 8 GB, RTX 5060 | Qwen3.8-27B **UD-IQ2_XXS** | 6.9 GB | 88.7% | **+1.0** (89.0 → 90.0, p=0.059) |
| **6 GB** | GTX 1660, RTX 2060, laptop cards | *measuring now* — Muse `Q1_0` (4.5 GB), Qwen3.8 `Q1_L` (5.7 GB) | — | — | — |
| **no usable GPU** | — | a hosted model, or the manual transport | — | **94.9–95.4%** (DeepSeek v4-pro) | n/a |

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
fixture is **+0.2 (p = 0.838)** at IQ3_XXS and **+0.4 (p = 0.474)** at IQ3_M.
So the advice stands — *don't bother with an adapter here* — but because it does
nothing, not because it costs you 3.6 points.

**8 GB is better served than you would expect.** Qwen3.8-27B at UD-IQ2_XXS is
6.9 GB and **88.7%** — a 27B model on an entry-level card, and 4.4 points ahead
of a 14B at Q4_K_M that would not fit anyway.

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

Two local candidates are being measured on the nine-novel fixture right now:
Muse **Q1_0** (4.5 GB) and Qwen3.8 **Q1_L** (5.7 GB). Both fit a 6 GB card at
`-c 8192`. Neither has a number yet, and a guess here would be worse than a
blank, so this row stays empty until they land — at which point it gets a real
recommendation like every other tier.

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

Usually **no**. Across every paired nine-novel run we have, exactly one family
gains and the rest lose, often badly. An adapter is not a free improvement — it
is a bet that has lost more often than it has won here.

| base | quant | prompt | base → adapter | verdict |
|---|---|---|---:|---|
| Qwen3.8-27B | Q4_K_M, reasoning off | `michel2_full` | 93.3 → **95.8** (+2.5) | **load it** |
| Qwen3.8-27B | Q4_K_M, reasoning low | `michel2_full` | 94.9 → **95.9** (+1.0) | **load it** |
| Qwen3.8-27B | Q3_K_XL | `michel2_full` | 95.2 → **96.0** (+0.8) | **load it** |
| Qwen3-14B | Q4_K_M | `default` | 67.6 → 72.6 (+5.0) | pointless — see below |
| Qwen3-14B | Q4_K_M | `michel2_full` | 84.3 → 84.3 (+0.1) | no, null |
| Qwen3.6-35B-A3B | IQ2_XXS | `michel2_full` | 91.6 → 88.3 (−3.2) | **no** |
| Muse-Glimmer-30B | IQ3_XXS | `michel2_full` | 92.6 → 89.0 (−3.6) | **no** |
| Qwen3.6-35B-A3B | IQ1_M | `michel2_full` | 89.3 → 85.3 (−4.0) | **no** |
| Muse-Glimmer-30B | Q4_K_M | `michel2_full` | 94.6 → 87.4 (−7.2) | **no** |
| Qwen3.6-35B-A3B | IQ3_XXS | `michel2_full` | 91.6 → **71.3** (−20.3) | **no** |
| Qwen3.6-35B-A3B | Q4_K_XL | `michel2_full` | 92.1 → **64.6** (−27.5) | **no** |

**Only the Qwen3.8-27B adapter earns its place**, and it earns it at every rung
we have measured. That is convenient, because Qwen3.8 is also the recommendation
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

- **6 GB cards.** Muse `Q1_0` (4.5 GB) and Qwen3.8 `Q1_L` (5.7 GB) are queued.
  Until those land there is no honest recommendation for this tier.
- **Muse below IQ3_XXS.** `IQ2_XXS` (7.4 GB) is queued.
- **Adapters.** Every row above is base-only. Adapters are measured separately
  and are only worth loading at the rungs where they are measured to help; see
  [Prompts and adapters](Prompts-and-Adapters).
