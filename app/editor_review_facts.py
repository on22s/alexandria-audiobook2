"""Existing per-line review facts, without starting or certifying a check."""
import math

from audio_validation import is_audio_file_present
from speaker_traits import get_entry_speaker
from speech_verification_receipt import get_current_speech_verification_receipt


def get_editor_review_facts(chunks, resolve_audio_path, book_token=None):
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
        rejected=get_current_speech_verification_receipt(chunk,book_token,failure=True)
        recheck=get_current_speech_verification_receipt(chunk,book_token,recheck=True,resolve_audio=resolve_audio_path)
        speech_status=speech['status'] if speech else 'not_checked'
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
        rows.append({'id': chunk['id'], 'uid': chunk['uid'], 'issues': issues,
                     'audio': audio, 'drift': drift_status,'speech':speech_status,
                     'speech_attempt':'rejected' if rejected else None,
                     'speech_recheck':recheck['status'] if recheck else None})
    return rows
