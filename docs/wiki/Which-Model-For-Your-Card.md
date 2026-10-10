# Which model for your card

Which language model to run for speaker attribution, for each amount of GPU memory.

**How these numbers were measured.** Unless a row says otherwise, every accuracy figure is the
same measurement:

- nine public-domain novels from PDNC, 2,655 lines with a known speaker;
- the `michel2_full` prompt, batch size 25, a JSON schema on each request;
- temperature 0 and reasoning low;
- no adapter.

Nothing is extrapolated between rows; where a number does not exist, the page says so. Rows
from another test set are labelled, because mixing test sets silently is how a +4.3 became a +0.3
elsewhere in this project.

The base-only figures below predate the 2026-09-28 rescore, which counts correct short names
such as SMALL for The Sign of the Four's Wooden-Legged Man. It raises some bases by up to about
1.3 points. Rescored paired figures are in [Results](Results.md).

## Read this first: context length decides whether a model fits

The file size is not the memory you need. The KV cache is, and it grows with the context you
ask for. Measured on one 5.4 GB model:

| context | total GPU memory |
|---|---:|
| `-c 4096` | **5.1 GB** |
| `-c 32768` | **11.3 GB** |

These reported figures illustrate context-dependent memory use for one model; they do not
predict memory for other architectures or quantizations. The earlier guidance reports a
5,966-token attribution window, but does not link the underlying token-count artifact.
That input length alone cannot establish the context needed for an entire run: output,
reasoning, longer rosters and review requests also need space. Start small and follow
[Choosing a context size](Setup-and-Serving.md#choosing-a-context-size). 8,192 can save
memory compared with 32,768, but neither is the right setting for every workload.

## The recommendations

| your card | typical cards | run this | file | base | load the adapter? |
|---|---|---|---:|---:|---|
| **32 / 24 GB** | RTX 5090, 4090, 3090 | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** | **yes** — +1.0 on the novels it never trained on (p=0.004) |
| **16 GB** | RTX 5080, 5070 Ti, RX 9070 XT / 9070, 9060 XT 16 GB | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** | **yes** — as above |
| **12 GB** | RTX 5070, Intel Arc B580 | Qwen3.8-27B **Q2_K_XL** | 9.4 GB | **93.7%** | no — +0.1 (p=0.88) |
| **8 GB** | RX 9060 XT 8 GB, RTX 5060 | Qwen3.8-27B **UD-IQ2_XXS** | 6.9 GB | 88.7% | **yes** — +1.3 on the held-out eight (p=0.045) |
| **6 GB** | GTX 1660, RTX 2060, laptop cards | Qwen3-8B **Q4_K_M** | 4.7 GB | **76.6%** | **yes** — +6.2 (p=7e−12) |
| **no usable GPU** | — | a hosted model, or the manual transport | — | **94.9–95.4%** *(four-book)* | n/a |

The Qwen3.8 adapter (`michel2v1`) trained on The Sun Also Rises, one of the nine novels, so its
honest gains are the eight novels it never saw. The Qwen3-8B window25 adapter never trained on
any of the nine.

### 16 GB: the best value

This tier covers most people — the RTX 5080 and 5070 Ti and AMD's whole RX 9070 line.
Qwen3.8-27B UD-Q3_K_XL is 12.5 GB, so at `-c 8192` it lands near 13 GB with room to spare and
scores **95.2%**. **Nothing above 16 GB buys a better number**: a 5090 runs the same file to the
same score.

### 12 GB

Qwen3.8 **Q2_K_XL** scores **93.7%** at **9.4 GB** — a gigabyte smaller than the Muse IQ3_XXS
once recommended here, and 1.1 points ahead of it, with real headroom at `-c 8192`.

**Skip the adapter at this tier.** Neither adapter measured here helps:

- the Qwen3.8 adapter is +0.1 (p=0.88) at Q2_K_XL, and −0.7 (p=0.17) on its held-out eight;
- the Muse window25 adapter was +0.2 at IQ3_XXS, +0.4 at IQ3_M and +0.2 at Q2_K_XL before the
  rescore — none significant — and the rescore leaves the cells it covers within ±0.5.

An older note here quoted a −3.6 cost. That came from the gen-3 Muse adapter, which was trained
on single-line examples and served with 25-line requests, so it measured the mismatch, not the
recipe (see [Prompts and adapters](Prompts-and-Adapters.md)).

One caveat: the three Muse window25 figures were not all measured with the same prompt text.
The IQ3_XXS cell ran on the A100 with the newer `MICHEL2_SYSTEM` (2,071 characters), the IQ3_M
and Q2_K_XL cells on A6000s with the older one (1,825 characters). On that fleet prompt and GPU
type were perfectly confounded, so read them as three separate small nulls that happen to
agree, not one trend.

### 8 GB

Qwen3.8-27B at UD-IQ2_XXS is 6.9 GB and scores **88.7%** — a 27B model on an entry-level card,
4.4 points ahead of a 14B at Q4_K_M that would not fit anyway. Its adapter adds +1.3 on the eight
held-out novels.

### 6 GB: pick a small model that works, not a shrunken large one

Qwen3-8B Q4_K_M is 4.7 GB and scores **76.6%** on the nine novels (77.0% on the four-book set) —
the best of the small models, with headroom at `-c 8192`.

**Load its window25 adapter.** It takes the model to **82.8% (+6.2, p=7e−12)** on the nine
novels, and to 82.3% (+5.3) on the four-book set. On the four-book set it fixed 90 of the base's
177 errors but broke 8.3% of the lines it had right — the highest breakage of any adapter measured
— so the gain is real but not free; see RECIPES §"The adapter does two separable things".

Shrinking a big model does not work here:

- Qwen3.8 `Q1_L` is **larger** (5.7 GB) and scores **30.1%**;
- Muse `Q1_0` (4.5 GB) never finished a run: three attempts, about 37 card-hours on two machines,
  and it never kept the 25-line JSON format long enough.

**Context length matters most at 6 GB.** Between `-c 8192` and a default of `-c 32768` the
measured difference was **about 6 GB** on one 5.4 GB model — the whole budget of a 6 GB card.

### No usable GPU

Having no GPU does not stop you, and it is not a consolation prize — it is the highest-scoring
option on this page.

- **A hosted model.** Set a remote LLM profile in Setup and point it at any OpenAI-compatible
  endpoint. On the four-book set with `michel2_full`, DeepSeek v4-pro scores **94.9%** with
  thinking off and **95.4%** with thinking low (8k budget), for about $0.50–0.75 per run of that
  set. On the nine novels it scores **98.0%**. A hosted model also leaves your GPU free to render
  audio while it annotates.
- **The manual transport**, where *you* are the model. The app writes each request to a file and
  waits for your reply, so you can paste it into any chat model you already use and paste the
  answer back. No API key, no GPU, no cost — slower and hands-on, but it completes whole books.
  The issues behind #609–#616 were found by driving a book end to end this way.

## If speed matters more than the last few points: Qwen3.6-35B-A3B

The table picks the most accurate file for each card, not the fastest, and the speed gap is
large. A3B is a mixture-of-experts model — 35B weights, but only about 3B active per token — so
it runs at small-model speed with near-large-model accuracy.

Measured 2026-09-27 on one RX 9070 XT, the same test for every row (28 PDNC novels, 12 windows
each, `michel2_full`, reasoning low, no adapter), end-to-end time per 25-line window:

| model | file | accuracy | time per window | whole 28-book run |
|---|---:|---:|---:|---:|
| Qwen3-8B Q4_K_M | 5.0 GB | 77.4% | 18.6 s | 1.7 h |
| Qwen3.5-9B Q4_K_M | 5.7 GB | 77.4% | 23.3 s | 2.2 h |
| **Qwen3.6-35B-A3B UD-IQ1_M** | 10.0 GB | **88.8%** | **19.7 s** | **1.8 h** |
| **Qwen3.6-35B-A3B UD-IQ2_XXS** | 10.8 GB | **87.5%** | **20.2 s** | **1.9 h** |
| Qwen3.8-27B UD-Q3_K_XL | 13.1 GB | 93.7% | 49.0 s | 4.6 h |

- A3B runs as fast as the 8B models while scoring about **11 points higher**.
- It is about **2.4× faster than Qwen3.8** for about 6 points less.
- Pick it when throughput matters (a whole library, re-running books, a card shared with other
  work); pick the 16 GB recommendation when accuracy does.
- Its window25 adapter (`qwen3.6-35b-a3b-rightsclean-window25-michel2v2`) helps only at
  **IQ1_M** (+1.5, p=0.01); it is flat at IQ2_XXS and IQ3_XXS, and **costs 2.1 points at
  Q4_K_XL**.

These are one card and one run each. Qwen3.8's smaller files (IQ2_XXS, Q2_K_XL) were not timed
with this test. A dense 27B is not expected to reach MoE speed at any quant, but that is an
expectation, not a measurement.

## Three measured surprises

**Don't pay for Q4.** Qwen3.8-27B at Q3_K_XL (12.5 GB) scored **95.2%**; at Q4_K_M (15.7 GB),
**94.9%**. The extra gigabytes bought nothing — the difference is within the noise. For Muse,
Q4_K_M reached 94.6% against 93.3% at Q3_K_XL, for 3 GB more.

**A small quant of a big model beats a big quant of a small model.** Qwen3.8-27B at IQ2_XXS is
**6.9 GB and 88.7%**; Qwen3-14B at Q4_K_M is **9.0 GB and 84.3%**. If you are choosing between a
14B that runs comfortably and a 27B that only just fits, squeeze in the 27B.

**The cliff is between Q3 and IQ2, not at Q4.** Qwen3.8 holds 95.2% at Q3_K_XL and falls to
88.7% at IQ2_XXS — one step, 6.5 points. Above that step, the ladder is nearly flat. If you can
reach 12.5 GB, reach it.

## Should you load an adapter?

**Only where it is measured to help.** The rule from [Prompts and adapters](Prompts-and-Adapters.md):
an adapter's value depends on how much the base is struggling. Every row below is paired on the
nine novels and uses the 2026-09-28 rescore. The "served its training prompt?" column was checked
cell by cell: every older row ran on the prompt text its adapter was trained on, so their losses
are real, not a prompt mismatch.

| base | adapter (training prompt) | quant | served its training prompt? | base → adapter | verdict |
|---|---|---|---|---:|---|
| Qwen3-8B | window25 (`michel2v2`) | Q4_K_M | yes | 76.6 → **82.8** (+6.2) | **load it** |
| Qwen3-8B | window25 (`michel2v2`) | UD-Q3_K_XL | yes | 71.8 → **80.8** (+9.0) | **load it** |
| Qwen3.8-27B | `michel2v1` | Q4_K_M, reasoning off | yes | 94.4 → **95.9** (+1.5, p=9e−5); held-out eight +1.2 (p=6e−4) | **load it** |
| Qwen3.8-27B | `michel2v1` | Q4_K_M, reasoning low | yes | 95.8 → 96.4 (+0.6, p=0.17); held-out eight **+1.1** (p=0.003) | **load it** |
| Qwen3.8-27B | `michel2v1` | Q3_K_XL | yes | 95.3 → 96.1 (+0.8, p=0.06); held-out eight **+1.0** (p=0.004) | **load it** |
| Qwen3.8-27B | `michel2v1` | IQ2_XXS | yes | 89.0 → 90.1 (+1.0, p=0.08); held-out eight **+1.3** (p=0.045) | **load it** |
| Qwen3.8-27B | `michel2v1` | Q2_K_XL | yes | 93.8 → 93.9 (+0.1, p=0.88) | no |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-IQ1_M | yes | 90.7 → **92.1** (+1.5, p=0.01) | **load it** |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-IQ2_XXS | yes | 90.9 → 91.6 (+0.7, p=0.28) | flat |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-IQ3_XXS | yes | 92.5 → 92.8 (+0.4, p=0.50) | flat |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-Q4_K_XL | yes | 93.2 → 91.1 (**−2.1**, p=0.0002) | **no** |
| Muse-Glimmer-30B | KL (`michel2v2`), seeds 1 / 2 | UD-Q3_K_XL | yes | 94.6 → 95.1 / 95.3 (+0.6 / +0.7) | marginal |
| Muse-Glimmer-30B | KL (`michel2v2`), seeds 1 / 2 | IQ3_M | **no** (older text) | 92.3 → 93.3 / 93.2 (+1.0 / +0.9, p=0.08) | on-prompt rerun running |
| Qwen3-14B | `attrv1` | Q4_K_M, `default` prompt | not recorded | 68.4 → 73.2 (+4.8) | pointless — see below |
| Qwen3-14B | `attrv1` | Q4_K_M, `michel2_full` prompt | no — a transfer test | 85.3 → 84.4 (−1.0) | no |
| Qwen3.6-35B-A3B | `michel2v1` | IQ1_M / IQ2_XXS | yes | −4.0 / −3.2 | **no** |
| Qwen3.6-35B-A3B | `michel2v1` | IQ3_XXS / Q4_K_XL | yes | **−20.3 / −27.5** | **no** |
| Muse-Glimmer-30B | `michel2v1` (gen 3) | IQ3_XXS / Q4_K_M | yes | −3.6 / −7.2 | **no** |

**The Qwen3-14B row is the trap worth understanding.** Under the `default` prompt the adapter
looks like a big win: +4.8 points. But 73.2% with the adapter is still far below the **85.3%** the
same model reaches with `michel2_full` and no adapter. The adapter was recovering ground the
prompt gives away for free. If an adapter win looks large, check which prompt the base used.

**Adapters are prompt-specific and quant-specific.** One trained for `michel2` and served under
`default` is a transfer test, not a matched result. And a number from one quant does not carry
to another: the old A3B adapter is −20.3 at IQ3_XXS but −3.2 at IQ2_XXS.

**Check that it loaded, and check the output.** One Muse adapter scored **4.7%** — the server was
up and answering, and almost everything was wrong. Only a paired run on one server, with the
adapter switched off and on, shows that; a run with the adapter alone would have looked like a
bad model.

## The complete measured ladder

Base models only, nine novels:

| model | quant | file | accuracy |
|---|---|---:|---:|
| Qwen3.8-27B | UD-Q3_K_XL | 12.5 GB | **95.2%** |
| Qwen3.8-27B | UD-Q4_K_M | 15.7 GB | 94.9% |
| Muse-Glimmer-30B | KQuant Q4_K_M | 16.0 GB | 94.6% |
| Qwen3.8-27B | Q2_K_XL | 9.4 GB | 93.7% |
| Muse-Glimmer-30B | UD-Q3_K_XL | 12.75 GB | 93.3% |
| Muse-Glimmer-30B | IQ3_XXS | 10.6 GB | 92.6% |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | 22.4 GB | 92.1% |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | 12.6 GB | 91.6% |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | 10.3 GB | 90.5–91.6% |
| Qwen3.6-35B-A3B | UD-IQ1_M | 9.6 GB | 89.3% |
| Qwen3.8-27B | UD-IQ2_XXS | 6.9 GB | 88.7% |
| Qwen3-14B | Q4_K_M | 9.0 GB | 84.3% |
| Qwen3-8B | Q4_K_M | 4.7 GB | 76.6% *(rescored)* |
| Qwen3.8-27B | Q1_L | 5.7 GB | 30.1% |
| Muse-Glimmer-30B | IQ2_XXS | 7.4 GB | 1.1% — below the JSON-format floor |

- The A3B IQ2_XXS row is a range because two runs of the same cell on different machines gave
  91.6% and 90.5%. That 1.1-point spread is a useful scale for every other row: differences under
  about a point are not differences.
- Muse IQ2_XXS is not a weak score but an absent one: 2,613 of 2,655 answers were empty. Do not
  run it without the adapter, which rescues it to 77.8% on the four-book set (+61.9) — the one
  rung where an adapter is essential rather than optional.

## The prompt matters more than any of this

On the four-book set, switching the prompt from `default` to `michel2_full` moved Qwen3.8-27B
from 82.9% to 89.8%, and Qwen3-14B from 65.9% to 82.0%. That is bigger than the gap between most
neighbouring rows above, and it costs nothing.

An example of getting this wrong: a nine-novel run with the `default` prompt and one line per
request scored Qwen3-8B at **33.7%**, against 76.6% for the same model with `michel2_full` at
batch 25. Pick `michel2_full` — the shipped default — before you spend money on a bigger card.

## Not yet measured

- **The KL adapter at IQ3_XXS.** Seed 1 scored −1.5 on an A6000 and seed 2 +1.5 on an A100; a run
  of seed 2 on the A6000, to tell seed from GPU, is queued.
- **Speed of Qwen3.8's smaller files** (IQ2_XXS, Q2_K_XL) with the speed test above.
