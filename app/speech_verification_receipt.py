"""Finite revision-bound speech evidence; polling never decodes/hashes audio."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

from editor_poll_snapshot import get_snapshot_digest
from speech_verification_compare import get_comparison_tokens
from speech_verification_runtime import get_verifier_file_identity
from word_timing import MAX_AUDIO_BYTES
from audio_validation import GeneratedAudioError

MAX_RECEIPT_BYTES = 128 * 1024
MAX_DIAGNOSTICS = 8
_AUDIO_FILE_VERSION_FIELDS = 5
_HEX64 = re.compile(r'[0-9a-f]{64}')
_STATUSES = frozenset(('unavailable','review','no_content_candidate',
    'corroborated_content_difference','review_confirmation_unavailable','review_short_acoustic_unavailable'))


class SpeechContentMismatchError(GeneratedAudioError):
    def __init__(self, receipt):
        super().__init__('Speech verification confirmed material missing or extra speech. '
                         'The previous take was kept. Review the line and regenerate it.')
        self.receipt = copy.deepcopy(receipt)


def get_speech_verification_error_fields(error):
    return {'speech_verification_failure':copy.deepcopy(error.receipt)
            if isinstance(error,SpeechContentMismatchError) else None}


def get_speech_verification_chunk_digest(chunk):
    # Use the same input definition as conditional publication/invalidation.
    from project import get_chunk_generation_inputs
    return get_snapshot_digest(get_chunk_generation_inputs(chunk))


def get_validated_speech_input(record, path, cancel_check=lambda: False):
    if (not isinstance(record, dict) or set(record) != {'version','text','language','provenance','audio'}
            or type(record['version']) is not int or record['version'] != 1
            or not isinstance(record['text'], str) or not 0 < len(record['text']) <= 20000
            or not isinstance(record['language'], str) or not 0 < len(record['language']) <= 64
            or not isinstance(record['provenance'], str) or not 0 < len(record['provenance']) <= 128
            or not isinstance(record['audio'], dict) or set(record['audio']) != {'sha256','size'}
            or not isinstance(record['audio']['sha256'], str) or not _HEX64.fullmatch(record['audio']['sha256'])
            or type(record['audio']['size']) is not int or not 0 < record['audio']['size'] <= MAX_AUDIO_BYTES):
        return None
    try:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in record['text']):
            return None
        actual = get_verifier_file_identity(path, MAX_AUDIO_BYTES, cancel_check)
        if any(actual[key] != record['audio'][key] for key in ('sha256','size')):
            return None
        return copy.deepcopy(record)
    except (OSError, ValueError):
        if cancel_check():
            raise ValueError('Speech verification cancelled.')
        return None


def get_pronunciation_ambiguity_regions(preparation, actual_text):
    """Scope uncertainty to actual replacement sequences, not every shared token.

    Older lexical traces lack output character offsets. When a replacement
    occurs more than once, all exact possible occurrences remain uncertain;
    unrelated occurrences of its individual tokens are not exempted.
    """
    tokens = get_comparison_tokens(actual_text)
    regions = set()
    for transform in preparation.get('transformations', []):
        if transform.get('type') != 'pronunciation_lexicon':
            continue
        for substitution in transform.get('substitutions', []):
            replacement = get_comparison_tokens(substitution.get('spoken', ''))
            if not replacement:
                continue
            for start in range(len(tokens) - len(replacement) + 1):
                if tokens[start:start + len(replacement)] == replacement:
                    regions.add((start, start + len(replacement)))
    return sorted(regions)


def _get_compact_identity(identity):
    assets = {key: {name: identity[key][name] for name in ('sha256','size')}
              for key in ('model','binary','decoder')}
    return dict(sha256=get_snapshot_digest(identity), role=identity['role'], assets=assets,
        libraries=[{'name':Path(row['path']).name,'sha256':row['sha256'],'size':row['size']}
                   for row in identity['libraries']],
        loaded_adapter_modules=copy.deepcopy(identity['loaded_adapter_modules']),
        architecture=copy.deepcopy(identity['architecture']),
        options=list(identity['options']), language=identity['language'], device=identity['device'])


def get_speech_verification_receipt(report, chunk, book_token, revision, speech_input):
    """Retain complete counts/identities and labeled bounded diagnostic excerpts.

    Exact API input is separate from authored text. Full raw report identity is
    retained without creating another unbounded on-disk history/cache.
    """
    result = dict(version=1, uid=chunk['uid'], book_token=book_token, audio_revision=revision,
        input_digest=get_speech_verification_chunk_digest(chunk),
        report_sha256=get_snapshot_digest(report), status=report['status'],
        stage=report.get('stage'), reason=report.get('reason'), seconds=report.get('seconds'),
        duration=report.get('duration'), audio=copy.deepcopy(report.get('audio')),
        expected_text=speech_input['text'] if speech_input else None,
        input_provenance=speech_input['provenance'] if speech_input else None,
        expected_sha256=hashlib.sha256(speech_input['text'].encode('utf-8')).hexdigest() if speech_input else None,
        language=speech_input['language'] if speech_input else None,
        ambiguous_expected_regions=copy.deepcopy(report.get('ambiguous_expected_regions',[])),
        short_acoustic=copy.deepcopy(report.get('short_acoustic')),
        notice='Bound to this generated take. ASR/acoustic evidence is not pronunciation certification.')
    for name in ('primary','confirmation'):
        if name not in report:
            continue
        entry = report[name]; comparison = entry['comparison']
        differences = [{key:copy.deepcopy(block[key]) for key in
            ('kind','expected_from','expected_to','observed_from','observed_to','expected','observed',
             'ambiguity','transcript_repetition')} for block in comparison['differences'][:MAX_DIAGNOSTICS]]
        result[name] = dict(identity=_get_compact_identity(entry['identity']),
            transcript_sha256=comparison['transcript_sha256'], seconds=entry['seconds'],
            expected_words=comparison['expected_words'],observed_words=comparison['observed_words'],
            matched_words=comparison['matched_words'],unmatched_expected=comparison['unmatched_expected'],
            unmatched_observed=comparison['unmatched_observed'],difference_count=len(comparison['differences']),
            differences=differences,diagnostics_abbreviated=len(comparison['differences']) > MAX_DIAGNOSTICS)
    result['corroborated_region_count'] = len(report.get('corroborated_regions', []))
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_RECEIPT_BYTES:
        raise ValueError('Speech verification receipt exceeds 128 KiB.')
    return result


def _is_digest(value):
    return isinstance(value,str) and _HEX64.fullmatch(value) is not None


def _is_finite_nonnegative(value):
    return type(value) in (int,float) and math.isfinite(value) and value >= 0


def _is_compact_pass(entry, role):
    if not isinstance(entry,dict):
        return False
    identity=entry['identity']
    if (not isinstance(identity,dict) or not _is_digest(identity['sha256']) or identity['role'] != role
            or identity['device'] != 'cpu' or not isinstance(identity['assets'],dict)
            or not _is_finite_nonnegative(entry['seconds'])):
        return False
    for name in ('model','binary','decoder'):
        asset=identity['assets'][name]
        if (not _is_digest(asset['sha256']) or type(asset['size']) is not int or asset['size'] <= 0):
            return False
    libraries=identity['libraries'];modules=identity['loaded_adapter_modules']
    if (not isinstance(libraries,list) or not 0 < len(libraries) <= 16
            or any(not isinstance(row,dict) or not isinstance(row['name'],str) or
                   not 0 < len(row['name']) <= 128 or not _is_digest(row['sha256']) or
                   type(row['size']) is not int or row['size'] <= 0 for row in libraries)
            or not isinstance(modules,dict) or not 0 < len(modules) <= 32
            or any(not isinstance(name,str) or not 0 < len(name) <= 128 or not _is_digest(value)
                   for name,value in modules.items())
            or not isinstance(identity['architecture'],dict)
            or not isinstance(identity['options'],list) or len(identity['options']) > 64
            or any(not isinstance(value,str) or len(value) > 128 for value in identity['options'])
            or not isinstance(identity['language'],str) or not 0 < len(identity['language']) <= 64):
        return False
    if not _is_digest(entry['transcript_sha256']):
        return False
    for key in ('expected_words','observed_words','matched_words','unmatched_expected','unmatched_observed'):
        if type(entry[key]) is not int or not 0 <= entry[key] <= 2048:
            return False
    if (entry['matched_words']+entry['unmatched_expected'] != entry['expected_words'] or
            entry['matched_words']+entry['unmatched_observed'] != entry['observed_words']):
        return False
    count=entry['difference_count']
    if not (type(count) is int and 0 <= count <= 32 and isinstance(entry['differences'],list)
            and len(entry['differences']) == min(count,MAX_DIAGNOSTICS)
            and type(entry['diagnostics_abbreviated']) is bool
            and entry['diagnostics_abbreviated'] == (count > MAX_DIAGNOSTICS)):
        return False
    for block in entry['differences']:
        if (not isinstance(block,dict) or block['kind'] not in ('insert','delete','replace')
                or any(type(block[key]) is not int for key in
                    ('expected_from','expected_to','observed_from','observed_to'))
                or not 0 <= block['expected_from'] <= block['expected_to'] <= entry['expected_words']
                or not 0 <= block['observed_from'] <= block['observed_to'] <= entry['observed_words']
                or any(not isinstance(block[key],str) or len(block[key]) > 160 for key in ('expected','observed'))
                or type(block['transcript_repetition']) is not bool
                or not isinstance(block['ambiguity'],list) or len(block['ambiguity']) > 3
                or any(reason not in ('numeric_normalization','pronunciation_projection','repeated_occurrence_anchor')
                       for reason in block['ambiguity'])):
            return False
    return True


def _is_receipt_schema(receipt):
    if (type(receipt['version']) is not int or receipt['version'] != 1 or receipt['status'] not in _STATUSES
            or not _is_digest(receipt['book_token']) or not _is_digest(receipt['input_digest'])
            or not _is_digest(receipt['report_sha256']) or not isinstance(receipt['stage'],str)
            or not 0 < len(receipt['stage']) <= 64
            or receipt['reason'] is not None and (not isinstance(receipt['reason'],str) or len(receipt['reason']) > 240)
            or not isinstance(receipt['audio_revision'],str) or not 0 < len(receipt['audio_revision']) <= 128):
        return False
    for name in ('seconds','duration'):
        if receipt[name] is not None and not _is_finite_nonnegative(receipt[name]):return False
    audio=receipt['audio']
    if (not isinstance(audio,dict) or set(audio) != {'sha256','size'} or type(audio['size']) is not int
            or audio['size'] <= 0 or not _is_digest(audio['sha256']) and
            not (audio['sha256'] is None and receipt['status'] == 'unavailable')):
        return False
    text=receipt['expected_text']
    regions=receipt['ambiguous_expected_regions']
    if (not isinstance(regions,(list,tuple)) or len(regions) > 2048 or
            any(not isinstance(region,(list,tuple)) or len(region) != 2 or
                any(type(value) is not int for value in region) or not 0 <= region[0] < region[1] <= 2048
                for region in regions)):
        return False
    if text is None:
        if (receipt['status'] != 'unavailable' or any(receipt[key] is not None for key in
                ('expected_sha256','input_provenance','language'))):return False
    elif (not isinstance(text,str) or not 0 < len(text) <= 20000
            or receipt['expected_sha256'] != hashlib.sha256(text.encode('utf-8')).hexdigest()
            or any(not isinstance(receipt[key],str) or not 0 < len(receipt[key]) <= bound
                   for key,bound in (('input_provenance',128),('language',64)))):
        return False
    for role in ('primary','confirmation'):
        if role in receipt and not _is_compact_pass(receipt[role],role):return False
    if receipt['status'] != 'unavailable' and 'primary' not in receipt:return False
    count=receipt['corroborated_region_count']
    if type(count) is not int or not 0 <= count <= 2048:return False
    if receipt['status'] == 'corroborated_content_difference':
        if (receipt['stage'] != 'complete' or count == 0 or 'confirmation' not in receipt or
                receipt['primary']['identity']['assets']['model']['sha256'] ==
                receipt['confirmation']['identity']['assets']['model']['sha256']):return False
    return True


def get_audio_file_version(path):
    """Return the bounded quick-stat identity used for saved-take freshness."""
    info = os.stat(path)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _is_audio_file_version(value):
    return (isinstance(value, list) and len(value) in (4, _AUDIO_FILE_VERSION_FIELDS)
            and all(type(item) is int and item >= 0 for item in value))


def _ctime_attests_change():
    # Windows exposes creation time as st_ctime, not a metadata-change time.
    # Speech verification itself is Linux-only; on Windows a saved receipt is
    # historical evidence and an explicit recheck still hashes the selected
    # take before relying on it.
    return not sys.platform.startswith('win')


def _version_matches_file(version, path):
    if not _is_audio_file_version(version) or len(version) != _AUDIO_FILE_VERSION_FIELDS:
        return False
    if not _ctime_attests_change():
        return False
    return get_audio_file_version(path) == version


def _matches_saved_input(receipt, chunk, book_token, *, failure, recheck):
    return bool(
        isinstance(receipt, dict)
        and len(json.dumps(receipt,ensure_ascii=False,allow_nan=False).encode('utf-8')) <= MAX_RECEIPT_BYTES
        and _is_receipt_schema(receipt)
        and receipt.get('uid') == chunk.get('uid')
        and receipt.get('audio_revision')
        and (failure or receipt['audio_revision'] == chunk.get('audio_revision'))
        and (book_token is None or receipt.get('book_token') == book_token)
        and not (failure and recheck)
        and receipt.get('input_digest') == get_speech_verification_chunk_digest(chunk))


def get_current_speech_verification_receipt(chunk, book_token=None, *, failure=False, recheck=False, resolve_audio=None):
    """Filter metadata; optional quick stat rejects replaced/missing selected takes.

    New receipts bind the file's ctime as well as size, inode and mtime. On
    Windows ctime is creation time, so this remains historical; explicit
    recheck hashes the selected take before using its evidence.
    """
    if failure and recheck:return None
    receipt = chunk.get('speech_verification_failure' if failure else
                        'speech_verification_recheck' if recheck else 'speech_verification')
    try:
        if not _matches_saved_input(receipt, chunk, book_token, failure=failure, recheck=recheck):
            return None
        if failure:
            if receipt['status'] != 'corroborated_content_difference':return None
        elif resolve_audio is not None:
            if not chunk.get('audio_path') or not _version_matches_file(
                    receipt.get('audio_file_version'), resolve_audio(chunk['audio_path'])):
                return None
        return copy.deepcopy(receipt)
    except (TypeError,ValueError,KeyError,OSError,OverflowError):
        return None


def _get_historical_state(chunk, book_token=None, resolve_audio=None, current_checked=False):
    """Classify saved evidence that no longer proves the current bytes."""
    receipt = chunk.get('speech_verification')
    try:
        if not _matches_saved_input(receipt, chunk, book_token, failure=False, recheck=False):
            return None
        if not _is_audio_file_version(receipt.get('audio_file_version')):
            return None
        if not current_checked and get_current_speech_verification_receipt(chunk, book_token,
                resolve_audio=resolve_audio) is not None:
            return None
        if not chunk.get('audio_path') or resolve_audio is None:
            reason = 'unavailable'
        else:
            try:
                os.stat(resolve_audio(chunk['audio_path']))
            except OSError:
                reason = 'unavailable'
            else:
                current = get_audio_file_version(resolve_audio(chunk['audio_path']))
                version = receipt['audio_file_version']
                if not _ctime_attests_change():
                    reason = 'platform'
                elif len(version) == 4:
                    reason = 'legacy' if current[:4] == version else 'changed'
                else:
                    reason = 'changed'
        return copy.deepcopy(receipt), reason
    except (TypeError,ValueError,KeyError,OSError,OverflowError):
        return None


def get_historical_speech_verification_receipt(chunk, book_token, resolve_audio):
    """Return a saved selected-take receipt that no longer proves current bytes."""
    state = _get_historical_state(chunk, book_token, resolve_audio)
    return state[0] if state is not None else None


def get_speech_verification_chunk_view(chunk, book_token, resolve_audio):
    """Detach public evidence without mutating stored rows or starting a check."""
    row=dict(chunk)
    row.pop('speech_verification_historical', None)
    historical = None
    historical_reason = None
    for field,failure,recheck in (('speech_verification',False,False),
            ('speech_verification_failure',True,False),('speech_verification_recheck',False,True)):
        if field not in row:
            continue
        row[field]=get_current_speech_verification_receipt(chunk,book_token,
            failure=failure,recheck=recheck,resolve_audio=resolve_audio)
        if field == 'speech_verification' and row[field] is None:
            state = _get_historical_state(chunk,book_token,resolve_audio,current_checked=True)
            if state is not None:
                historical, historical_reason = state
    if historical is not None:
        row['speech_verification_historical'] = historical
        row['speech_verification_historical_reason'] = historical_reason
    return row
