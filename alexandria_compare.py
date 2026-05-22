#!/usr/bin/env python3
"""
Alexandria Compare — diff a metadata.jsonl against an original EPUB or text file.
Shows each mismatch for your review. You approve every change; nothing is
written automatically.
"""

import os
import sys
import re
import json
import difflib
import argparse
import functools
from pathlib import Path

# ── Optional EPUB support ─────────────────────────────────────────────────────
try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    EPUB_AVAILABLE = True
except ImportError:
    EPUB_AVAILABLE = False

# ── ANSI colours ──────────────────────────────────────────────────────────────
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
SEP    = "─" * 72

# ── Annotation patterns (added by the TTS LLM) ───────────────────────────────
_EMPHASIS = re.compile(r'\*([^*]+)\*')   # *word* → word
_PAUSES   = re.compile(r'\.{3,}')        # ... and ....

# Smart quotes → ASCII. EPUBs use U+2019 ('right single quotation mark') as
# the apostrophe in contractions like "I'm" / "you're". Without this mapping,
# the punctuation strip in normalize() treats those characters as separators
# and splits the word: "I'm" → "i m". That breaks word-level alignment between
# the chunk (ASR usually emits ASCII apostrophes) and the source.
_SMART_QUOTES = str.maketrans({
    '‘': "'", '’': "'", '‚': "'", '‛': "'",
    '“': '"', '”': '"', '„': '"', '‟': '"',
})

# Common honorifics: audiobook narrators read these aloud ("mister", "doctor")
# but EPUBs typically abbreviate them. Without expansion the chunk's spelled
# form fuzzy-matches the source's abbreviation at only ~0.5, which is below
# the 0.55 threshold the trim uses — so the trailing/leading word gets
# dropped (e.g. chunk "MISTER SYAMA'S" vs source "Mr. Sayama's").
# We expand whole tokens only so we don't break unrelated words containing
# the abbreviation as a substring (e.g. "mrsmith" stays "mrsmith"; "jr."
# becomes "junior"). `st.` is intentionally NOT here — too many "Street"s.
_HONORIFICS = {
    'mr':   'mister',
    'mrs':  'missus',
    'ms':   'miss',
    'dr':   'doctor',
    'jr':   'junior',
    'sr':   'senior',
}
# Match the abbreviation as a standalone token (optionally followed by a period),
# bounded so we don't catch mid-word substrings ("mrsmith" stays "mrsmith") or
# unusual no-space forms ("Mr.Smith" stays alone — likely an extract artifact).
_HONORIFIC_RE = re.compile(
    r'(?<![\w.])(' + '|'.join(_HONORIFICS) + r')\.?(?=\s|$|[^\w.])',
    re.IGNORECASE,
)
def _expand_honorifics(text: str) -> str:
    return _HONORIFIC_RE.sub(lambda m: _HONORIFICS[m.group(1).lower()], text)

def strip_annotations(text: str) -> str:
    """Remove TTS markers so we compare actual words, not punctuation noise.
    Pauses (...) are replaced with a space — some LLM outputs have no
    whitespace around them (FACTS...WERE...THEY), so empty substitution
    would mash the surrounding words together into one non-word."""
    text = _EMPHASIS.sub(r'\1', text)
    text = _PAUSES.sub(' ', text)
    return text

def normalize(text: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace for fuzzy matching."""
    text = strip_annotations(text)
    text = text.translate(_SMART_QUOTES)    # I'm → I'm so the apostrophe survives
    # '&' is universally read aloud as "and" in prose. Without this expansion
    # the next regex strips it to whitespace, so a source like "Carrigdon &
    # Rudge" loses the token entirely and the chunk's audible "and" has
    # nothing to align against.
    text = text.replace('&', ' and ')
    # Honorific abbreviations: Mr./Mrs./Ms./Dr./Jr./Sr. → spelled forms so
    # the chunk's "MISTER SYAMA'S" can align with source "Mr. Sayama's".
    text = _expand_honorifics(text)
    text = text.lower()
    text = re.sub(r"[^\w\s']", ' ', text)   # keep apostrophes for contractions
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def to_words(text: str) -> list:
    return [w for w in normalize(text).split() if w]

# ── Source loaders ─────────────────────────────────────────────────────────────
def load_epub(path: str) -> str:
    if not EPUB_AVAILABLE:
        sys.exit(
            "EPUB support requires:\n"
            "  uv pip install ebooklib beautifulsoup4\n"
            "or:\n"
            "  pip install ebooklib beautifulsoup4"
        )
    book = epub.read_epub(path)
    parts = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        for tag in soup(['script', 'style']):
            tag.decompose()
        parts.append(soup.get_text(separator=' '))
    return '\n'.join(parts)

def load_source(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == '.epub':
        return load_epub(path)
    return p.read_text(encoding='utf-8', errors='replace')

def load_jsonl(path: str) -> list:
    entries = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if s:
                entries.append(json.loads(s))
    return entries

# ── Alignment ─────────────────────────────────────────────────────────────────
_FUZZY_KEEP_THRESHOLD = 0.55   # min char-level similarity to keep a boundary word.
# Lowered from 0.6 so 'saint'↔'st' (ratio 0.571, source has 'St.' abbreviated)
# and 'conali'↔'connolly' (ratio 0.571) survive trailing extension. False
# positives still very rare: 'cuttin'↔'karen' is 0.18, 'two'↔'002' is 0.0, etc.

_FUZZY_KEEP_THRESHOLD_LONG = 0.50  # relaxed bar for 1↔1 long-word boundaries.
# ASR-mistranscribed character names cluster just under 0.55: 'carser'↔'karsa'
# = 0.545, 'sineag'↔'synyg' = 0.545, 'beiroffs'↔'bairoth' = 0.533. With both
# words ≥5 chars the ratio carries enough signal; shorter words stay at 0.55
# so 'the'↔'she', 'of'↔'or'-class noise can't slip in.

_FUZZY_KEEP_THRESHOLD_PROPER_NOUN = 0.35  # deeper relaxation for known proper nouns.
# Japanese romanizations get severely ASR-mangled in ways char-fuzzy can't
# bridge: 'coodo'↔'kudou' = 0.40, 'youth'↔'yurie' = 0.40, 'caia'↔'kaya' = 0.50.
# K↔C substitution, vowel collapse, length drift. When we KNOW the source-
# side token is a recurring proper noun in this specific book (built into
# _PROPER_NOUNS at source load), the boundary alignment is almost certainly
# correct — relax the bar to 0.35 so the name actually lands in the span.

# Proper-noun lexicon for the currently-loaded source. Populated once during
# main() via _build_proper_nouns() after the OCR/diacritic source-clean.
# Stays empty when the script is imported as a library — _step_threshold
# falls back to the regular long-word path in that case.
_PROPER_NOUNS: frozenset = frozenset()


# Words frequently Title-cased mid-sentence in novels but NOT proper nouns —
# honorifics in direct address ('Father', 'Miss', 'Lord'), region adjectives
# ('Imperial', 'Western'), nationalities ('Japanese'), and a few structural
# words. Filtered out of the lexicon so the proper-noun threshold relaxation
# doesn't admit boundary matches like 'fish'/'miss' or 'rather'/'father'
# that happen to clear 0.35 but would be coincidental.
_PROPER_NOUN_STOPWORDS = frozenset({
    'father', 'mother', 'sister', 'brother', 'miss', 'mister', 'mistress',
    'doctor', 'lord', 'lady', 'madam', 'queen', 'king', 'prince', 'princess',
    'duke', 'duchess', 'son', 'daughter', 'uncle', 'aunt', 'cousin',
    'imperial', 'royal', 'western', 'eastern', 'northern', 'southern',
    'japanese', 'chinese', 'english', 'french', 'german', 'american',
    'contents', 'prologue', 'epilogue', 'chapter', 'preface', 'introduction',
    'special', 'general', 'major', 'minor', 'private', 'public',
})


def _build_proper_nouns(source_text: str) -> frozenset:
    """Extract recurring Title-cased tokens from source — character names and
    story-specific proper nouns like 'Grotesqueries', 'Spirit Sight'.

    Detection heuristic: a Title-cased word (≥4 chars) that appears at least
    twice **immediately after a lowercase word** so we catch mid-sentence
    occurrences and skip sentence-initial common words ('The'/'And'/'She').
    A character named 'Kudou' that shows up 100 times in the book will
    almost always appear mid-sentence at least twice; one-off Title-cased
    tokens (chapter titles, ALL CAPS headings) won't make the cut.
    `_PROPER_NOUN_STOPWORDS` drops common honorifics, region/nationality
    adjectives, and structural words that pass the frequency gate but
    shouldn't relax the boundary threshold.

    Returns a frozenset of normalized (lowercased) forms suitable for direct
    membership testing against `orig_match[i]` at trim boundaries.
    """
    from collections import Counter
    matches = re.findall(r'\b[a-z]+\b\s+(\b[A-Z][a-z]{3,}\b)', source_text)
    counts = Counter(matches)
    return frozenset(
        m.lower() for m, n in counts.items()
        if n >= 2 and m.lower() not in _PROPER_NOUN_STOPWORDS
    )


def _step_threshold(chunk_word: str, src_word: str, dc: int, ds: int) -> float:
    """Acceptance threshold for one trim extension step.

    Three tiers, applied only to 1↔1 steps (compound forms keep the default
    bar because their concatenated side already inflates length):
      - 0.35 when src_word is a known proper noun in this book
      - 0.50 when both words are ≥5 chars (the long-word relaxation)
      - 0.55 default
    """
    if dc == 1 and ds == 1:
        if len(chunk_word) >= 3 and src_word in _PROPER_NOUNS:
            return _FUZZY_KEEP_THRESHOLD_PROPER_NOUN
        if len(chunk_word) >= 5 and len(src_word) >= 5:
            return _FUZZY_KEEP_THRESHOLD_LONG
    return _FUZZY_KEEP_THRESHOLD


# EPUB OCR sometimes drops a stray digit between letters of a word
# ('thos1e', 't1he', 'Kars1a', 'S1leep'). The chunk's ASR transcription
# doesn't have the digit, so the merged output ends up with letter-digit-
# letter glitches in otherwise-correct prose. Strip these during source
# loading. Letter-digit endings ('M9', 'B4', 'iPhone3') aren't matched,
# only digits sandwiched between letters.
_OCR_DIGIT_GLITCH = re.compile(r'(?<=[A-Za-z])[0-9](?=[A-Za-z])')

# EPUB extraction sometimes separates a precomposed Latin diacritic from its
# stem ('fianc é' instead of 'fiancé', 'caf é' instead of 'café'). The audio
# narrator says one word, so the chunk has 'fiancé' as one token while the
# source has it split — alignment treats them as two separate words and the
# merge ends up with a stray 'é' floating in the output. Rejoin: ASCII letter
# + whitespace + single Latin-1/Latin-Extended letter followed by a word
# boundary. Range U+00C0–U+024F covers Latin-1 Supplement (À–ÿ) plus Latin
# Extended-A and -B. Constrained to a single trailing diacritic so we don't
# accidentally merge legitimate two-letter words like 'I é'/etc.
_DIACRITIC_REJOIN = re.compile(r'([A-Za-z])\s+([À-ɏ])(?=\W|$)')

# Mirror of the above for feminine -ée forms ('fiancée', 'née', 'protégée')
# that EPUBs split as 'fiancé e' / 'né e' / 'protégé e' (diacritic + space +
# lone ASCII letter). Restricted to a *lowercase* trailing letter so
# legitimate constructions like 'fiancé I once knew' (where 'I' starts a
# nested clause) stay split. Followed by a word boundary so we never join
# into the start of a real word ('fiancé event' → 'fiancée vent' would
# obviously be wrong).
_DIACRITIC_REJOIN_TAIL = re.compile(r'([À-ɏ])\s+([a-z])(?=\W|$)')

# Cached because the same (chunk-word, source-word) pairs recur constantly
# across the trim loops of every entry — e.g. proper-noun ASR mistranscriptions
# ('carser'↔'karsa') and short fillers show up at hundreds of boundaries per
# book. lru_cache lets repeat calls skip the ratio() work entirely.
@functools.lru_cache(maxsize=8192)
def _char_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# ── Number ↔ word equivalence ────────────────────────────────────────────────
# Audiobook narrators read digits aloud — "1611 hours" → "sixteen hundred eleven
# hours", "M9" → "M nine", chapter "002" → "two". The char-level fuzzy ext
# can't bridge these because "two" vs "002" has 0 common characters. We add
# a separate path: at boundary positions, try parsing the chunk side as a
# spelled number and the source side as a digit (or vice versa) and match
# numerically. Covers 0–9999 which is enough for years, page numbers, chapter
# markers, and ages.
_NUM_ONES  = {'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,
              'seven':7,'eight':8,'nine':9}
_NUM_TEENS = {'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,
              'fifteen':15,'sixteen':16,'seventeen':17,'eighteen':18,'nineteen':19}
_NUM_TENS  = {'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,
              'seventy':70,'eighty':80,'ninety':90}
_NUM_TOKENS = set(_NUM_ONES) | set(_NUM_TEENS) | set(_NUM_TENS) | {'hundred','thousand','and'}

def _parse_number(words: list):
    """Parse a list of tokens as an integer 0–9999. Returns int or None.

    Handles: digit strings ("2008"), single words ("two"), and spelled
    sequences ("forty seven", "two thousand eight", "sixteen hundred eleven",
    "two thousand and eight"). Hyphenated forms ("forty-seven") split first.
    """
    if not words:
        return None
    # Pure digit token (single word)
    if len(words) == 1 and re.fullmatch(r'\d{1,4}', words[0]):
        return int(words[0])
    # Spelled out — must contain at least one number word, no foreign words
    total, current = 0, 0
    saw_num = False
    for w in words:
        w = w.lower()
        # Hyphenated like "forty-seven" → recurse on parts
        if '-' in w:
            sub = _parse_number(w.split('-'))
            if sub is None:
                return None
            current += sub
            saw_num = True
            continue
        if w in _NUM_ONES:
            current += _NUM_ONES[w]; saw_num = True
        elif w in _NUM_TEENS:
            current += _NUM_TEENS[w]; saw_num = True
        elif w in _NUM_TENS:
            current += _NUM_TENS[w]; saw_num = True
        elif w == 'hundred':
            current = max(1, current) * 100; saw_num = True
        elif w == 'thousand':
            total += max(1, current) * 1000; current = 0; saw_num = True
        elif w == 'and':
            continue
        else:
            return None
    if not saw_num:
        return None
    n = total + current
    return n if 0 <= n <= 9999 else None


def _num_eq(a_words: list, b_words: list) -> bool:
    """True iff a_words and b_words parse to the same integer."""
    a = _parse_number(a_words)
    b = _parse_number(b_words)
    return a is not None and b is not None and a == b


def trim_span_to_alignment(
    chunk_words: list,
    orig_words: list,
    start: int,
    end: int,
) -> tuple:
    """Shrink an orig_words[start:end] span down to its actually-aligned region.

    find_best_match / realign return a fixed-width window (size == len(chunk_words)
    plus slop). The audio chunk rarely fills that window exactly — it may stop
    earlier than the window's tail (extra trailing source words) or skip leading
    source material (extra leading source words from prior content). Without
    trimming, the ORIGINAL display in the review UI shows source words that
    aren't actually in the audio.

    Two-stage trim:
      1. Anchor on the first and last EXACT-match ('equal') source words.
      2. Then extend OUTWARD greedily while the unmatched chunk word(s) adjacent
         to the boundary are character-level similar (≥ threshold) to the next
         source word(s). Each step tries 1↔1, 1↔2 (chunk merged / source split),
         and 2↔1 (chunk split / source contraction) so we handle ASR-merged
         compounds like 'lapcoat' ↔ 'lab coat'.
    """
    if end <= start:
        return start, end
    span = orig_words[start:end]
    sm = difflib.SequenceMatcher(None, chunk_words, span, autojunk=False)
    first_i = first_j = last_i = last_j = None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            if first_j is None:
                first_i, first_j = i1, j1
            last_i, last_j = i2, j2
    if first_j is None:
        return start, end

    # Switch to absolute indices into orig_words so the extension can grow
    # past the input [start, end) window. That matters when find_best_match's
    # search picked a window that's one word short of the chunk's actual
    # source content.
    abs_first = start + first_j
    abs_last  = start + last_j

    # Trailing extension — walks forward from the last 'equal'. Each step
    # tries 1↔1, 1↔2 (chunk merged ↔ source split), 2↔1 (chunk split ↔
    # source contraction), and number-equivalence over N↔1/1↔N for cases
    # like chunk "two thousand eight" ↔ source "2008".
    ci, sj = last_i, abs_last
    while ci < len(chunk_words) and sj < len(orig_words):
        s11 = _char_sim(chunk_words[ci], orig_words[sj])
        s12 = _char_sim(chunk_words[ci],
                        orig_words[sj] + orig_words[sj + 1]) \
              if sj + 1 < len(orig_words) else 0.0
        s21 = _char_sim(chunk_words[ci] + chunk_words[ci + 1],
                        orig_words[sj]) \
              if ci + 1 < len(chunk_words) else 0.0
        sim, dc, ds = max(((s11, 1, 1), (s12, 1, 2), (s21, 2, 1)),
                          key=lambda t: t[0])
        step_th = _step_threshold(chunk_words[ci], orig_words[sj], dc, ds)
        # Number-equivalence override: if the chunk's leading N words spell a
        # number that matches the source's digit token (or vice versa), accept
        # it as a perfect (1.0) match.
        if sim < step_th:
            nsim, ndc, nds = _num_eq_step_trailing(chunk_words, ci, orig_words, sj)
            if nsim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = nsim, ndc, nds
        # 1↔N fuzzy fallback for moderately-mashed source phrases
        if sim < step_th:
            nsim, ndc, nds = _one_to_N_trailing(chunk_words, ci, orig_words, sj)
            if nsim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = nsim, ndc, nds
        # 2↔2 fallback for "short-filler + character-name" boundaries:
        # chunk 'id beroth' ↔ source 'eyed bairoth' where neither word alone
        # fuzzy-matches but the pair clears the bar.
        if sim < step_th:
            tsim, tdc, tds = _two_by_two_trailing(chunk_words, ci, orig_words, sj)
            if tsim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = tsim, tdc, tds
        # Lookahead anchor (strongest fallback for ASR-mashed compound names)
        if sim < step_th:
            asim, adc, ads = _lookahead_anchor_trailing(chunk_words, ci, orig_words, sj)
            if asim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = asim, adc, ads
        if sim < step_th:
            break
        ci += dc
        sj += ds
    abs_last = sj

    # Leading extension — walks backward from the first 'equal'. Concat order
    # on either side preserves reading order, so chunk[ci-1]+chunk[ci] reads
    # 'should'+'have' (not the reverse) and src[sj-1]+src[sj] reads 'lab'+'coat'.
    ci, sj = first_i - 1, abs_first - 1
    while ci >= 0 and sj >= 0:
        s11 = _char_sim(chunk_words[ci], orig_words[sj])
        s12 = _char_sim(chunk_words[ci],
                        orig_words[sj - 1] + orig_words[sj]) \
              if sj - 1 >= 0 else 0.0
        s21 = _char_sim(chunk_words[ci - 1] + chunk_words[ci],
                        orig_words[sj]) \
              if ci - 1 >= 0 else 0.0
        sim, dc, ds = max(((s11, 1, 1), (s12, 1, 2), (s21, 2, 1)),
                          key=lambda t: t[0])
        step_th = _step_threshold(chunk_words[ci], orig_words[sj], dc, ds)
        # Number-equivalence override on the leading side too.
        if sim < step_th:
            nsim, ndc, nds = _num_eq_step_leading(chunk_words, ci, orig_words, sj)
            if nsim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = nsim, ndc, nds
        # 1↔N fuzzy fallback (leading direction)
        if sim < step_th:
            nsim, ndc, nds = _one_to_N_leading(chunk_words, ci, orig_words, sj)
            if nsim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = nsim, ndc, nds
        # 2↔2 fallback (leading direction)
        if sim < step_th:
            tsim, tdc, tds = _two_by_two_leading(chunk_words, ci, orig_words, sj)
            if tsim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = tsim, tdc, tds
        # Lookahead anchor (leading direction)
        if sim < step_th:
            asim, adc, ads = _lookahead_anchor_leading(chunk_words, ci, orig_words, sj)
            if asim >= _FUZZY_KEEP_THRESHOLD:
                sim, dc, ds = asim, adc, ads
        if sim < step_th:
            break
        abs_first = sj - ds + 1
        ci -= dc
        sj -= ds

    return abs_first, abs_last


def _num_eq_step_trailing(chunk_words, ci, orig_words, sj):
    """At trailing boundary (chunk[ci], src[sj]), try N↔1 and 1↔N number
    equivalence. Returns (1.0, dc, ds) on match, else (0.0, 0, 0).

    Only fires when at least one side contains a number-shaped token, to
    avoid wasted parsing on every step.
    """
    # Quick reject: at least one side needs to look numeric
    if not (_token_looks_numeric(chunk_words[ci]) or _token_looks_numeric(orig_words[sj])):
        return 0.0, 0, 0
    # Try N↔1: chunk has spelled number across multiple words, source has digit
    for N in (1, 2, 3, 4):
        if ci + N > len(chunk_words):
            break
        if _num_eq(chunk_words[ci:ci+N], [orig_words[sj]]):
            return 1.0, N, 1
    # Try 1↔N: source has spelled number, chunk has digit (rare)
    for N in (2, 3, 4):
        if sj + N > len(orig_words):
            break
        if _num_eq([chunk_words[ci]], orig_words[sj:sj+N]):
            return 1.0, 1, N
    return 0.0, 0, 0


def _num_eq_step_leading(chunk_words, ci, orig_words, sj):
    """At leading boundary (walking backward), try N↔1 and 1↔N number equivalence.
    Reading order is preserved (chunk[ci-N+1:ci+1] is the earlier→later sequence)."""
    if not (_token_looks_numeric(chunk_words[ci]) or _token_looks_numeric(orig_words[sj])):
        return 0.0, 0, 0
    # Try N↔1: chunk has spelled number, source has digit
    for N in (1, 2, 3, 4):
        if ci - N + 1 < 0:
            break
        if _num_eq(chunk_words[ci-N+1:ci+1], [orig_words[sj]]):
            return 1.0, N, 1
    # Try 1↔N: source has spelled number, chunk has digit
    for N in (2, 3, 4):
        if sj - N + 1 < 0:
            break
        if _num_eq([chunk_words[ci]], orig_words[sj-N+1:sj+1]):
            return 1.0, 1, N
    return 0.0, 0, 0


def _token_looks_numeric(w: str) -> bool:
    """Cheap precheck — token contains digits or is a known number word."""
    if not w:
        return False
    if any(c.isdigit() for c in w):
        return True
    return w.lower() in _NUM_TOKENS


# ── 1↔N fuzzy + lookahead anchor (handles ASR-mashed compound names) ─────────
# Pattern: chunk has a single mashup word ("Sagerasoske") that the audiobook
# narrator collapsed from a multi-word source phrase ("the boy in the
# high-collared uniform—Sagara Sousuke"). The 1↔1 / 1↔2 / 2↔1 alignment can't
# bridge this gap because the source has too many words for one chunk token.
# Two fallbacks, tried in order:
#   1. 1↔N fuzzy: concatenate N=3..5 source words and fuzzy-match against
#      the single chunk word. Catches "lapcoat"↔"lab coat tan"-like cases.
#   2. Lookahead anchor: if 1↔N fails, look for the chunk's NEXT TWO words
#      as a consecutive exact pair in the next ~20 source words. Strong
#      anchor (2 consecutive exact matches in a 20-word window) implies
#      the chunk's prior mashup word covers everything before that anchor.

def _one_to_N_trailing(chunk_words, ci, orig_words, sj):
    """Try 1↔N (N=3..5) at trailing boundary. Returns (sim, 1, N) or (0,0,0)."""
    if len(chunk_words[ci]) < 6:    # short chunk words → too many false positives
        return 0.0, 0, 0
    best = (0.0, 0, 0)
    for N in (3, 4, 5):
        if sj + N > len(orig_words):
            break
        concat = ''.join(orig_words[sj:sj+N])
        sim = _char_sim(chunk_words[ci], concat)
        if sim > best[0]:
            best = (sim, 1, N)
    return best if best[0] >= _FUZZY_KEEP_THRESHOLD else (0.0, 0, 0)


def _one_to_N_leading(chunk_words, ci, orig_words, sj):
    """Try 1↔N at leading boundary (walking backward)."""
    if len(chunk_words[ci]) < 6:
        return 0.0, 0, 0
    best = (0.0, 0, 0)
    for N in (3, 4, 5):
        if sj - N + 1 < 0:
            break
        concat = ''.join(orig_words[sj-N+1:sj+1])
        sim = _char_sim(chunk_words[ci], concat)
        if sim > best[0]:
            best = (sim, 1, N)
    return best if best[0] >= _FUZZY_KEEP_THRESHOLD else (0.0, 0, 0)


def _two_by_two_trailing(chunk_words, ci, orig_words, sj):
    """Try aligning a two-word chunk block to a two-word source block.

    Catches the "short-filler + character-name" pattern at the boundary:
    chunk 'id beroth' ↔ source 'eyed bairoth', or 'ay dalis' ↔ 'i dayliss'.
    Neither word alone fuzzy-matches the corresponding source word, but the
    concatenated pair does because the character-name fuzzy match carries
    enough signal across the joined string.

    Combined length gated at ≥6 chars on each side so trivial filler pairs
    ('at he' vs 'as the') can't slip in.
    """
    if ci + 1 >= len(chunk_words) or sj + 1 >= len(orig_words):
        return 0.0, 0, 0
    chunk_cat = chunk_words[ci] + chunk_words[ci + 1]
    src_cat   = orig_words[sj]  + orig_words[sj + 1]
    if len(chunk_cat) < 6 or len(src_cat) < 6:
        return 0.0, 0, 0
    sim = _char_sim(chunk_cat, src_cat)
    return (sim, 2, 2) if sim >= _FUZZY_KEEP_THRESHOLD else (0.0, 0, 0)


def _two_by_two_leading(chunk_words, ci, orig_words, sj):
    """Try aligning a two-word chunk block to a two-word source block at the
    leading boundary (walking backward). Concat order preserves reading
    direction: chunk[ci-1]+chunk[ci] and orig[sj-1]+orig[sj].
    """
    if ci - 1 < 0 or sj - 1 < 0:
        return 0.0, 0, 0
    chunk_cat = chunk_words[ci - 1] + chunk_words[ci]
    src_cat   = orig_words[sj - 1]  + orig_words[sj]
    if len(chunk_cat) < 6 or len(src_cat) < 6:
        return 0.0, 0, 0
    sim = _char_sim(chunk_cat, src_cat)
    return (sim, 2, 2) if sim >= _FUZZY_KEEP_THRESHOLD else (0.0, 0, 0)


def _lookahead_anchor_trailing(chunk_words, ci, orig_words, sj,
                                max_lookahead=20):
    """At trailing boundary, if chunk[ci] can't be aligned, see if chunk[ci+1]
    and chunk[ci+2] form a consecutive exact pair in orig_words[sj:sj+max_lookahead].
    A 2-word exact match in a 20-word window is a strong signal that chunk[ci]
    covers a long source phrase. Returns (1.0, 1, gap) where gap is how many
    source words to skip past, or (0, 0, 0).

    Both anchor words must be ≥4 chars to avoid spurious common-word anchors
    like "the the" or "a a".
    """
    if ci + 2 >= len(chunk_words):
        return 0.0, 0, 0
    a, b = chunk_words[ci + 1], chunk_words[ci + 2]
    if len(a) < 4 or len(b) < 4:
        return 0.0, 0, 0
    end = min(sj + max_lookahead - 1, len(orig_words) - 1)
    for k in range(sj, end):
        if orig_words[k] == a and orig_words[k + 1] == b:
            gap = k - sj
            if gap == 0:
                return 0.0, 0, 0     # anchor IS at sj; let the normal loop handle it
            return 1.0, 1, gap
    return 0.0, 0, 0


def _lookahead_anchor_leading(chunk_words, ci, orig_words, sj,
                               max_lookahead=20):
    """Mirror of _lookahead_anchor_trailing for the leading boundary."""
    if ci - 2 < 0:
        return 0.0, 0, 0
    # chunk[ci-2], chunk[ci-1] should be the anchor pair (preceding the mashup)
    a, b = chunk_words[ci - 2], chunk_words[ci - 1]
    if len(a) < 4 or len(b) < 4:
        return 0.0, 0, 0
    start = max(sj - max_lookahead + 1, 1)
    for k in range(sj, start - 1, -1):
        if orig_words[k] == b and orig_words[k - 1] == a:
            gap = sj - k
            if gap == 0:
                return 0.0, 0, 0
            return 1.0, 1, gap
    return 0.0, 0, 0


def _ratio(chunk_words: list, span_words: list) -> float:
    if not span_words:
        return 0.0
    return difflib.SequenceMatcher(None, chunk_words, span_words, autojunk=False).ratio()


def _span_size(n: int) -> int:
    """Source-side window size for a chunk of n ASR words.

    Source coverage rarely equals chunk word count exactly — ASR merges
    multiple source words into one (e.g., "a right" → "aright") and some
    chunks straddle a few extra source words. A fixed window of exactly n
    drops the chunk's trailing words when this happens. We add a small slop
    here; trim_span_to_alignment clips the result back to the actual
    aligned region, so the user-visible span is still tight.
    """
    return n + max(2, n // 10)


def find_best_match(
    chunk_words: list,
    orig_words: list,
    cursor: int,
    window: int = 200,
    backtrack: int = 40,
) -> tuple:
    """
    Slide a fixed-width window (len == chunk) over orig_words near cursor,
    then trim the winning window down to its actually-aligned region.
    Returns (best_start, best_end, ratio) where the ratio reflects the trimmed
    span (so it represents how well the chunk matches the audio content it
    actually covers, not how well it matches a fixed-width window).
    """
    n = len(chunk_words)
    if n == 0:
        return cursor, cursor, 1.0

    if cursor >= len(orig_words):
        return cursor, cursor, 0.0

    span_size = _span_size(n)

    best_ratio = 0.0
    best_start = cursor
    best_end   = min(cursor + span_size, len(orig_words))

    search_start = max(0, cursor - backtrack)
    search_end   = min(cursor + window, max(1, len(orig_words) - n // 2))

    # Reuse one SequenceMatcher across the scanning loop. chunk_words is
    # constant; setting it as seq2 (via set_seq2) builds the expensive b2j
    # position index once. Each iteration then only calls set_seq1(span),
    # which is cheap. ratio() is value-symmetric so the result is unchanged.
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(chunk_words)

    if search_end <= search_start:
        end_i = min(cursor + span_size, len(orig_words))
        span  = orig_words[cursor:end_i]
        if span:
            sm.set_seq1(span)
            r = sm.ratio()
            t_start, t_end = trim_span_to_alignment(chunk_words, orig_words, cursor, end_i)
            if t_end > t_start:
                return t_start, t_end, _ratio(chunk_words, orig_words[t_start:t_end])
            return cursor, end_i, r
        return cursor, cursor, 0.0

    for i in range(search_start, search_end):
        end_i = min(i + span_size, len(orig_words))
        span  = orig_words[i:end_i]
        if not span:
            continue
        sm.set_seq1(span)
        r = sm.ratio()
        if r > best_ratio:
            best_ratio = r
            best_start = i
            best_end   = end_i

    t_start, t_end = trim_span_to_alignment(chunk_words, orig_words, best_start, best_end)
    if t_end > t_start:
        best_start, best_end = t_start, t_end
        best_ratio = _ratio(chunk_words, orig_words[best_start:best_end])

    return best_start, best_end, best_ratio


def find_anchor_position(
    chunk_words: list,
    orig_match: list,
    min_ratio: float = 0.4,
) -> tuple:
    """
    Wide search across the ENTIRE source for the best position for this chunk.
    Used to determine where the audio first connects to the text, skipping
    any audio-only intro material (credits, narrator intros, etc.).
    Returns (start, end, ratio).
    """
    n = len(chunk_words)
    if n == 0 or len(orig_match) < n:
        return 0, n, 0.0

    stride = max(1, n // 4)
    chunk_set = set(chunk_words)
    required_overlap = max(1, int(min_ratio * 0.6 * n))

    best_ratio = -1.0
    best_start = 0

    # Reuse SequenceMatcher across both coarse and refine passes (see
    # find_best_match for rationale).
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(chunk_words)

    # Coarse search with fast set-overlap prefilter
    for i in range(0, len(orig_match) - n + 1, stride):
        span = orig_match[i : i + n]
        overlap = sum(1 for w in span if w in chunk_set)
        if overlap < required_overlap:
            continue
        sm.set_seq1(span)
        r = sm.ratio()
        if r > best_ratio:
            best_ratio = r
            best_start = i

    # Refine ±stride at stride=1 around the best coarse position
    refine_lo = max(0, best_start - stride)
    refine_hi = min(len(orig_match) - n, best_start + stride)
    for i in range(refine_lo, refine_hi + 1):
        span = orig_match[i : i + n]
        sm.set_seq1(span)
        r = sm.ratio()
        if r > best_ratio:
            best_ratio = r
            best_start = i

    return best_start, best_start + n, max(0.0, best_ratio)


def auto_anchor(
    entries: list,
    orig_match: list,
    max_attempts: int = 20,
    min_words: int = 10,
    min_ratio: float = 0.4,
) -> tuple:
    """
    Try the first few JSONL entries until one finds a confident position in
    the source. Returns (entry_idx, source_word_idx, ratio).
    If none anchor confidently, returns (0, 0, 0.0).
    """
    for entry_idx in range(min(max_attempts, len(entries))):
        chunk_words = to_words(entries[entry_idx].get('text', ''))
        if len(chunk_words) < min_words:
            continue
        start, _, ratio = find_anchor_position(chunk_words, orig_match, min_ratio)
        if ratio >= min_ratio:
            return entry_idx, start, ratio
    return 0, 0, 0.0


def realign(
    chunk_words: list,
    orig_match: list,
    cursor: int,
    max_search: int = 3000,
    min_ratio: float = 0.45,
) -> tuple:
    """
    Wider search ahead of cursor for a confident match. Called when the normal
    window search returns a weak ratio (cursor is lost). Returns
    (start, end, ratio) or (cursor, cursor, ratio) if no confident match
    found — indicating this entry has no source equivalent.
    """
    n = len(chunk_words)
    if n == 0 or cursor >= len(orig_match):
        return cursor, cursor, 0.0

    span_size = _span_size(n)

    search_end = min(cursor + max_search, len(orig_match) - n + 1)
    if cursor >= search_end:
        return cursor, cursor, 0.0

    stride = max(1, n // 4)
    chunk_set = set(chunk_words)
    required_overlap = max(1, int(min_ratio * 0.6 * n))

    best_ratio = -1.0
    best_start = cursor

    # Reuse SequenceMatcher across both passes (see find_best_match).
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(chunk_words)

    for i in range(cursor, search_end, stride):
        span = orig_match[i : i + span_size]
        overlap = sum(1 for w in span if w in chunk_set)
        if overlap < required_overlap:
            continue
        sm.set_seq1(span)
        r = sm.ratio()
        if r > best_ratio:
            best_ratio = r
            best_start = i

    refine_lo = max(cursor, best_start - stride)
    refine_hi = min(max(cursor, len(orig_match) - span_size), best_start + stride)
    for i in range(refine_lo, refine_hi + 1):
        span = orig_match[i : i + span_size]
        sm.set_seq1(span)
        r = sm.ratio()
        if r > best_ratio:
            best_ratio = r
            best_start = i

    best_ratio = max(0.0, best_ratio)
    if best_ratio < min_ratio:
        return cursor, cursor, best_ratio

    win_start = best_start
    win_end   = min(best_start + span_size, len(orig_match))
    t_start, t_end = trim_span_to_alignment(chunk_words, orig_match, win_start, win_end)
    if t_end > t_start:
        return t_start, t_end, _ratio(chunk_words, orig_match[t_start:t_end])
    return win_start, win_end, best_ratio


def parse_annotated_tokens(text: str) -> list:
    """
    Parse a TTS-annotated string into tokens carrying their prosody markers.
    Each token = {'word', 'leading_pause', 'trailing_pause', 'emphasized'}.
    Used by merge_annotations_with_source to preserve markers while replacing
    the actual words with the cleaner source text.
    """
    tokens = []
    pending_leading = ''
    # Expand multi-word emphasis spans ("*Trull Sengar*") into per-word
    # emphasis ("*Trull* *Sengar*") so the whitespace tokenizer below sees
    # each emphasized word as a self-contained *word* token. Without this,
    # *Trull splits off (starts with * but doesn't end with one) and so
    # does Sengar*, and the emphasis is lost on both.
    text = re.sub(
        r'\*([^*]+)\*',
        lambda m: ' '.join(f'*{w}*' for w in m.group(1).split()) or m.group(0),
        text,
    )
    # LLM/ASR output sometimes joins words with `...` instead of spaces
    # ("YOU...*DERONDL*...THE..."). Split those dot-runs out so the
    # whitespace tokenizer below sees each word and each pause separately —
    # otherwise the whole chain collapses into one garbled token and every
    # *emphasis* marker in it is silently dropped from the merge.
    text = re.sub(r'\.{3,}', r' \g<0> ', text)
    for raw in text.split():
        # Pure pause token (e.g. "..." or "....") — attach to NEXT word
        if re.fullmatch(r'\.{3,}', raw):
            if len(raw) > len(pending_leading):
                pending_leading = raw
            continue

        leading = pending_leading
        pending_leading = ''

        # Leading dots fused to the start of this token
        m = re.match(r'^(\.{3,})', raw)
        if m:
            d = m.group(1)
            leading = d if len(d) > len(leading) else leading
            raw = raw[len(d):]

        # Trailing dots fused to the end
        trailing = ''
        m = re.search(r'(\.{3,})$', raw)
        if m:
            trailing = m.group(1)
            raw = raw[:-len(m.group(1))]

        # LLM sometimes attaches sentence punctuation directly to the closing
        # emphasis marker ("*HULLO*!", "*PLEASANT*,", "*DURONDYL*?"). That
        # hides the closing * inside the token, so the endswith('*') check
        # below misses the emphasis. Peel any trailing non-word junk off a
        # clean *…* group; source punctuation comes back naturally through
        # the source spelling during merge.
        m = re.match(r'^(\*[^*\s]+\*)([\W_]+)$', raw)
        if m:
            raw = m.group(1)

        # Emphasis (*word*)
        emphasized = False
        if raw.startswith('*') and raw.endswith('*') and len(raw) >= 3:
            emphasized = True
            raw = raw[1:-1]

        # Strip surrounding non-word punctuation for matching
        word_for_match = re.sub(r"[^\w']", '', raw.translate(_SMART_QUOTES).lower())
        if not word_for_match:
            if trailing:
                pending_leading = trailing
            continue

        tokens.append({
            'word':           word_for_match,
            'leading_pause':  leading,
            'trailing_pause': trailing,
            'emphasized':     emphasized,
        })
    # Trailing `...` after the final word has nowhere to attach as
    # leading_pause — flush it onto the last token's trailing_pause so
    # terminal pauses aren't lost.
    if pending_leading and tokens:
        if len(pending_leading) > len(tokens[-1]['trailing_pause']):
            tokens[-1]['trailing_pause'] = pending_leading
    return tokens


def merge_annotations_with_source(annotated_text: str, source_words: list) -> str:
    """
    Return a new string using `source_words` (correct content/spelling/punct)
    while preserving the LLM's prosody markers (...., *word*) from
    `annotated_text` wherever words align.

    This is the heart of the [m]erge option — it lets users fix ASR errors
    without throwing away the prosody hints that keep TTS output expressive.
    """
    if not source_words:
        return ''

    annotated = parse_annotated_tokens(annotated_text)
    annot_words = [t['word'] for t in annotated]

    src_match = [
        re.sub(r"[^\w']", '', w.translate(_SMART_QUOTES).lower())
        for w in source_words
    ]

    # Word-level alignment between annotated and source.
    #
    # 'equal' opcodes are the easy case — chunk and source agree exactly.
    # 'replace' opcodes are where ASR mistranscribes a word (chunk "amander"
    # vs source "Amanda") or compounds get split differently (chunk "first"
    # "hand" vs source "firsthand"). Without same-position fuzzy mapping
    # inside 'replace' blocks, those chunk words' prosody markers (*emphasis*
    # and pauses) get dropped on the floor and the merge silently produces
    # plain source text, indistinguishable from [a]ccept original.
    sm = difflib.SequenceMatcher(None, annot_words, src_match, autojunk=False)
    src_to_tok = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                src_to_tok[j1 + k] = annotated[i1 + k]
        elif tag == 'replace':
            for k in range(min(i2 - i1, j2 - j1)):
                sim = difflib.SequenceMatcher(
                    None, annot_words[i1 + k], src_match[j1 + k], autojunk=False
                ).ratio()
                if sim >= _FUZZY_KEEP_THRESHOLD:
                    src_to_tok[j1 + k] = annotated[i1 + k]
            # Imbalanced replace: ASR may mash a multi-word name into one
            # token ("Tralesengar" vs "Trull Sengar") or split one into many
            # ("nine hundred forty third" vs "943rd"). The 1↔1 pass above
            # only checks paired positions, so the unpaired side gets no
            # emphasis. Try a concatenation match on whichever side is
            # longer and, if it clears the fuzzy bar, broadcast the chunk
            # emphasis across the matched source positions.
            chunk_n, src_n = i2 - i1, j2 - j1
            if chunk_n == 1 and src_n > 1 and j1 not in src_to_tok:
                cat = ''.join(src_match[j1:j2])
                sim = difflib.SequenceMatcher(
                    None, annot_words[i1], cat, autojunk=False
                ).ratio()
                if sim >= _FUZZY_KEEP_THRESHOLD:
                    for k in range(src_n):
                        src_to_tok[j1 + k] = annotated[i1]
            elif src_n == 1 and chunk_n > 1 and j1 not in src_to_tok:
                cat = ''.join(annot_words[i1:i2])
                sim = difflib.SequenceMatcher(
                    None, cat, src_match[j1], autojunk=False
                ).ratio()
                if sim >= _FUZZY_KEEP_THRESHOLD:
                    # Prefer the first emphasized chunk token so the
                    # emphasis carries onto the single source word.
                    chosen = next(
                        (annotated[i1 + k] for k in range(chunk_n)
                         if annotated[i1 + k]['emphasized']),
                        annotated[i1],
                    )
                    src_to_tok[j1] = chosen

    parts = []
    for i, src_word in enumerate(source_words):
        tok = src_to_tok.get(i)
        if tok:
            if tok['leading_pause']:
                parts.append(tok['leading_pause'])
            if tok['emphasized']:
                # Wrap the word's *letters* in asterisks, leaving any
                # surrounding punctuation outside: "library." -> "*library*."
                m = re.match(r"^(\W*)(.*?)(\W*)$", src_word)
                if m and m.group(2):
                    parts.append(f"{m.group(1)}*{m.group(2)}*{m.group(3)}")
                else:
                    parts.append(f"*{src_word}*")
            else:
                parts.append(src_word)
            if tok['trailing_pause']:
                parts.append(tok['trailing_pause'])
        else:
            parts.append(src_word)

    return ' '.join(parts)


def estimate_alignment_quality(
    entries: list,
    orig_match: list,
    initial_cursor: int,
    max_samples: int = 30,
) -> tuple:
    """Pre-scan the first ~max_samples entries to estimate how well the audio
    aligns with the chosen source. Returns (avg_ratio, n_sampled, n_below_60).

    A low average (< ~0.70) usually means the audiobook was narrated from a
    different translation/edition than the EPUB you provided — different
    publisher's intro credits, different editor's prose, or just the wrong
    file entirely. Catching this upfront beats discovering it after 50
    hand-edits.

    Mirrors the run() loop's full alignment logic (find_best_match → realign
    → full-source re-anchor) so the estimate reflects what the user will
    actually see, including recoveries that the new re-anchor catches.
    """
    cursor = initial_cursor
    ratios = []
    for idx in range(min(max_samples, len(entries))):
        chunk_words = to_words(entries[idx].get('text', ''))
        if len(chunk_words) < 5:
            continue
        start, end, ratio = find_best_match(chunk_words, orig_match, cursor)
        if ratio < 0.45:
            r_start, r_end, r_ratio = realign(chunk_words, orig_match, cursor)
            if r_ratio >= 0.55 and r_ratio > ratio + 0.15:
                start, end, ratio = r_start, r_end, r_ratio
            elif r_ratio < 0.30:
                a_start, a_end, a_ratio = find_anchor_position(
                    chunk_words, orig_match, min_ratio=0.6
                )
                if a_ratio >= 0.6 and a_ratio > ratio + 0.4:
                    t_start, t_end = trim_span_to_alignment(
                        chunk_words, orig_match, a_start, a_end
                    )
                    if t_end > t_start:
                        start, end = t_start, t_end
                        ratio = _ratio(chunk_words, orig_match[start:end])
                    else:
                        start, end, ratio = a_start, a_end, a_ratio
        ratios.append(ratio)
        cursor = end
    if not ratios:
        return 0.0, 0, 0, 0
    avg = sum(ratios) / len(ratios)
    low = sum(1 for r in ratios if r < 0.60)        # outright misalignment
    review_needed = sum(1 for r in ratios if r < 0.90)  # below auto-approve bar
    return avg, len(ratios), low, review_needed


def find_text_in_source(text: str, orig_match: list) -> int:
    """
    Fuzzy-find the given text in the source. Returns the source word index of
    the match, or -1 if not found with reasonable confidence.
    """
    text_words = to_words(text)
    n = len(text_words)
    if n == 0 or len(orig_match) < n:
        return -1

    text_set = set(text_words)
    required_overlap = max(1, int(0.5 * n))

    best_ratio = -1.0
    best_start = -1
    for i in range(0, len(orig_match) - n + 1):
        span = orig_match[i : i + n]
        overlap = sum(1 for w in span if w in text_set)
        if overlap < required_overlap:
            continue
        r = difflib.SequenceMatcher(None, text_words, span, autojunk=False).ratio()
        if r > best_ratio:
            best_ratio = r
            best_start = i

    return best_start if best_ratio >= 0.5 else -1

# ── Diff display ───────────────────────────────────────────────────────────────
def color_diff(a: list, b: list) -> tuple:
    """Return (a_colored_str, b_colored_str) with ANSI markup."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    a_out, b_out = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a_seg = ' '.join(a[i1:i2])
        b_seg = ' '.join(b[j1:j2])
        if tag == 'equal':
            a_out.append(a_seg)
            b_out.append(b_seg)
        elif tag == 'replace':
            a_out.append(RED + a_seg + RESET)
            b_out.append(GREEN + b_seg + RESET)
        elif tag == 'delete':
            a_out.append(RED + a_seg + RESET)
        elif tag == 'insert':
            b_out.append(GREEN + b_seg + RESET)
    return ' '.join(a_out), ' '.join(b_out)

def fmt_time(s: float) -> str:
    m, sec = divmod(int(s), 60)
    h, m   = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"

# ── Checkpoint ────────────────────────────────────────────────────────────────
def checkpoint_path(jsonl_path: str) -> Path:
    p = Path(jsonl_path)
    return p.with_name(f".{p.stem}_compare_progress.json")

def load_checkpoint(jsonl_path: str) -> dict:
    cp = checkpoint_path(jsonl_path)
    if cp.exists():
        return json.loads(cp.read_text())
    return {"decisions": {}, "cursor": 0}

def save_checkpoint(jsonl_path: str, decisions: dict, cursor: int):
    cp = checkpoint_path(jsonl_path)
    cp.write_text(json.dumps({"decisions": decisions, "cursor": cursor}, indent=2))

# ── Review log ────────────────────────────────────────────────────────────────
# Records every entry the user manually reviewed (auto-approved entries are
# skipped to keep the log signal-rich). The intent is to feed this back into
# the script so common edit patterns can be fixed at the source — for example,
# if every [e]dit reshapes the merge preview the same way, the merge function
# can be improved instead.
def review_log_path(output_path: str) -> Path:
    p = Path(output_path)
    return p.with_name(p.stem + '_review_log.jsonl')

def log_decision(log_path: Path, record: dict):
    """Append one decision as a JSON line. Best-effort: a logging failure
    must never abort the user's review session."""
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        pass

def remove_log_entries(log_path: Path, indices: set) -> int:
    """Rewrite the review log without records whose entry_idx is in `indices`.
    Returns the count removed. Used by in-session [u]ndo and by --reset-* flags."""
    if not log_path.exists():
        return 0
    kept, removed = [], 0
    with open(log_path, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get('entry_idx') in indices:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                pass
            kept.append(line)
    with open(log_path, 'w', encoding='utf-8') as f:
        for line in kept:
            f.write(line + '\n')
    return removed

def find_last_manual_idx(decisions: dict):
    """Highest entry index that was decided manually (not auto-approved and
    not a pre-anchor auto-keep). Returns None if there's nothing to undo."""
    for k in sorted((int(k) for k in decisions.keys()), reverse=True):
        d = decisions[str(k)]
        if not d.get('auto') and not d.get('pre_anchor'):
            return k
    return None

# ── Targeted reset ────────────────────────────────────────────────────────────
# `--reset` wipes everything; the flags below let the user undo specific
# entries without losing the rest of a long review session. Triggered from
# main() before any review work happens — the script does the reset, prints
# what changed, and exits.
def parse_reset_spec(reset_entry: str, reset_from: int, reset_range: str,
                     total_entries: int) -> set:
    """Collect all entry indices targeted by --reset-entry / --reset-from /
    --reset-range into a single sorted set. Returns empty set if none given."""
    indices = set()
    if reset_entry:
        for tok in reset_entry.split(','):
            tok = tok.strip()
            if not tok:
                continue
            try:
                indices.add(int(tok))
            except ValueError:
                sys.exit(f"--reset-entry: bad index {tok!r}")
    if reset_from is not None:
        if reset_from < 0:
            sys.exit(f"--reset-from: index must be ≥ 0, got {reset_from}")
        for i in range(reset_from, total_entries):
            indices.add(i)
    if reset_range:
        try:
            lo_s, hi_s = reset_range.split(':', 1)
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            sys.exit(f"--reset-range: expected N:M, got {reset_range!r}")
        if lo > hi:
            sys.exit(f"--reset-range: lo > hi ({lo} > {hi})")
        for i in range(lo, hi + 1):
            indices.add(i)
    return indices

def apply_targeted_reset(
    jsonl_path: str,
    output_path: str,
    log_path: Path,
    indices: set,
    also_clear_log: bool,
):
    """Restore each indexed entry's text from metadata.jsonl, drop its
    checkpoint decision, and optionally remove matching review-log records.
    The cursor in the checkpoint is rewound to the smallest reset index so the
    next normal run re-aligns from there (otherwise the cursor could be ahead
    of source content that the reset entries should have consumed)."""
    with open(jsonl_path, encoding='utf-8') as f:
        orig_lines = f.readlines()

    out_path = Path(output_path)
    if out_path.exists():
        with open(out_path, encoding='utf-8') as f:
            cur_lines = f.readlines()
    else:
        cur_lines = orig_lines.copy()

    restored, out_of_range = 0, []
    for idx in sorted(indices):
        if 0 <= idx < len(orig_lines) and idx < len(cur_lines):
            cur_lines[idx] = orig_lines[idx]
            restored += 1
        else:
            out_of_range.append(idx)

    with open(out_path, 'w', encoding='utf-8') as f:
        f.writelines(cur_lines)

    # Drop decisions; rewind cursor to the lowest reset index so the next
    # run re-anchors before that point instead of skipping ahead.
    cp_path = checkpoint_path(jsonl_path)
    popped = 0
    if cp_path.exists():
        cp = json.loads(cp_path.read_text())
        decisions = cp.get('decisions', {})
        for idx in indices:
            if decisions.pop(str(idx), None) is not None:
                popped += 1
        if indices:
            min_idx = min(indices)
            # cursor_after of the entry just before min_idx, if present
            prev = decisions.get(str(min_idx - 1)) if min_idx > 0 else None
            cp['cursor'] = prev['cursor_after'] if (prev and 'cursor_after' in prev) else 0
        cp_path.write_text(json.dumps(cp, indent=2, ensure_ascii=False))

    log_removed = 0
    if also_clear_log and log_path.exists():
        kept = []
        with open(log_path, encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get('entry_idx') in indices:
                        log_removed += 1
                        continue
                except json.JSONDecodeError:
                    pass
                kept.append(line)
        with open(log_path, 'w', encoding='utf-8') as f:
            for line in kept:
                f.write(line + '\n')

    print(f"Reset {len(indices)} target(s):")
    print(f"  {restored} JSONL line(s) restored from {jsonl_path}")
    print(f"  {popped} checkpoint decision(s) removed")
    if also_clear_log:
        print(f"  {log_removed} review log record(s) removed")
    else:
        print(f"  Review log untouched (pass --also-clear-log to remove records)")
    if out_of_range:
        print(f"  ⚠ Out of range (ignored): {sorted(out_of_range)}")

# ── Write output ──────────────────────────────────────────────────────────────
def write_output(entries: list, decisions: dict, output_path: str):
    """Write corrected JSONL. Entries with action 'accept', 'merge', or 'edit'
    get their text replaced; everything else is written through unchanged."""
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, entry in enumerate(entries):
            key = str(i)
            d   = decisions.get(key)
            if d and d['action'] in ('accept', 'merge', 'edit'):
                entry = dict(entry)
                entry['text'] = d['text']
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    print(f"\n{GREEN}✓ Corrected JSONL written → {output_path}{RESET}")

# ── Interactive review loop ───────────────────────────────────────────────────
def run(
    entries: list,
    orig_display: list,   # source words, original capitalisation + punctuation
    orig_match: list,     # source words, normalised (parallel to orig_display)
    decisions: dict,
    cursor: int,
    threshold: float,
    review_all: bool,
    jsonl_path: str,
    output_path: str,
    log_path: Path,
):
    total    = len(entries)
    auto_ct  = 0
    review_ct = 0

    print(f"\n{BOLD}Alexandria Compare{RESET}")
    print(f"  Entries      : {total}")
    print(f"  Auto-approve : similarity ≥ {threshold:.0%}  (override with --review-all)")
    print(f"  Checkpoint   : {checkpoint_path(jsonl_path)}")
    print(f"  Output       : {output_path}")
    print(f"  Review log   : {log_path}")
    if decisions:
        print(f"  Resuming     : {len(decisions)} entries already decided")
    print()
    print(f"  {DIM}Tip: the LLM's *emphasis* and ... pause markers carry the prosody "
          f"that keeps the trained TTS voice from sounding flat. When fixing ASR "
          f"errors, prefer {RESET}{BOLD}[m]erge{RESET}{DIM} over {RESET}{BOLD}[a]ccept original{RESET}{DIM} "
          f"— it uses the correct source words while keeping those markers.{RESET}")
    print()

    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        key = str(idx)

        # Already decided in a prior session — restore cursor and skip display
        if key in decisions:
            if 'cursor_after' in decisions[key]:
                cursor = decisions[key]['cursor_after']
            idx += 1
            continue

        chunk_text  = entry.get('text', '')
        chunk_words = to_words(chunk_text)

        start, end, ratio = find_best_match(chunk_words, orig_match, cursor)

        # ── Re-anchor when alignment looks lost ────────────────────────────────
        # Weak match within the narrow window? The cursor may be drifting past
        # a section the audio skipped (or vice versa). Try a wider search ahead.
        # If realign finds a confident jump, use it. If not, the chunk likely
        # has no source equivalent — keep cursor where it is.
        no_source_match = False
        if ratio < 0.45 and len(chunk_words) >= 5:
            r_start, r_end, r_ratio = realign(chunk_words, orig_match, cursor)
            if r_ratio >= 0.55 and r_ratio > ratio + 0.15:
                start, end, ratio = r_start, r_end, r_ratio
            elif r_ratio < 0.30:
                # Last-resort full-source re-anchor. Catches catastrophic
                # alignment loss caused by unusual EPUB ordering — e.g. when
                # the front matter (J Novel Club credits, copyright, etc.)
                # sits at the END of the source. auto_anchor sees credits
                # match credits and parks the cursor at ~99% of the file;
                # realign only searches forward, so it can never recover
                # the prologue prose that's actually at char 0. find_anchor_position
                # scans the WHOLE source and can jump backward.
                a_start, a_end, a_ratio = find_anchor_position(
                    chunk_words, orig_match, min_ratio=0.6
                )
                # Require both an absolute high bar and a clear improvement
                # over the local ratio, so we don't false-positive on
                # epigraphs / audio-only material that legitimately has no
                # source equivalent.
                if a_ratio >= 0.6 and a_ratio > ratio + 0.4:
                    t_start, t_end = trim_span_to_alignment(
                        chunk_words, orig_match, a_start, a_end
                    )
                    if t_end > t_start:
                        start, end = t_start, t_end
                        ratio = _ratio(chunk_words, orig_match[start:end])
                    else:
                        start, end, ratio = a_start, a_end, a_ratio
                    print(f"{DIM}  [entry {idx+1}] full-source re-anchor "
                          f"jumped cursor to source word {start} "
                          f"(ratio {ratio:.1%}){RESET}")
                else:
                    # Truly no good match anywhere — likely audio-only material
                    # (chapter epigraph, intro/outro). Don't advance cursor.
                    no_source_match = True

        if no_source_match:
            orig_span_display = "(no matching passage found in source within search range)"
            orig_span_words   = []
            new_cursor        = cursor   # don't advance
        else:
            orig_span_display = ' '.join(orig_display[start:end])
            orig_span_words   = orig_match[start:end]
            new_cursor        = end

        # ── Auto-approve high-similarity entries ──────────────────────────────
        if not review_all and ratio >= threshold:
            decisions[key] = {
                'action': 'keep',
                'text': chunk_text,
                'ratio': ratio,
                'cursor_after': new_cursor,
                'auto': True,
            }
            cursor = new_cursor
            auto_ct += 1
            if auto_ct % 200 == 0:
                save_checkpoint(jsonl_path, decisions, cursor)
                print(f"{DIM}  [{idx+1}/{total}] {auto_ct} auto-approved, checkpoint saved{RESET}")
            idx += 1
            continue

        # ── Show for review ───────────────────────────────────────────────────
        review_ct += 1
        a_col, b_col = color_diff(chunk_words, orig_span_words)

        # Build the merge preview: source words with LLM markers re-applied.
        # This is the option that preserves prosody (emphasis + pauses) while
        # fixing ASR errors — it's what keeps the trained TTS voice from
        # going flat.
        merge_preview = None
        if not no_source_match and ('*' in chunk_text or '..' in chunk_text):
            merge_preview = merge_annotations_with_source(
                chunk_text, orig_display[start:end]
            )
            # Don't show merge if it's identical to the plain original
            if merge_preview == orig_span_display:
                merge_preview = None

        print(SEP)
        print(
            f"{BOLD}Entry {idx+1}/{total}{RESET}  "
            f"{DIM}{entry.get('audio_filepath','?')}{RESET}  "
            f"{fmt_time(entry.get('start', 0))} → {fmt_time(entry.get('end', 0))}  "
            f"Match: {YELLOW}{ratio:.1%}{RESET}"
        )
        print(SEP)
        print(f"{CYAN}ANNOTATED :{RESET}  {chunk_text}")
        print(f"{CYAN}ORIGINAL  :{RESET}  {orig_span_display}")
        if merge_preview:
            print(f"{CYAN}MERGED    :{RESET}  {merge_preview}  "
                  f"{DIM}(original words + LLM prosody markers){RESET}")
        print()
        print(f"  {DIM}TRANS diff:{RESET}  {a_col}")
        print(f"  {DIM}ORIG  diff:{RESET}  {b_col}")
        print()

        menu = [f"{BOLD}[a]{RESET} accept original"]
        if merge_preview:
            menu.append(f"{BOLD}[m]{RESET} merge (keeps prosody)")
        menu.extend([
            f"{BOLD}[k]{RESET} keep annotation",
            f"{BOLD}[e]{RESET} edit manually",
            f"{BOLD}[s]{RESET} skip for now",
            f"{BOLD}[u]{RESET} undo last",
            f"{BOLD}[q]{RESET} quit & save",
        ])
        print("  " + "   ".join(menu))

        undone = False
        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = 'q'

            if choice == 'a':
                if no_source_match:
                    print(f"  {YELLOW}No source text to accept here. Choose [k], [e], or [s].{RESET}")
                    continue
                decisions[key] = {
                    'action': 'accept',
                    'text': orig_span_display,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {GREEN}✓ Accepted original {DIM}(prosody markers stripped){RESET}")
                break

            elif choice == 'm':
                if not merge_preview:
                    print(f"  {YELLOW}Merge not available for this entry "
                          f"(no source match or no markers to preserve).{RESET}")
                    continue
                decisions[key] = {
                    'action': 'merge',
                    'text': merge_preview,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {GREEN}✓ Merged: original words with preserved prosody markers{RESET}")
                break

            elif choice == 'k':
                decisions[key] = {
                    'action': 'keep',
                    'text': chunk_text,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {DIM}Kept annotation{RESET}")
                break

            elif choice == 'e':
                print(f"  Type replacement text (blank = keep annotation):")
                try:
                    replacement = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    replacement = ''
                if replacement:
                    decisions[key] = {
                        'action': 'edit',
                        'text': replacement,
                        'ratio': ratio,
                        'cursor_after': new_cursor,
                    }
                    print(f"  {GREEN}✓ Saved edit{RESET}")
                else:
                    decisions[key] = {
                        'action': 'keep',
                        'text': chunk_text,
                        'ratio': ratio,
                        'cursor_after': new_cursor,
                    }
                    print(f"  {DIM}Kept annotation (blank input){RESET}")
                break

            elif choice == 's':
                decisions[key] = {
                    'action': 'skip',
                    'text': chunk_text,
                    'ratio': ratio,
                    'cursor_after': new_cursor,
                }
                print(f"  {YELLOW}Skipped — will appear again on next run{RESET}")
                break

            elif choice == 'u':
                # Undo the most recent MANUAL decision. Auto-approves between
                # that decision and "now" are also popped, because their cursor
                # math depended on the (now-undone) decision's cursor_after —
                # they'll re-auto-approve on the next pass with the corrected
                # cursor. Pre-anchor auto-keeps are left alone since the
                # anchor logic runs before this loop.
                target = find_last_manual_idx(decisions)
                if target is None:
                    print(f"  {YELLOW}Nothing to undo — no prior manual decisions in this session.{RESET}")
                    continue

                to_remove = sorted(int(k) for k in decisions if int(k) >= target)
                for k in to_remove:
                    decisions.pop(str(k), None)

                # Rewind cursor to the entry just before `target`
                if target > 0 and str(target - 1) in decisions:
                    cursor = decisions[str(target - 1)].get('cursor_after', 0)
                else:
                    cursor = 0

                # Drop matching review-log records so the log reflects current
                # state rather than the undone attempt.
                n_log = remove_log_entries(log_path, set(to_remove))

                save_checkpoint(jsonl_path, decisions, cursor)
                tail = f" + {len(to_remove)-1} subsequent auto-approve(s)" if len(to_remove) > 1 else ""
                log_note = f", {n_log} log record(s) removed" if n_log else ""
                print(f"  {YELLOW}↶ Undone entry {target+1}{tail}{log_note}. Rewinding…{RESET}")
                idx = target
                undone = True
                break

            elif choice == 'q':
                cursor = new_cursor
                save_checkpoint(jsonl_path, decisions, cursor)
                write_output(entries, decisions, output_path)
                n_done = sum(1 for d in decisions.values() if d['action'] != 'skip')
                print(f"\n{YELLOW}Paused.{RESET}  {len(decisions)}/{total} entries seen, {n_done} decided.")
                print(f"Rerun the same command to resume from here.")
                sys.exit(0)

            else:
                valid = "a, m, k, e, s, u, or q" if merge_preview else "a, k, e, s, u, or q"
                print(f"  Enter {valid}")

        # [u]ndo doesn't produce a decision and already rewound idx/cursor —
        # skip the log+advance tail.
        if undone:
            continue

        # Record this manual decision so the session can be reviewed afterward
        # for script-improvement patterns. ('q' sys.exit()s above, so we only
        # log entries that actually produced a decision.)
        decided = decisions[key]
        log_decision(log_path, {
            'entry_idx':       idx,
            'audio':           entry.get('audio_filepath'),
            'start':           entry.get('start'),
            'end':             entry.get('end'),
            'ratio':           round(ratio, 4),
            'action':          decided['action'],
            'no_source_match': no_source_match,
            'annotated':       chunk_text,
            'original':        None if no_source_match else orig_span_display,
            'merge_preview':   merge_preview,
            'final_text':      decided['text'],
        })

        cursor = new_cursor
        save_checkpoint(jsonl_path, decisions, cursor)
        idx += 1

    # ── All entries processed ─────────────────────────────────────────────────
    write_output(entries, decisions, output_path)

    kept     = sum(1 for d in decisions.values() if d['action'] == 'keep' and not d.get('auto'))
    auto     = sum(1 for d in decisions.values() if d.get('auto'))
    accepted = sum(1 for d in decisions.values() if d['action'] == 'accept')
    merged   = sum(1 for d in decisions.values() if d['action'] == 'merge')
    edited   = sum(1 for d in decisions.values() if d['action'] == 'edit')
    skipped  = sum(1 for d in decisions.values() if d['action'] == 'skip')

    print(f"\n{BOLD}Complete!{RESET}")
    print(f"  Auto-approved (≥{threshold:.0%}) : {auto}")
    print(f"  Kept annotation             : {kept}")
    print(f"  Accepted original (stripped): {accepted}")
    print(f"  Merged (words + prosody)    : {merged}")
    print(f"  Edited manually             : {edited}")
    print(f"  Skipped                     : {skipped}")
    if accepted > merged * 3 and accepted > 50:
        print(f"\n  {YELLOW}⚠ You accepted {accepted} originals plain (no prosody markers).{RESET}")
        print(f"  {YELLOW}  Consider using [m]erge instead — it keeps the LLM's pause "
              f"and emphasis markers,{RESET}")
        print(f"  {YELLOW}  which is what stops the trained TTS voice from sounding "
              f"flat/monotone.{RESET}")
    if skipped:
        print(f"  {YELLOW}Re-run to review {skipped} skipped entries.{RESET}")

    # Clean up checkpoint on full completion (no skips remaining)
    cp = checkpoint_path(jsonl_path)
    if skipped == 0 and cp.exists():
        cp.unlink()
        print(f"  Checkpoint removed (all entries decided).")

    if log_path.exists():
        n_logged = sum(1 for _ in open(log_path, encoding='utf-8'))
        print(f"  Review log : {log_path} ({n_logged} manual decisions)")

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Compare metadata.jsonl transcriptions against an original EPUB or text file"
    )
    parser.add_argument("--jsonl",   required=True,
                        help="Path to metadata.jsonl (or extracted from the dataset zip)")
    parser.add_argument("--source",  required=True,
                        help="Path to the original .epub or .txt file")
    parser.add_argument("--output",
                        help="Output path for corrected JSONL "
                             "(default: <jsonl_name>_corrected.jsonl)")
    parser.add_argument("--threshold", type=float, default=0.90,
                        help="Similarity ratio above which entries are auto-approved "
                             "without showing them to you (default: 0.90)")
    parser.add_argument("--review-all", action="store_true",
                        help="Show every entry for review, ignoring --threshold")
    parser.add_argument("--reset", action="store_true",
                        help="Discard saved checkpoint and start from the beginning")

    # ── Targeted reset (undo specific decisions without losing the rest) ──
    parser.add_argument("--reset-entry", metavar="N[,N,...]",
                        help="Restore specific entries by index (comma-separated). "
                             "Replaces their lines in the corrected JSONL with the "
                             "originals from --jsonl and drops their checkpoint "
                             "decisions. Exits after reset.")
    parser.add_argument("--reset-from", type=int, metavar="N",
                        help="Reset every entry from index N to the end. Exits after reset.")
    parser.add_argument("--reset-range", metavar="N:M",
                        help="Reset entries in the inclusive range N..M. Exits after reset.")
    parser.add_argument("--also-clear-log", action="store_true",
                        help="When combined with --reset-entry/--reset-from/--reset-range, "
                             "also remove matching records from the review log "
                             "(default: leave the log intact).")

    # ── Alignment offset controls ─────────────────────────────────────────────
    # Audiobooks and source texts rarely start at the same point — the audio
    # may open with credits/narrator intro, the text may open with copyright
    # and TOC. These flags control where the alignment cursor starts.
    parser.add_argument("--source-start", type=int, metavar="N",
                        help="Manually start at source word N (skip auto-anchor)")
    parser.add_argument("--source-start-text", metavar="TEXT",
                        help="Fuzzy-search for TEXT in the source and start there "
                             "(e.g. --source-start-text \"It was a warm Saturday\")")
    parser.add_argument("--no-auto-anchor", action="store_true",
                        help="Disable automatic anchor detection (start at source word 0)")
    parser.add_argument("--review-preanchor", action="store_true",
                        help="Review JSONL entries before the anchor individually "
                             "(default: auto-keep them as-is since they're usually intro)")
    args = parser.parse_args()

    jsonl_path  = args.jsonl
    output_path = args.output or str(
        Path(jsonl_path).with_name(Path(jsonl_path).stem + '_corrected.jsonl')
    )
    log_path = review_log_path(output_path)

    print(f"Loading JSONL   : {jsonl_path}")
    entries = load_jsonl(jsonl_path)
    print(f"  {len(entries)} entries")

    # Targeted reset: undo specific entries and exit before any expensive
    # source loading / alignment work runs.
    reset_indices = parse_reset_spec(
        args.reset_entry, args.reset_from, args.reset_range, len(entries)
    )
    if reset_indices:
        apply_targeted_reset(
            jsonl_path, output_path, log_path, reset_indices, args.also_clear_log
        )
        sys.exit(0)

    print(f"Loading source  : {args.source}")
    source_text = load_source(args.source)
    # Strip EPUB OCR digit-in-word glitches ('thos1e' → 'those', 'Kars1a' →
    # 'Karsa') before tokenisation. The ASR-derived chunks never have these
    # digits, so leaving them in source produces visibly-wrong merged output.
    source_text = _OCR_DIGIT_GLITCH.sub('', source_text)
    # Rejoin precomposed Latin diacritics that got split from their stem
    # during EPUB extraction ('fianc é' → 'fiancé', 'fiancé e' → 'fiancée').
    source_text = _DIACRITIC_REJOIN.sub(r'\1\2', source_text)
    source_text = _DIACRITIC_REJOIN_TAIL.sub(r'\1\2', source_text)
    print(f"  {len(source_text):,} characters")

    # Build the per-book proper-noun lexicon. Used by _step_threshold to relax
    # the boundary acceptance bar when the source-side token is a known name —
    # critical for Japanese romanization ASR mistranscriptions like
    # 'coodo'↔'kudou' or 'youth'↔'yurie' that sit far below the default 0.55.
    global _PROPER_NOUNS
    _PROPER_NOUNS = _build_proper_nouns(source_text)
    if _PROPER_NOUNS:
        sample = ', '.join(sorted(_PROPER_NOUNS)[:8])
        more = f' +{len(_PROPER_NOUNS) - 8} more' if len(_PROPER_NOUNS) > 8 else ''
        print(f"  {len(_PROPER_NOUNS)} recurring proper nouns ({sample}{more})")

    # Build parallel word lists: display (original form) and match (normalised).
    #
    # Hyphens and dashes are split BEFORE whitespace tokenisation so that a
    # source compound like "twenty-minute" becomes two entries ["twenty",
    # "minute"] instead of one. Without this split, orig_match[i] would be
    # the string "twenty minute" (one element with an embedded space), and
    # the audio chunk's separately-spoken "twenty" / "minute" tokens fail to
    # align with it — causing those words to disappear from ORIGINAL when
    # trim_span_to_alignment runs. Loss of the hyphen in display is fine
    # for TTS training (the audio speaks the parts as separate words with
    # a slight pause anyway).
    # U+2500 (BOX DRAWINGS LIGHT HORIZONTAL) shows up in EPUB→text conversions
    # in place of a real em-dash (U+2014). Treat it as a dash for split purposes.
    _COMPOUND_SPLIT = re.compile(r'[-‐‑‒–—―─━]')
    source_tokens = _COMPOUND_SPLIT.sub(' ', source_text).split()
    orig_display, orig_match = [], []
    for w in source_tokens:
        m = normalize(w)
        if not m:
            continue   # pure punctuation token
        orig_display.append(w)
        orig_match.append(m)
    print(f"  {len(orig_display):,} words")

    # Checkpoint
    if args.reset:
        cp = checkpoint_path(jsonl_path)
        if cp.exists():
            cp.unlink()
            print("Checkpoint cleared — starting fresh.")
        if log_path.exists():
            log_path.unlink()
            print("Review log cleared — starting fresh.")
        decisions, cursor = {}, 0
    else:
        saved     = load_checkpoint(jsonl_path)
        decisions = saved.get("decisions", {})
        cursor    = saved.get("cursor", 0)
        if decisions:
            print(f"Resuming checkpoint: {len(decisions)} entries already decided, "
                  f"cursor at source word {cursor}")

    # ── Initial alignment: figure out where in the source to start ────────────
    # Skip this whole block if we're resuming a session.
    if not decisions:
        if args.source_start is not None:
            cursor = max(0, min(args.source_start, len(orig_match)))
            preview = ' '.join(orig_display[cursor:cursor+12])
            print(f"\nStarting at source word {cursor} (--source-start)")
            print(f"  Source: \"{preview}...\"")

        elif args.source_start_text:
            print(f"\nSearching source for: \"{args.source_start_text}\" ...")
            pos = find_text_in_source(args.source_start_text, orig_match)
            if pos < 0:
                sys.exit(f"{RED}Could not confidently locate that text in the source.{RESET}\n"
                         f"Try a longer / more distinctive phrase, or use --source-start N.")
            cursor = pos
            preview = ' '.join(orig_display[cursor:cursor+12])
            print(f"  ✓ Found at source word {cursor}")
            print(f"  Source: \"{preview}...\"")

        elif args.no_auto_anchor:
            cursor = 0
            print(f"\nAuto-anchor disabled — starting at source word 0")

        else:
            # Default: auto-detect where the audio first connects to the source
            print(f"\n🔍 Searching for initial alignment anchor "
                  f"(audio intro and text front-matter often don't line up)...")
            anchor_idx, anchor_pos, anchor_ratio = auto_anchor(entries, orig_match)

            if anchor_ratio > 0:
                preview = ' '.join(orig_display[anchor_pos:anchor_pos+12])
                print(f"  ✓ JSONL entry {anchor_idx} anchors at source word {anchor_pos} "
                      f"({YELLOW}{anchor_ratio:.1%}{RESET} match)")
                print(f"  Source preview: \"{preview}...\"")
                cursor = anchor_pos

                # Handle entries before the anchor: audio-only intro material
                # (credits, narrator intro, "this story is fiction" disclaimer, etc.)
                if anchor_idx > 0:
                    print()
                    print(f"  {YELLOW}⚠ {anchor_idx} JSONL entr{'y' if anchor_idx == 1 else 'ies'} "
                          f"before the anchor have no matching source text{RESET}")
                    print(f"     (likely audio intro/credits not present in the text)")
                    if args.review_preanchor:
                        print(f"     {DIM}--review-preanchor set: will show each individually{RESET}")
                    else:
                        for i in range(anchor_idx):
                            decisions[str(i)] = {
                                'action': 'keep',
                                'text':   entries[i].get('text', ''),
                                'ratio':  0.0,
                                'cursor_after': cursor,
                                'pre_anchor':   True,
                            }
                        save_checkpoint(jsonl_path, decisions, cursor)
                        print(f"     ✓ Auto-kept as-is "
                              f"({DIM}use --review-preanchor to review them individually{RESET})")
            else:
                print(f"  {YELLOW}⚠ No confident anchor found in the first "
                      f"{min(20, len(entries))} entries.{RESET}")
                print(f"  Starting at source word 0. If alignment is poor, retry with:")
                print(f"    --source-start N            (manual word offset)")
                print(f"    --source-start-text \"...\"   (search the source for a phrase)")
                cursor = 0

    # ── Divergence warning (fresh sessions only) ──────────────────────────────
    # If a meaningful chunk of the audio doesn't align with the source, it's
    # usually because the audiobook was narrated from a different translation
    # or edition than the EPUB. Flag it now so the user can swap sources
    # instead of grinding through 100 manual edits to find out.
    if not decisions:
        print(f"\n🔍 Estimating source/audio alignment quality...")
        avg, n_sampled, low_ct, review_ct = estimate_alignment_quality(
            entries, orig_match, cursor
        )
        if n_sampled >= 10:
            pct_low = low_ct / n_sampled
            pct_review = review_ct / n_sampled
            # Catastrophic: bad average OR many outright failures OR a meaningfully
            # large fraction of entries would need manual review (= consistent
            # divergence even when individual alignments mostly succeed).
            # Empirical: clean books sit around 10-15% review rate; the Archer
            # book (different-translation case) sits at ~30%.
            if avg < 0.70 or pct_low > 0.20 or pct_review >= 0.30:
                print()
                print(f"  {BOLD}{YELLOW}⚠ Possible source/audio divergence{RESET}")
                print(f"  {YELLOW}Sampled {n_sampled} entries — average alignment ratio "
                      f"{avg:.0%}, {review_ct} ({pct_review:.0%}) would need manual "
                      f"review{RESET}")
                if low_ct:
                    print(f"  {YELLOW}{low_ct} ({pct_low:.0%}) aligned at < 60% "
                          f"(matching is brittle){RESET}")
                print(f"  {YELLOW}Usually means the audiobook was narrated from a "
                      f"different translation/edition{RESET}")
                print(f"  {YELLOW}than the source you provided. Many entries will need "
                      f"manual edits.{RESET}")
                print(f"  {DIM}Continue, or Ctrl-C and try a different --source.{RESET}")
                print()
            else:
                print(f"  {DIM}sampled {n_sampled} entries — avg ratio {avg:.0%}, "
                      f"{review_ct} would need review. Looks good.{RESET}")

    run(
        entries      = entries,
        orig_display = orig_display,
        orig_match   = orig_match,
        decisions    = decisions,
        cursor       = cursor,
        threshold    = args.threshold,
        review_all   = args.review_all,
        jsonl_path   = jsonl_path,
        output_path  = output_path,
        log_path     = log_path,
    )

if __name__ == "__main__":
    main()
