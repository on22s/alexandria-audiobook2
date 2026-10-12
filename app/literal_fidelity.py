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

def _words(text):
    value=unicodedata.normalize('NFC',text).replace('’', "'")
    if value.isascii():
        for match in _WORD.finditer(value):yield match.group()
        return
    start=None
    for index,char in enumerate(value):
        mark=unicodedata.category(char).startswith('M')
        if char.isalnum() or mark:
            if start is None:
                if mark:raise UnverifiableLexeme('Combining mark without a lexical base')
                start=index
            continue
        if char in ('\u200c', '\u200d'):
            if start is None or index+1==len(value):
                raise UnverifiableLexeme('Unattached lexical joiner')
            following=value[index+1]
            if following.isalnum() or unicodedata.category(following).startswith('M'):
                continue
            raise UnverifiableLexeme('Unsupported lexical joiner chain')
        if char == "'" and start is not None and index+1<len(value):
            following=value[index+1]
            if following.isalnum() or unicodedata.category(following).startswith('M'):
                continue
        if start is not None:yield value[start:index]
        start=None
    if start is not None:yield value[start:]

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
