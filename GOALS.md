# Alexandria Audiobook — Quality Goals

What "good" means for this app, as numbers a script can check.

Every current value below is a measurement with a source you can re-run. Every
target is a commitment. Where there is no baseline yet, the goal is *to take
the measurement*, and it says so — an unmeasured target is a wish, and this
document does not contain wishes.

**Last updated:** 2026-08-16

## How to read this

Each goal starts with a plain-language box explaining what it is, why it
matters to someone listening to the finished audiobook, and why the target is
believed reachable. Then comes the technical detail: the **metric** (what is
counted), the **probe** (the script that counts it), the **current** value with
its evidence, and the **target**.

Three status markers:

- **MET** — measured at or beyond target. Keep a test so it stays there.
- **OPEN** — measured, below target. The gap is the work.
- **NO BASELINE** — not measured. The first task is the measurement, not the fix.

A target is only listed when something in the measured record suggests it is
reachable — a better arm, a cloud model, a human ceiling. Where the ceiling
itself is unknown, the goal says so rather than inventing a number.

> **Where things are.** Open goals come first; **met goals begin at line 2225** (`# Part II — Met`). The split is by status rather than topic, so what is left to do reads top-down without scrolling past what is finished. Goal numbers are unchanged — 2.7 is 2.7 in either part.

> **This line number is checked, not trusted.** `app/tests/test_goals_navigation.py` recomputes it and fails if it drifts, so moving a goal between parts cannot quietly leave the pointer wrong. Update the number when you move something, or run the test and let it tell you what it should be.

### A few words that repeat

- **A book's "gold" set** — a few hundred lines from that book where a human
  wrote down who really speaks each one. It is the answer key. Everything is
  scored against it.
- **An "arm"** — one complete attempt at a task using one particular method, so
  two arms can be compared fairly. Like running the same race twice with
  different shoes.
- **"Held-out"** — material deliberately kept away from the system while it was
  being built, so testing on it shows real ability rather than memory.
- **The "ceiling"** — the best score anything could plausibly get, measured by
  having a human compete against herself. Scores mean little without it.

---



# Part I — Open

*Still being worked on: measured and short of target, partly met, or not yet measured at all.*

## 1. Speaker attribution — who says which line



The core task. Everything downstream inherits its errors: a misattributed line
gets the wrong voice, and no amount of TTS quality repairs it.

### 1.1 Accuracy on the four annotated books

> **What this is.** The app reads a novel and decides, line by line, which
> character is speaking. This measures how often it gets that right.
>
> **Why it matters.** This is the decision the whole app rests on. If a line is
> credited to the wrong character, it gets read in the wrong character's voice.
> A listener hears the villain speaking in the heroine's voice and the scene
> falls apart — and no amount of beautiful narration fixes it, because the
> mistake happened before a single word was spoken aloud.
>
> **Why 75% is reachable.** The app can run on a small model on your own
> machine, or a very large one rented in the cloud. The big cloud model is
> better — but only by about two points. Two of the four books already clear
> 75% locally. We are asking the local model to do what it nearly does already,
> not to make a leap.

**Metric** — percent of gold-labelled lines assigned the correct speaker.
**Probe** — `app/experiments/` arms, aggregated in `results_index.csv`.
**Current** — best local arm per book, 616 scored arm rows:

| book | best local (qwen3-14b) | best cloud (llama-3.3-70b) | gap |
|---|---|---|---|
| grimgar03 | 84.4% | 86.8% | 2.4 |
| index18 | 81.5% | 82.6% | 1.1 |
| mushoku16 | 72.9% | 74.8% | 1.9 |
| owarimonogatari3 | 69.1% | 69.8% | 0.7 |

#### The wide context arrived, and it is worth 12.7 points — 2026-08-21

`two_stage_attribution_w3200.json`, 2,494 PDNC rows, the window widened from
400 to 3,200 characters as [[Rule 1.3]]'s context audit indicated:

| quote type | n | accuracy |
|---|---|---|
| Anaphoric | 723 | **71.2%** |
| Explicit | 543 | 64.5% |
| Implicit | 1228 | 62.9% |
| **overall** | **2494** | **65.6%** |

Against the 52.9% this document records for Explicit at the old window, that is
**+11.6 points on Explicit** and 65.6% overall. The ordering is strange in a way
worth following: **Anaphoric now outscores Explicit**, though Explicit names the
speaker beside the line and should be the easy case.

> This paragraph used to add "still far from the 99.3% the field reports on
> Explicit". That comparison is withdrawn. The 99.3% is measured with GOLD
> character mentions and the gold character list, which resolves for free what
> our arm must infer, and the authors call the setting unrealistic themselves.
> `external_comparability.json` records every external number this project
> cites with the protocol behind it; of seven, one transfers — Elson's .99,
> which we reproduced ourselves at **.9899** on the same pattern in our own
> data. The clearest warning is a single paper reporting zero-shot GPT-3.5 at
> **10.9%** on one corpus and **70.1%** on another, sixty points apart from
> protocol alone.

**The remaining error is mostly selection.** The gold speaker is in the
candidate roster for **100%** of rows, and the model answers something else on
857 of them.

> "Nothing is missing from the prompt" was too strong, and is corrected here.
> For quotations split by narration — `"Bah!" said Scrooge, "Humbug!"`, 31.3%
> of PDNC — the attribution sat between the parts and reached neither context,
> so among Explicit rows the annotator's own referring expression was absent
> from everything the model saw **69.1%** of the time against 1.6% for
> single-part quotes, costing 11.0 points. Fixed in #385; the fixtures now
> carry `inner_narration` and the prompt can show it. What remains after that
> is selection: single-part Explicit rows have the evidence 98.4% of the time
> and still score .717.

#### A refinement layer was tried on that gap. All three constraints lose.

DiLA (KDD '26) proposes LLM-proposes-then-constraint-repairs, and the shape
fits: every error above is a pick the roster already contained.
`constraint_refine.py` tests three constraints, each applied **alone** and
paired against the model's own output on identical rows — a pass with
interacting rules that improved the total would not say which rule earned it.

| constraint | changed | accuracy | fixed / broke | McNemar |
|---|---|---|---|---|
| baseline (the model) | — | **65.6%** | — | — |
| roster repair | 9 | 65.8% | 4 / 0 | 0.125 |
| alternation | 815 | 54.3% | 190 / 472 | 1.3e-28 |
| adjacency, last 120 chars | 863 | 49.9% | 85 / 478 | 2.2e-67 |
| adjacency, last 400 chars | 1734 | 34.1% | 167 / 953 | 4.1e-134 |
| adjacency, full 3200 | 2258 | **17.8%** | 135 / 1327 | 2.0e-246 |

**The most useful number here is 49.9%.** That is the best hand-rolled
proximity baseline — take the roster character named nearest before the quote —
and the model beats it by **15.7 points**. Whatever the model is doing, it is
not nearest-mention matching, and the 34.4% selection gap will not be closed by
positional rules. This is evidence *for* the arm, arrived at while trying to
improve it.

**Alternation fails for a measurable reason**: the model gives consecutive
quotes the same speaker 1,010 times and is right on **53.9%** of them. These
novels have long single-speaker runs, so the rule overwrites a majority-correct
decision.

**Roster repair is free but unproven.** Only 19 predictions fall outside the
roster at all — all misspellings, `MR. DARYY` for `MR. DARCY` — and repairing
them to the nearest roster member fixed 4 and broke 0. Never harmful, worth
0.2 points, and at n=9 changes not significant. Worth wiring in as hygiene, not
as a result.

**What this does not close.** Hand-specified constraints lose; it says nothing
about learned or soft ones, which is what DiLA actually builds. The finding is
narrower and firmer: the selection gap is not positional.

**The first version of the adjacency rule fired 15 times in 2,494 rows** — it
required exactly one roster name in `prev_context`, which is 3,200 characters
and typically holds four or five. Reporting "no separation" on 15 rows would
have been a statement about the rule's rarity dressed as a result.

**Target — every book ≥ 75% on the local model.** Two of four already clear it;
owarimonogatari3 needs +5.9 and mushoku16 +2.1.

#### index18's row is measured on a CORRUPT source and is not comparable

Found 2026-08-19. The `index18` text every arm in that row read holds **6,662
U+FFFD replacement characters** (1.4% of the file, against a 0.5% gate) and
**zero quote marks of any kind** — the encoding damage removed them. The book
was being attributed with the single strongest dialogue cue absent from the
page.

Re-extracted from the user's own EPUB it comes back with **0 replacement
characters and 1,375 spoken spans**, and on that clean text it attributes
*better than any other book in the corpus*: 11.1% of dialogue left with the
narrator, 97.1% token recall.

So 81.5% is not index18's accuracy. It is the accuracy of a method reading a
damaged copy, and the direction of the error is known (the clean text is
easier) but its size is not. **32 artifacts** rest on the corrupt file. Until
they are replayed, treat this row as withdrawn rather than as evidence either
way, and do not average it into a cross-book claim.

**Why not higher.** Setting it at 90% would be asking for something nothing has
reached on any book by any method.

**The honest caveat.** Median across all 616 arms is 46–67% depending on the
book. The best arm is not the shipped arm, and the spread between books (69.1
to 84.4 on the same method) is larger than the spread between most methods.
Book identity dominates — some novels are simply harder than others, and a
result from one book does not transfer to the next.

#### These numbers are measured on the HARD SUBSET, and understate real accuracy

The light-novel gold says how it was drawn: *"Sampled uniformly from spoken,
**non-deterministic**, textually unique segments."* Lines the deterministic
namer already resolves — the ordinary `"…," said Haruhiro` case — were
**excluded before sampling**. Every light-novel accuracy in this document is
therefore conditional on *the line being hard enough that the cheap path
failed*, not on a representative page of the book.

The PDNC evaluation does not filter that way: it takes `entries[:limit]`
straight off the fixture. Which is why the same base model, on human-annotated
gold, scores far higher there:

| set | gold labelled by | sampling | base model |
|---|---|---|---|
| PDNC Pride and Prejudice | humans (published corpus) | first N, unfiltered | **80.5%** |
| PDNC The Awakening | humans | first N, unfiltered | **86.0%** |
| PDNC The Sign of the Four | humans | first N, unfiltered | **80.5%** |
| four light novels | two frontier models | hard subset only | 46–67% median |

**Do not read that gap as genre difficulty, and do not read it as the
LLM-judged gold being wrong.** It is mostly the sampling. Comparing a
hard-subset score against a whole-population score and concluding anything
about the books, the judges, or the language is the exact error this table
exists to prevent.

Two consequences worth keeping straight:

- **Real-world accuracy on a whole book is higher than goal 1.1's numbers**,
  because most lines never reach the LLM at all. What 1.1 measures is the part
  that does.
- The one comparison that *is* clean: BookNLP, the field-standard tool, scores
  **54.2%** on PDNC Pride and Prejudice (n=1226) under this harness. That is a
  ruler from outside this project, on human gold.
- **The three PDNC books in the table above are the top third of the corpus**
  (ranks #2, #8 and #9 of 28 — see 1.3). Across the 25 novels nothing here has
  ever looked at, the same base model scores **71.0%**, not 80.5–86.0%. Quote
  those three as evidence of what PDNC can look like, never as PDNC's typical
  difficulty.

**Before any cross-set comparison, harmonise the sampling.** Running every
method on every book — which is worth doing — will produce nonsense if a hard
subset is scored against a full set.

---

### 1.3 Generalisation beyond the four books

> **What this is.** Checking the app works on novels it has never encountered,
> rather than only the handful used while building it.
>
> **Why it matters.** A cook who can only make one dish is not a cook. If the
> app only performs well on the four books it was tuned against, it is not a
> product — it is a demo. Users will bring their own books.
>
> **Why this is reachable.** It is mostly a bookkeeping problem, not a
> capability problem. Public-domain novels with human-written answer keys
> already exist — 28 of them, freely available. The previous test was spoiled
> because 25 of those 28 had accidentally been used during development, which
> is like grading a student on questions they had already seen. When the truly
> unseen books were scored, the drop was **4.4 points** — real, but modest. The
> work is running a clean test, not building a new ability.

**Metric** — accuracy on held-out books never used in development.
**Probe** — PDNC gold sets (`attribution_gold_pdnc_*.json`, 1270 / 640 / 584
rows) plus `attribution_gold_random.json`.
**Current** — measured cleanly 2026-08-08 (`pdnc_generalisation.py`) on the
**base** arm, which has no PDNC training at all, so contamination does not
apply to it:

| group | books | rows | accuracy |
|---|---|---|---|
| never looked at by this project | 25 | 3000 | **71.0%** |
| the three quoted in 1.1 | 3 | 360 | **83.6%** |

**Gap −12.6 points against a target of 5. OPEN, and failing by more than
double.**

**Three interventions were piloted against that gap on 2026-08-18 and none
earned its confirmatory run.** Each is a five-book English PDNC pilot at 120
lines per book, pre-declared to open a sealed twenty-book set only on passing
a gate:

| pilot | artifact | baseline → arm |
|---|---|---|
| evidence spans | `pdnc_evidence__pilot__local-llamacpp.json` | 58.5% → 59.5% |
| sequence-aware | `pdnc_sequence__pilot__local-llamacpp.json` | 57.7% → 60.0% |
| targeted sequence | `pdnc_targeted_sequence__pilot__local-llamacpp.json` | 73.5% / 74.5% / 74.8% |

All three gates held and **the confirmatory sets remain sealed**. Six correct
lines in 600 is what separates the evidence arm from its baseline; the
targeted arms span eight. These are reasons not to spend the confirmatory run,
not results about attribution — and the sealed set stays sealed precisely so
that a later intervention can still be tested on books nothing has seen.

**Scored as PAIRED comparisons 2026-08-19** — the rows are the same lines in
both arms, so the headline percentages above understate how little separates
them. Discordant pairs and a two-sided exact McNemar:

| pilot | arm-only wins | baseline-only wins | p |
|---|---|---|---|
| evidence spans | 31 | 25 | 0.50 |
| sequence-aware | 30 | 16 | **0.054** |
| context evidence | 30 | 25 | 0.59 |

And the noise floor, which is the number that decides how to read them: the
`evidence` and `sequence` runs each measured **the identical baseline
condition**, hours apart, and disagreed on **33 of 600 rows (5.5%)**. That
churn is the same size as the discordant counts the interventions are being
judged on — 56, 46 and 55 pairs. (`sequence` and `context_evidence` disagreed
on 0 rows, so those two baselines are one measurement reused, not two.)

Sequence-aware at p=0.054 is the only one worth another look, and the right
next step is a repeat rather than a confirmatory run: one arm at p≈0.05 with a
5.5% run-to-run floor is exactly the shape a lucky draw takes.

**IT WAS A LUCKY DRAW. Repeated 2026-08-20** on the same five pilot books
(`pdnc_sequence__pilot__repeat2.json`), the sequence-aware arm **reversed**:

| run | baseline | arm | discordant | p |
|---|---|---|---|---|
| first | 57.7% | 60.0% | 30 / 16 | 0.054 |
| **repeat** | 59.8% | **58.7%** | 28 / 35 | **0.45** |

The two runs of the identical BASELINE condition disagreed on **61 of 600 rows
(10.2%)** — a noise floor even larger than the 5.5% measured the day before,
and larger than the effect either run claimed to see. Sequence-aware
resolution is not supported, the sealed twenty-book set stays sealed, and the
cost of finding this out was one repeat instead of a confirmatory run.

That is three interventions piloted against this gap and three that did not
earn their confirmatory run. The gap is still −12.6 points and nothing tested
so far has moved it.

**The three books this project quotes are not typical books.** Ranked against
all 28: The Sign of the Four **#2**, The Awakening **#8**, Pride and Prejudice
**#9** — every one in the top third. Per-book accuracy runs from 50.0%
(Mansfield Park, The Gambler) to 91.7%, median 73.3%, IQR 62.3–80.6. The PDNC
figures cited in 1.1 are real measurements of favourably-placed books, and
generalise 12.6 points worse than they appear.

**Quote type does not explain it.** PDNC labels each quote Explicit, Implicit
or Anaphoric, and the obvious hypothesis is that hard books carry more implicit
attribution. They do not: books at ≥50% implicit quotes median **73.3%**, books
below 50% median **73.3%** — identical. Whatever drives a 41-point spread
between novels, it is not the quote-type mix.

**Failure telemetry, 2026-08-15.** Joining the saved base predictions to the
candidate rosters and context windows for the five weakest books explains all
272 errors (`pdnc_failure_telemetry.py`). Gold candidate recall is **100%**:
no error is caused by a missing gold speaker. The dominant class is choosing a
different valid candidate (**149, 54.8%**), followed by missing a gold alias
explicitly present in the supplied context (**59, 21.7%**), invalid/out-of-
roster answers (**39, 14.3%**), and one 25-row block with no saved predictions
(**9.2%**). Candidate expansion is therefore the wrong intervention. The next
attribution work should improve selection/context use and keep batch-failure
recovery measurable; constrained output alone can address at most the smaller
invalid-answer class.

**First-person narrator intervention, 2026-08-15.** A clean same-run controlled
comparison on 120 quotations from each of three first-person books found a
specific, correctable selection failure (`pdnc_narrator_prior__clean-3book.json`).
Supplying the narrator's exact character identity raised *The Gambler* from
**60/120 (50.0%) to 93/120 (77.5%)**, *The Sun Also Rises* from **65/120
(54.2%) to 91/120 (75.8%)**, and the stronger *Mysterious Affair at Styles*
control from **97/120 (80.8%) to 102/120 (85.0%)**. The combined result is
**222/360 (61.7%) to 286/360 (79.4%)**, within 4.2 points of the 83.6%
development figure. Paired changes were +40/−7, +29/−3, and +6/−1; the first
two are decisive (p=1.1e-6 and 2.6e-6), while the smaller control gain is not
significant (p=0.125).

The generic control, which told the model to infer a first-person narrator but
did not supply the name, was neutral on *The Gambler* and slightly worse on
*The Sun Also Rises* (`pdnc_narrator_prior__local-llamacpp-generic.json`). The
gain therefore comes from the explicit book-level identity, not generic prompt
wording. The clean three-book run validates the optional narrator hint now
implemented in PR #299 and meets this intervention's stated evidence target.
It does **not** erase the broader 28-book generalisation gap: narrator metadata
must be known and supplied, and not every weak book is first-person.

**Generic context-evidence intervention rejected, 2026-08-16.** The gated
five-book pilot (`pdnc_context_evidence__pilot__local-llamacpp.json`) moved
accuracy only from **346/600 (57.7%) to 351/600 (58.5%)**: +0.83 percentage
points, with 30 corrected rows and 25 regressions (paired p=0.590). That misses
both advance gates (+3 points and p<0.05), so the planned 20-book confirmatory
run was correctly not started. The 55 correctness-changing rows do not support
a safe subset rule: gains/losses were 14/13 when the gold speaker was explicitly
mentioned and 16/12 when absent, while the largest book effects went in opposite
directions (*The Sun Also Rises* +13, *Persuasion* -12). The intervention mostly
shifts dialogue-turn alignment rather than reliably following evidence. Keep the
verified exact narrator metadata path; do not ship the generic prompt.

**Target — a clean held-out number on ≥ 3 books, within 5 points of the
development books' figure.**

---

## 2. Voice — does it sound like the target speaker

#### First evidence outside English and Japanese-in-translation

Everything else on this page is four Japanese light novels in translation and
three English classics. On 2026-08-21 the syntactic-frame method behind #372
was rewritten for Chinese word order — English attributes after the quote,
Chinese before it — and measured on JY-QuotePlus, 8,144 quotations from a Jin
Yong novel (`chinese_attribution_frame.py`):

| | fires | accuracy where it fires |
|---|---|---|
| Chinese, addressee-aware | **41.8%** | **.9753** |
| English trigram on PDNC | 4.0% | .9899 |

The frame sits adjacent to the quotation on **92.9%** of Chinese quotations
against 4.0% in English, so the METHOD generalises and reaches ten times as far
while the PATTERN does not transfer at all.

This says something about the corpora as well as the method: a task whose
attribution frame is adjacent 92.9% of the time is not the same task as one at
4.0%, which is part of why published scores on different corpora cannot be
ranked against each other (`external_comparability.json`).

The corpus is fetched locally and not vendored — it carries no licence and
annotates a novel still in copyright.

### 2.1 Speaker similarity against a human ceiling

> **What this is.** The app can imitate a specific narrator's voice. This
> measures how close the imitation gets to the real person.
>
> **Why it matters.** It is the difference between "that sounds like a computer
> doing an impression" and "that sounds like her". But a similarity score on its
> own is meaningless — is 0.75 good? There is no way to know. So we also measure
> the same narrator against *herself*, reading different material. That is the
> ceiling: no imitation should beat a person being herself. Every score is read
> as a percentage of that ceiling.
>
> **Why 95% is reachable.** Japanese is already at 98% and English at 93%. This
> is not a hoped-for leap; it is bringing the weakest language up to where the
> strongest already sits.

**Metric** — ECAPA cosine similarity (a standard voice-fingerprint comparison),
generated vs the human reading, read against `human_vs_human` (same narrator,
different held-out line).
**Probe** — `app/experiments/ljspeech_score.py`.
**Current** — 2026-08-06, 150 held-out lines per language:

| language | ceiling | zero-shot clone | LoRA | clone as % of ceiling |
|---|---|---|---|---|
| English | 0.809 | 0.757 | 0.690 | 93% |
| Japanese | 0.796 | 0.779 | 0.755 | 98% |
| Chinese | *0.691 — anchor invalid* | 0.765 | 0.720 | — |

**Every eval set clones from a reference below the published useful range,
measured 2026-08-20** (`reference_audit.json`). Qwen's own cloning guide puts
the band at 10–15s and describes quality as "roughly linear from 3 to 15
seconds"; the BSC Wildspoof 2026 submission measured speaker similarity
degrading as prompts shorten. Ours: **aishell3 3.45s, kokoro 5.17s, ljspeech
6.15s** — all three in the bottom quarter of that range, and the shortest is
Chinese. The seven library voices are better at 8.3–11.0s, five of seven inside
the band and none above 11s.

This is an input we control and the audio to lengthen it already exists, so it
is the cheapest untried lever on this goal. One caution from the same
literature, worth stating so it is not tried by accident: BSC measured
*enhancing* the reference improving audio quality (UTMOS 3.51 → 3.89) while
**degrading** speaker similarity (SECS 0.35 → 0.28). Cleaning the reference is
the plausible move that backfires.

**Target — reach 95% of the ceiling in every language with a valid anchor.**

**A result worth stating plainly: the simple method beat the elaborate one.**
There are two ways to imitate a voice here. "Zero-shot cloning" just listens to
a short sample and mimics it. A "LoRA" is a small trained add-on, built from
many samples over hours of GPU time. The simple method won in all three
languages. That replicates an earlier finding and extends it across languages.

It is a measured comparison, not yet a recommendation — the LoRA is better at
matching the *melody* of speech (how pitch rises and falls) in English and
Chinese, while cloning is better at matching the *timbre* (what the voice
sounds like). The two disagree about which is better, so the question is not
closed.

---

### 2.5 Pitch range, not just pitch shape

> **What this is.** Whether the generated voice uses as much of its pitch range
> as the human did — how far it moves between its lowest and highest notes in a
> line.
>
> **Why it matters.** This is the difference between a reading and a recital. A
> voice can follow every rise and fall of the original and still deliver them
> all within a narrow band, and the result sounds flat — the thing people mean
> when they say synthetic speech is monotone. A listener notices immediately.
>
> **Why nothing caught it until now.** `f0_corr` measures the *correlation* of
> the pitch contour, which stays high when the whole shape is squashed: the ups
> and downs happen in the right places, just smaller. Correlation is deliberately
> blind to scale. So the app measured pitch and missed the flatness.

**Metric** — generated f0 spread (p90 − p10) ÷ human f0 spread, on the same line.
**Probe** — `app/experiments/pitch_quality_probe.py` (calls `pitch_stats`; the
`voice_compare_view` CLI renders a view, it does not report numbers).
**Current** — 100 clips per language, both arms, 0 dropped, 2026-08-08.
**MET ON SPREAD, OPEN ON MEDIAN.** The table below is the f0 *spread* ratio and
every cell is inside 0.90–1.15x. The target's second band is the f0 *median*
within 0.95–1.05x, and English cloning sits at **0.81x** - a voice pitched a
fifth of an octave low for a whole book, recorded further down this section.
A bare "MET." here read as though the goal were finished and nearly got it
promoted on 2026-08-20; the promotion was stopped by re-reading the target.

| set | clone | LoRA |
|---|---|---|
| English | 0.92x | 1.13x |
| Japanese | 0.91x | 1.02x |
| Chinese | 1.03x | 0.96x |

Every cell is inside 0.90–1.15x. At 12 clips this goal read OPEN on two cells —
English clone at 0.83x and Chinese LoRA at 0.81x — and both were sample size:
the same arms measure 0.92x and 0.96x over 100 clips. The 12-clip run's
artifact is not in the evidence tree, so the two runs cannot be reconciled
directly; what is claimed here is only what the n=100 artifact shows.

**The English median failure is real, and one MET cell is shakier than it
looks.** The human-vs-human null described at 2.6 puts English `f0_median`
between 0.9410 and 1.0658 across its whole range, so cloning's 0.81x is not
instrument spread. But the same null flags a cell this goal currently reads as
MET: **Chinese `f0_spread` falls outside its own 0.90–1.15 band on 13.65% of
same-speaker splits** (p5 0.8741, p95 1.1465) with nothing synthesised. The
Chinese spread cells at 1.03x and 0.96x are inside a band the measure cannot
reliably stay inside, so they are weak evidence rather than a clear pass. No
other measure in any language exceeds 0.8%.

**Target — f0 spread within 0.90–1.15x of the human, and f0 median within
0.95–1.05x.**

English cloning also drops median pitch to **0.81x** — a voice pitched a fifth
of an octave low for the whole book. ECAPA rates that same clone *higher* than
the LoRA, because embedding similarity is a timbre measure and this is a pitch
failure.

**A caution, since this section could invite the wrong fix.** The app
deliberately refuses to classify gender from pitch
(`test_pitch_is_not_used_as_a_gender_classifier`), and that decision is right —
male and female f0 distributions overlap heavily. Nothing here labels a
speaker. This asks only whether an arm preserved *that speaker's own* range,
which is a comparison between two clips of one person.

---

### 2.6 Voice quality, the measures the field says matter most

> **What this is.** Three standard measures of how a voice actually behaves:
> **jitter** (cycle-to-cycle wobble in pitch), **shimmer** (wobble in
> loudness), and **HNR** (how much of the sound is clean tone versus noise).
>
> **Why it matters.** They catch a failure nothing else here can: a synthetic
> voice being *too clean*. Human phonation wobbles slightly on every cycle; a
> vocoder often does not. A voice with near-zero jitter sounds subtly
> unnatural even when every other measure looks good.
>
> **Why these three.** A study comparing speakers ranked what actually
> separates voices: jitter first, then F2, then shimmer, then HNR — and
> *duration* among the least discriminative. This project measured ECAPA, f0
> contour, MCD and duration, so it had none of the top four and one of the
> bottom.

**Metric** — jitter, shimmer and HNR of the generated line ÷ the human's, plus
vocal tract length from formant dispersion.
**Probe** — `voice_quality`, `vocal_tract_length` (Praat via parselmouth).
**Current** — all five measures on 100 clips per language, both arms, 0
dropped (jitter/shimmer/HNR 2026-08-08, tract length 2026-08-09). **MET on
jitter and shimmer. MET on HNR after the eval speaker was corrected. OPEN on
tract length, one cell: the Chinese clone arm at 1.064x.**

**IT IS NOT SAMPLE SIZE. Re-measured 2026-08-20 over every clip the corpus has**
(`pitch_quality_probe_n200.json`; 200 requested, 150 available):

| Chinese arm | n=100 | n=150 |
|---|---|---|
| LoRA | 1.032x | **1.0272x** — inside |
| clone | 1.064x | **1.0607x** — outside |

Twice the sample moved the failing cell by 0.003. Sample size was the answer
the last two times a cell of this goal read OPEN - both were measured at n=12
and came inside at n=100 - and it was worth one run to find out whether that
held a third time. It does not.

So this stops being "one cell away" and becomes a statement about the method:
**zero-shot cloning does not preserve Chinese vocal tract length, and the LoRA
arm of the same measure does.** The gap is 1.1% past a 5% band, which is small
and consistent rather than noisy. Closing 2.6 now means either accepting that
cloning misses this cell, or not shipping cloning for Chinese - a product
decision, not a measurement, and the document should not pretend another run
will resolve it.

**The ruler was checked before that decision was taken, 2026-08-20, and it
holds.** "The band is achievable" was an assumption nobody had tested: every
number in 2.5 and 2.6 is a ratio of two medians, and that statistic has a
spread of its own. `instrument_null_test.py` measures it by splitting the
speaker's OWN human recordings into two disjoint halves and computing exactly
the ratio the goal computes — same speaker, same session, nothing synthesised
— over 2,000 random splits at the goal's own n=150.

| null, human vs human | zh `vtl_cm` | en `f0_median` |
|---|---|---|
| median | 1.0000 | 1.0000 |
| p5 – p95 | 0.9888 – 1.0106 | 0.9685 – 1.0326 |
| **full range** | **0.9588 – 1.0337** | **0.9410 – 1.0658** |
| outside its band | **0.0%** | 0.8% |

The Chinese clone's 1.0607x and English cloning's 0.81x both sit **outside the
entire null range**, not merely outside the band. So the instrument is
exonerated and **both failing cells are real measurements of the arm.** The
hypothesis that a 6% miss was formant-estimation noise is refuted, and the
product decision above stands unchanged.

**A mechanism did survive, and it is an input rather than the model.** The
clone arm has exactly one input besides the text, and `reference_audit.py`
(2026-08-20) shows it is unrepresentative in the same direction as each
failure:

| eval set | ref seconds | ref f0 ÷ corpus | ref vtl ÷ corpus | the arm's miss |
|---|---|---|---|---|
| aishell3 (zh) | **3.45** | 1.0028 | **1.0360** | vtl 1.0607x |
| ljspeech (en) | **6.15** | **0.9439** | 1.0704 | f0 median 0.81x |

The reference clip a Chinese clone is built from already has a 3.6% longer
apparent vocal tract than the speaker's own corpus median, and the English
reference is pitched 5.6% below its speaker's median. A clone copies its
reference; if the reference is off-centre, so is the clone. This is a
directional match on two of two, not a demonstration — it does not show the
magnitude follows — but it is cheap to test and it is an input we choose.

| set | jitter | shimmer | HNR |
|---|---|---|---|
| English LoRA | 0.99x | 0.92x | 0.98x |
| English clone | 0.94x | 0.88x | 1.02x |
| Japanese LoRA | 1.11x | 1.07x | 0.97x |
| Japanese clone | 1.09x | 1.07x | 0.96x |
| Chinese LoRA | 0.90x | 0.88x | **1.17x** |
| Chinese clone | 0.98x | 0.89x | 1.08x |

The Chinese LoRA was the "too clean" case at 12 clips — jitter 0.81x, HNR
1.20x. At 100 clips its jitter is 0.90x, inside the band: that half of the
finding was sample size. Its HNR is 1.17x, still outside 1.15 after a fivefold
sample increase.

**Clip length has been ruled out as the cause** (`hnr_length_probe.py`,
2026-08-08), using the two-direction test that settled 2.2:

| condition | clip length | HNR ratio |
|---|---|---|
| Chinese as-is | 3.1 s | 1.1793x |
| Chinese, clips joined | 8.9 s | 1.1818x |
| English as-is | 7.5 s | 0.9806x |
| English, truncated to Chinese length | 3.2 s | 0.9519x |

Nearly tripling the Chinese clip length moves the ratio by 0.0025, and
shortening English to Chinese length does not inflate it — it drifts 0.03 in
the opposite direction to the one a length artifact predicts. Short clips
destroyed the ECAPA anchor and do nothing to HNR, so the two are not the same
defect and 2.2 does not explain this.

**The corpus has been ruled out and the eval speaker has not**
(`corpus_hnr_baseline.py`, 2026-08-08; 40 AISHELL-3 speakers the adapter never
saw, 15 clips each, human recordings only):

| | median HNR |
|---|---|
| AISHELL-3, 40 unseen speakers | **12.02 dB** |
| LJSpeech (English) | 10.83 dB |
| Kokoro (Japanese) | 16.28 dB |
| SSB1585, the Chinese eval speaker | **9.39 dB** |

AISHELL-3 is *cleaner* than LJSpeech, which the English cell passes on, so
"recorded on consumer hardware" explains nothing. SSB1585 herself sits 2.63 dB
below her own corpus median at the **8th percentile** — 3 of 40 sampled
speakers are noisier than she is.

So the denominator is depressed, by the choice of eval speaker rather than by
the corpus. The generated side measures 11.17 dB against AISHELL-3's own 12.02
median: the adapter produces roughly corpus-typical phonation and looks too
clean only because it is divided by an atypically noisy narrator. Against a
median speaker the same audio would land near 0.93x — that number is
arithmetic on two medians, not a measurement.

**Status: CLOSED 2026-08-08.** The Chinese arm was re-run end to end against
**SSB0748**, the speaker closest to the corpus median (12.025 dB, +0.005 off),
with every other setting matched to the original: same trainer, lr 1e-6, 6
epochs, lora_r 64, seed 1234. New adapter, 150 lines, 300 clips, 0 dropped.
Artifacts are on main: `pitch_quality_SSB0748.json`, `aishell3_SSB0748_generate.json` and `corpus_hnr_baseline.json`. (This line used to cite an `agent/asr-hybrid-cjk` branch, which no longer exists on the remote - a closed goal pointing at evidence nobody can fetch. The artifacts themselves were merged and are named here instead.)

| | SSB1585 (9.39 dB, 8th percentile) | SSB0748 (12.02 dB, median) |
|---|---|---|
| human HNR | 9.39 dB | **12.02 dB** |
| LoRA HNR ratio | **1.1669x** (out of band) | **1.0571x** (in band) |
| clone HNR ratio | 1.0839x | 1.1219x |
| jitter | 0.9041x | 0.9457x |
| shimmer | 0.8793x | 0.8885x |

**Every Chinese cell of 2.6 is now inside 0.85–1.15x.** The "too clean" Chinese
LoRA was an artefact of picking an unusually noisy narrator to compare against.

**Where the prediction was wrong, and it matters.** The chain script committed
beforehand predicted ~0.93x, on the arithmetic that the generated side would
stay at 11.17 dB while the denominator rose to 12.02. The generated side did
not stay: the new adapter produces **12.71 dB**. The conclusion held — in band,
speaker selection was the cause — but the specific number was not predicted
correctly, because an adapter trained on a different speaker synthesises
different phonation.

**The unavoidable confound, stated.** Changing the eval speaker necessarily
changes the adapter too; there is no adapter for a speaker without training one.
So this run does not isolate speaker identity from adapter instance on its own.
What makes speaker selection the better explanation is the corpus baseline: 40
speakers the adapter never saw median 12.02 dB, and SSB1585 sat at the 8th
percentile of them. The rerun is consistent with that, not independent proof of
it.

| | SSB1585 (9.39 dB, 8th percentile) | SSB0748 (12.02 dB, median) |
|---|---|---|
| human HNR | 9.39 dB | **12.02 dB** |
| LoRA HNR ratio | **1.1669x** (out of band) | **1.0571x** (in band) |
| clone HNR ratio | 1.0839x | 1.1219x |
| jitter | 0.9041x | 0.9457x |
| shimmer | 0.8793x | 0.8885x |

**Every Chinese cell of 2.6 is now inside 0.85–1.15x.** The "too clean" Chinese
LoRA was an artefact of picking an unusually noisy narrator to compare against.

**Where the prediction was wrong, and it matters.** The chain script committed
beforehand predicted ~0.93x, on the arithmetic that the generated side would
stay at 11.17 dB while the denominator rose to 12.02. The generated side did
not stay: the new adapter produces **12.71 dB**. The conclusion held — in band,
speaker selection was the cause — but the specific number was not predicted
correctly, because an adapter trained on a different speaker synthesises
different phonation.

**The unavoidable confound, stated.** Changing the eval speaker necessarily
changes the adapter too; there is no adapter for a speaker without training one.
So this run does not isolate speaker identity from adapter instance on its own.
What makes speaker selection the better explanation is the corpus baseline: 40
speakers the adapter never saw median 12.02 dB, and SSB1585 sat at the 8th
percentile of them. The rerun is consistent with that, not independent proof of
it.

**Vocal tract length, now at 100 clips per language** (2026-08-09), both arms:

| arm | ratio | | arm | ratio |
|---|---|---|---|---|
| English LoRA | 1.004 | | English clone | 1.029 |
| Japanese LoRA | 1.016 | | Japanese clone | 1.042 |
| Chinese LoRA | 1.032 | | Chinese clone | **1.064** |

Five of six cells are inside 0.95–1.05. **The Chinese clone arm is not**, at
1.064 — every arm shifts the speaker's apparent vocal size slightly *upward*,
and that one shifts it past the target.

**The 12-clip entry claimed 0.98–1.08x and called this MET, but 1.08 was
already outside the 1.05 target.** It was recorded as met against its own
failing number. That is a bookkeeping error, not a measurement one, and it is
the reason this row is now stated per-arm rather than as a range: a range hides
which arm failed.

#### THE REFERENCE WAS THE MECHANISM — measured 2026-08-21

`reference_rebuild.py` gave each eval set a reference that is both long enough
and typical of its speaker (3.45/5.17/6.15s → 11.8/10.8/10.5s, each sitting
almost exactly on its speaker's corpus median). The clone arm was regenerated
against it, 150 lines per language, and scored with the same
`pitch_quality_probe` the committed numbers come from:

| cell | old reference | long reference | band | |
|---|---|---|---|---|
| **zh clone tract length** | 1.0607 | **1.0132** | 0.95–1.05 | OUTSIDE → **inside** |
| **en clone f0 median** | 0.8508 | **0.9551** | 0.95–1.05 | OUTSIDE → **inside** |
| ja clone f0 median | 0.9973 | 0.9910 | 0.95–1.05 | inside → inside |
| zh clone f0 spread | 1.0912 | 1.1571 | 0.90–1.15 | inside → **outside** |

**Both failing cells came inside**, and by margins the human-vs-human null puts
beyond its own spread: that null gives `zh vtl_cm` a full range of 0.9588–1.0337,
so 1.0132 is ordinary and 1.0607 was not.

**Three things this does not say.**

- **Length and typicality changed together**, deliberately, so a difference
  cannot be attributed to either alone. The first question was whether the
  input matters at all; separating the two costs another two arms and is only
  worth it now that the answer is yes.
- **The Chinese spread cell got worse**, and it is the one measure the null
  test flagged: `zh f0_spread` falls outside its own band on 13.65% of
  same-speaker splits with nothing synthesised. A move within a measure that
  unstable is not evidence of anything, in either direction.
- **One run, one reference per language.** The rebuilt reference is the
  candidate closest to its speaker's centre out of 30 considered; whether a
  different draw would do as well is untested.

**So 2.6's Chinese cell should not be closed as "cloning misses this and that
is a product decision" until this is replicated.** The product decision was
the honest reading of the evidence that existed; this is new evidence, and it
points at an input we choose rather than at the method.

**Target — jitter, shimmer and HNR within 0.85–1.15x; tract length within
0.95–1.05x.**

---

### 2.7 Adapters must not be trained on their own test data

> **What this is.** Keeping some of a narrator's recordings away from training,
> so there is honest material to test the finished voice on.
>
> **Why it matters.** Otherwise there is no way to know whether a voice
> genuinely learned to sound like someone, or simply memorised the specific
> lines it was shown. A student who sees the exam paper in advance tells you
> nothing about what they know.
>
> **Why this is cheap to fix.** The split already exists and is already
> correct — the trainer just doesn't use it.

**Metric** — adapters whose training set includes their validation split.
**Current** — every dataset zip splits **180 train / 20 val with zero
overlap**, and the trainer now uses the split, but the live manifest still
contains **12 of 75 shipped adapters trained on all 200 clips**, down from 21.
**OPEN**, last audited 2026-08-16 from each adapter's own training metadata.

**Evidence** — the identity gate re-ran on **2026-08-18 over all 67 adapters**
that carry a dataset path, scoring each against its own held-out val clips:
**59 passed, 8 failed** the 0.45 threshold, median 0.634. Every artifact is
`gate_promote__<adapter>.json` and now carries a commit, a clean-tree flag and
a harness hash — for example `gate_promote__crisp_mezzo_30s_f.json` (0.556,
pass) and `gate_promote__warm_alto_50s_f_gothic.json` (0.034, fail). The 87
artifacts this goal previously rested on had no provenance at all.

**The prior run of that gate measured nothing and said it had.** On
2026-08-17 all 67 adapters failed with `System error` opening val clips that
were readable throughout: the ECAPA subprocess runs with `cwd=app/`, so the
relative dataset path resolved against the wrong directory. The chain printed
`REGATE COMPLETE` and exited 0. Both are fixed — absolute paths at the cwd
boundary, and a strict gate that fails when the parts do — but any figure
quoted from that run is a figure from zero measurements.

The eight failures are being re-checked before anything is retrained: on
2026-08-07 all five failures of that day recovered on a rerun, two to ~0.67,
so failing twice is a different claim from failing once.

**Where the remaining twelve stand, 2026-08-16.** All 21 were retrained on the
honest split and independently gated; 9 were promoted with a rollback receipt,
including one voice that went 0.09 → 0.57. The twelve left need three
different things, and only one of them is more training:

- **1 rescued, promotable now.** `breathy_alto_50s_f_fantasy` failed at
  reference-rank 1 (0.404) and passed at rank 2 (**0.503**), against a
  contaminated shipped score of 0.291.
- **5 exhausted on reference choice.** The other rank-1 failures were retrained
  at rank 2 and went 0.056→0.077, 0.112→0.112, 0.089→0.054, 0.100→0.042,
  0.229→0.165 — four of five *worse*, all still far below the 0.45 bar, moving
  ±0.06 with no direction. Two reference choices have now failed to move them.
  **The lever is spent**, and the remaining explanation is the source data
  rather than the recipe: these datasets need rebuilding or retiring, not
  another retrain.
- **6 blocked by a comparison that cannot be made fair.** These passed their
  gate and were refused only for not beating their shipped score. That score
  was measured on clips the shipped adapter trained on — and re-measuring it
  does not help, because **a contaminated adapter has no held-out data by
  definition**: the retrain's 20-clip val split is training material for the
  shipped adapter and genuinely unseen for the clean one. Asked on
  2026-08-16, the re-measurement reproduced the original score to four
  decimals for all six. The clean adapters lose by 0.02–0.06 on clips their
  rival memorised, which is the expected result of a rigged comparison and not
  evidence they are worse. Settling it needs clips **neither** adapter ever
  saw — a different recording of the same narrator, or a slice withheld before
  either was trained. No such data exists today.

| trained on | adapters |
|---|---|
| all 200 clips, including its own val split | **21** |
| 180, the train split only | 46 |
| some other count (24, 81, 88, 116, 130, 170, 188) | 8 |

The remaining 21 adapters' held-out scores are measured partly on clips they
were trained on, and should be read as an upper bound rather than a held-out
result.

One trap for anyone re-running this audit: the retrained adapters record
`num_samples` while the older ones record `sample_count`. Two field names for
one concept - checking only one of them silently reports the wrong count, which
happened on the first pass of this audit.

**Target — train on `train/` only; 0 adapters trained on their own val split.**

Until then, every per-adapter fidelity number in the library is an **upper
bound**: it is measured on material the model has heard. It can still rank
adapters, because all are contaminated identically — a voice that scores badly
on its own training data is genuinely bad.

**Twenty clean retrains promoted 2026-08-15.** All 26 candidates that cleared
the retraining summary were independently regenerated and identity-gated on
six held-out lines. Twenty passed the 0.45 gate and beat the weights currently
shipped, so they were installed with receipt and rollback backup
`promotion_backups/20260815_152050.json`; five passed identity but did not beat
the current library and one failed identity at 0.393. Exact-source hash checks
confirmed every installed adapter matches the path recorded by its gate.

The live manifest now reports **21 of 75 adapters still trained on 200 clips**,
with 46 on the clean 180-clip split and eight at other sample counts. Seven
2026-08-08 promotions already contained clean 180-sample weights but retained
stale 200-sample manifest counts; reconciling those counts removed the apparent
disagreement with each adapter's training_meta.json (one per adapter
directory, 177 on this machine and none committed - so it is a description of
where to look, not a citation anyone else can follow). The goal
remains **OPEN**, but deployment—not merely retraining evidence—has removed
contamination from 20 shipped voices.

**Nineteen more clean retrains promoted 2026-08-15.** The first decontamination
run paired copied medoid audio with the first sample's unrelated transcript at
inference time. Repairing that metadata changed 52 candidates; all 40 remaining
candidates were then independently regenerated on six held-out lines. Thirty-
two passed the ≥0.45 identity floor and 19 also beat the shipped weights, so
only those 19 were installed. Seven failed identity and 14 passed identity but
did not beat the shipped voice; all 21 were left untouched. Receipt and rollback
backup: `promotion_backups/20260815_175016.json`.

#### The reference clip is CAUSAL — established by intervention, not correlation

> **What this is.** Before training a voice, the pipeline picks one recording
> to define "who this speaker is". Every one of the 200 training samples is
> anchored to that single file. This measures what happens when it is the
> wrong person.
>
> **Why it matters.** It was chosen as "whatever clip happens to be first",
> with no check. When that clip belonged to someone else — which happens when
> the automatic speaker-splitting misfires — the whole voice was built on the
> wrong identity, and nothing reported a problem.

**Metric** — adapter speaker-similarity when only the reference clip changes.
**Probe** — `app/experiments/reference_intervention.py`.
**Current** — 2026-08-07. Same 180 training clips, same seed, same epochs;
**only the reference file differs**:

| reference | adapter |
|---|---|
| medoid (most representative clip) | **0.695** |
| least-typical clip of the same narrator | 0.634 |
| **a different narrator entirely** | **0.154** |

**A wrong-speaker reference costs 0.541.** That drops a working voice into
exactly the range the broken library adapters occupied (0.004–0.14), which is
enough to explain all of them.

The fine-grained contrast transfers almost perfectly: a **0.058** change in
reference quality produced a **0.061** change in the adapter — **transfer ratio
1.06**. Small reference differences matter proportionally; large ones are
catastrophic.

**This supersedes the correlational finding below.** The +0.76 correlation was
consistent with a third cause — a messy dataset yielding both a bad reference
and a hard learning problem. The intervention rules that out: nothing varied
except one file.

**It also explains the "training lottery" that never existed.** Retrained
adapters recovered from 0.027 to 0.685 on apparently identical settings. The
extracted dataset zips carry no `ref.wav`, so `train_lora.py` fell back to its
*first training clip* — a different, usually better reference. The recovery was
a reference change all along. Three runs at a fixed seed give 0.739 / 0.736 /
0.685, so training itself is deterministic and **a retry loop would be
useless**.

#### The correlational evidence that led here

`train_lora.py` extracts the speaker embedding from ONE reference clip and uses
it for all 200 training samples, so a single wrong file sets the voice identity
regardless of how clean the training audio is.

Swept across all 75 adapters on 2026-08-07 (`ref_clip_match.py`):

- **correlation(reference matches its narrator, adapter quality) = +0.59**
- **7 adapters have a reference that is not their own narrator** (<0.3)
- **4 of those 7 also failed** as adapters

+0.59 is a real effect and not a complete explanation. The clearest cases are
stark — `husky_baritone_20s_m_anime` has a reference scoring **−0.026** against
its own training data and an adapter at 0.027 — but `warm_tenor_20s_m` reaches
0.725 with a reference at 0.096. A mismatched reference does not doom an
adapter, and a good adapter does not prove a good reference.

**What this changes:** for those 4, "retrain" is the wrong prescription. The
*reference clip* needs fixing first, which is cheaper than either retraining or
rebuilding the dataset.

**Retraining the seven with a corrected reference**, measured on held-out clips:

| adapter | shipped | retrained |
|---|---|---|
| husky_baritone_20s_m_anime | 0.004 | **0.691** |
| warm_baritone_40s_m_fantasy | 0.062 | **0.694** |
| warm_tenor_20s_m | 0.090 | 0.411 |
| velvety_mezzo_30s_f_gothic | 0.084 | 0.187 |
| silky_baritone_45s_m | 0.079 | 0.088 |
| husky_baritone_40s_m_military | 0.141 | 0.149 |
| warm_baritone_50s_m_gothic | 0.544 | 0.468 |

Two dead voices became good ones. The three that barely moved are the ones
whose DATASETS are mixed-speaker (2.7 above): there the reference was never the
binding constraint, and rebuilding is still required.

---

### 2.9 Pitch-carried meaning survives synthesis

> **What this is.** In Japanese and Chinese, pitch is not decoration. 箸
> (chopsticks) and 橋 (bridge) are both *hashi* and differ only in where the
> pitch falls; a Chinese syllable spoken on the wrong tone is a different
> word.
>
> **Why it matters, and why nothing else here can see it.** A wrong accent
> transcribes back to identical characters, so CER reports success. Speaker
> similarity (2.1) is an embedding distance and is blind to it. The listener
> this project has speaks English, and for these two languages can honestly
> report only that something "sounds off". This is the axis that failure hides
> in.
>
> **Why this is reachable.** Both intended pitches are recoverable without a
> human: OpenJTalk returns the accent nucleus of any Japanese sentence, and
> pypinyin returns the tone of every Chinese syllable. Verified on the
> minimal pair — 箸 comes back accent 1, 橋 accent 2.

**Metric** — two, kept separate on purpose. Against a human reading the same
line: f0 correlation in semitones after DTW, and Gross Pitch Error. Without a
reference: agreement between the produced f0 and the accent or tone the text
says it should have.
**Probe** — `app/experiments/prosody_fidelity.py` (reference-based),
`app/experiments/expected_prosody.py` (reference-free).
**Current** — reference-based, 40 lines per arm, 2026-08-17:

| language | arm | f0 correlation | GPE |
|---|---|---|---|
| Japanese | clone | 0.728 | 0.146 |
| Japanese | lora | 0.721 | 0.177 |
| Chinese | lora | 0.747 | 0.190 |
| Chinese | clone | 0.730 | 0.165 |
| English | clone | 0.403 | 0.471 |
| English | lora | 0.282 | 0.475 |

The fused measure — produced f0 against the *expected* accent, which is the
one that works on a real audiobook rather than an eval set — is **NO
BASELINE**. The extraction is built and verified; the comparison is not.

**No target yet, deliberately.** A correlation threshold invented before the
fused measure has ever run would be the "invented number" this document's
rules forbid. The first task is the measurement.

**Two things already worth carrying forward.** The Japanese and Chinese arms
disagree about which method wins — clone leads on Japanese pitch accent, LoRA
on Chinese tone — so "the clone beats the LoRA" should not be quoted as
language-independent; it was established on ECAPA, which cannot see either.
And English agrees with its reference far worse than either CJK language,
which is either a real weakness of that arm or an artifact of that eval set,
and n=40 cannot yet separate the two.

**Re-run at n=150 on 2026-08-20** — every line the three sources have, nearly
four times the sample, CPU only since the probe scores clips already on disk
(`prosody_fidelity_{en,ja,zh}_n150.json`):

| | f0 correlation n=40 → n=150 | GPE n=40 → n=150 |
|---|---|---|
| English LoRA | 0.2815 → **0.2889** | 0.4749 → 0.4729 |
| English clone | 0.4032 → **0.3428** | 0.4711 → 0.4721 |
| Japanese LoRA | 0.7211 → 0.7173 | 0.1769 → 0.1882 |
| Japanese clone | 0.7284 → 0.7418 | 0.1459 → 0.1456 |
| Chinese LoRA | 0.7472 → 0.7293 | 0.1898 → 0.1975 |
| Chinese clone | 0.7296 → 0.7277 | 0.1648 → 0.1776 |

**Two things it does settle.** The English deficit is not a small-sample fluke:
0.29-0.34 correlation against 0.72-0.74 for both CJK languages, and roughly
three times the gross pitch error, stable across a fourfold change in n. And
the arm difference is much smaller than n=40 suggested - English clone-over-
LoRA halved from 0.12 to 0.054, Chinese went from 0.018 apart to 0.002, and
Japanese stayed inside 0.025. **The arms are close; the languages are not.**

**What it does NOT settle, and cannot.** The question above is whether English
is weak because of the ARM or because of the EVAL SET, and more draws from the
same LJSpeech recordings cannot tell those apart - both predict the same score
at any n. Only a second English reference set can, and there is not one on
disk: `ljspeech` is the only English source among the five generate artifacts.
Generating one is the experiment that would close this, and it is a GPU job
nobody has queued.

---

## 3. Reliability — does a run finish and produce the right thing


### 3.1 Chunk completion on script generation

> **What this is.** A novel is too long to process at once, so it is cut into
> chunks. This tracks how many chunks get through without the app giving up on
> them.
>
> **Why it matters.** A failed chunk is a hole in the audiobook. Runs take
> hours, so failures discovered at the end are expensive in wall-clock time and
> in patience.
>
> **Why 99% is reachable.** The failures have been studied and sort into two
> named groups: one is a near-miss against a threshold and is fixable by
> adjusting that threshold; the other is the model occasionally losing the plot
> for no reason connected to the text. Neither is mysterious. One book in the
> most recent run completed 9 chunks out of 9 cleanly, so clean runs plainly
> happen.

**Metric** — chunks completing without exhausting retries.
**Probe** — `logs/review_responses.log`, per-run logs.
**Current** — every saved book carries a `<name>.json.generation_quality.json`
recording `total_chunks`, `accepted_chunk_count` and, on newer runs,
`model_name`. Read across all 34 of them 2026-08-08
(`chunk_completion.py`), no inference required:

| model | books | chunks | completion | worst book |
|---|---|---|---|---|
| gemma-4-e4b-uncensored | 19 | 1313 | **100.00%** | 100% |
| *unrecorded* | 15 | 1586 | 25.28% | 1.0% |

**MET on the only model that can be attributed** — gemma-4-e4b completes every
chunk of every book, 1313 for 1313, against a 99% target.

**The development set was MET before broader shipped-model reliability was.**
All four development books completed every chunk on qwen3-14b on 2026-08-10:

| book | chunks | completion |
|---|---|---|
| grimgar03 (run A) | 49/49 | 100% |
| grimgar03 (run B) | 49/49 | 100% |
| index18 | 81/81 | 100% |
| mushoku16 | 45/45 | 100% |
| owarimonogatari3 | 110/110 | 100% |

Two of these were ungeneratable 24 hours earlier. grimgar03 failed at chunk 1
of 49 because the repair logic refused a book for faithfully reproducing its
own repeated title; index18 was refused at the source gate over 6,662
replacement characters. Both are fixed, and grimgar03 was run twice because one
success does not distinguish a reliable book from a lucky one.

The unseen-book run on 2026-08-12 then found two new deterministic blockers:

| book | accepted chunks | result |
|---|---:|---|
| mushoku18 | 58/58 | written |
| mushoku23 | 120/120 | written, 11,387 entries |
| arc4_volume10wn | 132/154 | failed at chunk 133 |
| grimgar06 | 24/70 | failed at chunk 25 |

That is **334/402 = 83.1%** completion across the four unseen books, below the
99% target. Both failures exhausted the fixed retry policy and preserve valid
checkpoints. `arc4` repeatedly expanded a short repetitive passage until the
16,384-token ceiling; `grimgar06` repeatedly omitted parts of one passage even
after adaptive splitting. Retrying either unchanged is not a new measurement.
At that point the overall status was therefore **OPEN**: the development books
were 100%, but the shipped model had not generalised that reliability to these
unseen formats.

**Both blockers are now diagnosed without retrying them unchanged.** Arc4's
source contained extreme repeated phrases (up to 41 repeats) and confusable
Cyrillic characters. Source normalization collapsed the pathological repeats;
the completed artifact now records **154/154**, with chunk 133 accepted on its
first attempt at 99.1% source-token recall. Grimgar's single-pass outputs ended
normally but repeatedly omitted the same prose: full-chunk recall stayed
82.7–85.8%, and recursive halves/quarters were no more reliable, ruling out a
context ceiling. The production three-pass path deterministically presegments
quotes and completed the entire **71,602-word, 3,305-entry** book across 142
chunks.

**MET on an unseen four-book current-path rerun, 2026-08-16.** The clean
qwen3-14b campaign in `unseen_three_pass_20260815` completed **807/807 chunks
(100%)**, 411,746 words and 15,728 entries with no failure codes: mushoku18
110/110, grimgar06 142/142, mushoku23 241/241, and arc4_volume10wn 314/314.
This clears the 99% target on every book and confirms that the three-pass escape
path generalises across the four previously unseen formats.

**The qwen2.5-14b figures this goal used to quote are still not in the evidence
tree**, and the 15 historical failures remain unattributable - their manifests
record no model. That is now impossible for new runs, since both failure call
sites record `model_name`.

**The 15 failures cannot be attributed to any model, and that is the finding.**
All 15 have `status: failed` with `failure: chunk_failed_after_retries` after 8
to 33 attempts — genuine exhaustion, not interrupted runs. None records
`model_name`. All 15 response logs survive in `logs/responses/`, and none of
them names a model either: they record chunk, attempt, finish_reason and token
counts only. So the worst generation failures this app has ever produced are
permanently unattributable.

---

## 5. Text handling


### 5.2 Names pronounced consistently

> **What this is.** A dictionary telling the voice engine how to say unusual
> character names, so a name sounds the same on page 1 and page 300.
>
> **Why it matters.** Inconsistent pronunciation of a main character's name is
> the kind of flaw a listener cannot stop noticing once they have noticed it.
>
> **Why this is reachable, and the trap already avoided.** The machinery is
> built and tested; the dictionary just ships empty. Two traps were handled in
> advance. First, capitalisation matters: in one book "Felt" is a character and
> "felt" is an ordinary verb, appearing 242 and 65 times, and respelling the
> name must not touch the verb. Second, nicknames record *identity*, not
> *sound* — the app knows "Betty" is "Beatrice", but saying "Beatrice" aloud
> where the book wrote "Betty" would put a word in the audio that is not in the
> book. That is worse than mispronouncing it.

**Metric** — character names spoken the same way across a book.
**Probe** — `app/pronunciation.py`, `pronunciation.json` (ships empty).
**Current** — infrastructure exists, lexicon empty. **BASELINE TAKEN 2026-08-08** (`name_consistency.py`, 48 saved books):

| | |
|---|---|
| character names appearing in prose | 454 |
| spelled more than one way, beyond capitalisation | **23 (5.07%)** |
| of those, covered by the pronunciation lexicon | **0** |
| lexicon entries in total | 5 |

The dominant pattern is deliberate authorial syllabification — `Subaru` 1068
times against `Su-ba-ru` once, a stylistic stretch the engine will voice as
three separate syllables. Rare per book, but each instance is audible, and the
lexicon covers none of them.

**Capitalisation is excluded and that mattered.** Counting it gave 5x more
hits, all false: a character named Felt collides with every sentence-initial
"Felt", and the engine pronounces those identically. Only variation surviving
case-folding — an accent, a hyphen, a different letter — is counted.

**Measured on the prose, not the speaker labels.** The first version of this
counted variant `speaker` values and found 0 of 777, because those are
upper-cased and canonicalised upstream and cannot vary. That was a protected
surface, the same mistake as auditing TTS input rather than output (5.1).

#### WHEN a respelling is applied matters more than which one

The lexicon still ships empty, so nothing below changes the current number. It
changes the rule the lexicon has to follow when it is filled, and that rule was
not obvious. Measured 2026-08-19/20 over 1,582 terms with at least five book
appearances, paired, on the shipped hyphen form and on the same form with the
separator removed (`respelling_hyphen_allrows_n1600.json`,
`respelling_none_allrows_n1600.json`, scored by `respelling_selectivity.py`):

| | rescues where the plain reading FAILS | breaks where it already WORKS |
|---|---|---|
| hyphen (shipped) | 199 / 1306 = **15.2%** (CI 13.3–17.3) | 193 / 277 = **69.7%** (CI 63.9–75.0) |
| no separator | 131 / 1281 = **10.2%** (CI 8.6–12.0) | 219 / 301 = **72.8%** (CI 67.4–77.7) |

**Both forms break roughly seven of every ten words the engine was already
saying correctly.** Applied to every term the shipped form nets **+6 words**
(199 wins, 193 losses, p=0.80 — indistinguishable from doing nothing) and the
separator-free form nets **−88** (p=2.96e-06, actively harmful). Applied only
where the plain reading fails, the shipped form nets **+199** and can cost
nothing, because those words were not being said anyway.

So a respelling should be a **conditional**, not a substitution: apply it to a
term only when the plain rendering has been shown to fail on that term. That is
a property of the *policy*, not of the derivation table, and it is the same
finding either separator gives.

#### The recogniser and the ear disagree about the separator, and both are right

Removing the separator removes the pauses. Measured over 119 terms in all three
forms (`respelling_pauses_separators_3arm.json`), internal pause time against
the un-respelled control: `none` 43/74 discordant, **p=0.20 — indistinguishable
from ordinary speech**; `space` 87/107, p=3.8e-11; `dot` 115/118, p=1.65e-30.

A blinded listener rating nine terms (`earcheck_separator_results.json`) chose
the two pause-free forms in **8 of 9** words and the most-chopped form in
**none** — dot 0/9, p=0.075; plain-or-none 8/9, p=0.020.

But the separator is doing real work. Paired on the same 1,582 terms, the
hyphen rescues **167** of the words the plain reading fails against the
separator-free form's **98** (p=1.46e-06) — the pauses are part of what makes
the syllables individually recoverable. **The recogniser prefers the hyphen and
the ear rejects it.** That is a product trade-off between intelligibility and
naturalness, not a measurement to be resolved, and it should be decided
deliberately rather than by whichever instrument was run last.

**Target — a populated lexicon for the shipped demo book, and 0 substitutions
that alter a non-name word.**

---

### 5.3 Three-pass vs single-pass generation

> **What this is.** The app contains two different designs for reading a novel:
> the one that ships, and a more elaborate three-stage alternative that nothing
> currently uses.
>
> **Why it matters.** The second one has been carried along — with its own
> settings and instruction files — without anyone ever measuring whether it is
> better. It is either an unrealised improvement or dead weight, and right now
> nobody can say which.
>
> **Why this is reachable, and why either answer is fine.** It needs one fair
> comparison: both designs, same books, same settings, scored against the same
> answer key. Then it gets connected up or deleted. The goal is to *stop not
> knowing*. Carrying an unmeasured alternative forever is the only outcome that
> is not acceptable.

**Metric** — accuracy of `three_pass_generate.py` against the shipped single
pass, paired on line id.
**Probe** — `app/experiments/three_pass_vs_single.py`.
**Current** — **ANSWERED 2026-08-09.** Two books, both arms, qwen3-14b:

| book | single | three-pass | delta | comparable lines |
|---|---|---|---|---|
| mushoku16 | 45.5% | 40.3% | **−5.2** | 134 |
| owarimonogatari3 | 58.0% | 40.6% | **−17.5** | 143 |

**Three-pass loses on both.** Note the shape: three-pass sits at ~40% on both
books while single-pass ranges 45.5 to 58.0, which looks less like a method
that trails and more like one with a ceiling near 40% regardless of the book.

Three-pass is roughly **twice as fast** (40m against 76m on mushoku16, the one
book where both arms were timed in the same run). For an audiobook, where a
misattributed line is delivered in the wrong character's voice, 5 to 17 points
of accuracy is not worth halving the wall time. **Do not ship three-pass for
accuracy.**

**Getting the second book required a settings change, not a code fix.**
owarimonogatari3's three-pass arm aborted at 38m on one unattributable
one-entry batch, because `three_pass_generate` defaults to
`on_exhaustion='fail'` — correct for surfacing a failure rate, wrong for an
accuracy comparison. Re-run with `fallback` (production behaviour, unresolved
spans become UNKNOWN) it completed all 3929 entries in 63 minutes.

**Scope:** two Japanese light novels in translation. Goal 1.3 established that
this is the project's narrowest evidence base, and nothing here escapes it.

#### REOPENED 2026-08-20: the comparison could not see what the arms do to the text

The accuracy figures above are sound. What they are computed on is narrower
than the verdict drawn from them. `three_pass_vs_single.norm_text` is

```python
re.sub(r"[^0-9a-z]+", "", text.lower())
```

— every quote, underscore, dash and apostrophe deleted before the two arms are
paired. That is the *right* way to match two different segmentations of one
book, and it is why the comparison works at all. But it makes the metric
**structurally blind** to any change in those characters, and three-pass makes
exactly such a change on purpose: on a fully-quoted line it takes `text[1:-1]`
and logs `stripped_dialogue_delimiters`.

Measured over the very artifacts this verdict was computed from
(`script_text_fidelity.json`), it does not strip *some* quotes. It strips all:

| book | source quoted spans | single-pass kept | three-pass kept |
|---|---|---|---|
| index18 | 1245 | 460 — **37.0%** | 0 — **0%** |
| mushoku16 | 1074 | 657 — **61.2%** | 0 — **0%** |
| owarimonogatari3 | 2224 | 1033 — **46.5%** | 0 — **0%** |

**2,150 entries differ between the arms in a way the comparison could not
report.**

#### CORRECTION: the quote-dropping is deliberate, and the real defect was fixed 2026-08-18

Written before reading `1f6be7a`, which says it plainly: generation is *told*
to drop the outermost quotes, because `text` is what the TTS voice says. That
is right for the audio. The defect was never the missing punctuation — it was
that **the fact of a line being speech was thrown away rather than moved**, and
that compliance varied so widely (22%, 16%, 1% across three books) that
downstream code could rely on the marks being neither present nor absent.

`dialogue_spans.py` fixed it: the spoken text is mapped from the **source**,
before any model runs, and each entry carries `spoken` and `source_span`.
`spoken` absent means the line could not be located — a different claim from
`spoken: false`.

The commit also records, in advance, the trap this section fell into: *"a book
whose source carries 6,925 quote marks came to be recorded here as one that
does not mark dialogue with quotes: I was reading our own lossy output and
calling it the author's convention."* The retention figures below are from
artifacts generated on 2026-08-09 and 2026-07-19 — **both predate the fix** —
so they measure the old behaviour, not the current pipeline.

They are kept because they still establish the one thing the accuracy metric
could not see, and because the asymmetry they expose is real and was not fixed
until today: single-pass carried the map, **three-pass never did**.

#### The pre-fix numbers, and what they were mistaken for

Measured against the SOURCE rather than against the other arm, single-pass is
not a clean baseline that three-pass departs from. It discards 39–63% of the
book's quoted spans by itself. And on a production title outside this
comparison — `arc4_volume10wn`, generated by the shipped single-pass path —
retention collapses:

| | |
|---|---|
| quoted spans in the source | **3,434** |
| entries carrying a quote in the generated script | **67** |
| **retention** | **2.0%** |

Read correctly, that spread — 61% to 2% on the same instruction — is not a
scandal about lost punctuation. It is the evidence that **punctuation was never
a usable signal for whether a line is speech**, in either arm, which is exactly
why the map was built. The 2.0% book is not a broken audiobook; it is a book
whose script could no longer answer "which lines are dialogue" until
`source_span` carried the answer beside it.

**One caveat on the metric, and it is the user's.** Quote marks are one
convention among several — dialogue can be marked with dashes, with nothing at
all, or by layout, and a book using another convention would score 0% here
while losing nothing. That is why retention is measured against **each book's
own source**: `arc4_volume10wn` uses quote marks 3,434 times, so for that book
the measure is sound. It should not be applied to a book without first checking
that the book quotes at all.

**Whether that matters was also assumed, so it was measured at the speech
boundary** — `normalize_for_speech` is what the engine actually receives:

- **`"` survives to the engine.** It is not in `SPEECH_BREAKS`. So single-pass
  sends quote characters to TTS and three-pass sends none: a difference in what
  gets synthesised, not only in what is readable on the page.
- **`_` is removed and replaced by a sentence break.** `He said _hello_
  softly.` reaches the engine as `He said. hello. softly.` — three sentences
  where the author wrote one. This happens for **both** arms, so it is not a
  difference between them; it is a separate finding about emphasis markup
  becoming prosody. It is also rare in this corpus: one entry in three books.
- **`-` survives unchanged**, and is neither a differentiator nor altered.

**What this changes about the target.** "Wire it in or delete it" was to be
decided on accuracy alone. Accuracy still favours single-pass by 5.2 and 17.5
points and nothing here softens that. But the deletion case is now *stronger
and better founded* than the goal recorded — three-pass also destroys the
dialogue delimiters that reach the voice engine — while the comparison that
produced the verdict remains unable to say so on its own. The blindness is
pinned by `test_script_text_fidelity.py` rather than fixed, because fixing it
would break the pairing; the tests exist so the next reader of 5.3 finds a
statement of what it does not measure.

#### THE NEW TEST RAN, AND IT SPLITS 2-1 — 2026-08-20

**No GPU was needed after all.** The map is derived from the SOURCE, so it can
be applied to scripts generated before it existed: `retrofit_dialogue_map.py`
locates each entry's text in its own source and marks it. On the worst case in
the library — `arc4_volume10wn`, the 2%-retention book — it still locates 89.4%
of entries. The 5.3 pair retrofits at 84.1–95.7% (single) and 70.3–88.2%
(three-pass).

**Of the lines the source confirms are dialogue, and that BOTH arms located,
how many did each arm attribute to a character at all?**

| book | paired lines | single | three-pass | McNemar |
|---|---|---|---|---|
| index18 | 760 | **84.5%** | 75.7% | 1.8e-06 |
| mushoku16 | 917 | 58.9% | **84.5%** | 5.5e-50 |
| owarimonogatari3 | 1663 | **86.5%** | 77.1% | 7.2e-13 |

**Single-pass wins two, three-pass wins one — and it wins it on the book where
single-pass is worst** (58.9%, its only sub-80 figure). Every result is
overwhelmingly significant, so this is not noise; the arms fail *differently*,
and which is better depends on the book. That matches [[style_routing_per_book]]:
methods here split hard by writing style.

**This nearly went out wrong, twice.** The first version of the comparison
scored agreement about `spoken` and got 100% on every book — a tautology, since
both arms read that fact from the same source. The second counted `UNKNOWN` as
an attribution because it is not `NARRATOR`, which put three-pass ahead by
10–37 points on all three books; three-pass alone carries 118 UNKNOWN lines on
mushoku16. Counting an explicit "I cannot tell" as a success reversed two of
three results. Both traps are now pinned by tests.

**What it does NOT say.** This metric asks whether the arm named *anyone*, not
whether it named the right person — a wrong name counts as attributed. That is
the old 5.3 metric's question, and both are needed: single-pass is better at
*who*, three-pass is better at *not giving up*. For an audiobook the two
failures sound different — dialogue read in the narrator's voice against
dialogue read in the wrong character's voice — and which is worse is 7.1's
question, not this one's.

**The target should no longer read "wire it in or delete it."** Neither arm
dominates. The open question is whether the choice is per-book, and 5.3's
two-book sample cannot answer that.

#### THE WHOLE LIBRARY IS NOW MEASURABLE — 29 books, not 1

The map is derived from the source, so it retrofits: `retrofit_dialogue_map.py`
matched all **29 saved books** to their source texts by content (filenames do
not map, and no manifest records the pairing) and located **89.4–96.5%** of
entries in each. Nothing was regenerated and `scripts/` was not modified.

Asked which source to trust, the two candidates were measured rather than
argued. Extracting `Arc 1 - Volume 1.epub` through the app's own
`extract_epub_text` against the plain-text copy: 0.414 M chars against 0.418 M,
**89.3% of script lines located against 89.7%**, same convention detected. The
text extractions are faithful; either source serves.

**A third instance of the same bug had to be fixed first.**
`measure_dialogue_attribution.measurable()` refuses a book whose entries carry
too few quotation marks — correct when punctuation was the only way to see
dialogue, and paid for by the detector that found 22 spoken lines in a
6,173-entry book. But `classify()` already prefers the recorded `spoken` fact,
and the gate ran ahead of it and never consulted it. It refused **28 of 29
retrofitted books**, each reported as "does not mark dialogue with quotes"
while carrying a map built from a source that quotes 3,434 times. A guard built
for the guess, still blocking after the guess had been replaced.

**With that fixed, the shipped pipeline measures well:**

| | |
|---|---|
| books measured | **29 of 29** (was 1) |
| spoken lines | 36,705 |
| left attributed to NARRATOR | 951 |
| **rate** | **2.6%** (range 0.5–6.6% per book) |

This goal previously rested on one book. It now rests on the whole library, on
source-derived truth rather than punctuation, and the answer is that dialogue
is misfiled as narration about once in forty lines.

#### THE EXPANDED TEST, QUEUED 2026-08-20

The retrofitted answer above is on scripts generated 2026-08-09, which predate
a fortnight of changes to both generators — near-miss repair, narrator hints,
source speaker labels, the map itself. So it describes a pipeline that no
longer exists, and it rests entirely on **four Japanese light novels from one
person's library**, which is [[Rule 1.3]]'s standing complaint about this whole
project's evidence base.

`run_chains/dialogue_map_5_3_20260826.sh` re-runs both arms fresh on seven
books: the three light novels, plus **four PDNC novels** — public domain, with
quotation annotations published by other researchers, so the result is on
record and checkable by someone who is not us. PDNC also carries **gold speaker
labels**, which lets both axes be measured on one run: did the arm name anyone,
and was that anyone right.

**The four were chosen by PDNC's own quote types, not by feel.** Explicit
quotations name the speaker beside the line and are the easy case:

| novel | Explicit | Anaphoric | Implicit | quotes | characters |
|---|---|---|---|---|---|
| TheGambler | 12% | 50% | 39% | 767 | 27 |
| TheSignOfTheFour | 13% | 36% | 51% | 640 | 35 |
| TheMysteriousAffairAtStyles | 13% | 19% | 68% | 1861 | 30 |
| AHandfulOfDust | 18% | 9% | **74%** | 2337 | **104** |

`AHandfulOfDust` is the extreme on both axes at once — three quarters of its
dialogue names nobody, across a cast of 104. `AlicesAdventuresInWonderland`, at
82% Explicit, is deliberately excluded: it would flatter both arms.

Cost, scaled from mushoku16's measured 75.5 min single / 39.7 min three-pass at
0.29 MB: roughly **12–14 hours**. The public books run first, so a chain that
dies overnight has still produced the evidence that is not already here.

#### THE TEST AS ORIGINALLY QUEUED, 2026-08-20

The map makes 5.3 answerable on something the old key could not delete.
`dialogue_map_compare.py` compares the arms on `spoken`/`source_span` rather
than on punctuation: how many of each arm's entries can still be located in the
source, whether the source calls them speech, and — on the lines **both** arms
located — whether they agree, with McNemar over the disagreements.

**Three-pass was wired to the same map to make that fair.** It had never
carried one. Comparing before that would have measured which arm received a
patch, not which design is better — the same confound, one level up, that this
whole section is about.

Nothing can be scored yet: every script on disk predates the map, and the
comparator **refuses** such a pair rather than reporting 0% located as an arm
failure. `run_chains/dialogue_map_5_3_20260826.sh` re-runs both arms on
mushoku16 and owarimonogatari3 through the existing harness — one definition of
how to run an arm, not a second — and then scores accuracy, dialogue map and
text fidelity **on that one run**, so the axes cannot be attributed to
different generations. Roughly four hours.

What would move the verdict: 5.3 says delete three-pass on a 5.2–17.5 point
accuracy deficit. If it locates its lines as well as single-pass does, that
deficit is the whole case and it still loses. If it locates markedly fewer, the
case is stronger than recorded. If it locates **more**, that is the first
evidence in its favour and this goal should say so.

**Still not measured:** whether a listener can hear the difference between a
quote reaching the engine and not. That is 7.1's question and needs ears.

**Target — one clean comparison, then wire it in or delete it.**

---

#### Being re-answered on fresh scripts, and the interim disagrees

The 2026-08-09 answer was taken on scripts generated 2026-08-09, before a
fortnight of changes to both generators. A fresh run is in flight
(`dialogue_map_5_3_20260826.sh`). Two of three light novels have both arms:

| book | single | three-pass | delta | comparable |
|---|---|---|---|---|
| index18 | **70.9%** | 50.6% | −20.3 | 79 |
| mushoku16 | **46.3%** | 41.8% | −4.5 | 134 |

Single-pass leads on both, and **mushoku16 reverses** the recorded result,
which had three-pass much better there. Neither three-pass run failed: both
report `status: complete` with zero diagnostic failures and no exhaustion
fallbacks, so this is not a degraded arm. Three-pass was also the FASTER arm —
54 min against 113 on index18, 38 against 68 on mushoku16.

owarimonogatari3 is missing: the stage was killed by its own 6h cap at chunk 86
of 110 and produced neither arm, so the scoring step never ran and the run
wrote no combined artifact at all — which is why the table above is assembled
from the per-book files rather than cited. Re-queued after #381 made the
finished three-pass arms reusable. **The four PDNC books are the half that
makes this checkable by someone else, and they are running now.**

Treat the table above as interim: two books, 213 comparable lines, and
[[ab_underpowered_single_pass]] applies.

### 5.4 Transcription and clip boundaries in the preparer

> **What this is.** Before any voice can be trained, an audiobook has to be
> transcribed and cut into clips. This measures how well that first step works.
>
> **Why it matters.** It is upstream of everything in section 2. A clip cut
> 30 ms early loses a consonant off *every* training sample; one cut 300 ms
> late drags in a word from the next line. Neither shows up as an error — it
> shows up later as a voice that never quite sounds right, with no way to trace
> the cause. Getting the words wrong is the more obvious failure and the less
> damaging one.
>
> **Why boundaries decide the choice.** Word accuracy is easy to compare and
> tempting to rank on. But the preparer needs the audio *sliced*, so a backend
> that hears perfectly and cannot place a boundary is useless to it.

**Metric** — WER (CER for CJK) against human transcripts, plus alignment error
against known boundaries.
**Probe** — `app/experiments/asr_backends.py`. The alignment probe concatenates
clips with 0.5 s gaps, so boundary truth is *arithmetic* — the only answer key
in this project that cannot itself be wrong.
**Current** — 50 clips per language, 2026-08-06:

| lang | backend | WER/CER | align median | within 300 ms |
|---|---|---|---|---|
| EN | whisper.cpp base | 3.0% | **80 ms** | **90%** |
| EN | whisper.cpp large-v3 | **1.8%** | 45 ms | 80% |
| EN | SenseVoice | 3.8% | 10158 ms | 20% |
| ZH | whisper.cpp base | 44.3% | **84 ms** | **100%** |
| ZH | whisper.cpp large-v3 | 14.1% | 826 ms | 20% |
| ZH | SenseVoice | **11.3%** | 17957 ms | 10% |

**MET for English and Chinese. OPEN for Japanese — the n=10 result did not
survive confirmation at n=50, 2026-08-16.**

**Japanese: Silero windowing fixes the segmentation, 2026-08-16.** The
diagnosis below — that both whisper.cpp checkpoints fail to segment Japanese
— was correct about those checkpoints and wrong as a dead end. Putting a
Silero VAD in front of whisper.cpp finds the boundaries the checkpoints miss
(`asr_silero_whisper_ja_offset20.json`, held-out rows 20–29):

| JA arm | CER | align median | within 300 ms | segments |
|---|---|---|---|---|
| whisper.cpp base alone | — | — | — | 5/10 found |
| **silero + whisper.cpp** | **7.67%** | **39 ms** | **90%** | 14 predicted / 10 scored |

Against the target below that is CER 7.67% ≤ 20% and alignment 39 ms ≤ 150 ms
— both clear, with the best alignment median of any language. The probe scores
character-level for Japanese (`asr_backends` auto-detects CJK from the
reference), so the 7.67% is a CER despite the artifact's `wer_mean` key.

**Confirmation at n=50 refuted it, 2026-08-16.** Since 5.4 measures
transcription and alignment rather than voice identity, it does not need the
same-speaker design the build inherits from the voice goals, so a 50-clip set
was cut from the four Japanese novels already on disk
(`kokoro_ja_asr_set.py`, `asr_silero_whisper_ja_confirmation.json`):

| reader | n | CER | align median | within 300 ms |
|---|---|---|---|---|
| kouyahijiri-by-kyoka-izumi | 13 | 24.6% | 347 ms | 38% |
| kusamakura-by-soseki-natsume | 13 | 26.2% | 243 ms | 69% |
| botchan-by-soseki-natsume-2 | 11 | 29.2% | 400 ms | 36% |
| gan-by-ogai-mori | 13 | 34.9% | 117 ms | 69% |
| **pooled** | **50** | **28.7%** | **272 ms** | **58%** |

Against CER ≤ 20% and alignment median ≤ 150 ms, the pooled result fails both
and **every reader fails on CER**. This is not one bad recording dragging a
mean: the spread is 24.6–34.9% with no reader near target, and
over-segmentation is uniform at 1.8–2.1× expected.

#### That table is the ORTHOGRAPHIC column, and the reading column already exists

Found 2026-08-20 by reading the artifacts rather than the summary. Every
figure above is `wer_mean` — character agreement on the written form. This
project decided long ago that the written form is the wrong thing to score
Japanese on: `asr_backends.to_reading` exists precisely so "scoring compares
sounds not script", and its own docstring records the reason — *CER 28.7%, CER
on readings 9.9%. Two thirds of the "error" was orthography.*

**The reading-space score for this very set was taken on 2026-08-19** —
three days *after* the confirmation above — and never reached this document:

| `asr_ja_readings.json`, n=50, same build | value |
|---|---|
| `wer_mean` (orthographic, the table above) | 0.2871 |
| **`cer_reading_mean`** | **0.0989** |
| `cer_reading_median` | 0.0733 |

Three further artifacts agree within a point: `asr_ja_cutting_control` 0.1006,
`asr_ja_largev3_readings` 0.1023, `asr_ja_trimmed` 0.0971.

**So the CER half of this target is met at 9.9% against ≤ 20%**, and the
sentence "every reader fails on CER" is reading the wrong column. It is exactly
the [[Rule 19]] failure the document warns about — the numbers were right, the
sentence around them was not — and it is the second time this artifact's
`wer_mean` key has misled a reader into treating a CER as a WER.

Two things this does **not** say, both stated so the correction is not
overtaken by its own enthusiasm:

- **Per-reader reading CER is now measured too, and every reader passes.**
  `asr_reading_rescore.py` recomputes it from the hypotheses `--keep-hypotheses`
  already stored — arithmetic on committed text, no audio, no model, no GPU.
  The written column reproduces the table above exactly, which is what confirms
  it is the same set:

  | reader | n | CER as written | **CER on readings** |
  |---|---|---|---|
  | botchan-by-soseki-natsume-2 | 11 | 0.2921 | **0.0630** |
  | gan-by-ogai-mori | 13 | 0.3486 | **0.1010** |
  | kouyahijiri-by-kyoka-izumi | 13 | 0.2465 | **0.1362** |
  | kusamakura-by-soseki-natsume | 13 | 0.2620 | **0.0899** |
  | **pooled** | **50** | **0.2871** | **0.0989** |

  The worst reader is 13.6% against a 20% target. So "every reader fails on
  CER" is not merely reading the wrong column — it is inverted: on the measure
  this project argues is the correct one, **every reader passes**.
- **pykakasi returns *a* reading, not *the* reading.** 明日 is アス or アシタ
  depending on context and the converter cannot hear which. That ambiguity adds
  error rather than removing it, so 9.9% is a conservative figure — but it is
  also the gap that ふりがなWhisper (audio-conditioned readings, 1.23% CER on
  JSUT) is built to close, if this measure ever needs to be tighter.

**Alignment is the half that genuinely still fails**, at 272 ms pooled against
≤ 150 ms, and nothing above changes it. This goal stays OPEN for Japanese —
for one reason now, not two.

**The kokoro novel is the outlier, not the rule.** Its 7.67% / 39 ms stands
against five other Japanese samples between 24.6% and 34.9%. A control on
8 dataset-cut clips of kouyahijiri — the original cutting pipeline, so no
tooling of ours involved — scored 29.0% CER, agreeing with our own 24.6% for
that reader and confirming the CER finding is not an artifact of how we cut
audio.

Alignment is the part still open, and **our cutting is not the cause**
(`asr_ja_cutting_control.json`, 2026-08-16). The dataset-cut sets score 39 ms
(n=10) and 86 ms (n=8) while our four cut sets score 117–400 ms, which looked
like a tooling difference. Cutting the dataset's **own eight utterance ids**
with our pipeline settles it — the only variable left is the cutting:

| same 8 utterances | CER | align median | within 300 ms | segments |
|---|---|---|---|---|
| dataset-cut | 29.0% | 86 ms | 100% | 15/8 |
| our cut | 28.5% | **86 ms** | **100%** | 15/8 |

Identical. So the spread is **which clips are in a set**, not how they were
made — and the per-reader figures agree, running 117 ms on gan to 400 ms on
botchan. Note also that 15 predicted segments against 8 expected did not hurt
alignment at all, so over-segmentation is not the mechanism either.

That leaves clip content: leading or trailing silence, or utterances the VAD
splits differently. The earlier attempt to test this compared different
utterances and could not have isolated the variable; this one does.

**What this costs.** Silero windowing does fix Japanese *segmentation* — the
boundaries are found where whisper.cpp alone found 5 of 10 — but finding
boundaries is not the same as placing them accurately or transcribing between
them, and only the first was demonstrated at n=10.

**The Chinese remedy does not transfer, 2026-08-16**
(`asr_ja_largev3_hybrid.json`). Chinese was solved by splitting the two jobs,
so the same split was tried on the 50-clip Japanese set:

| JA backend | CER | align median |
|---|---|---|
| silero + base | 28.7% | 272 ms |
| large-v3 alone | 27.9% | 542 ms |
| hybrid (base + large-v3) | 27.8% | 328 ms |

Chinese gained **30 points** from large-v3 (44.3% → 14.1%). Japanese gains
**0.8**. Three backends spanning a 20x difference in model size land within
one point of each other, which rules out model capacity as the cause and
means the remaining explanation is not in the ASR at all.

Two candidates remain, and the second would change what this goal should
measure rather than how well we meet it:

1. The reference transcripts do not closely match the audio. These are
   LibriVox recordings aligned to Aozora texts, and the alignment is the
   dataset's, not ours.
2. **The metric is wrong for Japanese.** Character error rate punishes
   orthographic variation that is not an error: a model writing わたし where
   the reference has 私 has transcribed the word correctly and scores it as
   total failure. Japanese is the one language here where CER without
   orthographic normalisation is known to mislead, and no arm has ever been
   scored with it.

**It was the ruler, 2026-08-16** (`asr_ja_readings.json`,
`asr_backends --score-readings`). Scoring the same 50 clips on kana readings
rather than characters — the same audio, the same model output, the same
model:

| | mean | median | vs ≤20% |
|---|---|---|---|
| CER as written | 28.7% | 26.2% | FAIL |
| **CER on readings** | **9.9%** | **7.3%** | **PASS** |

**Two thirds of the "error" was orthography.** Japanese prose mixes kanji and
kana by authorial preference; a model picks its own convention; and CER
charges it for disagreeing about spelling with a transcript that had no single
correct spelling to begin with. `私` and `わたし` are the same word, read
identically aloud, and scored as total failure against each other.

That also explains the n=10 result this goal opened with. 7.67% was never
unrepresentative audio — it was a transcript whose orthographic conventions
happened to match the model's, which is why the honest four-reader number
lands at 9.9% rather than anywhere near 28%.

**So the CER condition is met and the alignment condition is not.**
Alignment stays at 272 ms against a 150 ms target, unchanged by any of this,
and remains the one genuinely open axis: dataset-cut clips score 39 ms and
86 ms while our four cut sets score 117–400 ms, with the seek defect ruled
out. Japanese transcription was never the problem; Japanese *boundaries*
still are.

**The old number is kept, not replaced.** `cer_reading_mean` is reported
alongside CER so every arm measured before this stays comparable, and the
character score remains the right one for languages that are not written in
two scripts at once.

**Chinese is solved by splitting the two jobs** (`whisper_cpp_hybrid`, added
2026-08-08). base decides the boundaries, large-v3 transcribes inside each one,
and each model is used only for the axis it wins:

| ZH arm | CER | align median | within 300 ms | segments |
|---|---|---|---|---|
| base | 44.3% | **84 ms** | **100%** | 10/10 |
| large-v3 | **14.1%** | 826 ms | 20% | 11/10 |
| **hybrid** | **14.5%** | **84 ms** | **100%** | 10/10 |

CER ≤20%, median ≤150 ms, ≥80% within 300 ms — all three met. The 0.4-point
CER cost against large-v3 alone is the price of transcribing inside windows the
model did not choose, and it is small: large-v3's advantage does *not* depend
on picking its own boundaries, which was the stated way this idea could have
failed.

**Japanese does not benefit, and the reason is upstream of the hybrid.** Both
checkpoints already fail to segment Japanese — base finds 5 boundaries out of
10 (median 2833 ms), large-v3 also 5 (4463 ms). The hybrid takes base's
segmentation, so it inherits that failure exactly: 25.2% CER, 2833 ms, 10%
within tolerance. Better transcription cannot repair boundaries that were never
found, and no combination of these two checkpoints will fix Japanese. That
needs a different segmenter, not a different pairing.

A first language-independent energy-VAD arm was measured 2026-08-15 and failed:
it oversegmented internal pauses (23 segments for 10 clips), with **510 ms**
median boundary error and only **20%** within tolerance. A Silero neural-VAD arm
was then tuned on clips 1–10, checked on 11–20, and frozen before the untouched
21–30 holdout. With 400 ms minimum silence and 250 ms speech padding, that
holdout reached **39 ms median**, **226 ms p90**, and **90% within 300 ms**,
meeting the boundary target (`asr_silero_vad_ja_holdout.json`). It still emits
14 segments for 10 clips, so neural VAD solves boundary placement but production
cannot assume one VAD segment equals one utterance. Silero is not installed in
either configured environment; this result used an isolated temporary install
and does not claim the app dependency is ready.

The proposed timestamp-gap coalescer was checked on CPU on 2026-08-16 and is
not safe. On the frozen holdout, the four extra within-utterance splits have
0.2–0.4 s gaps, but genuine adjacent utterances also have gaps as small as
0.4 s after VAD padding. A threshold that removes all extra splits therefore
also merges real boundaries. Production integration needs a segmenter-to-ASR
windowing design that tolerates internal splits, followed by downstream
transcription validation; it must not guess from timestamp gaps alone.

**A note on reproducing this.** The first run of the comparison omitted
`--build`, silently scored a different clip set, and produced base 58.0% /
large-v3 67.0% — which read as "the goal's table does not reproduce". It did
reproduce, exactly, once the same build was passed. Always pass
`--build ab_test_runtime/<corpus>_eval/build.json`.

**Target — CER ≤ 20% in every language, with alignment median ≤ 150 ms and
≥ 80% of boundaries within 300 ms.**

**The finding that shapes the choice: no single configuration wins both.**
`base` is the better aligner (84 ms, 100% in Chinese); `large-v3` is the better
transcriber (44.3% → 14.1% CER) and **ten times worse at boundaries**.
SenseVoice is the best Chinese transcriber of all and effectively cannot
segment — 1 of 10 boundaries, a 17-second median error.

So the reachable path is probably not to pick one. Words and boundaries can
come from different passes, and the failure modes are exactly complementary.
That hybrid is untested; nothing measured rules it out.

---

### 5.5 Foreign words are said as foreign words

> **What this is.** Ordinary Japanese and Chinese words that appear inside
> English prose — not character names, which 5.2 already covers. A translated
> light novel keeps the words the translator chose to leave untranslated.
>
> **Why it matters.** These are not mispronounced occasionally, they are
> mispronounced *every time, identically*, which is why 5.2's consistency
> metric cannot see them. Measured 2026-08-16 by rendering each in a carrier
> sentence and transcribing with an English and a Japanese ASR:
>
>     arigatou   heard as "Ara got to"   parsed as three English words
>     kawaii     heard as "Kauai"        the Hawaiian island
>     senpai     heard correctly         needs no entry at all
>
> **Why this is reachable.** The mechanism already exists and ships empty by
> design (5.2's `pronunciation.py`). What was missing was knowing *which*
> words need an entry, and that is now measured rather than guessed.

**Metric** — `recovers_word`: the term's kana reading appears **unbroken and
in order** in the transcript, compared on readings so 人間 and ニンゲン count as
the same word. Not WER: a respelling that works makes the ASR hear Japanese,
which WER punishes.

> **This definition is the goal's second one, and the first was wrong.** Until
> 2026-08-17 the score asked whether each kana appeared *anywhere*, in any
> order, so タナカ scored a perfect 1.0 against a transcript holding タ, ナ and
> カ in three unrelated words. Of 768 terms scoring 1.0, **only 51% contained
> the word**; the rest were the mispronunciations this goal exists to catch
> (`フタバ` → フォータバー, `セイイチ` → セイチー) scored as successes. The
> generation was checked before the scoring was blamed: 12 of 12 stored WAVs
> re-transcribe byte-identically, 0 of 12 plain/respelled pairs are the same
> audio, 0 render errors across 5,880 terms. The audio was always fine; the
> question asked of it was not. See Rule 21 and
> `app/experiments/rescore_respellings.py`.

**Probe** — `app/experiments/measure_respellings.py`, over candidates found by
`discover_foreign_terms.py` and `lexicon_corpus_scan.py`. Finished runs are
re-scored without regenerating audio by `rescore_respellings.py`.
**Current** — 9,381 candidates scanned from 6,501 EPUBs; **6,060 measured and
re-scored**. **OPEN.**

**Evidence** — `respelling_measure_rescored.json` (7,775 terms, the -eh
baseline every arm below is paired against) and `respelling_e_row__ay_n1600.json`
(1,419 terms, the whole pool containing an -eh mora). The three /e/-row arms
are `respelling_e_row__e.json`, `respelling_e_row__ei.json` and
`respelling_e_row__ay.json`; `respelling_e_row__ay_n1200.json` is **partial**
(1,129 of 1,200 terms) and is labelled so in the structural audit — it is
superseded by the n1600 run and should not be quoted.

**A LISTENER OVERTURNED THE ROW CHANGE THE SAME DAY, and the metric with it.**
Eight terms, three takes each, positions rotated, key hidden until each answer
was saved (`respelling_earcheck.json`). On the four terms that justified the
change — the ones where ASR heard the whole word in `-ay` and not in `-eh` —
the listener chose the `-ay` take **0 times**, and chose the *un-respelled*
take 3 of 4. Across all eight: no respelling 4, `-eh` 1, `-ay` 1, "none
sounded right" 2. The listener's choice matched the recogniser's in **2 of 8**.
`respell()` is back to `-eh`; `-ay` is not disproven, it is unsupported by the
only instrument that measures what this goal claims.

**Six of the eight notes named the same thing unprompted — pauses.** "Weird
pauses", "sounded robotic", "the biggest problem is the pausing". That is a
mechanism, and it measures: over 400 terms, respelled clips pause internally
where plain ones do not — **341 of 384 discordant terms, sign test p=1.1e-58**
(`respelling_pauses.json`) — and the two vowel rows are indistinguishable
(p=0.10). So the pause belongs to the *form* of a respelling, not its vowels.

**Why that would fool the metric.** `recovers_word` asks whether the reading
appears unbroken and in order in the transcript. A voice that says the pieces
cleanly, with gaps between them, satisfies that exactly — while a listener
hears a chopped non-word. The metric and the ear then diverge systematically,
which is what happened. **The hyphen is the suspect** (`seh-n-seh-ee`), and
`--separator none|space|dot` arms are queued to test it, with a second
listening test to follow. Until that lands, no respelling figure in this goal
should be read as a claim about how anything sounds.

**The /e/ row was measured and changed, 2026-08-18 — and reverted.** Paired on identical
terms against the shipped `-eh`, with the plain (no respelling) arm as an
explicit noise floor:

    arm    recovery   vs -eh    McNemar p    plain control p
    -ay      14.0%     2x        2.5e-11        0.77
    -ei      10.5%     -         0.25           0.51
    -e        2.0%     0.25x     1.9e-4         0.39

`-ay` held at 391, 780 and 1,419 terms as the sample grew and the control is
null at every size, so this is not the TTS→ASR drift that flips 34 verdicts in
391 on identical input. `respell()` now defaults to `-ay`; `-eh` stays
selectable so the comparison remains reproducible. **This changes no audio
today**: `pronunciation.json` ships 42 names with empty respellings and nothing
in the app calls `respell()` — the table feeds the measurement, not the
product.

**The result that matters is not a list of respellings, it is when to use
one** — and the honest answer is *rarely*:

| | terms | outcome |
|---|---|---|
| plain spelling already said it right | 967 | respelling **breaks 72%** of them |
| plain spelling got it wrong | 5,093 | respelling **rescues 13%** of them |
| net across all 6,060 | | **687 recovered against 701 lost** |

**Respelling is roughly break-even, and actively harmful applied broadly.** The
entry rule stands and is now the stronger claim: respell only where the plain
form demonstrably fails, and even there expect it to work about one time in
eight. Rule B (vowel absorption) is worse — 7 recoveries in 268 terms.

The useful output is the near-miss list: terms where the phonemes moved the
right way and one specific thing is still wrong — `カワラマチ` → こわらマチ,
`サカキバラ` → さっかきばら, `ウチガタナ` → うちがたんな. Those are fixable by a
hand-written entry in a way that "the model has no idea" is not.

**BOTH HALVES NOW EXIST, over the measured corpus (2026-08-20,
`lexicon_candidates.json`, built by `lexicon_from_measurements.py` from
artifacts already on disk - no GPU, no regeneration).** Of 7,607 measured terms:

| | |
|---|---|
| the plain reading already says the word | 933 — *an entry here would do harm* |
| measured to help → **entry** | **1,056** |
| recorded as one respelling could not fix | **5,618** |

Every term the plain reading fails is now in one of the two required states.
The 933 are excluded on measurement, not taste: respelling breaks **69.7%** of
the words the engine already said correctly.

**How far to trust the 1,056 is a separate question, and the answer is "less
than it looks".** Restricted to terms that more than one arm actually measured,
a rescue reproduced only **101 of 380 times (26.6%)**. Some of that gap is a
real separator effect - the hyphen rescues 15.2% against the no-separator
form's 10.2% - and the rest is the pipeline's own churn, which is not small: 34
of 391 verdicts flipped on IDENTICAL input in the plain control. Each entry
therefore records `arms_measuring`, `arms_rescuing` and `corroborated`, and
**101** of the 1,056 are corroborated by more than one arm while **676** were
measured only once and cannot be corroborated either way.

**So the lexicon is not written from this file automatically.** Shipping 1,056
entries built on single readings would be the 38%-versus-13% mistake wearing a
different hat. `--write-lexicon` exists and is off by default.

**Target — every term in the shipped books whose plain form does not produce
the word either has a measured entry or is recorded as one respelling could
not fix.**

**What remains between this and MET:** the record above is over the *measured
corpus*, not over *the shipped books*. Terms carry a book COUNT, not a book
list, so mapping these terms onto a specific shipped book needs a scan of that
book's text - which is cheap and has not been run. Until it is, this is the
right record of the wrong population, and the goal stays open. Both halves matter, and the second is now the larger: **87% of that
band was not rescued** by either derivation rule, so most of this goal's
output will be recorded failures rather than entries. A word a respelling
cannot help needs saying so, rather than leaving a blank that reads as an
oversight.

**Not settled by the ASR.** Kana agreement shows the phonemes moved; it does
not show the result sounds natural in an English sentence. Entries are
proposed by measurement and confirmed by ear — see 7.1.

---

## 6. Measurement integrity



Goals about the instruments themselves. These earned their place by failing.

> **Why a whole section on this.** Every number above is only worth what the
> thing that produced it is worth. A broken ruler does not announce itself — it
> just quietly reports plausible numbers that are wrong, and those numbers get
> believed and acted on. Each goal here exists because a measurement was
> trusted that should not have been.

### 6.5 Someone has looked at the audio

> **In short.** Every audio number in this document summarises a sound nobody
> has heard or seen. A mean hides the failure that a picture shows in a
> second: a clip that is half silence, a voice an octave out, a boundary in
> the wrong place, a "reference" recording that is the wrong speaker.
>
> **This has already cost twice.** The Chinese arm scored `human_vs_human`
> 0.691 while its own arms reached 0.720 and 0.765 — a narrator matching
> herself worse than a synthetic voice matched her. It took writing an
> anchor-validity check to notice, and a spectrogram would have shown it
> immediately. On 2026-08-16 Japanese held 28% CER across base, large-v3 and
> the hybrid alike; three models spanning a 20x size range agreeing that
> precisely is not a model problem, and no one has yet looked at the clips to
> see what it is.

**Metric** — audio arms whose clips have a rendered view before their numbers
are believed.
**Probe** — `app/experiments/voice_compare_view.py` (waveform, mel-spectrogram
and f0 against the human, per line) and `app/experiments/asr_clip_view.py`
(waveform, spectrogram, reference and hypothesis, for the clips an ASR arm
scored worst).
#### A person listened, and heard something no metric here measures — 2026-08-21

`listening_verdicts.json`. The first human ratings this project has ever
carried: 3 of the 10 character pairs in `character_distinctiveness.json`, one
rater, unblinded. The distinctiveness numbers held up — the pair at 0.9
semitones drew "probably_different… sometimes sounds like a little girl but
not always", and both pairs above 6 semitones drew "clearly_different", so the
ordering the metric gives matches what was heard on all three.

**The verdicts agreed. The notes did not.** Two of the three say the audio is
*bad* — "the highs of it were breaking, like the character's voice was
cracking, it was very bad", "very unpleasant". Both point at one clip,
`chapter_audio/2702aec220b0.wav` (Subaru, `lora`), which appeared in both
rated pairs. Nothing in this document measures that. Distinctiveness, f0
spread, speaker similarity and duration ratio can all be MET on a clip a
listener calls unpleasant, and on this evidence at least one of them is.

That clip carries **the highest median f0 of Subaru's 15 clips, 386.5 Hz
against the character's own 264.8 Hz** — about 7 semitones above centre. It is
one clip heard twice, so "this render is bad" and "the voice breaks when
pushed high" both fit; three higher-pitched Subaru clips and a control were
sent to the rater on 2026-08-21 to separate them, and are unanswered.

**A cracking metric was built and discarded before it was used** ([[Rule 21]]).
Frame-to-frame f0 jumps above 6 semitones looked diagnostic on Subaru
(12–28% of frames) until the NARRATOR — whose audio drew no complaint — scored
10.4–17.8%, overlapping Subaru entirely and exceeding the flagged clip's
15.9%. It measures YIN octave errors, not audible cracking. Recorded in the
artifact so it is not rebuilt.

**Current** — **6 views and 3 rated pairs, still OPEN.** Three views from 2026-08-06 (`ljspeech`,
`kokoro`, `aishell3`) and three added 2026-08-16: `kokoro_same_speaker.html`,
`aishell3_SSB0748.html`, and `asr_clip_view/japanese_worst.html` — the ten
worst-scoring Japanese clips with their waveform, spectrogram, reference,
what the model returned, and the audio to play.

The second of those paid for itself the day the goal was written. Reading the
worst clips beside their transcripts is what showed the Japanese "error" was
kanji-versus-kana rather than mishearing, which turned a 28.7% CER into 9.9%
and moved 5.4's transcription condition from failing to met — a mean could
not have said that, and had not, across three backends and six weeks.

**Still unlooked-at:** the honest retrains, both reference-rank campaigns, the
21 identity gates, and the promoted adapters now shipping.

**The gates could not be looked at, and now can.** `verify_adapter_identity.py`
records one number and a verdict - `median_ecapa`, `passed` - with no rows and
no clip paths, so nothing said which audio a gate had scored. The audio was
there the whole time: the gate renders into `<adapter>/identity_check/` and
leaves it, 95 such directories exist, and it consumes the dataset's val split
in order, so `check_<i>.wav` is the model's reading of val line `<i>`.
`gate_view.py` reconstructs the pairs without regenerating anything - which
matters, because a regenerated clip is a different sample and could not explain
the number already recorded. Four rendered 2026-08-20, deliberately spanning
the range: 0.034 and 0.393 (both FAIL) against 0.752 and 0.781 (both PASS).

**And the views are not in the repository.** Every one of the six this goal
credits is untracked - they are 9-18 MB of HTML with the audio embedded, and
the view directories are gitignored on purpose. Seventy gates at ten megabytes
each is not a repository, so that will not change. But it means this goal's
evidence has never survived a clone, which is the condition 6.1-6.3 exist to
prevent. `audio_views.json` is the part that does survive: for each view, the
arm, the file, its size and SHA-256, and the gate number it was rendered to
explain. It records `looked_at: false` for all ten, because rendering is not
looking and this goal is about the looking.

**Target — a rendered view for every audio arm whose numbers appear in this
document.**

Neither probe is a metric and neither should become one. Comparing raw
waveforms sample-by-sample is meaningless for TTS — two identical-sounding
readings differ completely in phase and micro-timing. Eyes are for catching
the gross failure a number hides, not for ranking arms.

---

## 7. The finished audiobook


### 7.1 A listener prefers what we ship

> **What this is.** Playing finished audio to a person and asking which
> version is better, without telling them which is which.
>
> **Why it matters.** Every other goal in this document measures a part:
> whether the right character was credited, how close a voice sits to its
> target in cosine distance, whether a clip's duration falls in a band. Not
> one of them measures the thing the project actually makes. Each goal's box
> opens by explaining why it matters "to someone listening" — and then no goal
> asks anyone to listen. A pipeline can pass all twenty-five and still produce
> an audiobook nobody wants to hear, and nothing here would notice.
>
> **Why this is reachable now.** The instrument is already built and sealed.

**Metric** — blinded preference between paired renders of the same passage.
**Probe** — `app/experiments/blinded_listening.py`, which renders the sets and
conceals the key in `ab_test_runtime/blinded_listening_concealed_key.json`.
**Current** — **NO BASELINE.** The package exists and has never been rated:
20 clips across 8 sets, key still concealed, and the artifact records its own status
plainly — `"No human ratings are included; this artifact only prepares
it."`

**No target yet, deliberately.** A preference threshold invented before any
human has heard a set would be the "invented number" this document's own rules
forbid. The first task is the listening, not the fix.

**A listener HAS now rated a different package, and it worked.** On 2026-08-19
the same person rated nine terms of the respelling separator comparison
(`earcheck_separator_results.json`) — four takes each, shuffled per term, key
held in a file the page never contained. It produced a usable result at
p=0.020 and agreed with the pause measurement, and the free-text notes are what
identified the mechanism in the first place. So the method is not the obstacle;
this goal's own 20-clip package simply has not been put in front of anyone.
It measures a different thing — paired renders of a passage, not respelling
forms — and is still unrated.

**This is the cheapest open goal in the document** and the only one that
cannot be run on the GPU: no card, no code, no experiment design. One person,
headphones, and the concealed key afterwards.

---

# Part II — Met

*Measured at or beyond target, each keeping a test so it stays there. Nothing here needs work; it needs not to regress.*

## 1. Speaker attribution — who says which line

### 1.2 Close the selection gap

> **What this is.** Before deciding who spoke, the app assembles a shortlist of
> plausible characters. Two separate things can go wrong: the right name might
> not be on the shortlist at all, or it might be there and get passed over.
> This measures the second.
>
> **Why it matters.** It tells us where the real problem is. The shortlist
> contains the correct name **85%** of the time, but the app only picks it
> **29.9%** of the time. So the information is already sitting in front of it in
> the overwhelming majority of cases — and it looks straight past it. That is a
> very different problem from not knowing, and it needs a very different fix.
>
> **Why 50% is reachable.** We are not asking for new information, new models,
> or more reading. The answer is already present. Getting from "picks it 3 times
> in 10" to "picks it 5 times in 10", when the answer is on the page 8.5 times
> in 10, is closing a gap rather than inventing an ability. This is the single
> biggest known opportunity in the app.

**Metric** — of lines where the correct speaker is present in the candidate
roster, the percent where the model picks it.
**Probe** — see memory `attribution_selection_not_recall`.
**Current** — re-measured 2026-08-08 on the shipped **qwen3-14b**
(`selection_gap_recheck.py`, the `closed_set` open-roster arm, 793 rows across four books).
**MET.**

| | qwen3.5-9b (this goal's original basis) | qwen3-14b (shipped) |
|---|---|---|
| roster recall | 85.0% | **91.6%** |
| selection | **29.9%** | **62.9%** |

| book | recall | selection | n |
|---|---|---|---|
| grimgar03 | 95.2% | 65.3% | 396 |
| index18 | 89.9% | 73.0% | 99 |
| mushoku16 | 84.6% | 60.9% | 136 |
| owarimonogatari3 | 89.5% | 52.4% | 162 |

**Target — selection ≥ 50% with roster recall held at ≥ 85%. MET: 62.9% at
91.6% recall, and every individual book clears 50% as well.**

#### The English measurement, where recall is not a factor at all

Measured 2026-08-19 on three PDNC books — Pride and Prejudice, The Awakening,
The Sign of the Four — 2,494 quotes, qwen3-14b, one request per quote with the
cast supplied (`two_stage_attribution_full.json`, rescored by
`two_stage_selection_gap.py` into `two_stage_selection_gap.json`).

**Roster recall was 100%: the correct speaker was in the supplied cast on
2,494 of 2,494 rows.** So every one of the **1,134** wrong answers was a wrong
choice, and this goal's metric is the whole of the error here.

| quote type | n | wrong | |
|---|---|---|---|
| Anaphoric | 723 | 277 | 38.3% |
| **Explicit** | 543 | 256 | **47.1%** |
| Implicit | 1,228 | 601 | 48.9% |

**Explicit quotes are the ones where the text names the speaker**, and they are
the second-worst bucket — worse than Anaphoric, where the speaker is only
referred to by a pronoun. A method that misses nearly half the cases the text
answers outright is not making a hard judgement badly; something more basic is
wrong, and that contrast is the thing to chase rather than the 54.5% headline.

**Read the artifact's `in_candidates` field with care.** It records `false` on
2,250 of those 2,494 rows, which inverts the finding above. That is a harness
bug, fixed 2026-08-20: the run passed the roster display lines
(`MRS. BENNET [also: BENNET]`) where an exact membership test was applied, so
every character with an alias read as absent. `manifest` now refuses a
decorated candidate list rather than computing the field wrongly, and the
recomputation above expands each line into the names it stands for. The 84
other artifacts in this directory pass plain candidate lists and are unaffected.

**The gap was the model, not the method.** 29.9% was measured on qwen3.5-9b,
which a later six-model comparison put ~17 points behind the shipped model on
this task; the true difference here is 33 points. Selection is stable across
backends — the same book varies by 3.1 points (grimgar03) and 5.0 (mushoku16)
across four and five runs — so this is not one lucky artifact.

Nothing was built to close this. The goal was written around a number from a
model that does not ship, and the warning to re-measure before spending
anything on it was correct: the entire 55-point gap it described was 29
points of weaker model.

The four rejected approaches in the table below were therefore tested against
a deficit that mostly was not there on the shipped model. That does not
resurrect them — they were measured and they failed — but it does mean the
premise they were attacking has changed, and owarimonogatari3 at 52.4% is now
the only book near the line.

> **CAUTION: these figures were measured on a model that is not the one that
> ships.** The 147-line random gold set behind them has `source_run`
> **qwen3.5-9b**, and a six-model comparison on the same frozen harness later
> showed that model to be the *weakest selector tested*:
>
> | model | open | closed-6 | oracle |
> |---|---|---|---|
> | **qwen/qwen3-14b** (production) | 48.3% | 36.7% | **66.0%** |
> | ministral-3-14b | 47.6% | 41.5% | 61.2% |
> | phi-4 | 45.6% | 32.7% | 59.2% |
> | **qwen3.5-9b** (source of 29.9%) | 35.4% | 34.7% | **49.0%** |
>
> The 49% "conditional ceiling" this goal was written against is a property of
> **qwen3.5-9b, not of the task**. The production model reaches 66.0% on the
> same measurement.
>
> To be exact: 29.9% is end-to-end pipeline selection and 66.0% is conditional
> selection given an oracle set, so they are not the same number and one does
> not replace the other. But the *framing* — "the answer is present and the
> model looks past it" — was calibrated on a model 17 points worse at exactly
> that.
>
> **That re-measurement was done on 2026-08-08 and closed the goal:** selection
> is 62.9% on qwen3-14b at 91.6% roster recall. The warning is kept because it
> was correct — the entire gap this goal was built around was a weaker model —
> but it is no longer an instruction. Nothing here needs re-measuring.

**Why this and not more context.** Two independent measurements say supply is
not the constraint. Feeding the model more of the book is treating the wrong
problem, and has been tried.

#### What has already been tried, with numbers

Recorded here because this knowledge lived in ~30 scattered artifacts, and on
2026-08-06 that cost a session in which three already-rejected ideas were
proposed as if new. **Read this before proposing a fix for the selection gap.**

| approach | result | verdict |
|---|---|---|
| widen attribution context (w1→w4) | +10.0 grimgar03 (4 repeats), +3.0 index18, **−5.0 mushoku16**, 0.0 owari | book-dependent, not a fix |
| route per book | leave-one-out router **56.5%** vs fixed **57.2%** | **worse than picking one setting** |
| constrain decoding to the roster (GBNF) | open arm: 0.0, −1.0, −1.4, −1.2 | no gain where it matters |
| shrink candidate set to 6 | +1.8 grimgar03, **−6.1 index18, −12.5 mushoku16, −4.9 owari** | loses the right name |
| oracle candidate set | +10 to +18 everywhere | not achievable; it needs the answer |

The shape of it: **`closed-oracle` wins big and `closed-6` loses**, and the only
difference is whether the shortlist contains the right name. Constraining the
model is not the lever. Whether the answer is *in the list* is.

**Routing deserves its own warning.** Every routing gain quoted before
`realizable_router` was fitted — the best arm per book read off the results
afterwards. When the choice must be made without seeing the held-out book, the
router wins 4 families, loses 5, ties 6, and lands **below** a fixed setting.
An oracle-routed number is not an achievable number.

#### The one lever positive on all four tested books — and only four

**Scope first, because the result is easy to overstate and was.** This ran on
grimgar03, index18, mushoku16 and owarimonogatari3. All four are Japanese light
novels **in English translation** — one genre, one language, one translation
pipeline. It has never run on the three PDNC public-domain English novels, on
the Chinese WP/JY sets, or on any Japanese-language text. Read every number
below as "four books of one kind", not "every book".

`roster_quality` varied the roster instead of the model. Adding the names
`build_roster` missed beats the generated roster on all four, and beats a
*perfect* roster too:

| book | generated | augmented | gold |
|---|---|---|---|
| grimgar03 | 59.7 | **63.6** | 61.6 |
| index18 | 67.4 | **71.7** | 69.6 |
| mushoku16 | 48.9 | **51.9** | 48.9 |
| owarimonogatari3 | 40.7 | **45.1** | 42.6 |

**+3.0 to +4.4, same direction every time** — the only intervention measured so
far with no book that it hurts. The experiment pre-registered this reading:
*"augmented >> generated → roster extraction is worth fixing, and the size of
the effect is the prize."*

The misses are not walk-on parts: ten characters across four books that
`build_roster` never found, including HITOGAMI with 9 lines in mushoku16 and
OUGI with 10 in owarimonogatari3.

The `inflated` arm — a gold roster plus twenty decoys — is the guard rail:
30.9% on owarimonogatari3 against 40.7% generated. **Adding names is only safe
when they are real.** A recall fix that pads the list will lose more than it
gains.

**So the next move on 1.2 is `build_roster` in `three_pass_generate.py`, not a
prompt, a constraint, or a router — but see the scope note first.**

#### Before acting on it: widen the book set

The cheapest way to find out whether this generalises is to run the same
experiment on PDNC. It is not blocked by missing data:

- PDNC ships its own curated roster (74 names for Pride and Prejudice), which
  is what the `gold` arm needs, and from a published annotated corpus rather
  than this project's own judging.
- Its gold sets are LARGER than the light novels': 1270 / 640 / 584 rows
  against 396 / 162 / 136 / 99.
- **PDNC contamination does not apply here.** That contamination concerns the
  distilled adapter's training set. `roster_quality` trains nothing; it runs
  the base model and varies only the roster at inference. Nothing is fitted, so
  held-out status is irrelevant to this particular experiment.

**What it actually costs** — corrected after reading the script's data
dependencies rather than guessing at them, having first written "an afternoon"
here without checking:

1. **A three-pass checkpoint per book. This is the real cost.**
   `roster_quality` reads `segmented` and `named` from a prior pipeline run
   (`matrix_20260725-115148/<model>/<book>/result.json.threepass_checkpoint.json`).
   Only six light novels have one. The PDNC books have never been through the
   pipeline, so each needs a **GPU segmentation run first** — mushoku16's single
   pass took 80 minutes at 45 chunks, and Pride and Prejudice is a longer book.
   Budget hours per novel, not minutes.
2. **Source text** must be placed where the script expects it; the matrix
   `inputs/` directory holds only the eight light novels.
3. **`roster_additions` does not exist in PDNC gold.** The light novels carry a
   hand-curated list of names the judges found missing; PDNC instead carries a
   `roster` of 74 curated names. So `additions` becomes `roster - generated`,
   which is arguably a cleaner definition — derived from a published corpus
   rather than from this project's own judging.
4. The hardcoded four-book decoy pool needs widening. This part *is* trivial;
   it was the only part visible without reading the data flow.

Chinese (WP/JY) would need more work: those sets use a different structure
(`dataset`/`results` rather than `entries`). No Japanese-language attribution
gold exists at all — the light novels are Japanese-origin but English text.

If roster augmentation holds on three English public-domain novels of a
different century and genre, it is a real finding and goal 1.3 gains evidence
at the same time. If it does not, then it was a property of translated light
novels and the whole recommendation changes.

#### Still open

`candidates.py` exists, states its own plan — *"an upper bound on recall;
ablate afterwards to find the smallest reliable set"* — and has no artifact.
The size-versus-recall curve it proposed was never run. Given `closed-6` fails
by losing the right name and `closed-oracle` wins by keeping it, that curve is
the one measurement that would say whether a small, honest candidate set is
reachable at all.

---

**The arm is written "open-roster" rather than by its bare code name.**
`test_goals_navigation` decides which half a goal belongs in by reading its
status words, and treats a goal claiming both statuses as unfinished - which is
right, and is why 2.6 stays where it is. This section used to name that arm by
its code name, which is one of those two status words, so the check read the
whole goal as unfinished. Its placement in Part I passed for two weeks while
its own target line said MET. Do not restore the code name here.

**Promoted to Part II on 2026-08-20**, after checking the claim rather than
taking the wording: pooled selection 62.9% at 91.6% recall clears the target,
every individual book clears 50% selection, and the result survives dropping
`index18` - whose source file was later found corrupt (6,662 replacement
characters, no quote marks) - at 61.5% selection and 91.8% recall over the
remaining 694 rows. `mushoku16`'s own recall is 84.6%, just under the 85%
figure, which the target states pooled rather than per book.

## 7. The finished audiobook

### 7.2 The text we extract is the text in the book

> **What this is.** Checking that what we pull out of an EPUB is complete,
> correctly ordered, and free of things the book does not contain.
>
> **Why it matters.** This is the first stage of the pipeline, and everything
> downstream inherits its mistakes. A dropped chapter heading is a missing
> signpost; a duplicated one is a sentence the narrator reads twice.

**Metric** — TOC entries resolved; headings neither lost nor duplicated,
measured on real books rather than fixtures.
**Probe** — `app/experiments/epub_extraction_fidelity.py`, which instruments
the shipped extractor rather than reimplementing its judgement, and exits
non-zero when anything is duplicated or dropped so a chain can gate on it.
**Current** — across the six shipped ReZero EPUBs, 2026-08-16:

| | |
|---|---|
| TOC anchors resolved | **89 / 89** |
| unresolved | 0 |
| titles inserted | 0 |
| **duplicated** | **0** |

**MET.**

**Why this goal exists is the measurement that opened it.** The same six books
on the morning of 2026-08-16, before the duplicate check was fixed: 89 of 89
anchors resolved, and all four titles the extractor judged missing were
**already in the text**, differing only by curly quotes, dash variants, or a
`Volume 40` / `Light Novel` prefix. Zero titles recovered, four headings
duplicated — each read aloud twice by the narrator.

**Every unit test passed throughout.** Synthetic fixtures match themselves
exactly, so none could express the defect. Extraction was the one stage
measured only against material we wrote ourselves, and that is precisely how
a feature ran over the whole library degrading every book in it while the
suite stayed green.

**Target — 0 duplicated and 0 dropped headings across the shipped library.**
Met, and now re-runnable rather than remembered.

---

## 2. Voice — does it sound like the target speaker


### 2.2 Repair the Chinese anchor

> **What this is.** Making sure the measuring instrument works before trusting
> what it measures.
>
> **Why it matters.** In Chinese, the narrator scored **worse against herself**
> than the synthetic voices scored against her. Read that again: the real person
> was judged less like herself than a machine imitation was. That is impossible
> as a fact about voices, so it is a fact about the ruler. Any Chinese voice
> conclusion drawn from this data is unreliable — including the flattering ones.
>
> **Why this is reachable.** There is an obvious suspect. The Chinese clips are
> much shorter — about 3 seconds, against 7 for English — and this kind of
> voice-fingerprinting is known to get shaky on short audio. Testing it is
> cheap: chop the English clips down to 3 seconds and see whether its ceiling
> collapses too. If it does, we have the answer in an afternoon.

**Metric** — `human_vs_human` must exceed every arm it bounds.
**Probe** — `find_invalid_anchors` in `ljspeech_score.py`, tested in
`app/tests/test_score_anchor.py`. Anchor construction: `build_anchor_side`.
**Current** — **CAUSE FOUND AND FIXED 2026-08-06.**

**Clip length was the whole cause**, established in both directions:

| direction | result |
|---|---|
| truncate ENGLISH clips to the Chinese median (3.17 s) | anchor **0.783 → 0.632**, below its own clone arm |
| join same-speaker CHINESE clips to 6.9 s | anchor **0.670 → 0.837**, clears both arms |
| join to 10.2 s / 13.6 s | 0.867 / 0.901 |

Shorten a good anchor and it breaks; lengthen a broken one and it repairs. Not
the corpus, not the narrator, not the language, and not ECAPA being unsuited to
Chinese — the clips were too short for a speaker embedding to be stable.

**The fix needed no new data.** All 150 Chinese clips are one speaker, and a
speaker embedding does not care about sentence continuity, only about quantity
of voiced material. `build_anchor_side` now joins consecutive same-speaker
clips until each side of the anchor carries `ANCHOR_MIN_SECONDS = 7.0`, chosen
from the knee of that curve.

**Target — every eval set's anchor above all of its arms. MET, confirmed in
evidence 2026-08-07.** All three sets re-scored, `anchor_invalid` empty in
each:

| set | anchor | clone | LoRA |
|---|---|---|---|
| English | 0.8328 | 0.7567 | 0.6899 |
| Japanese | 0.8355 | 0.7789 | 0.7551 |
| Chinese | 0.7655 | 0.7651 | 0.7197 |

Chinese clears its clone arm by **0.0004**. That satisfies the target and is
not a margin to lean on: a Chinese result that depends on the anchor sitting
above the clone should be treated as unresolved rather than passing.

**What this retroactively rescues.** The Chinese ARM numbers were always fine —
clone 0.765, LoRA 0.720. Only the yardstick was broken, so those measurements
become readable rather than being discarded.

**A note for other eval sets.** Any future set whose clips are short inherits
this. The guard is `find_invalid_anchors`, which now has a known cause to point
at rather than only a symptom.

---

### 2.3 Adapters that stop talking

> **What this is.** Making sure a trained voice knows when the sentence is
> over.
>
> **Why it matters.** This one already bit us, expensively. Two trained voices
> produced **163.8 seconds of audio for a 7-second line** — every single time.
> They never learned to stop, so they babbled until the system cut them off. The
> cruel part: the training reports looked completely normal. Nothing was wrong
> until you actually listened.
>
> **Why this is already met, and how it stays met.** The cause turned out to be
> one setting — the training speed dial — set five times too high. Turned down,
> three voices in three languages all came out correct. It is now checked
> automatically two ways: a short listening test before any voice is used, and a
> test that stops the wrong setting from creeping back in.

**Metric** — median generated duration ÷ human duration, per adapter.
**Probe** — `app/experiments/verify_adapter_stops.py`, gate at 3.0x.
**Current** — 1.01x / 0.87x / 0.94x across the three languages. **MET.**

**Target — hold median within 0.8–1.25x, and never ship an adapter above
3.0x.**

Training loss looked ordinary (2.9 and 3.4), so only generated output reveals
this. Protected by the gate plus `app/tests/test_training_defaults.py`.

---

### 2.4 Duration fidelity in normal use

> **What this is.** Whether a spoken line lasts about as long as a human would
> take to say it.
>
> **Why it matters.** A line delivered in three-quarters of the natural time
> sounds rushed and clipped. It is the same *kind* of fault as the babbling
> voices above — wrong length, no error message, nobody told — but in the
> opposite direction and much subtler, which is exactly what makes it easy to
> ship by accident.
>
> **Why this is reachable.** Five of the six measured cases are already inside
> the target. Only Japanese zero-shot cloning sits outside it, so this is one
> specific case to investigate, not a broad weakness.

**Metric** — mean `dur_ratio` across held-out lines (1.00 = matches the human).
**Current** — 100 clips per language, both arms, 2026-08-08, plus the
narrator-controlled Japanese clone replication below (`duration_probe.py`).
**MET at the median; per-line spread remains a quality opportunity.**

Reproduced unchanged on 2026-08-12 in
`duration_probe_20260811_overnight.json`: Japanese clone median **0.7584**
with **94.0%** of clips outside the band. This confirms the baseline; it is
not an intervention or an improvement.

**Diagnosed 2026-08-15: the comparison does not isolate a duration defect.**
The Japanese clone's generated pace matches the pace implied by its reference
clip: generated duration / reference-rate-predicted duration has median
**1.023** across the same 100 lines. The reference reads 38 non-space
characters in 5.166 s (7.36 chars/s), while the held-out book's human is about
26% slower. Slowing production output to match that different reading would
erase the cloned speaker's pace.

The valid same-speaker measurement was completed 2026-08-15. Thirty held-out
clips and the excluded reference all come from the same *Kokoro* LibriVox
recording, whose 103 chapters are read by ekzemplaro. Japanese clone median is
**0.9269**, inside the 0.90–1.10 target (p10–p90 **0.78–1.05**, 43.3% outside).
The old 0.758 result was therefore dominated by reader/session pace mismatch,
not evidence for language-specific time stretching. Evidence:
`kokoro_same_speaker_generate.json` and
`duration_probe_same_speaker_20260815.json`.

**Outlier analysis, 2026-08-15.** Reading the 30 generated WAVs directly found
that ratio variation tracks text length more than source duration: Spearman
correlation is +0.486 with non-space character count, +0.407 with the human's
characters/second, +0.226 with punctuation, and only +0.184 with human clip
duration. The shortest-text tertile has median ratio **0.845**, versus **0.962**
for the longest. This is a targeting clue, not a causal result at n=30; a
length-controlled intervention is the next duration experiment. Evidence:
`duration_outlier_analysis.json`.

| arm | median ratio | p10–p90 | clips outside band |
|---|---|---|---|
| English LoRA | 0.959 | 0.83–1.09 | 35% |
| English clone | 1.018 | 0.85–1.17 | 35% |
| Japanese LoRA | 0.929 | 0.78–1.15 | 57% |
| Japanese clone | **0.758** | 0.65–0.86 | **94%** |
| Japanese clone, same narrator/book | **0.927** | 0.78–1.05 | 43% |
| Chinese LoRA | 0.935 | 0.83–1.10 | 45% |
| Chinese clone | **0.896** | 0.78–1.03 | 57% |

**This is the twelve-clip finding that survived.** The Japanese clone arm was
0.76 at n=12 and is 0.7584 at n=100 — the same number to three decimals. Three
other twelve-clip findings dissolved under proper sampling on the same day
(2.5's two cells, 2.6's jitter), so the lesson is that sample size has to be
checked, not that small samples are always wrong.

The per-clip column is new and was hidden by the medians. Every arm, including
the four whose medians pass, puts a third to a half of its individual clips
outside 0.90–1.10. A median near 1.0 built from clips scattered on both sides
is a different defect from a uniformly rushed arm, and only the Japanese clone
is uniform: at 94% outside with a p90 of 0.86, nearly every clip is short, not
merely the average.

**Target — every arm within 0.90–1.10.**

The existing safety check catches runaway voices at 3.0x. It cannot see 0.76.

---

### 2.8 A voice stays the same voice across a whole book

> **What this is.** Whether a voice slowly wanders into someone else over the
> course of a book, rather than staying recognisably one person.
>
> **Why it matters.** The product is ten hours of one consistent voice. Every
> other voice measurement here is a SINGLE LINE, and a slow drift is invisible
> to them: each line is scored against its own reference, so a voice that
> wandered steadily would score the same at the start and the end while
> sounding obviously wrong to someone who sat through it.
>
> **The answer is that it does not drift.** Measured at book length, a voice is
> as much itself on line 2000 as on line 1.

**Metric** — speaker similarity to an anchor built from the opening lines, as a
function of position through the run.
**Probe** — `app/experiments/voice_drift.py`.
**Current** — 2026-08-08, three adapters, **2000 consecutive lines each**, zero
failures. **MET.**

| adapter | first third | last third | fitted change |
|---|---|---|---|
| husky_tenor_30s_m_literary | 0.776 | 0.771 | −0.005 |
| warm_mezzo_30s_f_fantasy_2 | 0.739 | 0.738 | +0.009 |
| warm_baritone_40s_m_2 | 0.728 | 0.728 | +0.004 |

**Target — |drift| ≤ 0.03 across a book-length run.** All three clear it with
an order of magnitude to spare.

#### The 400-line result was noise, and this supersedes it

An earlier run over 400 lines reported drift of −0.018, −0.050 and −0.017 and
was written up here as a real defect, with pitch rising +1.9% to +6.6% offered
as its mechanism. At five times the length **every part of that reverses**:

| | 400 lines | 2000 lines |
|---|---|---|
| husky_tenor_30s_m_literary | −0.018 | −0.005 |
| warm_mezzo_30s_f_fantasy_2 | **−0.050** | **+0.009** |
| f0 across the run | **rises** 1.9–6.6% | **falls** 1.2–4.3% |

A trend that shrinks toward zero as the sample grows, and whose direction
flips, is line-to-line variation being fitted as a slope. Four hundred lines
was simply too short: normal variation over a few hundred lines looks like a
trend, and `polyfit` will always return one.

**What did not change:** vocal tract length is stable to within 1% over 2000
lines on all three adapters, and HNR within 1%. The speaker's physical voice
properties hold. Those were flat at 400 lines too, and they are the numbers
that were right both times.

**The lesson, since this is the second time it has bitten.** The original entry
carried the caveat — *"a 400-line drift of −0.05 does not license a claim about
5,000 lines; whether it is linear, plateaus, or accelerates is unmeasured"* —
and the goal was still written as though the drift were real. Stating a caveat
is not the same as heeding it. A measurement that cannot distinguish trend from
noise should be reported as **NO BASELINE**, not as a defect with a target
attached.

---

## 3. Reliability — does a run finish and produce the right thing

#### What "the same voice" does not yet include

The criterion above is drift in a speaker measure across a long run, and it is
met. It is narrower than the goal's title. Natural long-form read speech has
properties nobody has checked in our output — ParaTTS (TASLP 2022) names three
from the phonetics literature: **pitch reset** at the start of a paragraph,
**declination** across it, and **lengthening** at initial and final positions.
It also describes our exact architecture as its baseline: "synthesize each
sentence in a paragraph and then combine them… the prosody in the combined
paragraph speech may become inconsistent."

Attempted on 2026-08-21 and **not decidable**: across the 14 narration runs in
the one rendered chapter, the aggregate looked like declination (−7.8 Hz from
first third to last) but the paired test says otherwise — 8 runs down, 6 up,
sign test p = 0.79. Fourteen runs cannot answer it; more rendered audio can.

Recorded because the aggregate is tempting and wrong, not because the question
is settled.

### 3.2 Every generated file is real audio

> **What this is.** Confirming that every audio file the app claims to have
> made actually exists and actually contains sound.
>
> **Why it matters.** The worst failures are the quiet ones. A missing or empty
> file that nothing complains about becomes a silent gap in the finished
> audiobook, discovered by a listener rather than by us.
>
> **Why this stays at zero.** All seven ways the app can produce audio were
> routed through a single checkpoint, so there is one place to verify rather
> than seven places to remember. A new generation path cannot bypass it without
> deliberately going around.

**Metric** — files that are absent, empty, or unreadable after generation.
**Probe** — `validate_generated_audio` in `app/audio_validation.py`, funnelled
through `_save_wav`.
**Current** — 0 known escapes since the funnel was added. **MET.**

**Target — 0. Any regression is a release blocker.**

---

### 3.3 One character, one voice

> **What this is.** Making sure two different characters never end up sharing
> the same voice by accident.
>
> **Why it matters.** This was a real bug with a memorable shape. The app
> stripped titles from names to help match them — so "Mr. Bennet" and
> "Mrs. Bennet" both became "Bennet", and a husband and wife were given one
> voice between them. It affected six books out of twenty-eight. Nothing
> errored. The audiobook was simply wrong, and only a listener would ever know.
>
> **Why this stays fixed.** Both directions are now tested, which matters
> because the first attempt at a fix broke the opposite case: "EMILIA" and
> "Emilia" are one character and *must* be merged, while Mr and Mrs Bennet are
> two and must not.

**Metric** — distinct roster entries sharing a voice through a name-matching
bug.
**Probe** — `app/tests/test_generate_personas.py`.
**Current** — fixed. **MET.**

**Target — 0, with the couple case and the case-variant case both tested.**

#### The inverse case is real, and this metric does not cover it

Measuring the rendered chapter on 2026-08-21 (`character_distinctiveness.py`,
150 clips, 5 characters) found **one character rendered as two voices**:
`NATSUKI SUBARU` at **181.4 Hz** and `Subaru` at **264.8 Hz**,
**6.55 semitones apart**, on a listener threshold of about 1.95. The script
split one person across two labels and the pipeline gave each its own voice.

That is not what the metric above counts. This goal asks whether two DIFFERENT
characters wrongly share ONE voice; this is one character wrongly holding TWO.
The mechanism differs too — no name-matching bug is involved, the labels simply
never merged. **Open, and uncounted by the current probe.**

The same run flagged `LITTLE GIRL` and `SATELLA` — genuinely different
characters — at **0.90 semitones**, below the threshold. Whether they are
distinguishable by timbre is not something pitch can answer, and is one of the
questions in the listening package under 6.5.

---

### 3.4 Reproducible output

> **What this is.** Running the same job twice with the same settings should
> produce byte-for-byte identical audio.
>
> **Why it matters.** Without it, no comparison is trustworthy. If two runs
> differ on their own, there is no way to tell whether a change improved
> anything or the dice simply landed differently. Reproducibility is what makes
> every other number on this page mean something.
>
> **Why this is reachable.** It is already done for audio. Voices were being
> generated without a fixed starting seed for 70 of 71 characters; each now
> derives a stable one from its own name. The same discipline needs extending to
> the text side.

**Metric** — identical seed and inputs produce byte-identical audio.
**Probe** — waveform SHA-256 comparison.
**Current** — seeded generation confirmed deterministic; `character_voice_seed`
now derives a stable per-character seed. **MET for TTS.**

**Target — extend to the LLM path: same seed, same model, same script output.**

---

## 4. Speed and cost


### 4.1 Faster than real time

> **What this is.** How long the app takes to produce audio, compared with how
> long that audio lasts.
>
> **Why it matters.** It is the number a user actually feels. Right now a
> 10-hour audiobook costs roughly 10 hours of computer time — start it and come
> back tomorrow. Getting comfortably below 1.0 is the difference between
> "overnight" and "over lunch".
>
> **Why this is reachable.** Two of the three languages are already at 0.97–0.98
> and English at 0.91, so the target is a modest tightening rather than a
> redesign.

**Metric** — generation seconds ÷ audio seconds.
**Current** — median **0.91x / 0.98x / 0.97x**, slowest 1.21x (n=300 per
language, RX 9070 XT). **MET, barely.**

**Target — median ≤ 0.90x, worst case ≤ 1.5x.**

---

### 4.2 Local should not need the cloud

> **What this is.** Keeping the version that runs on your own machine roughly as
> good as the version that rents a much larger computer.
>
> **Why it matters.** Cloud runs cost money per hour and send your book to
> someone else's computer. If local is nearly as good, that is a real choice
> rather than a compromise — and the app has no dependency it cannot survive
> losing.
>
> **Why this is reachable.** It is already true: local is at 97–99% of cloud on
> all four books. This goal exists to *defend* a property already held, because
> properties like this are usually lost by accident rather than by decision.

**Metric** — best local accuracy ÷ best cloud accuracy, per book.
**Current** — 97.2% / 98.7% / 97.5% / 99.0%. **MET.**

**Target — hold local within 5% of cloud on every book.**

---

## 5. Text handling


### 5.1 Nothing unspeakable reaches the TTS

> **What this is.** Catching characters that have no spoken form before they
> reach the voice engine — things like `■`, `♪`, `∞`, or Chinese/Japanese
> characters embedded in English text.
>
> **Why it matters.** Nobody knows what the engine does with them. It might skip
> them, mangle them, or emit noise. There is currently no check at all, so
> whatever happens, happens silently.
>
> **What the first count found.** Counting the eight source books on 2026-08-06
> gave a partial answer. Chinese or Japanese characters appear inside otherwise
> English text in **five of eight books**, between 23 and 779 times each. And
> one book, index18, turned out to be **1.4% corrupt** — 6,662 "unknown
> character" marks left behind by a bad text conversion. The app already refuses
> that book at the door, which is correct, and is why it does not appear in the
> three-pass comparison.
>
> **What is still uncounted.** The source files are only the front door. Nobody
> has yet counted what reaches the *voice engine* after all processing, which is
> where the damage would actually occur. That is the measurement still owed.

**Metric** — characters passed to TTS with no spoken form.
**Probe** — source-level count is a script over the input `.txt` files; the
TTS-level count does not exist yet.
**Current** — **measured at the TTS boundary 2026-08-08**
(`tts_boundary_audit.py`, over `normalize_for_speech`'s output for all 48
saved books, 98,134 lines):

| | |
|---|---|
| unspeakable characters in raw script text | 56 |
| removed by normalization | 56 |
| **reaching the TTS engine** | **0** |

**Target — count what reaches the engine, then drive it to 0 with a
verbalization pass. MET, with one exclusion stated below.**

**The zero was not proof the code was sound.** Measured the same day, these
passed through `normalize_for_speech` unchanged: `♪` `∞` `★` `→` `♥` and
`U+FFFD`. Only `■` was handled, and only because it sits in `SPEECH_BREAKS`.
The audit read 0 because the 48 saved books do not happen to contain the
others — index18's 6,662 U+FFFD are refused by the *source gate* before the
book is ever saved. **The protection was the gate, not the normaliser**, and
anything reaching `scripts/` by another route had none.

`verbalize_symbols` now closes it: named symbols are spoken (`∞` → "infinity",
`→` → "to", `×` → "times"), and anything in an unspeakable Unicode category —
So, Sm, Sk, Co, Cn, Cs, plus U+FFFD — is dropped and **recorded** in the
transformation list rather than removed silently. Currency is deliberately
excluded, since `$` and `£` are speakable. 11 tests in
`test_speech_verbalization.py`.

**The exclusion: pictographic kana are not covered and cannot be, by this
method.** `へ` used as a drawing of a mouth is category Lo — a letter — and
identical to `へ` the particle. Any rule that dropped it would delete Japanese
text. Catching that needs context, not character class, and is not attempted
here.

**Outside evidence that the exclusion is the expensive half.** Liu et al.,
*A First Look at Bugs in LLM Inference Engines* (ACM TOSEM 2025, DOI
10.1145/3788873), hand-coded 929 closed bug reports across llama.cpp, vLLM,
DeepSpeed, MLC-LLM and TensorRT-LLM. Among the bugs whose symptom was bad
output rather than a crash, **character-level faults are the largest single
factor at 36%** — ahead of incoherent semantics (20%), inconsistent output
(17%), length (15%) and repetition (13%). That is a survey of *engines*, not
of this app, and it says nothing about how often pictographic kana appear in a
real book; it is a prior about where output faults cluster, not a measurement
of ours. Taken with the fact that every other factor on their list already has
a gate here, it argues the remaining context-dependent character work is worth
more than it looks, and it is the reason this goal is recorded as MET *with*
an exclusion rather than simply MET.

---

## 6. Measurement integrity


### 6.1 A ceiling must bound its arms

> **In short.** If the "best possible score" comes out lower than a score
> something actually achieved, the test is broken and must say so out loud
> instead of printing a tidy table. This is now automatic.

Covered at 2.2. Enforced by `find_invalid_anchors`, reported in every score
artifact as `anchor_invalid`.

**Target — 0 comparisons published from an eval set with an invalid anchor.
MET 2026-08-08**, now asserted rather than observed.

`test_score_anchor.py` pins the *detector* against constructed inputs.
`test_published_anchors.py` pins the *artifacts*: it walks every
`*_score.json` in the evidence tree and asserts each records
`anchor_invalid`, that it is empty, and — re-derived from the summary rather
than trusting the recorded flag — that every arm scores below its ceiling. A
correct detector nobody reads is exactly the 2026-08-06 failure, so the goal
needed a check over real files, not fixtures.

---

### 6.2 One source per decision

> **In short.** Any setting written down in more than one place will eventually
> disagree with itself, and the disagreement will be silent. The training-speed
> dial was written in four places; three said one thing, one said another — and
> the button in the interface used one of the wrong ones. That is how the
> babbling voices in 2.3 reached users' hands.
>
> **Why this is reachable.** Each case is small and permanent once fixed: write
> the value once, have everything else refer to it, and add a test that fails if
> a copy reappears. Two done, one known outstanding.

**Metric** — settings defined in more than one place.
**Current** — three found and fixed: the training learning rate,
`is_remote_llm`, and `config["llm"]` versus `config["llm_local"]` — the last
of which cost an hour on 2026-08-06 when a run dialled a dead endpoint while a
working server sat idle. **MET 2026-08-16**
(`app/tests/test_llm_config_source.py`, 8 tests).

`config["llm"]` is a mirror of the active profile, not a source: `/api/config`
copies the profile named by `llm_mode` into it and refuses to save a
disagreement, but anything writing config.json outside that endpoint updates
one and not the other. The rule for resolving the active profile had been
written out by hand in two different spellings, each with a comment citing
Rule 15. It now lives in `lmstudio_settings.get_active_llm_config`, with the
two former spellings kept in the test as the reference behaviour the single
implementation must reproduce.

Two experiment probes were also reading `llm_local` directly, so they ignored
the toggle while reproducing a pipeline that honours it.
`benchmark_runner._get_llm_benchmark_target` is deliberately exempt and the
test records why: it resolves an explicitly named endpoint so both can be
measured independently of the toggle, which is a different question.

**Target — 0 known parallel definitions; each new one gets a test that asserts
the copies agree.** The guard is
`SingleImplementationTests.test_the_active_profile_rule_lives_in_one_place`,
which fails on a re-introduced copy anywhere under `app/`.

---

### 6.3 Indexes describe committed state

> **In short.** The record of results must be reproducible by someone else on a
> fresh copy. One index was quietly built partly from files that only existed on
> this machine, so it looked perfect here and was permanently wrong everywhere
> else.

**Metric** — index checks passing on a clean checkout.
**Current** — **MET**, after `collect_results.py` was found scoring gitignored
files and stamping rows with file mtimes.

**Target — every index check passes from a fresh clone with no untracked
files.**

---

### 6.4 No skipped tests

> **In short.** A test that skips is not a test that passes, and counting it as
> one is how a fault hides. Three tests here were skipping quietly. Rewriting
> them so they could not skip immediately exposed a genuine bug in the very
> thing they were meant to be checking — the skip had been covering it.

**Metric** — tests skipped in the release verifier.
**Current** — 0. **MET.**

**Target — 0. A skip is a failure.**

---

## Priority

If only three things get worked on:

1. **Generalisation (1.3)** — the app scores 71.0% on 25 PDNC novels it has
   never seen against 83.6% on the three it quotes, a −12.6 point gap against
   a target of 5. The three quoted books rank #2, #8 and #9 of 28. This is now
   the largest known overstatement in the document.
2. **Per-line duration spread (2.4)** — the narrator-controlled Japanese clone
   median is 0.927 and meets the goal, disproving the earlier cross-reader
   0.758 diagnosis. However, 43% of individual Japanese clips remain outside
   the band, similar to the other language arms.
3. **Train/val contamination (2.7)** — **12** of 75 shipped adapters still
   carry weights trained on their own val split, down from 21 on 2026-08-16.
   The remaining twelve split evenly and need opposite work: six failed their
   identity gate outright (0.056–0.404), while six *passed* and were refused
   only for not beating their shipped score — a score measured on clips that
   adapter trained on, so the honest number is being compared against an
   inflated one. Half of what is left is arithmetic, not training.

**Selection (1.2) was #1 on this list until 2026-08-08 and is now MET** — the
29.9% it was built on came from a model that does not ship. Re-measuring goals
before working on them has now twice been worth more than working on them.

**1.3 has now cost two attempts, and both missed the same way.** Broad
sequence scored +2.33 points (p=0.054) and the targeted selector +1.33
(p=0.134), against a fixed gate of +3.0 and p<0.05. Both supplied more
CONTEXT. But 1.2 established that the roster already holds the right name
about 85% of the time while the model picks it 29.9% — a SELECTION failure,
not a context one. No selection-side intervention has been tried, and each
context attempt spends pilot books from the 15 still sealed. The next
experiment here should change how the answer is chosen, not how much the
model is told.

Then: **7.1, the blinded listening test** — the cheapest open goal in the
document, already packaged and never rated, and the only measurement that
requires a person rather than the GPU. The Japanese transcription gap (5.4)
is now measured rather than pending, and may be a metric problem rather than a
pipeline one. The three-pass baseline (5.3) is already answered and should not
be listed as pending. Reliability 3.1 is MET after the 2026-08-16 unseen
four-book current-path rerun completed all 807 chunks.

## Rules for changing this file

- A current value moves only with an artifact and a date.
- A target moves only with a stated reason.
- Never delete an OPEN goal because it proved difficult. Convert it, or record
  why it was abandoned.
- Do not add a target without evidence that it is reachable. `NO BASELINE` is a
  respectable status; an invented number is not.
