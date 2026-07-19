# Algorithm review brief (for Fable)

## Purpose

This repo (Alexandria Audiobook2, `app/`) has accumulated a handful of
hand-rolled text-matching, deduplication, and boundary-detection routines
over time — built to solve a specific bug as it came up, not designed from a
known algorithm. That's produced working code, but some of it is doing jobs
that well-established algorithms already solve more robustly (handling edge
cases the ad-hoc version doesn't) or more efficiently (avoiding brute-force
scans). You (Fable) were trained on a lot of published algorithms; the ask
is to read through the specific functions below and answer, for each one:
**is there a known, well-tested algorithm that's a better fit than what's
here, and is switching actually worth it?**

This is a **scouting/analysis task, not an implementation task**. Produce a
written report, not code changes — see "Deliverable" below.

## Ground rules

- **Don't propose a rewrite just because a fancier algorithm exists.** Every
  function below is small, currently-working, unit-tested code in a
  synchronous CPU-bound pipeline (no GPU/ML involved in these specific
  functions). A hand-rolled O(n²) loop over ~100-word entries is not a
  problem worth solving with something asymptotically better if the constant
  factors don't matter at this scale — say so explicitly when that's your
  conclusion, don't manufacture a finding to have something to report.
- **Auditability matters more than cleverness here.** This pipeline's whole
  design principle (see `app/CLAUDE.md`) is deterministic, explainable
  checks with printed evidence (line/column offsets, before/after diffs,
  recall percentages) — several of the memories on this project note that a
  vaguer/fuzzier version of a check has caused real production incidents
  before. A more "powerful" algorithm that produces a harder-to-explain
  verdict is a regression even if its aggregate accuracy is higher. Flag
  this tradeoff explicitly wherever it applies.
- **No new heavy dependencies without strong justification.** The project
  already leans on Python's stdlib (`difflib`, `re`, `collections.Counter`)
  and avoids adding libraries for things stdlib can do adequately. If your
  proposed algorithm needs a new dependency (e.g. `python-Levenshtein`,
  `rapidfuzz`, `numpy`), say so and weigh it against a stdlib-only
  alternative — don't assume the dependency is free.
- Every function listed below already has unit test coverage (mostly in
  `app/test_chunk_quality.py`, `app/test_speaker_identity.py`,
  `app/test_script_preflight.py`, `app/test_review_script.py` — search for
  the function name to find its tests). Read the tests before proposing a
  change; they encode the actual edge cases this code has been bitten by
  before (e.g. Cyrillic lookalike characters, CJK/Japanese tokenization,
  adjacent-duplicate detection across chunk boundaries).

## Areas to review

### 1. Fuzzy string similarity — `difflib.SequenceMatcher` usage

- `app/speaker_identity.py:83` (`stabilize_speaker_identities`) — matches a
  newly-seen speaker label against already-established speakers using
  `SequenceMatcher(None, key, candidate_key).ratio()` (Ratcliff/Obershelp
  algorithm) to catch aliasing like "MAN 2 (VILLAIN)" vs "MAN 1 (VILLAIN)".
- `app/review_script.py:734` and `:771` — uses the same `difflib` approach,
  including a whole-array `SequenceMatcher(None, original_texts,
  corrected_texts, autojunk=False)` diff to find what a review pass changed.

Ratcliff/Obershelp is a reasonable stdlib default, but it's not the only
option and has known weaknesses (e.g. less intuitive results than
Levenshtein/edit-distance for short strings, `autojunk` heuristics that can
misfire on repetitive text). Is there a better fit for short-label matching
specifically (character/speaker names, often 1-4 words) vs. the
long-document diffing in `review_script.py` — should these even use the same
algorithm? Consider Levenshtein distance, Jaro-Winkler (designed for short
strings like names), or token-based approaches (Jaccard/cosine over word
sets) as alternatives, but weigh the "no new dependency" constraint above.

### 2. Chunk/entry boundary overlap detection — `app/generate_script.py:138`

`_get_boundary_overlap(left_entries, right_entries, minimum_words=3)`: finds
the longest normalized *exact* word-for-word suffix-of-left that equals a
prefix-of-right, by brute-force trying every size from `min(len(left),
len(right))` down to `minimum_words`. This is the classic "longest suffix of
A that is a prefix of B" problem — used here to detect when adaptive
chunk-splitting caused the model to regenerate a few words of overlap across
a chunk boundary. Look at whether the Z-function / KMP failure-function
approach (which solves this in linear time and is a textbook algorithm) is
worth it here, and separately, whether *exact* matching is even the right
tool — a near-duplicate overlap (paraphrased, not verbatim) would currently
be missed entirely, unlike the fuzzy matching used elsewhere in this file.

### 3. Recall/quality scoring — `app/chunk_quality.py`

`validate_chunk_quality` (and its helpers `_tokens`, `_ngrams`,
`_counter_recall`, `~line 18-115`) score how much of a source chunk survived
into the model's output using: (a) a **bag-of-tokens multiset recall**
(`_counter_recall` — order-insensitive, `Counter`-based, essentially a
one-sided Jaccard-like count), and (b) an **ordered-trigram recall**
(`_ngrams(tokens, 3)` compared the same way) as a weak proxy for sequence
order. Thresholds live in `MIN_SOURCE_TOKEN_RECALL` /
`MIN_ORDERED_TRIGRAM_RECALL` (both 0.90).

This is functionally a hand-rolled approximation of sequence alignment
(comparing two token sequences for how much of one survives, in what order,
in the other) without using an actual alignment algorithm. Consider whether
Needleman-Wunsch/Smith-Waterman-style alignment, or `difflib.SequenceMatcher`
applied at the token level (already used elsewhere in this codebase, see
area 1), would give a more principled recall signal than
bag-of-trigrams — specifically: would it better distinguish "the model
paraphrased a sentence" (should probably still pass) from "the model dropped
a whole paragraph" (should fail) than the current metric does? This is the
single highest-value area to review, since **this exact scoring function is
what caused this session's biggest real production incident** (see
`generate_script_truncation_failure` context in project memory / recent git
history around `_build_retry_feedback_message`, `NEAR_MISS_RECALL_THRESHOLD`)
— a more informative or more stable metric here has outsized real value.
Don't just consider raw accuracy — consider whether an alignment-based
approach can also produce *which specific words/spans were dropped* as
output, which the current Counter-based approach cannot (it only produces an
aggregate percentage), and whether that would let `_build_retry_feedback_message`
(`app/generate_script.py`) give the model much more targeted retry feedback
than "you covered 82%, try again."

### 4. Adjacent-duplicate-block detection — `app/script_preflight.py:72`

`find_adjacent_duplicate_blocks(texts, source_text)`: brute-force scan for
immediately-repeated blocks of entries (block sizes 5 down to 2), checking
exact list equality (`left == right`) at every offset — O(block_sizes ×
n) with an `occupied` set to avoid double-counting overlapping matches. This
detects when a chunk got processed twice (e.g. after an adaptive split
recombination) and produced duplicate entries at the seam. Consider whether
a repeat-detection algorithm (e.g. suffix-array-based repeat finding, or
just a hash-based rolling-window approach) is meaningfully better here, or
whether — given the practical `n` here is at most a few hundred entries per
chunk — the current brute force is already fine and not worth touching.

### 5. Known source-text corruption + new front-matter stripping —
   `app/source_normalization.py`

`normalize_known_source_corruptions` does exact substring replacement from a
small fixed dict (`KNOWN_SOURCE_CORRUPTIONS`, e.g. Cyrillic lookalike
characters substituted into Latin text by a lossy OCR/copy-paste pipeline
upstream). `strip_known_front_matter` (added 2026-07-19) detects a specific
compiler's front-matter block via one fixed regex anchor. Both are
deliberately narrow, evidence-based pattern matches, not general algorithms
— is that the right call, or is there a well-known **OCR-error-correction**
or **near-duplicate-detection** algorithm (e.g. edit-distance-based spell
correction, confusable-character normalization à la Unicode confusables
tables) that would generalize better as more corrupted-source patterns are
discovered over time, without losing the auditability this file's design
explicitly prioritizes (see "Ground rules" above)?

### 6. Character/voice name deduplication — search these on your own

Not yet traced in detail this session, but worth checking for the same
"hand-rolled fuzzy matching" pattern: `app/find_nicknames.py` (speaker-alias
detection), the "merge duplicate character names" feature referenced in
`app/static/index.html`/`app/static/js/app-*.js` (character merging across a
whole batch), and `voice_library.json`-related matching in `app/app.py`'s
`/api/voice_library/apply*` routes. Also check the Voice Lab pipeline's
`voice_analysis.py --phase dedup` (dedup of generated audio samples,
described in project memory as embedding/clustering-based, not text) for
whether its clustering approach (if any) is a reasonable choice.

## Deliverable

A written report (markdown is fine, doesn't need to be code) covering, for
each area above:
1. **What the current code actually does** (confirm/correct my summary above
   by reading the real function).
2. **Whether a known algorithm is a better fit**, named specifically (not
   "some fuzzy matching algorithm" — name it: Levenshtein, Jaro-Winkler,
   Needleman-Wunsch, Z-function, MinHash/LSH, whatever applies).
3. **Concrete tradeoffs**: correctness/robustness gained, complexity or
   dependency cost, whether it changes the auditability of the output (per
   Ground rules), and whether the current scale (chunk sizes, entry counts —
   all in the hundreds at most, not millions) makes an asymptotic
   improvement actually matter in practice.
4. **A recommendation**: worth doing, worth doing only if X happens first
   (e.g. "only if recall-scoring incidents recur"), or not worth doing.

Do not write or modify any code as part of this task — report findings only.
If, after this review, some findings look clearly worth implementing, that
becomes a separate follow-up task with its own plan (per this repo's Rule 14
in `app/CLAUDE.md`: non-trivial implementation work gets planned and
approved before code is written).
