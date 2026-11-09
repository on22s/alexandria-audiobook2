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

Nothing here proves acoustic accuracy or that a worker's reported
intervals were produced by the claimed model: token times are model
estimates, and parent validation is structural consistency (bounds,
text scopes, frame offsets, cursor continuity) plus preserved
provenance. Actual-model inference on known PCM is separate bounded
evidence; no all-language or device accuracy is claimed. The callers
validate exact normalized text correspondence, ordering and bounds
before any time is used as a clip.
"""
import math
import re
import unicodedata

MAX_ALIGN_SECONDS = 180.0
# Proven sequential scheduling (parent diagnostics v4/v6): an estimated
# 20-second text target with a 3-second audio tail, grown in 3-second
# steps by the worker against the measured previous-end cursor. These
# values only *schedule* inference; clips come from actual model tokens.
DEFAULT_WINDOW_TARGET_SECONDS = 20.0
WINDOW_TAIL_SECONDS = 3.0
WINDOW_GROWTH_SECONDS = 3.0
MAX_SEQUENTIAL_CALLS_PER_WINDOW = 64
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
    raw = text[start + 1:dot_index].strip(".")
    word = raw.lower()
    if not word:
        return False
    if word in _ABBREVIATIONS:
        return True
    if len(raw) == 1 and raw.isalpha() and raw.isupper():
        return True
    return False


_AA2_WORDS_RE = re.compile(r"\w+(?:['’]\w+)*", re.UNICODE)


def count_units(text):
    """Count AA2 word tokens, roughly matching an aligner's tokens."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    folded = folded.replace("’", "'").replace("‘", "'")
    return len(_AA2_WORDS_RE.findall(folded))


def _is_hangul_l(code):
    return (0x1100 <= code <= 0x115F) or (0xA960 <= code <= 0xA97F)


def _is_hangul_v(code):
    return (0x1161 <= code <= 0x11A7) or (0xD7B0 <= code <= 0xD7C6)


def _is_hangul_t(code):
    return (0x11A8 <= code <= 0x11FF) or (0xD7FB <= code <= 0xD7FF)


def _normalized_clusters(text):
    """Original (start, end) clusters that NFKC must compose together.

    Boundaries are decided on NFKD decomposition scalars (UAX #15):
    a compatibility decomposition can introduce a non-starter or a
    Hangul Jamo that canonically composes across original scalars
    (e.g. halfwidth voiced marks, compatibility Hangul Jamo), even
    when the raw original scalar looked like a starter. Merging is
    conservative — only non-starter-led and Hangul L/V/LV-T boundaries
    fuse — so ordinary text stays in minimal clusters while the
    concatenation of cluster NFKCs equals whole-text NFKC.
    """
    text = text or ""
    length = len(text)
    if not length:
        return []
    decompositions = [unicodedata.normalize("NFKD", char) for char in text]
    clusters = []
    index = 0
    while index < length:
        stop = index + 1
        while stop < length:
            first = decompositions[stop][0] if decompositions[stop] else text[stop]
            code = ord(first)
            if unicodedata.combining(first) or unicodedata.category(first).startswith("M"):
                stop += 1
                continue
            prev_decomp = decompositions[stop - 1]
            prev_last = prev_decomp[-1] if prev_decomp else text[stop - 1]
            prev_code = ord(prev_last)
            if _is_hangul_v(code) and _is_hangul_l(prev_code):
                stop += 1
                continue
            if _is_hangul_t(code) and (
                    _is_hangul_v(prev_code)
                    or (0xAC00 <= prev_code <= 0xD7A3 and (prev_code - 0xAC00) % 28 == 0)):
                stop += 1
                continue
            break
        clusters.append((index, stop))
        index = stop
    return clusters


def _get_normalized_stream_detailed(text):
    """Canonical AA2 stream plus original (start, end) per char.

    One canonical rule shared by stream and token key: keep the
    full folded text (including ignored punctuation/marks) with
    per-char original provenance, then retain exactly the spans of
    the AA2 word regex in folded coordinates and project them back
    to original indices. Punctuation, whitespace and combining
    marks therefore break word adjacency in folded coordinates
    (they are real string positions), while NFKC compositions,
    casefold expansions, Hangul and Japanese provenance survive.
    """
    folded_chars = []
    starts = []
    ends = []
    for start, stop in _normalized_clusters(text or ""):
        folded = unicodedata.normalize("NFKC", text[start:stop])
        folded = folded.casefold().replace("’", "'").replace("‘", "'")
        for char in folded:
            folded_chars.append(char)
            starts.append(start)
            ends.append(stop)
    folded = "".join(folded_chars)
    out_chars, out_starts, out_ends = [], [], []
    for match in _AA2_WORDS_RE.finditer(folded):
        for pos in range(match.start(), match.end()):
            out_chars.append(folded[pos])
            out_starts.append(starts[pos])
            out_ends.append(ends[pos])
    return "".join(out_chars), out_starts, out_ends


def get_normalized_stream(text):
    """Build a letters/digits/apostrophe stream plus original indices.

    Mirrors the upstream forced-aligner processor's keep rule (letters,
    numbers, apostrophe) over NFKC + casefolded text, matching
    AA2's get_normalized_words vocabulary via epub_readaloud. Indices
    refer to the original text (start of the composing cluster), so
    casefold expansions (ß->ss) and contractions (combining marks,
    Hangul jamo) map back without going out of bounds.
    """
    normalized, starts, _ends = _get_normalized_stream_detailed(text or "")
    return normalized, starts


def get_token_key(token_text):
    """Normalize an aligner token the same way as expected text."""
    if not isinstance(token_text, str):
        return ""
    folded = unicodedata.normalize("NFKC", token_text)
    folded = folded.casefold().replace("’", "'").replace("‘", "'")
    return "".join(_AA2_WORDS_RE.findall(folded))


def _map_tokens_internal(text, tokens):
    normalized, starts, ends = _get_normalized_stream_detailed(text)
    spans = []
    unmatched = []
    cursor = 0
    for token in tokens:
        key = get_token_key(token.get("text") if isinstance(token, dict) else "")
        if not key:
            unmatched.append(token)
            continue
        position = normalized.find(key, cursor)
        if position < 0:
            unmatched.append(token)
            continue
        start = starts[position]
        end = ends[position + len(key) - 1]
        spans.append((token, start, end))
        cursor = position + len(key)
    return spans, unmatched, normalized


def map_tokens_to_text(text, tokens):
    """Locate aligner tokens inside ``text`` in normalized space.

    Returns (spans, unmatched): spans are (token, start_char, end_char)
    where the offsets refer to the original ``text``. Matching is
    monotonic forwards only; callers must still require full coverage
    (see :func:`require_exact_coverage`), because this locator alone
    would let a repeated word match an earlier occurrence.
    """
    spans, unmatched, _normalized = _map_tokens_internal(text, tokens)
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

    spans, unmatched, normalized = _map_tokens_internal(text, tokens)
    if unmatched:
        raise ValueError(
            "Alignment tokens do not correspond to the spoken text: "
            "%d unmatched token(s), first: %r"
            % (len(unmatched), unmatched[0].get("text") if isinstance(unmatched[0], dict) else unmatched[0]))
    matched = sum(len(get_token_key(token.get("text"))) for token, _s, _e in spans)
    if matched != len(normalized):
        raise ValueError(
            "Alignment covers %d of %d normalized characters; "
            "refusing to treat a partial result as aligned."
            % (matched, len(normalized)))
    # Normalized cursor search already enforces order (echoed tokens are
    # unmatched). Original ranges may legitimately overlap only when
    # consecutive tokens split one expanding original cluster (fi/ss):
    # reject a move backwards to an earlier cluster start, allow
    # sharing the same start.
    previous_start = 0
    for _token, start, _end in spans:
        if start < previous_start:
            raise ValueError("Alignment token order does not follow the text.")
        previous_start = start
    return spans


def get_validated_aligner_tokens(tokens, duration_sec, *, enforce_slice_limit=True):
    """Validate aligner tokens against bounds. Never repaired.

    The shared core rules are: finite positive duration, non-empty
    bounded token list, every token positive/monotonic/non-overlapping
    and within ``duration_sec``. With ``enforce_slice_limit`` (the
    default) ``duration_sec`` must also be a single real inference
    slice within MAX_ALIGN_SECONDS. Whole-row validation passes
    ``enforce_slice_limit=False``: a row assembled from bounded
    sequential slices may legitimately exceed 180 seconds, and must
    not be misclassified as one inference call. Per-slice callers keep
    the default and stay capped.
    """
    if (not isinstance(duration_sec, (int, float))
            or isinstance(duration_sec, bool)
            or not math.isfinite(duration_sec)
            or duration_sec <= 0):
        raise ValueError("Alignment slice duration is invalid.")
    if enforce_slice_limit:
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
    span_index = 0
    span_count = len(spans)
    for start, end in fragments:
        while span_index < span_count and spans[span_index][2] <= start:
            span_index += 1
        inside = []
        probe = span_index
        while probe < span_count and spans[probe][1] < end:
            if spans[probe][2] > start:
                inside.append(spans[probe])
            probe += 1
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


def _partition_long_sentence(text, sentence, rate):
    """Split one over-limit sentence at whitespace word boundaries.

    Returns contiguous {start, end, words} pieces covering the original
    sentence exactly once. Pieces aim at DEFAULT_WINDOW_TARGET_SECONDS
    of estimated narration; a single word needing more than
    MAX_ALIGN_SECONDS is refused (it cannot be partitioned further).
    """
    start, end = sentence["start"], sentence["end"]
    matches = list(re.finditer(r"\S+", text[start:end]))
    if not matches:
        raise ValueError(
            "One sentence needs more than the %d-second aligner limit "
            "and has no word boundary to partition at; split the "
            "editor row first." % MAX_ALIGN_SECONDS)
    pieces = []
    piece_start = start
    index = 0
    while index < len(matches):
        stop = index
        while stop < len(matches):
            cand_end = start + matches[stop].end()
            if stop > index and count_units(text[piece_start:cand_end]) * rate \
                    > DEFAULT_WINDOW_TARGET_SECONDS:
                break
            stop += 1
        if stop == index:
            stop = index + 1
        if stop < len(matches):
            piece_end = start + matches[stop].start()
        else:
            piece_end = end
        piece_words = count_units(text[piece_start:piece_end])
        if piece_words * rate > MAX_ALIGN_SECONDS:
            raise ValueError(
                "One sentence needs %.1fs of narration, more than the "
                "%d-second aligner limit; split the editor row first."
                % (piece_words * rate, MAX_ALIGN_SECONDS))
        pieces.append({"start": piece_start, "end": piece_end,
                       "words": piece_words})
        index = stop
        piece_start = piece_end
    return pieces


def get_aligner_windows(row_duration_sec, sentences, text=None):
    """Plan alignment windows for one editor row.

    ``sentences`` are {start, end, words} char ranges covering the row
    text exactly once (see cover_ranges). Word-rate estimation only
    *schedules* windows; produced clips must come from actual aligner
    tokens verified by require_exact_coverage. Every window's audio
    stays within MAX_ALIGN_SECONDS; the final window of a short row is
    the whole row.

    A single sentence is never refused merely for exceeding the
    20-second target: up to the 180-second cap it gets its own window.
    A sentence needing more than the cap is partitioned at word
    boundaries when ``text`` is supplied (final sentence envelopes are
    derived by the exporter from validated tokens and PCM frames;
    derive_fragment_times is the seconds-domain reference helper);
    without text the old explicit refusal is kept.

    ``first_sentence``/``last_sentence`` index the *expanded* sentence
    list after any partition (identical to the input list when no
    partition was needed).
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

    # Punctuation-only sentences carry no alignable words: fold
    # their char ranges into the adjacent spoken sentence so every
    # character is covered exactly once without planning a
    # zero-length window or an empty-token sentence clip. An
    # all-punctuation row still refuses (no alignable words).
    sentences = [dict(s) for s in sentences]
    spoken = [i for i, s in enumerate(sentences) if s["words"] > 0]
    if not spoken:
        raise ValueError("Row text has no alignable words.")
    for i, sentence in enumerate(sentences):
        if sentence["words"] > 0:
            continue
        prev_idx = max((j for j in spoken if j < i), default=None)
        next_idx = min((j for j in spoken if j > i), default=None)
        target = prev_idx if prev_idx is not None else next_idx
        if target < i:
            sentences[target]["end"] = max(sentences[target]["end"], sentence["end"])
        else:
            sentences[target]["start"] = min(sentences[target]["start"], sentence["start"])
    sentences = [s for s in sentences if s["words"] > 0]
    total_words = sum(s["words"] for s in sentences)
    if total_words <= 0:
        raise ValueError("Row text has no alignable words.")
    rate = float(row_duration_sec) / float(total_words)

    expanded = []
    for sentence in sentences:
        if sentence["words"] * rate > MAX_ALIGN_SECONDS:
            if not isinstance(text, str) or not text:
                raise ValueError(
                    "One sentence needs %.1fs of narration, more than the "
                    "%d-second aligner limit; split the editor row first."
                    % (sentence["words"] * rate, MAX_ALIGN_SECONDS))
            expanded.extend(_partition_long_sentence(text, sentence, rate))
        else:
            expanded.append(sentence)
    sentences = expanded
    total_words = sum(s["words"] for s in sentences)
    prefix_words = [0]
    for sentence in sentences:
        prefix_words.append(prefix_words[-1] + sentence["words"])

    windows = []
    index = 0
    while index < len(sentences):
        if (total_words - prefix_words[index]) * rate <= DEFAULT_WINDOW_TARGET_SECONDS \
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
            est_start = prefix_words[index] * rate
            start = est_start
        est_end = prefix_words[stop] * rate
        if est_end - est_start > MAX_ALIGN_SECONDS:
            raise ValueError(
                "One sentence needs %.1fs of narration, more than the "
                "%d-second aligner limit; split the editor row first."
                % (est_end - est_start, MAX_ALIGN_SECONDS))
        window_end = min(float(row_duration_sec), est_end + WINDOW_TAIL_SECONDS)
        # Round an estimated endpoint inward if floating addition
        # makes its duration exceed the strict per-call cap.
        # Actual PCM slices and model timestamps remain unchanged.
        window_end = min(window_end, start + MAX_ALIGN_SECONDS)
        if window_end - start > MAX_ALIGN_SECONDS:
            window_end = math.nextafter(window_end, start)
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


def validate_sequential_windows(text, planned_windows, windows, merged_tokens,
                                *, sample_rate, total_frames, row_duration_sec):
    """Independently validate worker-reported sequential windows.

    The parent never trusts worker-declared success: this re-derives
    every bound from the immutable row PCM geometry (``total_frames`` /
    ``sample_rate``) and the planned text scopes, re-runs the strict
    token + exact-coverage guards per window and on the merged row,
    and requires the worker's merged tokens to equal the offset
    concatenation of its own windows. Returns validated row tokens.
    """
    if type(sample_rate) is not int or sample_rate <= 0:
        raise ValueError("Alignment sample rate is invalid.")
    if type(total_frames) is not int or total_frames <= 0:
        raise ValueError("Alignment row frame count is invalid.")
    if not isinstance(planned_windows, list) or not planned_windows:
        raise ValueError("Alignment window plan is malformed.")
    if not isinstance(windows, list) or len(windows) != len(planned_windows):
        raise ValueError(
            "Alignment worker reported %r windows for %d planned windows."
            % (len(windows) if isinstance(windows, list) else None,
               len(planned_windows)))
    if len(windows) > 10000:
        raise ValueError("Alignment worker reported too many windows.")
    if not isinstance(merged_tokens, list) or not merged_tokens:
        raise ValueError("Alignment worker returned no merged tokens.")

    frame_seconds = 1.0 / float(sample_rate)
    max_slice_frames = int(round(MAX_ALIGN_SECONDS * sample_rate))
    rebuilt = []
    previous_cursor_sec = 0.0
    total_calls = 0
    for position, (plan, window) in enumerate(zip(planned_windows, windows)):
        if not isinstance(window, dict):
            raise ValueError("Alignment window %d is malformed." % position)
        if window.get("char_start") != plan["char_start"] \
                or window.get("char_end") != plan["char_end"]:
            raise ValueError(
                "Alignment window %d text scope does not match the plan." % position)
        start_frame, end_frame = window.get("start_frame"), window.get("end_frame")
        if type(start_frame) is not int or type(end_frame) is not int \
                or not 0 <= start_frame < end_frame <= total_frames:
            raise ValueError("Alignment window %d frame bounds are invalid." % position)
        if end_frame - start_frame > max_slice_frames + 1:
            raise ValueError("Alignment window %d exceeds the aligner limit." % position)
        offset = start_frame * frame_seconds
        slice_duration = (end_frame - start_frame) * frame_seconds
        for key, expected in (("offset_sec", offset),
                              ("slice_duration_sec", slice_duration)):
            value = window.get(key)
            if (type(value) not in (int, float) or isinstance(value, bool)
                    or not math.isfinite(value)
                    or abs(float(value) - expected) > 1e-6):
                raise ValueError("Alignment window %d %s is inconsistent." % (position, key))
        calls = window.get("calls")
        if type(calls) is not int or not 1 <= calls <= MAX_SEQUENTIAL_CALLS_PER_WINDOW:
            raise ValueError("Alignment window %d call count is out of bounds." % position)
        total_calls += calls
        if position == 0 and start_frame != 0:
            raise ValueError("Alignment windows do not start at the row start.")
        if position and abs(offset - previous_cursor_sec) > 2 * frame_seconds + 0.002:
            raise ValueError(
                "Alignment window %d does not start at the measured "
                "previous-end cursor." % position)
        piece = text[plan["char_start"]:plan["char_end"]]
        validated = get_validated_aligner_tokens(window.get("tokens"), slice_duration)
        require_exact_coverage(piece, validated)
        for token in validated:
            rebuilt.append({
                "text": token["text"],
                "start_time": offset + token["start_time"],
                "end_time": offset + token["end_time"],
            })
        previous_cursor_sec = offset + validated[-1]["end_time"]
    if total_calls > len(planned_windows) * MAX_SEQUENTIAL_CALLS_PER_WINDOW:
        raise ValueError("Alignment worker exceeded its sequential call bound.")
    if len(rebuilt) != len(merged_tokens):
        raise ValueError("Alignment merged tokens do not match its windows.")
    for mine, theirs in zip(rebuilt, merged_tokens):
        if not isinstance(theirs, dict) or theirs.get("text") != mine["text"] \
                or type(theirs.get("start_time")) not in (int, float) \
                or type(theirs.get("end_time")) not in (int, float) \
                or abs(float(theirs["start_time"]) - mine["start_time"]) > 1e-6 \
                or abs(float(theirs["end_time"]) - mine["end_time"]) > 1e-6:
            raise ValueError("Alignment merged tokens do not match its windows.")
    row_tokens = get_validated_aligner_tokens(
        rebuilt, row_duration_sec, enforce_slice_limit=False)
    require_exact_coverage(text, row_tokens)
    return row_tokens
