"""Bounded ordered transcript evidence; ASR discrepancies are not acoustic truth."""
from collections import Counter
import difflib
import hashlib
import unicodedata

from word_timing import get_word_tokens

MAX_TEXT = 20000
MAX_TOKENS = 2048
MAX_CELLS = 1048576
MAX_BLOCKS = 32
MAX_SNIPPET = 160
_NUMBER_WORDS = frozenset(('zero one two three four five six seven eight nine ten eleven twelve '
    'thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty '
    'seventy eighty ninety hundred thousand million billion first second third fourth fifth sixth '
    'seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth '
    'seventeenth eighteenth nineteenth twentieth thirtieth fortieth fiftieth sixtieth seventieth '
    'eightieth ninetieth hundredth thousandth millionth billionth half quarter point').split())


class SpeechComparisonUnavailable(ValueError):
    """An explicit comparison bound/notation prevents an honest whole comparison."""


def get_comparison_tokens(text):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise SpeechComparisonUnavailable('text_limit')
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise SpeechComparisonUnavailable('invalid_unicode')
    normalized = unicodedata.normalize('NFC', text)
    if any(unicodedata.category(char).startswith('M') for char in normalized):
        raise SpeechComparisonUnavailable('unsupported_combining_notation')
    tokens = [word for word, _, _ in get_word_tokens(normalized)]
    if len(tokens) > MAX_TOKENS:
        raise SpeechComparisonUnavailable('token_limit')
    return tokens


def _get_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _is_numeric(tokens):
    return any(any(char.isdigit() for char in word) or word in _NUMBER_WORDS for word in tokens)


def get_speech_comparison(expected, transcript, *, ambiguous_expected_tokens=(), ambiguous_expected_regions=()):
    """Describe full ordered differences, retaining multiplicity and uncertainty.

    Matching-block opcodes are deliberately not labeled minimum-edit WER. Token
    indices refer to NFC comparison tokens, never invented authored offsets.
    Callers keep exact source/expected/raw ASR bytes as separate bound evidence.
    """
    wanted, observed = get_comparison_tokens(expected), get_comparison_tokens(transcript)
    if not wanted:
        raise SpeechComparisonUnavailable('empty_expected_speech')
    if (len(wanted) + 1) * (len(observed) + 1) > MAX_CELLS:
        raise SpeechComparisonUnavailable('comparison_limit')
    if (not isinstance(ambiguous_expected_tokens, (tuple, list, set, frozenset))
            or len(ambiguous_expected_tokens) > MAX_TOKENS
            or any(not isinstance(word, str) or len(word) > MAX_TEXT for word in ambiguous_expected_tokens)):
        raise SpeechComparisonUnavailable('invalid_pronunciation_ambiguity')
    ambiguity = set(ambiguous_expected_tokens)
    if (not isinstance(ambiguous_expected_regions, (tuple, list)) or len(ambiguous_expected_regions) > MAX_TOKENS
            or any(not isinstance(region, (tuple, list)) or len(region) != 2 or
                   any(type(value) is not int for value in region) or
                   not 0 <= region[0] < region[1] <= len(wanted) for region in ambiguous_expected_regions)):
        raise SpeechComparisonUnavailable('invalid_pronunciation_regions')
    blocks, matched = [], 0
    expected_counts, observed_counts = Counter(wanted), Counter(observed)
    for kind, left, right, start, end in difflib.SequenceMatcher(None, wanted, observed, autojunk=False).get_opcodes():
        if kind == 'equal':
            matched += right - left
            continue
        if len(blocks) == MAX_BLOCKS:
            raise SpeechComparisonUnavailable('diagnostic_block_limit')
        reference, hypothesis = wanted[left:right], observed[start:end]
        reasons = []
        if _is_numeric(reference + hypothesis):
            reasons.append('numeric_normalization')
        if (ambiguity.intersection(reference) or
                any(left < stop and begin < right for begin, stop in ambiguous_expected_regions)):
            reasons.append('pronunciation_projection')
        if any(expected_counts[word] > 1 for word in reference):
            reasons.append('repeated_occurrence_anchor')
        repeated = kind == 'insert' and any(observed_counts[word] > expected_counts[word] > 0 for word in hypothesis)
        blocks.append(dict(kind=kind, expected_from=left, expected_to=right,
            observed_from=start, observed_to=end, expected=' '.join(reference)[:MAX_SNIPPET],
            observed=' '.join(hypothesis)[:MAX_SNIPPET],
            expected_tokens=reference, observed_tokens=hypothesis,
            ambiguity=reasons, transcript_repetition=bool(repeated)))
        if kind == 'delete':
            # Keep the full opcode above, while isolating only protected tokens
            # from material deletion evidence. A name/number at the start of a
            # truncated tail must not exempt the rest of that tail.
            regions=[]
            for index in range(left,right):
                protected = (_is_numeric([wanted[index]]) or wanted[index] in ambiguity or
                    any(begin <= index < stop for begin,stop in ambiguous_expected_regions))
                if protected:
                    continue
                if regions and regions[-1][1] == index:
                    regions[-1][1]=index+1
                else:
                    regions.append([index,index+1])
            blocks[-1]['unambiguous_deletions']=[dict(kind=kind,expected_from=begin,
                expected_to=stop,expected_tokens=wanted[begin:stop],observed_tokens=[])
                for begin,stop in regions]
    return dict(version=1, expected_sha256=_get_digest(expected), transcript_sha256=_get_digest(transcript),
        expected_words=len(wanted), observed_words=len(observed), matched_words=matched,
        unmatched_expected=len(wanted)-matched, unmatched_observed=len(observed)-matched,
        differences=blocks, empty_transcript=not observed,
        status='review' if blocks else 'no_transcript_difference',
        notice='ASR token evidence does not certify pronunciation or complete spoken content.')


def get_material_regions(comparison):
    """Conservative candidate regions for a separately qualified corroborator.

    Numerical/approved projection ambiguity is not erased or turned into a hard
    failure. Repeated anchors remain reported; an exact repeated insertion can
    still be corroborated against complete authored multiplicity.
    """
    if comparison['empty_transcript'] or comparison['expected_words'] < 6:
        return []
    result = []
    for block in comparison['differences']:
        if block['kind'] == 'delete':
            for region in block['unambiguous_deletions']:
                missing=len(region['expected_tokens'])
                if missing >= 3 and missing / comparison['expected_words'] >= .3:
                    result.append(region)
            continue
        # Shared ASR respelling of names can produce long replacement blocks.
        # Retain them as review evidence, without promoting them to content loss.
        if block['kind'] == 'replace':
            continue
        if set(block['ambiguity']) - {'repeated_occurrence_anchor'}:
            continue
        missing, extra = len(block['expected_tokens']), len(block['observed_tokens'])
        if max(missing, extra) >= 3 and max(missing, extra) / comparison['expected_words'] >= .3:
            result.append(dict(kind=block['kind'], expected_from=block['expected_from'],
                expected_to=block['expected_to'], expected_tokens=list(block['expected_tokens']),
                observed_tokens=list(block['observed_tokens'])))
    return result


def get_corroborated_regions(primary, confirmation):
    """Compare regions only; provider distinctness/qualification is mandatory upstream."""
    if primary['expected_sha256'] != confirmation['expected_sha256']:
        raise ValueError('Confirmation expected speech changed')
    other = get_material_regions(confirmation)
    corroborated = []
    for region in get_material_regions(primary):
        if region in other:
            corroborated.append(region)
            continue
        if region['kind'] != 'insert':
            continue
        for candidate in other:
            if (candidate['kind'] != 'insert' or
                    candidate['expected_from'] != region['expected_from']):
                continue
            # Corroborate only exact ordered subregions of an extra passage.
            # This tolerates neither arbitrary fuzzy spelling nor missing-word
            # substitutions: both full transcripts remain in the evidence.
            matches = difflib.SequenceMatcher(None, region['observed_tokens'],
                candidate['observed_tokens'], autojunk=False).get_matching_blocks()
            shared = [region['observed_tokens'][block.a:block.a + block.size]
                      for block in matches if block.size >= 3]
            if sum(map(len, shared)) >= max(3, primary['expected_words'] * .3):
                corroborated.append({**region, 'exact_shared_extra_regions': shared,
                    'confirmation_observed_tokens': candidate['observed_tokens']})
                break
    return corroborated

from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_verification_code
_LOADED_SPEECH_VERIFICATION_CODE_SHA256 = _get_loaded_verification_code()
