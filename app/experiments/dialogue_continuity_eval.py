"""Bounded, resumable baseline/deterministic continuity evaluation.

This is a CPU dry-run framework and the adapter boundary for later native work.
It reuses the existing continuity planner, voice/seed materialization and
blinded-listening validators. It does not render natively, score voices, run
ASR, or claim that #1258 is complete.

Run from app/:
  python -m experiments.dialogue_continuity_eval run --dry-run ...
  python -m experiments.dialogue_continuity_eval package ...
  python -m experiments.dialogue_continuity_eval import ...
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
import wave

from audio_validation import GeneratedAudioCancelled, GeneratedAudioError, validate_generated_audio
from experiments.blinded_listening import ListeningPackageError
from experiments.provenance import file_sha256, provenance
from experiments.run_dialogue_continuity import get_continuity_output_path
from experiments.plan_dialogue_continuity import get_continuity_experiment_packet
from experiments.render_dialogue_continuity import get_continuity_voice_inputs
from project_scope_files import capture_scope_file
from source_upload_publication import move_source_upload_exclusively
from synthesis_recovery_identity import get_recovery_digest
from utils import atomic_json_write, is_path_inside, secure_filename
from word_timing import MAX_AUDIO_BYTES

VERSION = 1
MAX_PAIRS = 128
MAX_LISTENERS = 1024
MAX_JSON_BYTES = 4 * 1024**2
MAX_RUN_BYTES = 16 * 1024**2
MAX_STATE_BYTES = 16 * 1024**2
ARMS = ('baseline', 'deterministic')
CATEGORIES = ('input_fidelity', 'content_verification', 'voice_scoring', 'human_preference')


class ContinuityEvaluationError(ValueError):
    """A bounded experiment refused unsafe or incomplete input."""


class ContinuityEvaluationCancelled(ContinuityEvaluationError):
    """The caller cancelled between owned units; no partial pair is published."""


def _read_bytes(path, limit=MAX_JSON_BYTES):
    facts, raw = capture_scope_file(Path(path).parent.resolve(), Path(path).name,
                                    limit=limit, include_bytes=True)
    if raw is None:
        raise ContinuityEvaluationError(f'input is missing: {path}')
    return raw, facts


def _read_json(path, limit=MAX_JSON_BYTES):
    raw, facts = _read_bytes(path, limit)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ContinuityEvaluationError(f'invalid JSON: {path}') from error
    return value, raw, facts


def _get_source_revision(root):
    root = Path(root).resolve()
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root,
                                       text=True).strip()
        status = subprocess.check_output(
            ['git', 'status', '--porcelain', '--untracked-files=all'],
            cwd=root, text=True)
        dirty = bool(status.strip())
        revision = {'head': head, 'dirty': dirty}
        if dirty:
            diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'],
                                           cwd=root)
            untracked = subprocess.check_output(
                ['git', 'ls-files', '--others', '--exclude-standard'],
                cwd=root, text=True).splitlines()
            digest = hashlib.sha256(diff)
            for relative in untracked:
                path = (root / relative).resolve()
                if not is_path_inside(str(path), str(root)) or not path.is_file():
                    continue
                digest.update(relative.encode())
                with path.open('rb') as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(block)
            revision['worktree_sha256'] = digest.hexdigest()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ContinuityEvaluationError('source revision is unavailable') from error
    return revision


def _write_json(path, value):
    atomic_json_write(value, str(path))


def _write_new_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.building-', dir=str(path.parent))
    os.close(fd)
    try:
        _write_json(temporary, value)
        _publish_new_file(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_cancel(cancel_check):
    if cancel_check is not None and cancel_check():
        raise ContinuityEvaluationCancelled('continuity evaluation cancelled')


def _confined_path(base, relative, label):
    if not isinstance(relative, str) or not relative:
        raise ContinuityEvaluationError(f'{label} path is missing')
    parts = Path(relative)
    if parts.is_absolute() or '..' in parts.parts:
        raise ContinuityEvaluationError(f'{label} escapes its run directory')
    current = Path(base)
    for part in parts.parts:
        current = current / part
        if current.is_symlink():
            raise ContinuityEvaluationError(f'{label} path contains a symlink')
    path = (Path(base) / relative).resolve()
    if not is_path_inside(str(path), str(base)):
        raise ContinuityEvaluationError(f'{label} escapes its run directory')
    return path


def _get_rows(plan, selected_rows):
    rows = plan['plan']['rows']
    if selected_rows is None:
        selected_rows = list(range(len(rows)))
    if (not isinstance(selected_rows, (list, tuple)) or not selected_rows
            or len(selected_rows) > MAX_PAIRS or len(set(selected_rows)) != len(selected_rows)
            or any(type(index) is not int or not 0 <= index < len(rows) for index in selected_rows)):
        raise ContinuityEvaluationError(
            f'choose between 1 and {MAX_PAIRS} distinct valid rows')
    return [(index, rows[index]) for index in selected_rows]


def get_evaluation_identity(source_raw, voices_raw, config_raw, plan, selected_rows,
                            source_revision=None):
    rows = _get_rows(plan, selected_rows)
    return {
        'version': VERSION,
        'evaluator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_revision': copy.deepcopy(source_revision),
        'source_sha256': hashlib.sha256(source_raw).hexdigest(),
        'voices_sha256': hashlib.sha256(voices_raw).hexdigest(),
        'config_sha256': hashlib.sha256(config_raw).hexdigest(),
        'planner_loaded_code_sha256': plan['planner_loaded_code_sha256'],
        'plan_source_digest': plan['plan']['source_digest'],
        'selected_rows': [index for index, _ in rows],
    }


def get_pair_input_digest(row, voices, config):
    return get_recovery_digest({'row': row, 'voices': voices, 'config': config})


def _get_fidelity_sha256(state):
    """Bind the complete checkpoint, including materialized synthesis inputs."""
    if not isinstance(state, dict):
        raise ContinuityEvaluationError('pair fidelity evidence is invalid')
    try:
        return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True,
                                         allow_nan=False).encode('utf-8')).hexdigest()
    except (TypeError, ValueError) as error:
        raise ContinuityEvaluationError('pair fidelity evidence is invalid') from error


def _validate_pair_state(state, plan, identity, index):
    if not isinstance(state, dict) or state.get('version') != VERSION:
        raise ContinuityEvaluationError(f'run pair {index} state is malformed')
    if (state.get('row_index') != index or state.get('uid') != plan['uid']
            or state.get('speaker') != plan['speaker']):
        raise ContinuityEvaluationError(f'run pair {index} row identity differs from the plan')
    if (state.get('config_sha256') != identity.get('config_sha256')
            or state.get('voices_sha256') != identity.get('voices_sha256')
            or state.get('source_revision') != identity.get('source_revision')):
        raise ContinuityEvaluationError(f'run pair {index} source/config/voices differ from the run')
    requested = state.get('requested') if isinstance(state.get('requested'), dict) else {}
    for arm in ARMS:
        saved_request = requested.get(arm) if isinstance(requested.get(arm), dict) else {}
        expected_request = plan['arms'][arm]
        if (saved_request.get('text') != expected_request['text']
                or saved_request.get('instruct') != expected_request.get('instruct')):
            raise ContinuityEvaluationError(f'run pair {index} saved request differs from the plan')


@contextmanager
def _capture_audio_snapshot(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
        raise ContinuityEvaluationError('continuity audio is missing or exceeds its size limit')
    raw, facts = _read_bytes(path, MAX_AUDIO_BYTES)
    # Decode the bounded captured bytes, not a path that can change after stat.
    with tempfile.TemporaryDirectory(prefix='aa2-continuity-validation-') as temporary:
        snapshot = Path(temporary) / 'audio.wav'
        snapshot.write_bytes(raw)
        yield snapshot, facts
    after, _ = capture_scope_file(path.parent.resolve(), path.name, limit=MAX_AUDIO_BYTES)
    if (not after['exists'] or after['sha256'] != facts['sha256']
            or after['size'] != facts['size']):
        raise ContinuityEvaluationError('continuity audio changed during validation')


def _wav_facts(path, cancel_check=None):
    with _capture_audio_snapshot(path) as (snapshot, facts):
        validate_generated_audio(str(snapshot), 'continuity dry arm', cancel_check=cancel_check)
        with wave.open(str(snapshot), 'rb') as handle:
            frames = handle.getnframes()
            if not frames:
                raise ContinuityEvaluationError('dry arm wrote no frames')
    return {'sha256': facts['sha256'], 'size': facts['size'], 'frames': frames}


def _write_dry_wav(path, payload, sample_rate=16000):
    """A deterministic non-silent CPU fixture; never a speech-quality claim."""
    frame_count = max(160, min(1600, len(payload) * 4))
    seed = int.from_bytes(hashlib.sha256(payload).digest()[:2], 'little')
    frames = bytearray()
    for index in range(frame_count):
        value = ((index + seed) * 97) % 2000 - 1000
        frames += int(value).to_bytes(2, 'little', signed=True)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return path


def dry_render_pair(row, voices, seeds, destination, cancel_check=None):
    """Render a deterministic CPU pair with the same submitted text as the plan."""
    destination = Path(destination)
    arms = {}
    for arm in ARMS:
        _check_cancel(cancel_check)
        request = copy.deepcopy(row[arm])
        path = destination / (arm + '.wav')
        _write_dry_wav(path, f"{row['uid']}:{arm}:{request['text']}:{seeds}".encode())
        arm_facts = _wav_facts(path, cancel_check)
        arms[arm] = {'request': request, 'submitted_text': request['text'],
                     'audio': {key: arm_facts[key] for key in ('sha256', 'size')},
                     'path': path.name}
    return arms


def _load_run(path):
    run_path = Path(path) / 'run.json'
    if not run_path.is_file():
        raise ContinuityEvaluationError('run manifest is missing')
    value, _, _ = _read_json(run_path, MAX_RUN_BYTES)
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise ContinuityEvaluationError('unsupported run manifest')
    return value


def _verify_completed_pair(pair_dir, pair, pair_input_digest):
    pair_dir = Path(pair_dir)
    state_path = pair_dir / 'state.json'
    if not state_path.is_file():
        raise ContinuityEvaluationError('completed pair has no state')
    state, _, _ = _read_json(state_path, MAX_STATE_BYTES)
    if (not isinstance(state, dict) or state.get('version') != VERSION
            or state.get('status') != 'complete' or state.get('input_digest') != pair_input_digest):
        raise ContinuityEvaluationError('saved pair inputs changed; refusing reuse')
    for arm in ARMS:
        item = state.get('arms', {}).get(arm)
        if (not isinstance(item, dict) or not isinstance(item.get('audio'), dict)
                or not isinstance(item.get('path'), str)):
            raise ContinuityEvaluationError('saved pair arm is incomplete')
        arm_path = _confined_path(pair_dir, item['path'], 'saved pair arm')
        if not arm_path.is_file():
            raise ContinuityEvaluationError('saved pair arm is incomplete')
        try:
            actual = _wav_facts(arm_path)['sha256']
        except Exception as error:
            raise ContinuityEvaluationError('saved pair audio changed or is invalid; refusing reuse') from error
        if actual != item['audio'].get('sha256'):
            raise ContinuityEvaluationError('saved pair audio changed; refusing reuse')
    if pair.get('fidelity_sha256') != _get_fidelity_sha256(state):
        raise ContinuityEvaluationError('saved pair fidelity evidence changed; refusing reuse')
    return state


def run_collection(source_path, voices_path, config_path, output_dir, *,
                   selected_rows=None, render_pair=dry_render_pair,
                   cancel_check=None, source_root=None, source_revision=None):
    """Run a bounded fixed collection, resuming only matching completed pairs."""
    source_raw, _ = _read_bytes(source_path)
    voices, voices_raw, _ = _read_json(voices_path)
    config, config_raw, _ = _read_json(config_path)
    if not isinstance(voices, dict) or not isinstance(config, dict):
        raise ContinuityEvaluationError('voices and config must be objects')
    packet = get_continuity_experiment_packet(source_raw)
    rows = _get_rows(packet, selected_rows)
    root = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
    revision = source_revision or _get_source_revision(root)
    identity = get_evaluation_identity(source_raw, voices_raw, config_raw, packet, selected_rows,
                                       revision)
    selected_records = [{'row_index': index, 'uid': row['uid'], 'speaker': row['speaker'],
                         'arms': {arm: {'text': row[arm]['text'],
                                        'instruct': row[arm].get('instruct')} for arm in ARMS}}
                        for index, row in rows]
    output = get_continuity_output_path(root, output_dir)
    existing = output.exists()
    if existing:
        run = _load_run(output)
        if run.get('identity') != identity:
            raise ContinuityEvaluationError('evaluation inputs/code changed; refusing to reuse this run')
        if 'selected' not in run:
            run['selected'] = selected_records
        elif run['selected'] != selected_records:
            raise ContinuityEvaluationError('evaluation inputs/code changed; refusing to reuse this run')
    else:
        output.mkdir(parents=True)
        run = {'version': VERSION, 'status': 'running', 'identity': identity,
               'source_revision': revision, 'selected': selected_records, 'pairs': [],
               'created_unix': time.time(), 'provenance': provenance(__file__)}
        _write_json(output / 'run.json', run)
    plan_by_index = {item['row_index']: item for item in selected_records}
    if not isinstance(run.get('pairs'), list) or len(run['pairs']) > len(rows):
        raise ContinuityEvaluationError('run manifest has invalid pair inventory')
    pair_by_index = {}
    for item in run['pairs']:
        if (not isinstance(item, dict) or type(item.get('row_index')) is not int
                or item['row_index'] not in plan_by_index
                or item['row_index'] in pair_by_index
                or item.get('status') not in ('complete', 'incomplete', 'failed')
                or type(item.get('attempt')) is not int or item['attempt'] < 1):
            raise ContinuityEvaluationError('run manifest has invalid or duplicate pair inventory')
        pair_by_index[item['row_index']] = item
    status = 'complete'
    for index, row in rows:
        pair_digest = get_pair_input_digest(row, voices, config)
        record = pair_by_index.get(index)
        if record is not None and record.get('status') == 'complete':
            state = _verify_completed_pair(_confined_path(output, record.get('path'), 'completed pair'),
                                           record, pair_digest)
            _validate_pair_state(state, plan_by_index[index], identity, index)
            if record.get('input_digest') != pair_digest:
                if 'input_digest' in record:
                    raise ContinuityEvaluationError('saved pair inputs changed; refusing reuse')
                record['input_digest'] = pair_digest
                _write_json(output / 'run.json', run)
            continue
        try:
            _check_cancel(cancel_check)
        except ContinuityEvaluationCancelled:
            status = 'cancelled'
            break
        attempt = 1 if record is None else int(record.get('attempt', 1)) + 1
        label = secure_filename(row['uid']) or 'row'
        pair_root = _confined_path(output, f'pairs/{index:03d}_{label}', 'new pair')
        while (pair_root / f'attempt-{attempt}').exists():
            attempt += 1
        pair_dir = pair_root / f'attempt-{attempt}'
        pair_dir.mkdir(parents=True)
        pair_dir = _confined_path(output, os.path.relpath(pair_dir, output), 'new pair')
        cancelled = False
        state = {'version': VERSION, 'row_index': index, 'uid': row['uid'],
                 'speaker': row['speaker'], 'input_digest': pair_digest,
                 'attempt': attempt, 'status': 'incomplete',
                 'requested': {arm: copy.deepcopy(row[arm]) for arm in ARMS},
                 'source_revision': revision, 'config_sha256': identity['config_sha256'],
                 'voices_sha256': identity['voices_sha256']}
        _write_json(pair_dir / 'state.json', state)
        try:
            effective_voices, voice_inputs, seeds = get_continuity_voice_inputs(
                voices, row['speaker'], index)
            state['effective_voice_inputs'] = voice_inputs
            state['seeds'] = seeds
            state['model_settings'] = copy.deepcopy(config.get('tts'))
            arms = render_pair(row, effective_voices, seeds, pair_dir, cancel_check)
            if not isinstance(arms, dict) or set(arms) != set(ARMS):
                raise ContinuityEvaluationError('renderer did not return both arms')
            for arm in ARMS:
                item = arms[arm]
                if not isinstance(item, dict) or not isinstance(item.get('submitted_text'), str):
                    raise ContinuityEvaluationError('renderer omitted submitted text')
                relative = str(item.get('path', arm + '.wav'))
                path = (pair_dir / relative).resolve()
                if not is_path_inside(str(path), str(pair_dir)):
                    raise ContinuityEvaluationError('renderer wrote outside its pair directory')
                facts = _wav_facts(path, cancel_check)
                if item.get('audio') is not None and item['audio'] != {
                        key: facts[key] for key in ('sha256', 'size')}:
                    raise ContinuityEvaluationError('renderer audio identity changed')
                state.setdefault('arms', {})[arm] = {
                    'request': copy.deepcopy(item.get('request', row[arm])),
                    'submitted_text': item['submitted_text'],
                    'audio': {key: facts[key] for key in ('sha256', 'size')},
                    'path': relative}
            if state['arms']['baseline']['audio']['sha256'] == state['arms']['deterministic']['audio']['sha256']:
                raise ContinuityEvaluationError('baseline and deterministic audio are identical')
            state['status'] = 'complete'
        except (ContinuityEvaluationCancelled, GeneratedAudioCancelled) as error:
            state.update(status='incomplete', reason=str(error))
            _write_json(pair_dir / 'state.json', state)
            status = 'cancelled'
            cancelled = True
        except Exception as error:
            state.update(status='failed', reason=f'{type(error).__name__}: {str(error)[:240]}')
            _write_json(pair_dir / 'state.json', state)
            status = 'failed'
        else:
            _write_json(pair_dir / 'state.json', state)
        record = {'row_index': index, 'uid': row['uid'], 'attempt': attempt,
                  'path': os.path.relpath(pair_dir, output), 'status': state['status'],
                  'input_digest': pair_digest, 'fidelity_sha256': _get_fidelity_sha256(state)}
        if record['row_index'] in pair_by_index:
            run['pairs'] = [record if item.get('row_index') == index else item
                            for item in run['pairs']]
        else:
            run['pairs'].append(record)
        pair_by_index[index] = record
        _write_json(output / 'run.json', run)
        if cancelled:
            break
    run['pairs'] = sorted(pair_by_index.values(), key=lambda item: item['row_index'])
    completed = [item['row_index'] for item in run['pairs'] if item.get('status') == 'complete']
    run.update(status=status, completed_rows=completed,
               missing_rows=[index for index, _ in rows if index not in completed])
    run['results'] = {
        'input_fidelity': _get_input_fidelity(output, run, rows),
        'content_verification': {'status': 'not_provided',
            'reason': 'This CPU framework does not run ASR; use the existing verifier separately.'},
        'voice_scoring': {'status': 'not_provided',
            'reason': 'No voice scorer is invoked by this framework.'},
        'human_preference': {'status': 'not_provided',
            'reason': 'Build and import a blinded package to record preference.'}}
    _write_json(output / 'run.json', run)
    return run


def _get_input_fidelity(output, run, rows):
    checked, failed = [], []
    plan_by_index = {item['row_index']: item for item in run.get('selected', [])
                     if isinstance(item, dict) and type(item.get('row_index')) is int}
    for index, row in rows:
        record = next((item for item in run['pairs']
                       if item['row_index'] == index and item['status'] == 'complete'), None)
        if record is None:
            continue
        plan = plan_by_index.get(index)
        if plan is None:
            raise ContinuityEvaluationError('run manifest lacks the selected plan row')
        state, _, _ = _read_json(output / record['path'] / 'state.json', MAX_STATE_BYTES)
        for arm in ARMS:
            expected = plan['arms'][arm]['text']
            submitted = state.get('arms', {}).get(arm, {}).get('submitted_text')
            (checked if submitted == expected else failed).append(
                {'row_index': index, 'arm': arm})
    return {'status': 'failed' if failed else 'passed' if checked else 'incomplete',
            'checked_arms': len(checked), 'failed_arms': failed}


def _path_identity(path):
    info = os.stat(path, follow_symlinks=False)
    return info.st_dev, info.st_ino


def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _publish_new_file(source, destination):
    source, destination = Path(source), Path(destination)
    try:
        move_source_upload_exclusively(str(source), str(destination))
    except OSError as error:
        if os.path.lexists(destination):
            raise ContinuityEvaluationError(f'refusing to overwrite existing {destination}') from error
        raise


def _clear_directory_fd(fd):
    try:
        with os.scandir(fd) as iterator:
            entries = list(iterator)
    except (OSError, NotImplementedError, TypeError, ValueError):
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd)
                try:
                    _clear_directory_fd(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=fd)
            else:
                os.unlink(entry.name, dir_fd=fd)
        except (OSError, NotImplementedError, TypeError, ValueError):
            continue


def _remove_owned_dir(path, identity):
    """Clear an owned staging directory without deleting a path replacement.

    Directory removal by descriptor is not portable, so the empty owned
    directory may remain on platforms without that primitive. Contents are
    cleared through the opened descriptor; a replacement path is untouched.
    """
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except (OSError, NotImplementedError, TypeError, ValueError):
        return
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != identity:
            return
        _clear_directory_fd(fd)
    finally:
        os.close(fd)


def build_blind_package(run_dir, package_dir, public_path, key_path, *, seed=None):
    """Build the exact A/B manifest and a separate concealed key."""
    if seed is None:
        seed = secrets.randbits(64)
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ContinuityEvaluationError('randomization seed must be a nonnegative 64-bit integer')
    run = _load_run(run_dir)
    if run.get('status') != 'complete' or run.get('missing_rows'):
        raise ContinuityEvaluationError('run is not complete; preserve it and resume before packaging')
    selected = run.get('identity', {}).get('selected_rows')
    if (not isinstance(selected, list) or not selected or len(selected) > MAX_PAIRS
            or len(set(selected)) != len(selected)
            or any(type(index) is not int or index < 0 for index in selected)):
        raise ContinuityEvaluationError('run manifest has invalid selected rows')
    selected_records = run.get('selected')
    if not isinstance(selected_records, list):
        raise ContinuityEvaluationError('run manifest lacks authoritative selected rows')
    plan_by_index = {}
    for item in selected_records:
        if not isinstance(item, dict) or type(item.get('row_index')) is not int:
            raise ContinuityEvaluationError('run manifest has a malformed selected row')
        if item['row_index'] in plan_by_index:
            raise ContinuityEvaluationError('run manifest has duplicate selected rows')
        arms = item.get('arms')
        if (not isinstance(arms, dict) or set(arms) != set(ARMS)
                or any(not isinstance(arms[arm], dict)
                       or not isinstance(arms[arm].get('text'), str)
                       or not isinstance(arms[arm].get('instruct'), (str, type(None)))
                       for arm in ARMS)):
            raise ContinuityEvaluationError('run manifest has a malformed selected row')
        plan_by_index[item['row_index']] = item
    if set(plan_by_index) != set(selected):
        raise ContinuityEvaluationError('run manifest selected rows differ from identity')
    records = {}
    for record in run.get('pairs', []):
        if not isinstance(record, dict) or type(record.get('row_index')) is not int:
            raise ContinuityEvaluationError('run manifest has a malformed pair record')
        if record['row_index'] in records:
            raise ContinuityEvaluationError('run manifest has duplicate pair records')
        records[record['row_index']] = record
    completed, fidelity = [], {'status': 'passed', 'checked_arms': 0, 'failed_arms': []}
    for index in selected:
        record = records.get(index)
        if record is None or record.get('status') != 'complete':
            raise ContinuityEvaluationError(f'run is missing a completed pair for row {index}')
        pair_dir = _confined_path(Path(run_dir), record.get('path'), 'completed pair')
        state, _, _ = _read_json(pair_dir / 'state.json', MAX_STATE_BYTES)
        if state.get('status') != 'complete':
            raise ContinuityEvaluationError(f'run pair {index} is not complete')
        plan = plan_by_index[index]
        _validate_pair_state(state, plan, run.get('identity', {}), index)
        if state.get('input_digest') != record.get('input_digest'):
            raise ContinuityEvaluationError(f'run pair {index} digest differs from the run')
        arms = state.get('arms') if isinstance(state.get('arms'), dict) else {}
        for arm in ARMS:
            expected_request = plan['arms'][arm]
            submitted = arms.get(arm, {}).get('submitted_text') if isinstance(arms.get(arm), dict) else None
            if not isinstance(submitted, str):
                raise ContinuityEvaluationError(f'run pair {index} lacks submitted-text evidence')
            fidelity['checked_arms'] += 1
            if submitted != expected_request['text']:
                fidelity['failed_arms'].append({'row_index': index, 'arm': arm})
        completed.append((record, state, pair_dir))
    if fidelity['failed_arms']:
        fidelity['status'] = 'failed'
        raise ContinuityEvaluationError('run failed the input-fidelity gate; refusing to package it')
    for record, state, _pair_dir in completed:
        if record.get('fidelity_sha256') != _get_fidelity_sha256(state):
            raise ContinuityEvaluationError('pair fidelity evidence changed; refusing to package it')
    package_dir, public_path, key_path = Path(package_dir), Path(public_path), Path(key_path)
    for path in (package_dir, public_path, key_path):
        if os.path.lexists(path):
            raise ContinuityEvaluationError(f'refusing to overwrite existing {path}')
    if os.path.realpath(public_path) == os.path.realpath(key_path):
        raise ContinuityEvaluationError('public manifest and concealed key must be separate files')
    for path in (public_path, key_path):
        if is_path_inside(str(path), str(package_dir)):
            raise ContinuityEvaluationError('manifests must be outside the package directory')
    temporary = Path(tempfile.mkdtemp(prefix=package_dir.name + '.building-',
                                       dir=str(package_dir.parent)))
    stage_identity = _path_identity(temporary)
    _chmod(temporary, 0o755)
    key_temp = public_temp = None
    temporary_moved = False
    try:
        rng = random.Random(seed)
        public_sets, key_sets = [], []
        for record, state, pair_dir in completed:
            ordered = list(ARMS)
            rng.shuffle(ordered)
            samples, mapping = [], {}
            for sample_index, arm in enumerate(ordered):
                if _path_identity(temporary) != stage_identity:
                    raise ContinuityEvaluationError('staging directory changed; refusing to publish')
                item = state['arms'][arm]
                source = _confined_path(pair_dir, item.get('path'), 'pair audio')
                if not source.is_file() or source.stat().st_size > MAX_AUDIO_BYTES:
                    raise ContinuityEvaluationError('pair audio is missing or exceeds its size limit')
                filename = f"pair_{record['row_index']:03d}_{string.ascii_uppercase[sample_index]}.wav"
                destination = temporary / filename
                with _capture_audio_snapshot(source) as (snapshot, captured):
                    if captured['sha256'] != item['audio']['sha256']:
                        raise ContinuityEvaluationError('pair audio changed before packaging')
                    shutil.copy2(snapshot, destination)
                _chmod(destination, 0o644)
                validate_generated_audio(str(destination), 'packaged continuity audio')
                digest = file_sha256(destination)
                if digest != item['audio']['sha256']:
                    raise ContinuityEvaluationError('pair audio changed before packaging')
                samples.append({'file': filename, 'sha256': digest})
                mapping[filename] = {'arm': arm, 'row_index': record['row_index'],
                                     'uid': state['uid'],
                                     'source_sha256': item['audio']['sha256']}
            if len({sample['sha256'] for sample in samples}) != len(samples):
                raise ContinuityEvaluationError('pair audio is identical across arms')
            set_id = f"pair_{record['row_index']:03d}"
            public_sets.append({'id': set_id, 'kind': 'baseline_vs_continuity',
                                'samples': samples})
            key_sets.append({'id': set_id, 'uid': state['uid'], 'mapping': mapping})
        fidelity_result = {'status': fidelity['status'],
                           'checked_arms': fidelity['checked_arms'],
                           'failed_arm_count': len(fidelity['failed_arms'])}
        key = {'version': VERSION, 'status': 'complete', 'randomization_seed': seed,
               'run_identity': run['identity'], 'sets': key_sets,
               'input_fidelity': copy.deepcopy(fidelity_result), 'missing_rows': [],
               'provenance': provenance(__file__)}
        key_fd, key_temp = tempfile.mkstemp(prefix=key_path.name + '.building-',
                                            dir=str(key_path.parent))
        os.close(key_fd)
        _write_json(key_temp, key)
        _chmod(key_temp, 0o600)
        public = {'version': VERSION, 'status': 'complete',
                  'package_dir': str(package_dir.resolve()), 'sets': public_sets,
                  'concealed_key_sha256': file_sha256(key_temp),
                  'missing_rows': [], 'provenance': provenance(__file__),
                  'input_fidelity': fidelity_result,
                  'results': {**{name: {'status': 'not_provided'}
                                 for name in CATEGORIES if name != 'input_fidelity'},
                              'input_fidelity': copy.deepcopy(fidelity_result)},
                  'limitations': ['CPU/dry-run framework; no native audio, ASR or voice score.',
                                  'No human ratings are included until answers are imported.']}
        public_fd, public_temp = tempfile.mkstemp(prefix=public_path.name + '.building-',
                                                  dir=str(public_path.parent))
        os.close(public_fd)
        _write_json(public_temp, public)
        _chmod(public_temp, 0o644)
        if _path_identity(temporary) != stage_identity:
            raise ContinuityEvaluationError('staging directory changed; refusing to publish')
        try:
            move_source_upload_exclusively(str(temporary), str(package_dir))
        except OSError as error:
            if os.path.lexists(package_dir):
                raise ContinuityEvaluationError(
                    f'refusing to overwrite existing {package_dir}') from error
            raise
        temporary_moved = True
        _publish_new_file(key_temp, key_path)
        key_temp = None
        _publish_new_file(public_temp, public_path)
        public_temp = None
        return public, key
    except BaseException:
        raise
    finally:
        if not temporary_moved:
            _remove_owned_dir(temporary, stage_identity)
        for path in (key_temp, public_temp):
            if path is not None:
                Path(path).unlink(missing_ok=True)


def _load_package(package_dir, public_path, key_path):
    public, _, _ = _read_json(public_path)
    key, key_raw, _ = _read_json(key_path)
    if not isinstance(public, dict) or not isinstance(key, dict):
        raise ContinuityEvaluationError('blind package manifests must be objects')
    if public.get('status') != 'complete' or key.get('status') != 'complete':
        raise ContinuityEvaluationError('blind package is not complete')
    if public.get('version') != VERSION or key.get('version') != VERSION:
        raise ContinuityEvaluationError('unsupported blind package version')
    if hashlib.sha256(key_raw).hexdigest() != public.get('concealed_key_sha256'):
        raise ContinuityEvaluationError('concealed key changed')
    if not isinstance(public.get('package_dir'), str) or os.path.realpath(package_dir) != os.path.realpath(public['package_dir']):
        raise ContinuityEvaluationError('package directory differs from manifest')
    public_sets = public.get('sets')
    key_sets = key.get('sets')
    if (not isinstance(public_sets, list) or not isinstance(key_sets, list)
            or not 1 <= len(public_sets) <= MAX_PAIRS or len(key_sets) != len(public_sets)):
        raise ContinuityEvaluationError('key/package set inventory differs')
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in public_sets + key_sets):
        raise ContinuityEvaluationError('key/package set inventory differs')
    public_ids = [item['id'] for item in public_sets]
    keyed = {item['id']: item for item in key_sets}
    if (len(set(public_ids)) != len(public_ids) or len(keyed) != len(key_sets)
            or set(keyed) != set(public_ids)):
        raise ContinuityEvaluationError('key/package set inventory differs')
    identity = key.get('run_identity')
    selected = identity.get('selected_rows') if isinstance(identity, dict) else None
    if (not isinstance(selected, list) or len(selected) != len(public_sets)
            or any(type(index) is not int or index < 0 for index in selected)
            or len(set(selected)) != len(selected)):
        raise ContinuityEvaluationError('key/package selected inventory differs')
    expected_indices = {f'pair_{index:03d}': index for index in selected}
    if set(expected_indices) != set(public_ids):
        raise ContinuityEvaluationError('key/package selected inventory differs')
    expected_fidelity = {'status': 'passed', 'checked_arms': 2 * len(selected),
                         'failed_arm_count': 0}
    if (key.get('input_fidelity') != expected_fidelity
            or public.get('input_fidelity') != key['input_fidelity']
            or key.get('missing_rows') != [] or public.get('missing_rows') != []):
        raise ContinuityEvaluationError('package fidelity evidence differs from concealed key')
    expected_files = set()
    sample_records = []
    for item in public_sets:
        mapping = keyed[item['id']].get('mapping')
        samples = item.get('samples')
        if not isinstance(mapping, dict) or not isinstance(samples, list) or len(samples) != 2:
            raise ContinuityEvaluationError('key lacks a complete two-sample package set')
        names = []
        for sample in samples:
            if (not isinstance(sample, dict) or set(sample) != {'file', 'sha256'}
                    or not isinstance(sample['file'], str)):
                raise ContinuityEvaluationError('invalid public sample')
            entry = mapping.get(sample['file'])
            if not isinstance(entry, dict) or entry.get('source_sha256') != sample['sha256']:
                raise ContinuityEvaluationError('package/key hash mismatch')
            if (entry.get('row_index') != expected_indices[item['id']]
                    or not isinstance(keyed[item['id']].get('uid'), str)
                    or entry.get('uid') != keyed[item['id']]['uid']):
                raise ContinuityEvaluationError('key/package row inventory differs')
            names.append(sample['file'])
            sample_records.append(sample)
        arms = {mapping[name].get('arm') for name in names if isinstance(mapping.get(name), dict)}
        if set(names) != set(mapping) or len(mapping) != 2 or arms != set(ARMS):
            raise ContinuityEvaluationError('package/key mapping is not a one-to-one A/B pair')
        if len(set(names)) != 2 or expected_files.intersection(names):
            raise ContinuityEvaluationError('package sample inventory reuses an audio file')
        if samples[0]['sha256'] == samples[1]['sha256']:
            raise ContinuityEvaluationError('package sample inventory has identical audio across arms')
        expected_files.update(names)
    if set(os.listdir(package_dir)) != expected_files:
        raise ContinuityEvaluationError('package file inventory changed')
    for sample in sample_records:
        path = (Path(package_dir) / sample['file']).resolve()
        if not is_path_inside(str(path), str(package_dir)):
            raise ContinuityEvaluationError('packaged sample escapes package')
        if (Path(package_dir) / sample['file']).is_symlink():
            raise ContinuityEvaluationError('packaged audio must not be a symlink')
        if not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
            raise ContinuityEvaluationError('packaged audio is missing or exceeds its size limit')
        try:
            facts = _wav_facts(path)
        except Exception as error:
            raise ContinuityEvaluationError('packaged audio is invalid') from error
        if facts['sha256'] != sample['sha256']:
            raise ContinuityEvaluationError('packaged audio changed')
    return public, key


def import_answers(package_dir, public_path, key_path, answers_path):
    """Read human answers without filling missing ones."""
    public, key = _load_package(package_dir, public_path, key_path)
    answers, _, _ = _read_json(answers_path)
    if (not isinstance(answers, dict)
            or answers.get('concealed_key_sha256') != public.get('concealed_key_sha256')):
        raise ContinuityEvaluationError('answers belong to a different package')
    listeners = answers.get('listeners')
    if not isinstance(listeners, list) or not listeners:
        raise ContinuityEvaluationError('answers must contain listeners')
    if len(listeners) > MAX_LISTENERS:
        raise ContinuityEvaluationError(f'answers exceed the {MAX_LISTENERS}-listener limit')
    public_sets = {item['id']: item for item in public['sets']}
    keyed = {item['id']: item for item in key['sets']}
    missing, preferences, voice_scores, seen = [], [], [], set()
    for listener in listeners:
        listener_id = listener.get('id') if isinstance(listener, dict) else None
        if not isinstance(listener_id, str) or not listener_id or listener_id in seen:
            raise ContinuityEvaluationError('listener IDs must be unique strings')
        seen.add(listener_id)
        supplied = listener.get('answers')
        if not isinstance(supplied, list):
            raise ContinuityEvaluationError('listener answers must be a list')
        answered = {}
        for answer in supplied:
            if (not isinstance(answer, dict) or not isinstance(answer.get('set_id'), str)
                    or answer.get('set_id') not in public_sets):
                raise ContinuityEvaluationError('answer names an unknown set')
            if answer['set_id'] in answered:
                raise ContinuityEvaluationError('duplicate answer for one set')
            answered[answer['set_id']] = answer
        for set_id in public_sets:
            answer = answered.get(set_id)
            if answer is None:
                missing.append({'listener': listener_id, 'set_id': set_id})
                continue
            choice = answer.get('preference')
            samples = {item['file'] for item in public_sets[set_id]['samples']}
            if not isinstance(choice, str):
                raise ContinuityEvaluationError('preference must be A/B filename or tie')
            if choice == 'tie':
                preferences.append({'listener': listener_id, 'set_id': set_id, 'arm': 'tie'})
            elif choice in samples:
                preferences.append({'listener': listener_id, 'set_id': set_id,
                                    'arm': keyed[set_id]['mapping'][choice]['arm']})
            else:
                raise ContinuityEvaluationError('preference must be A/B filename or tie')
            scores = answer.get('voice_scores')
            if scores is not None:
                if not isinstance(scores, dict) or set(scores) != samples:
                    raise ContinuityEvaluationError('voice_scores must cover each sample')
                for filename, score in scores.items():
                    if type(score) not in (int, float) or not 1 <= score <= 5:
                        raise ContinuityEvaluationError('voice scores must be 1..5')
                    voice_scores.append({'listener': listener_id, 'set_id': set_id,
                                         'arm': keyed[set_id]['mapping'][filename]['arm'],
                                         'score': score})
    counts = {arm: sum(item['arm'] == arm for item in preferences) for arm in ARMS + ('tie',)}
    expected_scores = len(seen) * sum(len(item['samples']) for item in public_sets.values())
    missing_scores = max(0, expected_scores - len(voice_scores))
    result = {
        'version': VERSION, 'status': 'incomplete' if missing else 'complete',
        'package_dir': str(package_dir), 'listener_count': len(seen),
        'package_concealed_key_sha256': public.get('concealed_key_sha256'),
        'human_preference': {'answered': len(preferences),
                             'expected': len(seen) * len(public_sets),
                             'missing': missing, 'counts': counts},
        'input_fidelity': copy.deepcopy(public.get('input_fidelity',
                           {'status': 'not_provided',
                            'reason': 'The package was not built from a run manifest with fidelity results.'})),
        'package_missing_rows': copy.deepcopy(public.get('missing_rows', [])),
        'content_verification': {'status': 'not_provided',
                                 'reason': 'Use the existing CPU verifier separately.'},
        'voice_scoring': ({'status': 'incomplete' if missing_scores else 'complete',
                           'scores': len(voice_scores), 'expected': expected_scores,
                           'missing': missing_scores, 'values': copy.deepcopy(voice_scores),
                           'counts': {arm: sum(item['arm'] == arm for item in voice_scores)
                                      for arm in ARMS}}
                          if voice_scores else {'status': 'not_provided'}),
        'limitations': ['Missing answers are reported, not imputed.',
                        'Input fidelity, content verification, voice scoring and preference are separate.']}
    return result


def _parse_rows(value):
    if value is None:
        return None
    return [int(item) for item in value.split(',') if item]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--source', required=True)
    run.add_argument('--voices', required=True)
    run.add_argument('--config', required=True)
    run.add_argument('--out', required=True)
    run.add_argument('--rows')
    run.add_argument('--dry-run', action='store_true', required=True)
    package = sub.add_parser('package')
    package.add_argument('--run', required=True)
    package.add_argument('--package-dir', required=True)
    package.add_argument('--out', required=True)
    package.add_argument('--key', required=True)
    package.add_argument('--seed', type=int, default=None)
    imp = sub.add_parser('import')
    imp.add_argument('--package-dir', required=True)
    imp.add_argument('--out', required=True)
    imp.add_argument('--key', required=True)
    imp.add_argument('--answers', required=True)
    imp.add_argument('--results', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            if not args.dry_run:
                raise ContinuityEvaluationError('native continuity rendering is not wired in this dry-run framework')
            result = run_collection(args.source, args.voices, args.config, args.out,
                                    selected_rows=_parse_rows(args.rows))
            print(json.dumps({'status': result['status'], 'completed': result['completed_rows'],
                              'missing': result['missing_rows']}))
            fidelity = result.get('results', {}).get('input_fidelity', {}).get('status')
            return 0 if result['status'] == 'complete' and fidelity == 'passed' else 1
        elif args.command == 'package':
            public, _ = build_blind_package(args.run, args.package_dir, args.out,
                                            args.key, seed=args.seed)
            print(json.dumps({'sets': len(public['sets']), 'key': args.key}))
        else:
            if os.path.lexists(args.results):
                raise ContinuityEvaluationError('refusing to overwrite results')
            if is_path_inside(args.results, args.package_dir):
                raise ContinuityEvaluationError('results must be outside the package directory')
            result = import_answers(args.package_dir, args.out, args.key, args.answers)
            _write_new_json(args.results, result)
            print(json.dumps({'status': result['status'],
                              'missing': len(result['human_preference']['missing'])}))
            return 0 if result['status'] == 'complete' else 1
    except (ContinuityEvaluationError, ListeningPackageError, GeneratedAudioError,
            OSError, TypeError, ValueError, KeyError, AttributeError) as error:
        parser.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
