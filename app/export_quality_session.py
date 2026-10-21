"""Bounded session evidence; reading it never probes or decodes a delivery."""
import copy
from itertools import islice
import math
import os
from pathlib import Path
import time

from export_quality import MAX_DIAGNOSTICS, get_export_file_version

REPORT_LIMIT = 32
REJECTION_LIMIT = 16
TASKS = ('audio', 'm4b_export', 'chapter_export', 'audacity_export')
STATE_KEYS = ('export_quality_reports', 'export_quality_rejections', 'export_quality_attempt')
FIELDS = ('version', 'status', 'stage', 'reason', 'format', 'expected_format',
    'verification_scope', 'file', 'file_version', 'sha256', 'output_bytes', 'plan_sha256',
    'expected_frames', 'decoded_frames', 'sample_rate', 'channels', 'codec',
    'source_shape', 'options', 'duration_delta_seconds', 'duration_tolerance_seconds',
    'silence_intervals', 'unexpected_silence_count', 'unexpected_silence_seconds',
    'unexpected_silence_ranges', 'longest_unexpected_silence', 'diagnostics',
    'diagnostics_abbreviated', 'chapters_checked', 'tags_checked', 'tracks_checked',
    'members_checked', 'track_summaries', 'track_summaries_abbreviated', 'seconds', 'member')


def _bounded(value, depth=0):
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:1024]
    if depth < 3 and isinstance(value, list):
        return [_bounded(item, depth + 1) for item in value[:MAX_DIAGNOSTICS]]
    if depth < 3 and isinstance(value, dict):
        return {str(key)[:80]: _bounded(item, depth + 1)
                for key, item in islice(value.items(), MAX_DIAGNOSTICS)}
    return None


def get_report_summary(report):
    return {key: _bounded(report[key]) for key in FIELDS if key in report}


class ExportQualityAttempt:
    """One admitted worker records successes independently from failed attempts."""
    def __init__(self, state, book_token, options=None, *, audacity=False):
        self.state, self.book_token = state, book_token
        self.attempt = dict(book_token=book_token, status='running', completed_files=0,
                            started_at=time.time())
        state['export_quality_attempt'] = self.attempt
        self.kwargs = {'export_quality_callback': self.publish}
        if not audacity and options is not None:
            self.kwargs['export_quality'] = options

    def publish(self, report):
        if report.get('status') not in ('verified', 'review'):
            raise ValueError('Only completed export evidence can be published.')
        summary = dict(get_report_summary(report), book_token=self.book_token)
        # Replace the previous receipt for this book/file without clearing other
        # successfully published deliveries on a later failure or cancellation.
        reports = [item for item in self.state.get('export_quality_reports', [])
                   if (item['book_token'], item.get('file')) != (self.book_token, summary.get('file'))]
        self.state['export_quality_reports'] = (reports + [summary])[-REPORT_LIMIT:]
        self.attempt = {**self.attempt, 'completed_files': self.attempt['completed_files'] + 1}
        self.state['export_quality_attempt'] = self.attempt

    def finish(self, success, message):
        status = 'done' if success else ('cancelled' if message in ('Export cancelled', 'Merge cancelled') else 'failed')
        self.attempt = {**self.attempt, 'status': status}
        self.state['export_quality_attempt'] = self.attempt
        if status == 'failed':
            self._reject({'status': 'failed', 'stage': 'export', 'reason': str(message)[:240]})

    def fail(self, error):
        self.attempt = {**self.attempt, 'status': 'failed'}
        self.state['export_quality_attempt'] = self.attempt
        report = get_report_summary(getattr(error, 'report', {}))
        report.update(status='failed', reason=str(error)[:240])
        self._reject(report)

    def _reject(self, report):
        rejected = self.state.get('export_quality_rejections', [])
        self.state['export_quality_rejections'] = (rejected + [dict(report,
            book_token=self.book_token, attempted_at=self.attempt['started_at'])])[-REJECTION_LIMIT:]


def get_session_evidence(root, states, book_token):
    """Metadata invalidation only: SHA values describe the completed check.

    This does not rehash audio or promise detection of edits that preserve all
    filesystem metadata. Export publication itself uses complete byte proofs.
    """
    reports, rejected, attempts = [], [], []
    unavailable = 0
    for name in TASKS:
        state = states[name]
        captured = copy.deepcopy({key: state.get(key) for key in STATE_KEYS})
        attempt = captured['export_quality_attempt']
        if attempt and attempt.get('book_token') == book_token:
            attempts.append(dict(attempt, task=name))
        rejected.extend(dict(item, task=name) for item in captured['export_quality_rejections'] or []
                        if item.get('book_token') == book_token)
        for report in captured['export_quality_reports'] or []:
            if report.get('book_token') != book_token:
                continue
            try:
                relative = report['file']
                if (not isinstance(relative, str) or '\\' in relative or '\0' in relative
                        or Path(relative).is_absolute() or '..' in Path(relative).parts):
                    raise ValueError('Unsafe export filename.')
                path = os.path.join(root, relative)
                if os.path.commonpath((os.path.realpath(root), os.path.realpath(path))) != os.path.realpath(root):
                    raise ValueError('Export escaped its book.')
                if get_export_file_version(path) != report['file_version']:
                    raise ValueError('Completed output metadata changed.')
                # An overlapping completion must not resurrect a replaced receipt.
                if report not in state.get('export_quality_reports', []):
                    raise ValueError('Export receipt was replaced.')
            except (OSError, ValueError, KeyError, TypeError):
                unavailable += 1
                continue
            reports.append(dict(report, task=name))
    return dict(book_token=book_token, reports=reports, rejected_attempts=rejected,
        attempts=attempts, unavailable=unavailable, identity_check='metadata_only',
        reports_per_task_limit=REPORT_LIMIT, rejections_per_task_limit=REJECTION_LIMIT,
        history_scope='app_session')
