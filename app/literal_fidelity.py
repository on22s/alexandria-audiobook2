"""Fail-closed ordered words for explicitly literal annotation, not speech prep."""
import re
import unicodedata
from itertools import chain, zip_longest

# Keep case and lexical apostrophes; punctuation/emphasis delimiters are not
# words. NFC and typographic apostrophes are the only character equivalences.
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_POLICY = 'literal_words_v1:NFC,case-sensitive,lexical-marks-and-joiners,apostrophe-form,punctuation-delimiters'

class UnverifiableLexeme(ValueError):
    pass

def _normalized_with_offsets(text):
    """NFC text with each output character's original raw character interval.

    One pending segment is composed at a time, so a combining mark or a
    composable starter (including Hangul Jamo) keeps the raw span of the whole
    segment while a non-composing starter closes it. This is the same NFC
    policy as the word tokenizer, never a second approximation of it.
    """
    normalized, spans = [], []
    buffer, buffer_start, buffer_end = '', 0, 0
    for index, char in enumerate(text):
        if buffer:
            previous = unicodedata.normalize('NFC', buffer)
            combined = unicodedata.normalize('NFC', buffer + char)
            if unicodedata.combining(char) or len(combined) <= len(previous):
                buffer += char
                buffer_end = index + 1
                continue
            for output in previous.replace('’', "'"):
                normalized.append(output)
                spans.append((buffer_start, buffer_end))
            buffer = ''
        buffer, buffer_start, buffer_end = char, index, index + 1
    if buffer:
        for output in unicodedata.normalize('NFC', buffer).replace('’', "'"):
            normalized.append(output)
            spans.append((buffer_start, buffer_end))
    return ''.join(normalized), spans


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
        if char in ('\u200c', '\u200d'):
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
    normalized, spans = _normalized_with_offsets(text)
    for word, start, end in _word_spans(normalized):
        yield word, spans[start][0], spans[end - 1][1]


def _words(text):
    for word, _, _ in iter_literal_words(text):
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
