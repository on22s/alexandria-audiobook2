"""Fail-closed ordered words for explicitly literal annotation, not speech prep."""
import re
import unicodedata
from itertools import chain, zip_longest

from silent_word_characters import (SILENT_WORD_CHARACTERS,
                                    ZERO_WIDTH_JOINER,
                                    ZERO_WIDTH_NON_JOINER,
                                    remove_silent_word_characters)

# Keep case and lexical apostrophes; punctuation/emphasis delimiters are not
# words. NFC and typographic apostrophes are the only character equivalences.
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_POLICY = 'literal_words_v1:NFC,case-sensitive,lexical-marks-and-joiners,apostrophe-form,punctuation-delimiters'

class UnverifiableLexeme(ValueError):
    pass

def _normalized_with_offsets(text):
    """Whole-string NFC text with each output character's raw interval.

    Raw characters are decomposed first, so a character whose canonical
    decomposition begins with a nonstarter (Tibetan U+0F73, for example) joins
    the preceding combining sequence even though its own combining class is
    zero. Canonical ordering and composition are then applied per sequence and
    the result is checked against the exact whole-string NFC baseline.
    """
    decomposed = []
    for index, char in enumerate(text):
        if char in SILENT_WORD_CHARACTERS:
            continue
        for piece in unicodedata.normalize('NFD', char):
            decomposed.append((piece, index, index + 1))
    sequences, sequence, built = [], [], ''
    for item in decomposed:
        char = item[0]
        if sequence:
            candidate = unicodedata.normalize('NFC', built + char)
            if (len(candidate) <= len(built) or unicodedata.combining(char)
                    or unicodedata.combining(unicodedata.normalize('NFD', char)[0])):
                sequence.append(item)
                built = candidate
                continue
            sequences.append(sequence)
            sequence, built = [], ''
        sequence = [item]
        built = unicodedata.normalize('NFC', char)
    if sequence:
        sequences.append(sequence)
    pieces = []
    for sequence in sequences:
        composed = unicodedata.normalize('NFC', ''.join(item[0] for item in sequence))
        raw_start = min(item[1] for item in sequence)
        raw_end = max(item[2] for item in sequence)
        for output in composed.replace('’', "'"):
            if output not in SILENT_WORD_CHARACTERS:
                pieces.append((output, raw_start, raw_end))
    normalized = ''.join(piece[0] for piece in pieces)
    # Delete the silent characters BEFORE NFC so a base letter and its combining
    # mark on either side of one can still compose (#2316).
    baseline = unicodedata.normalize(
        'NFC', remove_silent_word_characters(text)).replace('’', "'")
    if normalized != baseline:
        raise ValueError('Literal NFC offset alignment differs from whole-string NFC')
    return normalized, [(piece[1], piece[2]) for piece in pieces]


def _word_spans(value):
    """Token positions in already normalized text, by the literal word rule."""
    if value.isascii():
        for match in _WORD.finditer(value):
            yield match.group(), match.start(), match.end()
        return
    start = None
    for index, char in enumerate(value):
        mark = unicodedata.category(char).startswith('M')
        if char.isalnum() or mark:
            if start is None:
                if mark:
                    raise UnverifiableLexeme('Combining mark without a lexical base')
                start = index
            continue
        # Both joiners stay inside the literal word: a literal word is compared
        # character for character, so deleting a glyph-joining hint would change
        # it. The comparison key deletes ZWJ instead (silent_word_characters),
        # because there the two spellings are the same word.
        if char in (ZERO_WIDTH_NON_JOINER, ZERO_WIDTH_JOINER):
            if start is None or index + 1 == len(value):
                raise UnverifiableLexeme('Unattached lexical joiner')
            following = value[index + 1]
            if following.isalnum() or unicodedata.category(following).startswith('M'):
                continue
            raise UnverifiableLexeme('Unsupported lexical joiner chain')
        if char == "'" and start is not None and index + 1 < len(value):
            following = value[index + 1]
            if following.isalnum() or unicodedata.category(following).startswith('M'):
                continue
        if start is not None:
            yield value[start:index], start, index
        start = None
    if start is not None:
        yield value[start:], start, len(value)


def iter_literal_words(text):
    """The single literal tokenizer, yielding (word, raw_start, raw_end)."""
    if text.isascii():
        yield from _word_spans(text)
        return
    normalized, spans = _normalized_with_offsets(text)
    for word, start, end in _word_spans(normalized):
        yield (word, min(span[0] for span in spans[start:end]),
               max(span[1] for span in spans[start:end]))


def _words(text):
    """Word-only path: whole-string NFC, no per-character offset vector."""
    value = unicodedata.normalize(
        'NFC', remove_silent_word_characters(text)).replace('’', "'")
    for word, _, _ in _word_spans(value):
        yield word

def audit_literal_words(source_text, entries):
    """Compare a streaming source/entry word sequence; never change either.

    Numbers, abbreviations, symbols and pronunciation changes belong in a
    separately traced speech stage. No arbitrary transform exemption is used.
    This cannot prove punctuation, speaker attribution or acoustic fidelity.
    """
    report={'policy':_POLICY,'passed':False,'matched_words':0}
    if not isinstance(source_text,str) or not source_text.strip():
        return {**report,'reason':'unverifiable_source'}
    if not isinstance(entries,list) or not entries or any(
            not isinstance(e,dict) or not isinstance(e.get('text'),str)
            or not e['text'].strip() for e in entries):
        return {**report,'reason':'unverifiable_entries'}
    missing=object()
    left=_words(source_text)
    right=chain.from_iterable(_words(e['text']) for e in entries)
    try:
        for index,(source,output) in enumerate(zip_longest(left,right,fillvalue=missing)):
            if source!=output:
                # Bounded evidence; a giant token never enters a report unbounded.
                return {**report,'reason':'ordered_word_mismatch','word_index':index,
                        'expected':None if source is missing else source[:80],
                        'output':None if output is missing else output[:80]}
            report['matched_words']+=1
    except UnverifiableLexeme:
        return {**report,'reason':'unverifiable_unicode_lexeme'}
    if not report['matched_words']:
        return {**report,'reason':'unverifiable_wordless_source'}
    return {**report,'passed':True}
