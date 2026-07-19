# Algorithm review report (Fable, 2026-07-19)

Response to `FABLE_ALGORITHM_REVIEW.md`. All functions below were read
directly this session (files named per area); no code was changed.

**Summary of recommendations:**

| Area | Verdict |
|---|---|
| 1. `SequenceMatcher` fuzzy label matching | Keep as-is |
| 2. Boundary overlap detection | Keep as-is (exact matching is correct, not a gap) |
| 3. Chunk-quality recall scoring | **Worth doing** — add token-level `SequenceMatcher` span extraction for retry feedback; keep Counter gates |
| 4. Adjacent-duplicate blocks | Keep as-is |
| 5. Corruption dict / front-matter strip | Keep as-is; a data-driven middle path exists if the dict grows |
| 6. Nickname co-occurrence + alias resolution | Keep as-is; strictness in `_parse_alias_response` is consistent with codebase policy |
| 7. Voice Lab clustering / diarization | Don't swap `cluster_voices` for scipy (constraints are why it's hand-rolled); auto-diarization pre-check is a plausible future feature, not an algorithm fix |

---

## Area 1 — `difflib.SequenceMatcher` fuzzy similarity

**What the code does (confirmed).** Two genuinely different uses:

- `app/speaker_identity.py` — `stabilize_speaker_identities` merges only
  *exact* normalized matches (`_identity_key`: casefold + strip non-word
  chars). `SequenceMatcher.ratio()` (line 83) is used only in
  `_uncertain_candidates` to **flag** near-miss labels for human review at
  ratio ≥ 0.90 — it never auto-merges. There's also a hand rule
  (`_is_extended_person_name`) for "Emilia" vs "Emilia Smith"-style
  extensions, with digit/kinship-word exclusions.
- `app/review_script.py` — `diff_entries` (line 771) runs a whole-array
  `SequenceMatcher(None, original_texts, corrected_texts, autojunk=False)`
  to align entry lists before field comparison, so a split/merge doesn't
  make every later entry look changed. A per-pair character-level `ratio()`
  (line 734) is used only as a display "magnitude" for highlight snippets.

**Is a known algorithm a better fit?**

- For the short-label case, **Jaro-Winkler** is the textbook algorithm for
  name matching (prefix-weighted, designed for exactly 1–4-word person
  names). Normalized **Levenshtein** would also behave more predictably
  than Ratcliff/Obershelp on very short strings. But neither is in the
  stdlib — you'd hand-roll ~30 lines or take `jellyfish`/`rapidfuzz`.
- For the entry-array alignment in `diff_entries`, `SequenceMatcher` over
  lists of hashable items **is** the right named algorithm (LCS-style
  sequence alignment); `autojunk=False` already disables the one heuristic
  the brief worries about. Nothing better exists in stdlib.

**Tradeoffs.** The label-matching output is advisory only — a slightly
suboptimal similarity score costs, at worst, one extra or one missed
*review suggestion*, never a wrong merge. The 0.90 threshold on
concatenated keys already catches the motivating "MAN 2 (VILLAIN)" vs
"MAN 1 (VILLAIN)" case (1 char differs in ~11 → ratio ≈ 0.9+). Switching
metrics would require re-tuning the threshold against the existing tests
for zero behavioral upside, and a new dependency fails the ground rules.

**Recommendation: not worth doing**, for either call site. If review
suggestions ever prove noisy in practice, try Jaro-Winkler (hand-rolled,
stdlib-only, behind the same 0.90-style threshold) before adding a
dependency — but there's no current evidence of a problem.

---

## Area 2 — Boundary overlap (`app/generate_script.py:138`)

**What the code does (confirmed).** `_get_boundary_overlap` compares only
the **last entry of the left array vs the first entry of the right array**
(not whole arrays), tokenizes both with `\w+` casefold, and brute-forces
the longest suffix-of-left = prefix-of-right from `min(len(l), len(r))`
down to 3 words. A hit raises `AdjacentArrayOverlapError` in
`clean_json_string`, which **rejects the whole LLM response** (blocking).

**Known algorithm.** Yes, textbook: the **KMP failure function** (or
Z-function) over `right + sentinel + left` finds the longest
suffix/prefix overlap in O(n). The current loop is O(k²) worst case.

**Tradeoffs.** The inputs are word lists of two single entries — tens of
words, rarely low hundreds — and this runs once per adjacent-array pair
per LLM response. The asymptotic win is unmeasurable at this scale, and
the brute-force version is more obviously correct to a reader (it
literally states the property it checks). Descending iteration already
returns the *longest* overlap first, same as KMP would.

On the brief's second question — should this be fuzzy? **No.** This check
gates a hard rejection. The annotation task requires verbatim source
transfer, so a *verbatim* seam repeat is unambiguous evidence of
double-generation; a paraphrase-tolerant version would raise blocking
errors on legitimate repeated dialogue ("No. No, wait—" spanning a seam)
and trade a rare miss for false rejections of valid responses. The
`minimum_words=3` floor exists for exactly this reason. If paraphrased
seam overlaps are ever actually observed in production, the right shape is
a token-level `SequenceMatcher` ratio at the seam surfaced as a
`manual_review` finding — not a fuzzier blocking check.

**Recommendation: not worth doing.**

---

## Area 3 — Recall scoring (`app/chunk_quality.py`) — the high-value area

**What the code does (confirmed).** `validate_chunk_quality` computes
(a) one-sided multiset token recall (`_counter_recall`), (b) the same
recall over ordered trigrams (`_ngrams(tokens, 3)`), (c) an output/source
length ratio, plus Unicode-introduction checks and duplicate-block checks.
Tokenization (`_tokens`) splits CJK/Hiragana/Katakana/Hangul/Thai words
into per-character tokens (the Japanese tests depend on this). Both recall
thresholds are 0.90; `generate_script.py`'s `NEAR_MISS_RECALL_THRESHOLD =
0.75` splits retry wording into "close, fill in gaps" vs "you stopped
early", and `_build_retry_feedback_message` can only ever say *"you
covered about N%"* — the Counter approach structurally cannot say **what**
is missing.

**Is a known algorithm a better fit?** Partially — but not as a metric
replacement.

- The named algorithm for "how much of sequence A survives, in order, in
  sequence B, and where are the gaps" is global sequence alignment —
  **Needleman-Wunsch** — of which stdlib's
  `difflib.SequenceMatcher(None, source_tokens, output_tokens,
  autojunk=False)` is a practical LCS-family equivalent, already used at
  the sequence level in this very codebase (`review_script.py:771`). Its
  `get_opcodes()` yields exactly the artifact the Counter approach can't:
  the **`delete` spans** — the literal source token runs absent from the
  output, with offsets.
- On the brief's paraphrase question: note the framing is inverted for
  this pipeline. The task is verbatim annotation, not summarization — a
  paraphrased sentence is *supposed* to score poorly (the near-miss retry
  message explicitly asks for "precise" phrasing). So alignment does not
  need to be more paraphrase-tolerant than trigrams; its value here is
  **localization**, not accuracy.

**Tradeoffs.**

- *Gained:* `_build_retry_feedback_message` could quote the actual dropped
  spans — "your response omitted the passage beginning '…' (≈120 words)"
  — which is precisely the targeted feedback the 2026-07-19 incident
  showed the model needs (the 11%→86%→5% regression was caused by wrong
  *wording*, not wrong *scoring*). Deterministic, printable evidence —
  strictly in line with the auditability ground rule.
- *Cost:* zero dependencies. `SequenceMatcher` on token lists is O(n²)
  worst case; at chunk scale (a few thousand tokens) with
  `autojunk=False` this is milliseconds-to-tens-of-milliseconds per failed
  attempt, and it only needs to run on the failure path.
- *Risk to avoid:* do **not** replace `_counter_recall` as the pass/fail
  gate. The 0.90 / 0.75 thresholds are calibrated against real incidents
  and encoded in tests
  (`test_boundary_allows_lowest_calibrated_intact_coverage`,
  `test_volume_10_style_early_stop_fails_all_coverage_signals`); an
  alignment-based ratio is a different distribution and would silently
  shift calibration (Rule 9 territory). Full Needleman-Wunsch with tuned
  gap penalties adds nothing over difflib here and would mean hand-rolled
  DP or numpy.

**Recommendation: worth doing — additively.** Keep the Counter gates
exactly as they are; add an alignment pass (token-level `SequenceMatcher`,
`autojunk=False`) that runs when recall checks fail, extracts the top
deleted source spans, and feeds them into `_build_retry_feedback_message`.
Separate follow-up task with its own plan per Rule 14.

**Side note on `find_nicknames.collect_context`** (the brief's aside): the
regex-per-token choice is correct for its stated reason (combined
alternation drops prefix-colliding co-occurrences) and the job is narrow
(co-occurrence over already-labeled entries, ≤600-char texts). No change
warranted; see Area 6.

---

## Area 4 — Adjacent duplicate blocks (`app/script_preflight.py:72`)

**What the code does (confirmed).** For block sizes 5→2, slide a window
and compare `left == right` (Python list equality), with an `occupied`
set preventing overlapping double-reports and a ≥8-chars-per-entry guard
against trivially short lines. Callers use the `source_occurrences` count
to distinguish source-supported repetition from generation artifacts.

**Known algorithms.** Rabin-Karp rolling hashes, suffix-array/suffix-
automaton repeat finding — all real, all designed for inputs orders of
magnitude larger than a few hundred entries with block sizes ≤5. Current
cost is ~4·n list comparisons of ≤5 short strings: effectively free.
List equality is also exact by construction; a rolling-hash version
reintroduces collision handling for zero benefit.

**One observation (not a defect):** block sizes run 5..2, so a *single*
immediately-repeated entry is out of scope. That looks deliberate —
one-line repetition is common legitimate prose ("No. No.") and would be
noise — but it's worth confirming that intent is written down somewhere
(a test asserts the ≥2 behavior would do).

**Recommendation: not worth touching.**

---

## Area 5 — Known corruptions & front matter (`app/source_normalization.py`)

**What the code does (confirmed).** A two-entry exact-substring dict
(`{"саге": "care", "пар": "nap"}` — Cyrillic lookalikes) applied
case-insensitively with per-hit line/column/before/after evidence; and a
front-matter stripper gated on both a `Manifesto.` document prefix **and**
a fixed compiler-template anchor regex, returning `(text, None)` untouched
on any shape mismatch.

**Known algorithms.** The generalizing options are real and named:
**Unicode TR39 confusables ("skeleton") normalization** for
lookalike-character folding, and **edit-distance spell correction**
(Norvig-style / SymSpell) for OCR errors. Both are the wrong call here:

- Global confusable folding would silently rewrite *legitimate* non-Latin
  text — and worse, it would destroy an existing detection channel:
  `chunk_quality.py` and `script_preflight.py` deliberately treat
  introduced Cyrillic/mixed-script words as **error evidence**
  (`unsupported_cyrillic`, `mixed_script_word`). Normalizing confusables
  upstream would blind those checks.
- Edit-distance correction produces probabilistic, hard-to-audit rewrites
  of an author's text — the exact class of vague check this project's
  ground rules (and its incident history) prohibit.

**A middle path if the dict grows:** the corruption pattern here is
specifically *mixed-script words* (Cyrillic chars inside Latin prose),
which `audit_unicode_text` already detects with offsets. If dozens more
corruptions appear, generate the replacement dict *from* the TR39
confusables table but apply it **only to words already flagged as
mixed-script** — the confusables table becomes data feeding the same
exact-match, evidence-emitting machinery. That generalizes without losing
auditability. Not needed at 2 entries.

**Recommendation: no change now.** The narrow, evidence-based design is
correct and load-bearing.

---

## Area 6 — Name deduplication (`app/find_nicknames.py`)

**What the code does (confirmed).** Matches the brief's description:

1. `collect_context` — per-token compiled `\b`-anchored regexes tested
   independently per entry (≤600 chars), specifically to avoid the
   combined-alternation prefix-consumption bug ("beat" vs "beatrice").
2. `_parse_alias_response` — resolves LLM-proposed variant→canonical pairs
   via exact case-insensitive dict lookup; non-matching variants are
   silently dropped (`label_by_norm.get(...) → continue`). Group labels
   and NARRATOR are filtered.

**Is a known algorithm a better fit?**

1. The named upgrade for multi-pattern matching with overlap awareness is
   **Aho-Corasick** — but it needs a dependency or a nontrivial hand-roll,
   and the workload (dozens of patterns × entries capped at 600 chars,
   early-exit at 300 co-occurrences) is trivially small. The current
   approach is the right tool. No change.
2. The exact-match strictness is a real gap in *coverage* but I read it as
   the correct *policy*, and it's consistent with the codebase's own
   precedent: `stabilize_speaker_identities` (Area 1) uses fuzzy matching
   only to **suggest** and never to **act**, while `_parse_alias_response`
   **acts** (its output drives merges that change which voice reads
   lines — the same stakes `diff_entries` flags narrator→character changes
   for manual review over). A fuzzy resolution step here would convert
   "missed alias" (recoverable — rerun, or handle in the UI merge tool)
   into occasional "wrong merge" (silent, hard to notice, wrong voice on
   real lines). Asymmetric downside; strict is right.

**Recommendation: no change.** Cheap optional improvement if misses ever
matter in practice: *log* dropped variants that fuzzy-match a known label
at ≥0.90 (reusing `_uncertain_candidates`) as review output — visibility
without auto-acting. Note the brief's "not yet traced" items (UI
character-merge feature, `/api/voice_library/apply*`) remain untraced in
this report too.

---

## Area 7 — Voice Lab audio pipeline

**Positive baseline (confirmed by read):** `voice_analysis.py` uses
`scipy.spatial.distance.cdist` cosine similarity over ECAPA embeddings
(lines 291, 537), `scipy.stats.wasserstein_distance` for prosody EMD
(line 583), and real `umap-learn` for projection (line 627). Established
tools, correctly reached for.

**`app/voice_clustering.py` — `cluster_voices` (read in full).** It is a
from-scratch complete-link agglomerative clusterer, but the hand-rolling
is justified by three properties scipy's
`linkage(method="complete")`/`fcluster` does not provide:

1. **Cannot-link constraints:** `split` overrides block specific merges
   *during* agglomeration (`blocked()` check inside the merge loop). This
   is **constrained agglomerative clustering**; scipy has no native
   support, and post-processing scipy's dendrogram to "un-merge" a
   forbidden pair is not well-defined (the forbidden merge changes every
   subsequent merge decision). Must-link (`merge`) alone could be
   pre-seeded, but cannot-link is structural.
2. **Determinism with explicit tie-breaking:** candidates are sorted by
   `(-similarity, sorted-labels)` — reproducible output independent of
   input order, which scipy does not guarantee under ties.
3. **Decision evidence:** every merge emits a
   `manual_merge`/`threshold_merge` record with the minimum cross
   similarity — the auditability requirement again.

The naive rescan-all-pairs loop is O(n³)-ish, but n = narrator datasets
(tens). **Recommendation: do not swap.** This is the case the brief
anticipated: the constraint logic is exactly why it was hand-rolled.

**Diarization gap (verified):** `--auto-detect-speakers` hard-errors at
`alexandria_preparer_rocm_compatible.py:3046` ("not implemented; use
`--diarize`"); real pyannote 3.1 diarization exists behind the opt-in
`--diarize` flag (lines 916, 3218). An automatic "does this file need
diarization" pre-check is feasible with tools already in the repo:
windowed ECAPA embeddings within one clip + the existing
similarity-threshold clustering — essentially cheap **speaker-change
detection**. But that's a *feature decision* (cost: a model pass over
every input by default; benefit only for users who don't know to pass
`--diarize`), not an algorithmic deficiency. **Recommendation: only if
multi-speaker audio processed without `--diarize` becomes an observed
problem.** Caveat: per the brief's own note, I did not read the preparer
end-to-end — this subsection is based on targeted reads around the
diarization code paths.

**`voice_profiler.py` passage extraction (read lines ~300–420):** ASIN
match → title-token-ratio match → numeric fallback for EPUB discovery,
then spine-based extraction skipping the first 20%. Pure heuristics for a
"give the LLM a flavor passage" job where a mediocre passage still works.
Forced alignment is explicitly and deliberately out of scope
(`app/tts.py:65`); nothing here warrants it. **No change.**

**`audit_voice_datasets.py` (skimmed structure only):** per-clip signal
metrics and warnings; no text/audio matching algorithms present to
review.

---

## Overall

One finding worth acting on: **Area 3's alignment-based dropped-span
extraction for retry feedback** — stdlib-only, additive (doesn't touch
calibrated gates), and directly targeted at the mechanism behind this
project's biggest recent incident. Everything else is either already the
right tool, deliberately strict for good documented reasons, or operating
at a scale where a textbook algorithm swap buys nothing and costs
auditability. Per the brief and Rule 14, the Area 3 change would be a
separate planned follow-up task.
