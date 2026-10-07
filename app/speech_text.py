"""Conservative speech preparation and non-prose risk classification."""
import re
import logging


from speech_policy import (SPEECH_BREAKS, SPEECH_WORDS, VERBALIZED_SYMBOLS,
                           SPOKEN_SYMBOLS, get_spoken_symbol, get_scene_break_text,
                           extract_delivery_cues, SCENE_BREAK_MARKER,
                           is_unspeakable_character, UNSPEAKABLE_CATEGORIES,
                           REPLACEMENT_CHARACTER)

# Unicode categories with no spoken form at all: symbols, private use,
# unassigned, surrogates and non-whitespace controls. Currency (Sc) is deliberately excluded - "$" and
# "£" are speakable and belong in a table, not in a silent drop.
#
# WHY A CATCH-ALL AND NOT A LIST. The named tables above can only cover
# symbols someone thought of. An audiobook meets whatever the source file
# contains, and the failure is silent: the engine receives a character with no
# pronunciation and its behaviour is undefined - it may skip it, or emit noise,
# or mispronounce the surrounding words. Dropping the unknown is the
# conservative choice, because a dropped symbol is a symbol the listener was
# never going to hear correctly anyway.
_UNSPEAKABLE_CATEGORIES = UNSPEAKABLE_CATEGORIES

_SPACE_RE = re.compile(r"[ \t]{2,}")
_ORPHAN_PUNCT_RE = re.compile(r"(?:\.\s*){2,}")
_URL_RE = re.compile(
    r"(?:https?://|https?//|www\.)\S+|\b(?:[a-z0-9-]+\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]+)(?![a-z0-9-])"
    r"(?:/[a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
    re.IGNORECASE)
_LABELLED_IDENTIFIER_RE = re.compile(
    r"\b(?:isbn(?:-1[03])?|issn|lccn|lcc|ddc|doi|sku|catalog(?:ue)?|serial|model|account|"
    r"reference|ref|id)\s*(?:no\.?|number|#|:)?\s*[a-z0-9][a-z0-9._:/-]*",
    re.IGNORECASE)
_ISBN_RE = re.compile(r"\b97[89](?:-?\d){10}\b")
_CONTENTS_RE = re.compile(r"^\s*(?:contents|navigation|table of contents)\b",
                          re.IGNORECASE)
_COMPACT_IDENTIFIER_RE = re.compile(
    r"\b(?=[a-z0-9._:/-]{5,}\b)(?=[a-z0-9._:/-]*[a-z])"
    r"(?=[a-z0-9._:/-]*\d)[a-z0-9]+(?:[._:/-][a-z0-9]+)+\b",
    re.IGNORECASE)


def get_speech_risks(text):
    """Return evidence-backed categories that merit review, without mutation."""
    value = str(text or "")
    risks = []
    if _URL_RE.search(value):
        risks.append("url")
    if (_LABELLED_IDENTIFIER_RE.search(value) or _COMPACT_IDENTIFIER_RE.search(value)
            or _ISBN_RE.search(value)):
        risks.append("identifier")
    structural = re.sub(r"(?<!\w)(\*{1,3}|_{1,3})(?=\S)([^\n]*?\S)\1(?!\w)", r"\2", value)
    break_count = sum(structural.count(mark) for mark in SPEECH_BREAKS)
    if (break_count >= 2 or (value.count("|") >= 2 and "\n" in value)
            or (_CONTENTS_RE.search(value) and value.count(".") >= 3)):
        risks.append("list_or_table")
    return risks


def verbalize_symbols(text):
    """-> (text, transformations). Named symbols spoken, unknown ones dropped.

    Runs after SPEECH_WORDS and the structural-break pass so anything those
    already handle keeps its existing behaviour - `■` stays a sentence break
    rather than becoming a silent drop.
    """
    spoken, dropped, out = [], [], []
    for ch in text:
        word = get_spoken_symbol(ch)
        if word:
            out.append(f" {word} ")
            spoken.append(ch)
        elif is_unspeakable_character(ch):
            out.append(" ")
            dropped.append(ch)
        else:
            out.append(ch)
    transformations = []
    if spoken:
        transformations.append({"type": "verbalized_symbol",
                                "symbols": sorted(set(spoken))})
    if dropped:
        # Recorded, not silent: a character removed without a trace is
        # indistinguishable from one that was never in the source, and this
        # list is the evidence for goal 5.1's count.
        transformations.append({"type": "dropped_unspeakable",
                                "symbols": sorted(set(dropped)),
                                "count": len(dropped)})
    return "".join(out), transformations


def get_speech_normalization(text):
    """Return normalized text and every applied transformation as new data."""
    if not text:
        return {"text": text, "changed": False, "transformations": [],
                "risk_categories": get_speech_risks(text)}
    original = str(text)
    normalized, hints = extract_delivery_cues(original)
    transformations = []
    if hints:
        transformations.append({"type": "delivery_cues", "hints": hints})
    scene_text = get_scene_break_text(normalized)
    if scene_text != normalized:
        normalized = scene_text
        transformations.append({"type": "structural_break"})
    for symbol in SPEECH_WORDS:
        word = get_spoken_symbol(symbol)
        if symbol == "©":
            # Avoid adding a second spoken "copyright" from its symbol,
            # without deleting repeated words that were authored in the text.
            normalized, redundant = re.subn(
                r"(\bcopyright\s*)©|©(\s*copyright\b)",
                lambda match: match.group(1) or match.group(2), normalized, flags=re.IGNORECASE)
            if redundant:
                transformations.append({"type": "dropped_redundant_symbol",
                                        "symbol": symbol, "replacement": ""})
        if symbol in normalized:
            normalized = normalized.replace(symbol, f" {word} " if word else " ")
            transformations.append({"type": "spoken_symbol" if word else "dropped_reference_mark",
                                    "symbol": symbol, "replacement": word})
    normalized = re.sub(r"\s*" + re.escape(SCENE_BREAK_MARKER) + r"\s*", ". ", normalized)
    normalized, symbol_transformations = verbalize_symbols(normalized)
    transformations.extend(symbol_transformations)
    normalized, count = _ORPHAN_PUNCT_RE.subn(". ", normalized)
    if count:
        transformations.append({"type": "collapsed_periods", "count": count})
    normalized, count = _SPACE_RE.subn(" ", normalized)
    if count:
        transformations.append({"type": "collapsed_spacing", "count": count})
    # Proper-noun respellings, applied LAST so a lexicon entry is never
    # mangled by symbol or break handling. Recorded as a transformation like
    # everything else: a silent respelling would be untraceable, with the
    # listener hearing one thing and the script saying another.
    try:
        from pronunciation import apply_pronunciation
        spoken, applied = apply_pronunciation(normalized)
        if applied:
            normalized = spoken
            transformations.append({"type": "pronunciation_lexicon",
                                    "substitutions": applied})
    except Exception as error:                          # noqa: BLE001
        # A broken lexicon must never stop a book generating.
        logging.getLogger(__name__).warning(
            "Pronunciation processing failed; continuing without substitutions: %s", error)
    stripped = normalized.strip(" .\t\n")
    bounded = stripped + "." if stripped else ""
    if bounded != normalized:
        transformations.append({"type": "normalized_sentence_boundary",
                                "trimmed_characters": len(normalized) - len(stripped),
                                "appended_period": bool(stripped)})
    normalized = bounded
    return {"text": normalized, "changed": normalized != original,
            "transformations": transformations,
            "risk_categories": get_speech_risks(original)}


def normalize_for_speech(text):
    """Return only the prepared text for existing TTS callers."""
    return get_speech_normalization(text)["text"]


def get_speech_preparation(text, instruct=None):
    """Return new text/instruction values with shared, deduplicated cues."""
    normalized = get_speech_normalization(text)
    hints = [hint for change in normalized["transformations"]
             if change["type"] == "delivery_cues" for hint in change["hints"]]
    instruction = instruct
    if hints:
        existing = str(instruct or "").strip()
        additions = [hint for hint in hints if hint not in existing]
        instruction = " ".join(filter(None, [existing] + additions))
    return {"text": normalized["text"], "instruct": instruction}
