"""Existing per-line review facts, without starting or certifying a check."""
import math

from audio_validation import is_audio_file_present
from speaker_traits import get_entry_speaker


def get_editor_review_facts(chunks, resolve_audio_path):
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
        rows.append({'id': chunk['id'], 'uid': chunk['uid'], 'issues': issues,
                     'audio': audio, 'drift': drift_status})
    return rows
