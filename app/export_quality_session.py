"""Bounded session evidence; reading it never probes or decodes a delivery."""
import copy
import hashlib
import json
from itertools import islice
import logging
import math
import os
import stat
from pathlib import Path
import time

from export_quality import MAX_DIAGNOSTICS, get_export_file_version
from project_scope_files import MAX_SCRIPT_BYTES
from script_selection_binding import BINDING_NAME, MAX_BINDING_BYTES

logger = logging.getLogger('AlexandriaUI')
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
    'members_checked', 'track_summaries', 'track_summaries_abbreviated', 'seconds', 'member',
    'input_revision', 'input_binding', 'attempt_started_at', 'source_scope', 'skipped_chunks')


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


# One take entry per chunk row; the row cap below is the coherent bound.
_MAX_INPUT_TAKES = 10000
_MAX_INPUT_ROWS = 10000
_MAX_SCRIPT_CAPTURE = MAX_SCRIPT_BYTES
_MAX_METADATA_CAPTURE = 64 * 1024**2


def get_export_input_revision(root, book_token, capture=None):
    """Pure read: bound the active input revision without decoding audio.

    Returns ``{'revision': hex|None, 'scope': 'full'|'scoped'|'unknown',
    'scope_sha256': hex|None, 'reason': str, 'script_sha256': hex|None,
    'voice_sha256': hex|None}``. ``revision is None`` is explicit unbound
    evidence (caller must never certify current input from it); this helper
    never raises for capture/parsing failure and never weakens export
    validation. The caller holds the existing book lock.

    ``capture`` is an optional constructor-boundary callback
    ``capture(relative, limit, include_bytes) -> (info, raw)`` with the same
    shape as ``capture_scope_file``; when None the local bounded authority
    is used. No core import happens here. Script/voice/binding/chunk/journal
    identities are exact file hashes (streaming where bytes are not needed);
    per-take evidence is filesystem ``[dev, ino, size, mtime_ns, ctime_ns]``
    metadata only — no opening, decoding or hashing of take audio, and no
    full output rehash. Metadata identity does not prove content if an edit
    preserves all metadata; that limit is disclosed in overview evidence.
    The component ``script_sha256``/``voice_sha256`` digests are the already
    captured fenced raw-byte hashes, exposed so callers need not re-read
    and re-hash the active script/voice (efficiency-4).
    """
    unavailable = {'revision': None, 'scope': 'unknown', 'scope_sha256': None,
                   'script_sha256': None, 'voice_sha256': None,
                   'reason': 'Input revision could not be captured; treat export evidence as unbound.'}
    if not isinstance(book_token, str) or not book_token or root is None:
        return unavailable
    try:
        root_path = Path(root)
    except (TypeError, ValueError):
        return unavailable
    if capture is not None and not callable(capture):
        return unavailable

    def _capture(relative, limit, include_bytes=False):
        if capture is not None:
            return capture(relative, limit, include_bytes)
        from project_scope_files import capture_scope_file
        return capture_scope_file(str(root_path), relative, limit=limit,
                                  include_bytes=include_bytes)

    def _file_sha(relative, limit):
        try:
            info, _raw = _capture(relative, limit, False)
        except (OSError, ValueError, TypeError, RecursionError):
            return 'unavailable'
        if not info.get('exists'):
            return None
        sha = info.get('sha256')
        return sha if isinstance(sha, str) else 'unavailable'

    try:
        state_info, state_raw = _capture('state.json', _MAX_METADATA_CAPTURE, True)
        if not state_info.get('exists') or state_raw is None:
            return unavailable
        else:
            state = json.loads(state_raw)
        if not isinstance(state, dict):
            return unavailable
        if '_source_scope' not in state:
            scope, scope_sha = 'full', None
        else:
            scope = 'scoped'
            try:
                scope_raw = json.dumps(state['_source_scope'], sort_keys=True,
                                       ensure_ascii=False, separators=(',', ':'),
                                       allow_nan=False).encode('utf-8')
            except (TypeError, ValueError, RecursionError):
                return unavailable
            scope_sha = hashlib.sha256(scope_raw).hexdigest()
    except (OSError, ValueError, TypeError, RecursionError):
        return unavailable

    script_sha = _file_sha('annotated_script.json', _MAX_SCRIPT_CAPTURE)
    voice_sha = _file_sha('voice_config.json', _MAX_SCRIPT_CAPTURE)
    binding_sha = _file_sha(BINDING_NAME, MAX_BINDING_BYTES)
    if script_sha is None or any(value == 'unavailable' for value in (script_sha, voice_sha, binding_sha)):
        return unavailable
    try:
        chunks_info, chunks_raw = _capture('chunks.json', _MAX_METADATA_CAPTURE, True)
        if chunks_raw is None:
            return unavailable
        chunks_sha = chunks_info.get('sha256') if chunks_info.get('exists') else None
        journal_info, journal_raw = _capture('chunks.json.status.jsonl',
                                             _MAX_METADATA_CAPTURE, True)
        journal_sha = journal_info.get('sha256') if journal_info.get('exists') else None
    except (OSError, ValueError, TypeError, RecursionError):
        return unavailable

    try:
        from chunk_status_journal import ChunkStatusJournal
        identity = {key: state.get(key) for key in ('active_book_id', 'book_generation')}
        rows = ChunkStatusJournal(str(root_path / 'chunks.json'), identity).get_rows_from_bytes(
            chunks_raw, journal_raw if (journal_info.get('exists') if isinstance(journal_info, dict) else False) else None)
    except (OSError, ValueError, TypeError, RecursionError, AttributeError):
        return unavailable
    if not isinstance(rows, list) or len(rows) > _MAX_INPUT_ROWS:
        return unavailable
    if any(not isinstance(row, dict) for row in rows):
        return unavailable

    takes = []
    try:
        from audio_take_history import _version
        root_real = os.path.realpath(root_path)
        for row in rows:
            audio_path = row.get('audio_path')
            if not isinstance(audio_path, str) or not audio_path:
                entry = [None, None]
            elif ('\\' in audio_path or '\0' in audio_path
                    or Path(audio_path).is_absolute() or '..' in Path(audio_path).parts):
                entry = [audio_path, None]
            else:
                candidate = root_path / audio_path
                try:
                    info = os.lstat(candidate)
                    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        entry = [audio_path, None]
                    elif os.path.commonpath((root_real, os.path.realpath(candidate))) != root_real:
                        entry = [audio_path, None]
                    else:
                        entry = [audio_path, list(_version(info))]
                except OSError:
                    entry = [audio_path, None]
            takes.append(entry)
            if len(takes) > _MAX_INPUT_TAKES:
                return unavailable
    except (OSError, ValueError, TypeError, RecursionError):
        return unavailable
    canonical_rows = sorted(
        [{key: row.get(key) for key in ('uid', 'status', 'audio_path', 'audio_revision')}
         for row in rows],
        key=lambda item: str(item.get('uid')))
    payload = {'book_token': book_token, 'script': script_sha, 'voice': voice_sha,
               'binding': binding_sha, 'chunks': chunks_sha, 'journal': journal_sha,
               'scope': scope, 'scope_sha': scope_sha,
               'rows': canonical_rows, 'takes': takes}
    try:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError):
        return unavailable
    return {'revision': hashlib.sha256(raw).hexdigest(), 'scope': scope,
            'scope_sha256': scope_sha, 'reason': '',
            'script_sha256': script_sha if isinstance(script_sha, str) else None,
            'voice_sha256': voice_sha if isinstance(voice_sha, str) else None}


class ExportQualityAttempt:
    """One admitted worker records successes independently from failed attempts."""
    def __init__(self, state, book_token, options=None, *, audacity=False,
                 data_dir=None, capture=None):
        self.state, self.book_token = state, book_token
        self.data_dir, self.capture = data_dir, capture
        started_at = time.time()
        self.started_at = started_at
        self.start_revision = None
        input_binding, source_scope = 'unbound', 'unknown'
        if data_dir is not None:
            try:
                info = get_export_input_revision(data_dir, book_token, capture)
            except Exception:
                logger.warning('Export input revision capture failed', exc_info=True)
                info = None
            if info is not None and info.get('revision') is not None:
                self.start_revision = info['revision']
                input_binding = 'current'
                source_scope = info.get('scope', 'unknown')
            else:
                input_binding = 'unavailable'
        self.attempt = dict(book_token=book_token, status='running', completed_files=0,
                            started_at=started_at,
                            input_revision=self.start_revision,
                            input_binding=input_binding, source_scope=source_scope)
        state['export_quality_attempt'] = self.attempt
        self.kwargs = {'export_quality_callback': self.publish}
        if not audacity and options is not None:
            self.kwargs['export_quality'] = options

    def _capture_bound(self):
        """Recapture the input revision without nested book admission (e-8).

        The caller already holds book admission on the export path; taking
        the non-reentrant lock again would deadlock. Reuse the live
        admission when present, otherwise take it once with recover=False
        and recheck the book token inside it. Any failure is explicit
        unbound evidence, never ``current``.
        """
        from contextlib import nullcontext
        from book_state_transaction import (ensure_book_state,
                                            get_book_operation_token,
                                            is_book_state_admitted)
        scope = (nullcontext() if is_book_state_admitted(self.data_dir)
                 else ensure_book_state(self.data_dir, recover=False))
        with scope:
            return (get_export_input_revision(self.data_dir, self.book_token, self.capture)
                    if get_book_operation_token(self.data_dir) == self.book_token else None)

    def publish(self, report):
        if report.get('status') not in ('verified', 'review'):
            raise ValueError('Only completed export evidence can be published.')
        summary = dict(get_report_summary(report), book_token=self.book_token)
        # Bind to the start input only if it is unchanged at publication.
        # Failed/unknown capture is explicit unbound evidence; publish
        # never raises for capture failure and never reclassifies the
        # export-quality verdict itself.
        # Chapter/Audacity deliveries never certify full-book completion,
        # so they skip the per-output recapture (altitude-4) and stay
        # explicitly historical instead of paying a full revision capture
        # per chapter.
        full_book = summary.get('file') in ('cloned_audiobook.mp3', 'audiobook.m4b')
        if self.data_dir is None or not full_book:
            summary.update(input_revision=None, input_binding='unbound',
                           attempt_started_at=self.started_at, source_scope='unknown')
        else:
            bound = None
            try:
                bound = self._capture_bound()
            except Exception:
                logger.warning('Export input revision capture failed', exc_info=True)
                bound = None
            if (bound is not None and self.start_revision is not None
                    and bound.get('revision') == self.start_revision):
                summary.update(input_revision=self.start_revision,
                               input_binding='current',
                               attempt_started_at=self.started_at,
                               source_scope=bound.get('scope', 'unknown'))
            elif bound is None or bound.get('revision') is None or self.start_revision is None:
                summary.update(input_revision=None, input_binding='unavailable',
                               attempt_started_at=self.started_at, source_scope='unknown')
            else:
                summary.update(input_revision=None, input_binding='mismatch',
                               attempt_started_at=self.started_at,
                               source_scope=bound.get('scope', 'unknown'))
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
