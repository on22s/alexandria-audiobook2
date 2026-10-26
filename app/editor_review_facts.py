"""Existing per-line review facts, without starting or certifying a check."""
import math

from audio_validation import is_audio_file_present
from speaker_traits import get_entry_speaker
from speech_verification_receipt import (get_current_speech_verification_receipt,
    get_historical_speech_verification_receipt,
    get_validated_historical_speech_verification_receipt)


def get_editor_review_facts(chunks, resolve_audio_path, book_token=None, *, include_readiness=False):
    rows = []
    for chunk in chunks:
        issues = []
        if get_entry_speaker(chunk).upper() in {'', 'UNKNOWN'}:
            issues.append('unknown')
        if chunk.get('status') == 'error':
            issues.append('failed')
        rendered = chunk.get('status') == 'done'
        audio = 'not_rendered'
        if rendered:
            audio = 'present' if is_audio_file_present(chunk.get('audio_path'), resolve_audio_path) else 'missing'
            if audio == 'missing':
                issues.append('missing')
        drift = chunk.get('drift')
        drift_status = 'not_rendered' if not rendered else 'unmeasured'
        if rendered and drift is not None:
            if not isinstance(drift, dict) or drift.get('error'):
                drift_status = 'unavailable'
            else:
                score = drift.get('score')
                if type(score) in (int, float) and math.isfinite(score) and type(drift.get('flagged')) is bool:
                    drift_status = 'flagged' if drift['flagged'] else 'measured'
                else:
                    drift_status = 'unavailable'
            if drift_status == 'flagged':
                issues.append('drift')
        speech=get_current_speech_verification_receipt(chunk,book_token,resolve_audio=resolve_audio_path)
        if speech is not None:
            historical=None
        else:
            historical=(get_historical_speech_verification_receipt(
                chunk,book_token,resolve_audio=resolve_audio_path)
                if chunk.get('speech_verification') is not None else None)
            if historical is None:
                historical=get_validated_historical_speech_verification_receipt(
                    chunk.get('speech_verification_historical'),chunk,book_token)
        rejected=get_current_speech_verification_receipt(chunk,book_token,failure=True)
        recheck=get_current_speech_verification_receipt(chunk,book_token,recheck=True,resolve_audio=resolve_audio_path)
        speech_status=speech['status'] if speech else 'not_checked'
        if historical:
            issues.append('speech_historical')
            historical_status=historical.get('status')
            if historical_status in ('review','review_confirmation_unavailable','review_short_acoustic_unavailable'):
                issues.append('speech_review')
            elif historical_status == 'unavailable':
                issues.append('speech_unavailable')
            elif historical_status == 'corroborated_content_difference':
                issues.append('speech_difference')
        if rejected:
            issues.append('speech_rejected')
        if speech_status in ('review','review_confirmation_unavailable','review_short_acoustic_unavailable'):
            issues.append('speech_review')
        elif speech_status == 'unavailable':
            issues.append('speech_unavailable')
        elif speech_status == 'corroborated_content_difference':
            issues.append('speech_difference')
        if recheck and 'speech_unavailable' not in issues:
            issues.append('speech_unavailable')
        row = {'id': chunk['id'], 'uid': chunk['uid'], 'issues': issues,
                     'audio': audio, 'drift': drift_status,'speech':speech_status,
                     'speech_attempt':'rejected' if rejected else None,
                     'speech_recheck':recheck['status'] if recheck else None}
        if include_readiness:
            # Reuse the receipt filter above: an old or malformed receipt is
            # not a new check, and metadata currency is not a quality pass.
            row['speech_evidence'] = ('current_metadata' if speech else
                'not_current' if historical is not None or chunk.get('speech_verification') is not None
                else 'not_checked')
        rows.append(row)
    return rows
