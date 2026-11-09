"""Qwen3-ForcedAligner pure core (#1844) — bounded, fail-closed alignment helpers.

Pure standard library only; no torch/qwen_asr import here. Sentence
splitting and token mapping are adapted from ReadAloudMaker
(https://github.com/REFLEXGAMING007/ReadAloudMaker, MIT License, pinned
commit 8ba70a83383e8a5bddd8b859016ffbbcefccba18, Copyright (c) 2026
ReadAloudMaker contributors): the sentence boundary logic,
punctuation-free token mapping and window planner were re-derived over
AA2's own normalization. Two deliberate divergences from the donor:

* Times are never clamped, stretched or repaired. A non-monotonic,
  saturating, zero-length, out-of-bounds or text-mismatched aligner
  result raises instead of being made plausible (AA2's 1 ms SMIL
  quantum rule has the same shape in epub_readaloud).
* Casefold expansions are tracked per normalized character instead of
  being truncated to the first character.

Nothing here certifies accuracy: token times are model estimates. The
callers validate exact normalized text correspondence, ordering and
bounds before any time is used as a clip.
"""
import math
import unicodedata

MAX_ALIGN_SECONDS = 180.0
DEFAULT_WINDOW_TARGET_SECONDS = 150.0
WINDOW_TAIL_SECONDS = 30.0
WINDOW_OVERLAP_SECONDS = 0.25
MAX_TOKENS_PER_SLICE = 20000
MAX_TEXT_CHARS = 20000

# The forced aligner covers 11 languages (HF model card); the ASR models
# support more, but only these are valid for forced alignment here.
SUPPORTED_ALIGNER_LANGUAGES = (
    "Chinese", "English", "Cantonese", "French", "German", "Italian",
    "Japanese", "Korean", "Portuguese", "Russian", "Spanish",
)

_LANGUAGE_NAMES = {
    "en": "English", "eng": "English",
    "de": "German", "deu": "German", "ger": "German",
    "fr": "French", "fra": "French", "fre": "French",
    "es": "Spanish", "spa": "Spanish",
    "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese",
    "ru": "Russian", "rus": "Russian",
    "zh": "Chinese", "zho": "Chinese", "chi": "Chinese",
    "ja": "Japanese", "jpn": "Japanese",
    "ko": "Korean", "kor": "Korean",
    "yue": "Cantonese", "cantonese": "Cantonese",
}

TERMINATORS = ".!?…。！？"
CLOSERS = "\"'”’)]}）」』»"

_ABBREVIATIONS = frozenset(
    ("mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
     "fig", "no", "vol", "ch", "chap", "ed", "al", "inc", "ltd", "co",
     "corp", "dept", "univ", "approx", "est", "min", "max", "sec", "hr",
     "mt", "ft", "ave", "blvd", "rd", "gen", "col", "capt", "lt", "sgt")
)

# Float rounding guard only: the aligner rounds times to milliseconds, so
# a token ending exactly at the slice end is valid to within 5 ms.
_TIME_TOLERANCE_SECONDS = 0.005


def get_aligner_language_name(value):
    """Map an EPUB language tag or name to an aligner language name."""
    if isinstance(value, str) and value.strip():
        cleaned = value.strip().replace("_", "-").split("-")[0].lower()
        for name in SUPPORTED_ALIGNER_LANGUAGES:
            if cleaned == name.lower():
                return name
        if cleaned in _LANGUAGE_NAMES:
            return _LANGUAGE_NAMES[cleaned]
    raise ValueError(
        "Qwen3-ForcedAligner does not support language %r. Supported: %s"
        % (value, ", ".join(SUPPORTED_ALIGNER_LANGUAGES)))


def is_aligner_language_supported(value):
    try:
        get_aligner_language_name(value)
        return True
    except ValueError:
        return False


def split_sentences(text):
    """Return sentence ranges as (start, end) over ``text``.

    Ranges do not overlap but may not cover whitespace between sentences;
    use :func:`cover_ranges` when every character must belong to one.
    """
    length = len(text)
    if not text.strip():
        return []

    bounds = []
    index = 0
    while index < length:
        char = text[index]
        if char not in TERMINATORS:
            index += 1
            continue
        if (char == "." and 0 < index < length - 1
                and text[index - 1].isdigit()
                and text[index + 1].isdigit()):
            index += 1
            continue
        end = index + 1
        while end < length and text[end] in TERMINATORS:
            end += 1
        while end < length and text[end] in CLOSERS:
            end += 1
        if char == "." and _is_abbreviation(text, index):
            index = end
            continue
        if end >= length or text[end].isspace() or char in "。！？":
            bounds.append(end)
        index = end

    ranges = []
    start = 0
    for boundary in bounds:
        if boundary > start:
            ranges.append((start, boundary))
            start = boundary
    if start < length:
        ranges.append((start, length))
    return [item for item in ranges if text[item[0]:item[1]].strip()]


def cover_ranges(text, ranges):
    """Extend sentence ranges so they cover every character exactly once."""
    ranges = [item for item in ranges if text[item[0]:item[1]].strip()]
    if not ranges:
        return []

    covered = []
    for index, (start, end) in enumerate(ranges):
        if index == 0:
            start = 0
        covered.append([start, end])
    for index in range(len(covered) - 1):
        covered[index][1] = covered[index + 1][0]
    covered[-1][1] = len(text)
    return [(start, end) for start, end in covered if end > start]


def _is_abbreviation(text, dot_index):
    start = dot_index - 1
    while start >= 0 and (text[start].isalnum() or text[start] in ".-"):
        start -= 1
    word = text[start + 1:dot_index].strip(".").lower()
    if not word:
        return False
    if word in _ABBREVIATIONS:
        return True
    if len(word) == 1 and word.isalpha() and word.isupper():
        return True
    return False


def count_units(text):
    """Count runs of word characters, roughly matching an aligner's tokens."""
    total = 0
    inside = False
    for char in text:
        kept = char.isalnum() or char == "'"
        if kept and not inside:
            total += 1
        inside = kept
    return total


def _is_kept_char(char):
    if char == "'":
        return True
    category = unicodedata.category(char)
    return category.startswith("L") or category.startswith("N")


def get_normalized_stream(text):
    """Build a letters/digits/apostrophe stream plus original indices.

    Mirrors the upstream forced-aligner processor's keep rule (letters,
    numbers, apostrophe) over NFKC + casefolded text, matching
    AA2's get_normalized_words vocabulary via epub_readaloud.
    """
    characters = []
    indices = []
    folded = unicodedata.normalize("NFKC", text or "")
    folded = folded.replace("’", "'").replace("‘", "'")
    folded = folded.casefold()
    # Indices refer to the pre-casefold normalized string; casefold only
    # expands characters (e.g. ß->ss), so track expansions properly by
    # normalizing/casefolding per original character instead.
    for index, char in enumerate(folded):
        if not _is_kept_char(char):
            continue
        for lowered in char.lower():
            if _is_kept_char(lowered):
                characters.append(lowered)
                indices.append(index)
    return "".join(characters), indices


def get_token_key(token_text):
    """Normalize an aligner token the same way as expected text."""
    if not isinstance(token_text, str):
        return ""
    folded = unicodedata.normalize("NFKC", token_text)
    folded = folded.replace("’", "'").replace("‘", "'")
    folded = folded.casefold()
    return "".join(char for char in folded if _is_kept_char(char))


def map_tokens_to_text(text, tokens):
    """Locate aligner tokens inside ``text`` in normalized space.

    Returns (spans, unmatched): spans are (token, start_char, end_char)
    where the offsets refer to the original ``text``. Matching is
    monotonic forwards with a whole-text fallback for repeated tokens;
    callers must still require full coverage (see
    :func:`require_exact_coverage`), because this locator alone would
    let a repeated word match an earlier occurrence.
    """
    normalized, original_indices = get_normalized_stream(text)
    spans = []
    unmatched = []
    cursor = 0

    for token in tokens:
        key = get_token_key(token.get("text") if isinstance(token, dict) else "")
        if not key:
            continue
        position = normalized.find(key, cursor)
        if position < 0:
            position = normalized.find(key)
        if position < 0:
            unmatched.append(token)
            continue
        start = original_indices[position]
        end = original_indices[position + len(key) - 1] + 1
        spans.append((token, start, end))
        cursor = position + len(key)

    return spans, unmatched


def require_exact_coverage(text, tokens):
    """Require aligner tokens to correspond exactly to ``text``.

    Exact means: no unmatched tokens, and the matched characters cover
    the whole normalized text stream. Partial coverage or fabricated
    extra tokens fail closed. Returns the matched spans.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Alignment text is empty.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("Alignment text exceeds its size limit.")
    if not isinstance(tokens, list) or not tokens:
        raise ValueError("Alignment returned no tokens.")
    if len(tokens) > MAX_TOKENS_PER_SLICE:
        raise ValueError("Alignment returned too many tokens.")

    spans, unmatched = map_tokens_to_text(text, tokens)
    if unmatched:
        raise ValueError(
            "Alignment tokens do not correspond to the spoken text: "
            "%d unmatched token(s), first: %r"
            % (len(unmatched), unmatched[0].get("text") if isinstance(unmatched[0], dict) else unmatched[0]))
    normalized, _indices = get_normalized_stream(text)
    matched = sum(len(get_token_key(token.get("text"))) for token, _s, _e in spans)
    if matched != len(normalized):
        raise ValueError(
            "Alignment covers %d of %d normalized characters; "
            "refusing to treat a partial result as aligned."
            % (matched, len(normalized)))
    # Span start offsets must be strictly forward (no echoed duplicate
    # matching an earlier occurrence silently).
    previous = 0
    for _token, start, _end in spans:
        if start < previous:
            raise ValueError("Alignment token order does not follow the text.")
        previous = _end
    return spans


def get_validated_aligner_tokens(tokens, duration_sec):
    """Validate raw aligner tokens against slice bounds. Never repaired."""
    if (not isinstance(duration_sec, (int, float))
            or isinstance(duration_sec, bool)
            or not math.isfinite(duration_sec)
            or duration_sec <= 0):
        raise ValueError("Alignment slice duration is invalid.")
    require_slice_within_limit(duration_sec)
    if not isinstance(tokens, list) or not tokens:
        raise ValueError("Alignment returned no tokens.")
    if len(tokens) > MAX_TOKENS_PER_SLICE:
        raise ValueError("Alignment returned too many tokens.")

    validated = []
    previous_end = 0.0
    previous_start = 0.0
    seen = set()
    for position, token in enumerate(tokens):
        if not isinstance(token, dict):
            raise ValueError("Alignment token %d is malformed." % position)
        label = token.get("text")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Alignment token %d has no text." % position)
        start, end = token.get("start_time"), token.get("end_time")
        if (any(type(v) not in (int, float) or isinstance(v, bool)
                for v in (start, end))
                or not math.isfinite(start) or not math.isfinite(end)):
            raise ValueError("Alignment token %d has invalid times." % position)
        start, end = float(start), float(end)
        if not 0.0 <= start < end <= duration_sec + _TIME_TOLERANCE_SECONDS:
            raise ValueError(
                "Alignment token %d is reversed or out of bounds." % position)
        if position and start < previous_start:
            raise ValueError("Alignment tokens are not monotonic; refusing to repair them.")
        if position and start < previous_end:
            raise ValueError("Alignment tokens overlap; refusing to repair them.")
        key = (start, end)
        if key in seen:
            raise ValueError("Alignment timestamps saturate; refusing to treat them as measured.")
        seen.add(key)
        previous_start, previous_end = start, end
        validated.append({"text": label, "start_time": start, "end_time": end})
    return validated


def require_slice_within_limit(duration_sec):
    if (not isinstance(duration_sec, (int, float))
            or isinstance(duration_sec, bool)
            or not math.isfinite(duration_sec)
            or duration_sec > MAX_ALIGN_SECONDS):
        raise ValueError(
            "Alignment slice of %r seconds exceeds the %d-second aligner limit."
            % (duration_sec, MAX_ALIGN_SECONDS))


def derive_fragment_times(fragments, spans):
    """Attach (start, end) times to fragments from matched token spans.

    Each fragment needs at least one token; fragments with no token
    (e.g. punctuation-only) raise instead of borrowing a neighbour's
    time. Returned clips are monotonic because tokens are validated
    monotonic; a violation anywhere raises.
    """
    if not fragments:
        raise ValueError("No fragments to time.")
    times = []
    previous_end = 0.0
    for start, end in fragments:
        inside = [(t, s, e) for t, s, e in spans if e > start and s < end]
        if not inside:
            raise ValueError(
                "Fragment (%d, %d) has no aligned tokens; refusing to invent timing."
                % (start, end))
        mark = min(t["start_time"] for t, _s, _e in inside)
        stop = max(t["end_time"] for t, _s, _e in inside)
        if stop <= mark:
            raise ValueError("Aligned fragment has no positive duration.")
        if mark < previous_end - _TIME_TOLERANCE_SECONDS:
            raise ValueError("Aligned fragment times go backwards; refusing to repair them.")
        times.append((mark, stop))
        previous_end = stop
    return times


def get_aligner_windows(row_duration_sec, sentences):
    """Plan alignment windows for one editor row.

    ``sentences`` are {start, end, words} char ranges covering the row
    text exactly once (see cover_ranges). Word-rate estimation only
    *schedules* windows; produced clips must come from actual aligner
    tokens verified by require_exact_coverage. Every window's audio
    stays within MAX_ALIGN_SECONDS; the final window of a short row is
    the whole row. A single sentence needing more than the limit is
    refused explicitly instead of being silently truncated.
    """
    if (not isinstance(row_duration_sec, (int, float))
            or isinstance(row_duration_sec, bool)
            or not math.isfinite(row_duration_sec)
            or row_duration_sec <= 0):
        raise ValueError("Row duration is invalid.")
    if not isinstance(sentences, list) or not sentences:
        raise ValueError("No sentences to align.")
    for sentence in sentences:
        if (not isinstance(sentence, dict)
                or type(sentence.get("start")) is not int
                or type(sentence.get("end")) is not int
                or type(sentence.get("words")) is not int
                or sentence["start"] < 0
                or sentence["end"] <= sentence["start"]
                or sentence["words"] < 0):
            raise ValueError("Alignment sentence is malformed.")

    total_words = sum(s["words"] for s in sentences)
    if total_words <= 0:
        raise ValueError("Row text has no alignable words.")
    rate = float(row_duration_sec) / float(total_words)

    windows = []
    index = 0
    while index < len(sentences):
        if sum(s["words"] for s in sentences[index:]) * rate <= DEFAULT_WINDOW_TARGET_SECONDS \
                and windows == []:
            stop = len(sentences)
        else:
            stop = index
            words = 0
            while stop < len(sentences):
                if words and (words + sentences[stop]["words"]) * rate > DEFAULT_WINDOW_TARGET_SECONDS:
                    break
                words += sentences[stop]["words"]
                stop += 1
            if stop == index:
                stop = index + 1
        if not windows:
            start = 0.0
            est_start = 0.0
        else:
            est_start = sum(s["words"] for s in sentences[:index]) * rate
            start = est_start
        est_end = sum(s["words"] for s in sentences[:stop]) * rate
        if est_end - est_start > DEFAULT_WINDOW_TARGET_SECONDS:
            raise ValueError(
                "One sentence needs %.1fs of narration, more than the "
                "%d-second aligner limit; split the editor row first."
                % (est_end - est_start, MAX_ALIGN_SECONDS))
        window_end = min(float(row_duration_sec), est_end + WINDOW_TAIL_SECONDS)
        window_end = min(window_end, start + MAX_ALIGN_SECONDS)
        windows.append({
            "start_sec": start,
            "end_sec": window_end,
            "char_start": sentences[index]["start"],
            "char_end": sentences[stop - 1]["end"],
            "first_sentence": index,
            "last_sentence": stop,
        })
        if len(windows) > 10000:
            raise ValueError("Alignment window planning did not converge.")
        index = stop
    return windows
