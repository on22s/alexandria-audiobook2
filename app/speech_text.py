"""Conservative speech preparation and non-prose risk classification."""
import functools
import re
import logging


from speech_numbers import get_book_date_order, verbalize_numbers
from speech_policy import (SPEECH_BREAKS, SPEECH_WORDS, VERBALIZED_SYMBOLS,
                           SPOKEN_SYMBOLS, get_spoken_symbol, get_spoken_word_text,
                           get_spoken_text_spacing, get_scene_break_text,
                           extract_delivery_cues, SCENE_BREAK_MARKER,
                           is_unspeakable_character, UNSPEAKABLE_CATEGORIES,
                           REPLACEMENT_CHARACTER, has_trailing_punctuation,
                           is_sentence_terminated)

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


@functools.lru_cache(maxsize=65536)
def _get_symbol_rendering(ch):
    """-> (kind, replacement) for one character: kind is "spoken", "dropped"
    or None (left as is). Pure in the character - the symbol tables and
    Unicode categories are fixed for the process - so each distinct
    character is classified once, not once per chunk."""
    word = get_spoken_symbol(ch)
    if word:
        return "spoken", get_spoken_word_text(word)
    if is_unspeakable_character(ch):
        return "dropped", " "
    return None, ch


def verbalize_symbols(text):
    """-> (text, transformations). Named symbols spoken, unknown ones dropped.

    Runs after SPEECH_WORDS and the structural-break pass so anything those
    already handle keeps its existing behaviour - `■` stays a sentence break
    rather than becoming a silent drop.
    """
    if type(text) is not str:
        text = "".join(text)
    spoken, dropped, table = [], [], {}
    for ch in dict.fromkeys(text):
        kind, replacement = _get_symbol_rendering(ch)
        if kind == "spoken":
            spoken.append(ch)
        elif kind == "dropped":
            dropped.append(ch)
        else:
            continue
        table[ord(ch)] = replacement
    transformations = []
    if spoken:
        transformations.append({"type": "verbalized_symbol",
                                "symbols": sorted(spoken)})
    if dropped:
        # Recorded, not silent: a character removed without a trace is
        # indistinguishable from one that was never in the source, and this
        # list is the evidence for goal 5.1's count - every occurrence, not
        # every distinct glyph.
        transformations.append({"type": "dropped_unspeakable",
                                "symbols": sorted(dropped),
                                # One pass however many distinct glyphs were
                                # dropped: delete them all and compare lengths.
                                "count": len(text) - len(text.translate(
                                    dict.fromkeys(map(ord, dropped))))})
    return (text.translate(table) if table else text), transformations


def get_prelexicon_speech_normalization(text):
    """Return the exact prepared coordinate space before pronunciation substitution."""
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
            normalized = normalized.replace(symbol, get_spoken_word_text(word))
            transformations.append({"type": "spoken_symbol" if word else "dropped_reference_mark",
                                    "symbol": symbol, "replacement": word})
    normalized = re.sub(r"\s*" + re.escape(SCENE_BREAK_MARKER) + r"\s*", ". ", normalized)
    # Money with cents, rising ranges and slash dates (#1546), before symbols.
    normalized, number_transformations = verbalize_numbers(normalized, get_book_date_order())
    transformations.extend(number_transformations)
    normalized, symbol_transformations = verbalize_symbols(normalized)
    transformations.extend(symbol_transformations)
    normalized, count = _ORPHAN_PUNCT_RE.subn(". ", normalized)
    if count:
        transformations.append({"type": "collapsed_periods", "count": count})
    normalized, count = _SPACE_RE.subn(" ", normalized)
    if count:
        transformations.append({"type": "collapsed_spacing", "count": count})
    if any(change["type"] in {"spoken_symbol", "verbalized_symbol", "dropped_reference_mark"}
           for change in transformations):
        normalized, count = get_spoken_text_spacing(normalized)
        if count:
            transformations.append({"type": "normalized_punctuation_spacing", "count": count})
    return {"text": normalized, "changed": normalized != original,
            "transformations": transformations,
            "risk_categories": get_speech_risks(original)}


def get_speech_normalization(text):
    """Return normalized text and every applied transformation as new data."""
    prepared = get_prelexicon_speech_normalization(text)
    if not text:
        return prepared
    original = str(text)
    normalized = prepared["text"]
    transformations = prepared["transformations"]
    # Proper-noun respellings, applied LAST so a lexicon entry is never
    # mangled by symbol or break handling. Recorded as a transformation like
    # everything else: a silent respelling would be untraceable, with the
    # listener hearing one thing and the script saying another.
    pronunciation_decisions = []
    try:
        from pronunciation import ensure_speech_pronunciation_result, get_pronunciation_text_digest
        resolution = ensure_speech_pronunciation_result(normalized)
        spoken, applied = resolution["text"], resolution["substitutions"]
        pronunciation_decisions = resolution["decisions"]
        if applied:
            normalized = spoken
            transformations.append({"type": "pronunciation_lexicon",
                                    "substitutions": applied})
    except Exception as error:                          # noqa: BLE001
        # A broken lexicon must never stop a book generating.
        logging.getLogger(__name__).warning(
            "Pronunciation processing failed; continuing without substitutions: %s", error)
    stripped = normalized.strip(" \t\r\n").lstrip(" .\t\r\n")
    # A comma or semicolon is an authored continuation cue. Adding a full stop
    # after it creates malformed TTS input (",." / ";.").
    appended_period = (bool(stripped) and not is_sentence_terminated(stripped)
                       and not has_trailing_punctuation(stripped, ',;'))
    bounded = stripped + "." if appended_period else stripped
    if bounded != normalized:
        transformations.append({"type": "normalized_sentence_boundary",
                                "trimmed_characters": len(normalized) - len(stripped),
                                "appended_period": appended_period})
    normalized = bounded
    result = {"text": normalized, "changed": normalized != original,
              "transformations": transformations,
              "risk_categories": get_speech_risks(original)}
    if pronunciation_decisions:
        result["pronunciation_decisions"] = pronunciation_decisions
        result["pronunciation_prepared_sha256"] = get_pronunciation_text_digest(prepared["text"])
    return result


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


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
