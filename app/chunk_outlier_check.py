"""Currency contracts for advisory CPU acoustic evidence."""
import math
import json
import ntpath
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event
from editor_poll_snapshot import get_snapshot_digest
from chunk_outliers import OUTLIER_LABELS, OUTLIER_METRICS

MAX_SCAN_CHUNKS = 2000
MAX_SCAN_SECONDS = 600
MAX_SCAN_WORKERS = 4


class OutlierCheckError(ValueError):
    """An actionable refusal safe to display without local file diagnostics."""


class OutlierCheckCancelled(RuntimeError):
    pass


def get_outlier_voice_config(root):
    from pathlib import Path

    def reject_constant(value):
        raise ValueError('Nonfinite JSON constant')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    try:
        path = Path(root) / 'voice_config.json'
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {}
        voices = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
        if not isinstance(voices, dict):
            raise ValueError('Voice configuration must be an object')
        return voices
    except (ValueError, UnicodeError, OSError):
        raise OutlierCheckError('Cannot read voice_config.json; restore or repair it before checking.') from None


def check_chunk_outliers(snapshot, check_active, progress=None, *, decode=None):
    from chunk_outliers import decode_take_acoustics, get_outlier_scores
    from speaker_traits import get_entry_speaker
    from tts import get_resolved_speaker
    from voice_drift import get_drift_voice_groups
    decode = decode or decode_take_acoustics
    chunks, voices = snapshot['chunks'], snapshot['book']['voices']
    targets = [index for index, chunk in enumerate(chunks)
               if chunk.get('status') == 'done' and chunk.get('audio_path')]
    if len(chunks) > MAX_SCAN_CHUNKS:
        raise OutlierCheckError(f'Outlier check supports at most {MAX_SCAN_CHUNKS} saved lines; '
                                'split the book before checking. No results saved.')
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    stopped = Event()

    def ensure_active():
        if stopped.is_set():
            raise OutlierCheckCancelled('Outlier check stopped; no results saved.')
        check_active()
        if time.monotonic() >= deadline:
            raise OutlierCheckError(f'Outlier check exceeded its {MAX_SCAN_SECONDS}-second deadline; '
                                    'split the book into smaller books. No results saved.')

    ensure_active()
    _groups, voice_keys = get_drift_voice_groups(chunks, voices)
    groups, measurements = {}, {}
    root = os.path.realpath(snapshot['root'])
    def measure(index):
        ensure_active()
        chunk = chunks[index]
        relative = chunk['audio_path']
        unavailable = {'error': 'Audio is missing or outside the selected book.'}
        if (not isinstance(relative, str) or '\x00' in relative or
                ntpath.splitdrive(relative)[0] or relative.startswith(('/', '\\'))):
            return unavailable
        candidate = os.path.abspath(os.path.join(root, relative.replace('\\', '/')))
        if os.path.commonpath([root, candidate]) != root or candidate == root:
            return unavailable
        path = os.path.realpath(candidate)
        try:
            contained = os.path.commonpath([root, path]) == root and path != root
        except ValueError:
            contained = False
        try:
            if not contained or not os.path.isfile(path):
                return unavailable
            before = os.stat(path)
            measurement = decode(path, check_active=ensure_active)
            after = os.stat(path)
        except FileNotFoundError:
            return unavailable
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise OutlierCheckError('A take changed during the outlier check; no results saved.')
        ensure_active()
        return measurement

    pending = {}
    remaining = iter(targets)
    with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as pool:
        try:
            for index in targets[:MAX_SCAN_WORKERS]:
                pending[pool.submit(measure, next(remaining))] = index
            while pending:
                ensure_active()
                completed, _pending = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = pending.pop(future)
                    measurements[index] = future.result()
                    if progress:
                        progress(len(measurements), len(targets))
                    next_index = next(remaining, None)
                    if next_index is not None:
                        pending[pool.submit(measure, next_index)] = next_index
        finally:
            stopped.set()
            for future in pending:
                future.cancel()
    stopped.clear()
    for index in targets:
        character = get_resolved_speaker(get_entry_speaker(chunks[index]), voices)
        groups.setdefault((character, voice_keys[index]), []).append(index)
    results = {}
    for members in groups.values():
        ensure_active()
        scores = get_outlier_scores([measurements[index] for index in members])
        for index, score in zip(members, scores):
            results[index] = dict(score, index=index, uid=chunks[index]['uid'])
    ensure_active()
    return [results[index] for index in targets]


def get_outlier_population_key(chunks):
    from project import get_chunk_generation_inputs
    fields = ("uid", "id", "status", "audio_path", "audio_revision")
    return get_snapshot_digest([{**get_chunk_generation_inputs(chunk),
                                 **{field: chunk[field] for field in fields if field in chunk}}
                                for chunk in chunks])


def get_outlier_snapshot(project_manager, expected_book_token):
    import copy
    import os
    from book_state_transaction import ensure_book_state, get_book_operation_token, get_book_snapshot
    from utils import file_lock
    root = os.path.realpath(project_manager.root_dir)
    with ensure_book_state(root), project_manager._chunks_lock, file_lock(project_manager.chunks_path):
        if os.path.realpath(project_manager.root_dir) != root or get_book_operation_token(root) != expected_book_token:
            raise ValueError('Outlier active book changed')
        chunks = copy.deepcopy(project_manager._read_chunks())
        try:
            book = get_book_snapshot(root, include_voices=False)
            book['voices'] = get_outlier_voice_config(root)
        except OutlierCheckError:
            raise
        except FileNotFoundError:
            raise OutlierCheckError('Book inputs are missing; restore or import the annotated script before checking.') from None
        except (ValueError, UnicodeError):
            raise OutlierCheckError('Book inputs are invalid; repair state.json or voice_config.json before checking.') from None
        return {'root': root, 'book': book, 'chunks': chunks,
                'population_key': get_outlier_population_key(chunks)}


def get_outlier_fact(chunk, population_key, voices_key=None):
    if chunk.get("status") != "done":
        return {"status": "not_rendered"}
    evidence = chunk.get("outlier")
    if evidence is None:
        return {"status": "unmeasured"}
    if not isinstance(evidence, dict) or evidence.get("population_key") != population_key:
        return {"status": "unavailable", "error": "Missing or stale population evidence"}
    if evidence.get('voices_key') is not None and evidence['voices_key'] != voices_key:
        if voices_key is None:
            return {'status': 'unavailable', 'error': 'Voice configuration unavailable; restore or repair it before checking.'}
        return {'status': 'unavailable', 'error': 'Voice configuration changed; repeat the outlier check.'}
    records = evidence.get("metrics")
    records = records if isinstance(records, dict) else {}
    usable, positive = [], False
    for metric in OUTLIER_METRICS:
        record = records.get(metric)
        valid = (isinstance(record, dict) and
                 all(type(record.get(field)) in (int, float) and math.isfinite(record[field])
                     for field in ("value", "median", "spread", "z")) and
                 record["spread"] > 0 and type(record.get("flagged")) is bool)
        usable.append(valid)
        positive = positive or (valid and record["flagged"])
    missing = [metric for metric, valid in zip(OUTLIER_METRICS, usable) if not valid]
    status = "flagged" if positive else "measured" if all(usable) and not evidence.get("error") else "unavailable"
    return {"status": status, "metrics": records,
            "missing": missing, "error": evidence.get("error")}
