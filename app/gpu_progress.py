"""Current-run unit measurements for queue ETA, independent of model loading."""
import json
import math
import os
from pathlib import Path
import tempfile
import time
import subprocess
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_WORKER_TOKENS = {}


def get_gpu_progress_process_token(library, pid):
    return subprocess.check_output(
        ['bash', '-c', 'source "$1"; get_pending_process_token "$2"', 'progress',
         str(library), str(pid)], text=True, stderr=subprocess.DEVNULL).strip()


def save_gpu_progress(path, document):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(document, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def get_gpu_progress_identity(environment):
    keys = ('GPU_PROGRESS_RUN_ID', 'GPU_PROGRESS_OWNER_PID',
            'GPU_PROGRESS_OWNER_TOKEN', 'GPU_PROGRESS_JOB')
    if not any(environment.get(key) for key in keys):
        return None
    if not all(environment.get(key) for key in keys):
        raise ValueError('Incomplete GPU progress owner identity')
    return dict(zip(('run_id', 'owner_pid', 'owner_token', 'job'),
                    (environment[key] for key in keys)))


def validate_gpu_progress_counter(completed, total):
    if (type(completed) is not int or type(total) is not int
            or total <= 0 or not 0 <= completed <= total):
        raise ValueError('GPU progress requires integer 0 <= completed <= positive total')


def record_gpu_progress(phase, completed, total):
    """Publish explicit worker counters; never infer progress from cached files."""
    identity = get_gpu_progress_identity(os.environ)
    if identity is None:
        return
    validate_gpu_progress_counter(completed, total)
    if not isinstance(phase, str) or not phase.strip():
        raise ValueError('GPU progress phase must be named')
    path = Path(os.environ['GPU_PROGRESS_FILE'])
    worker_pid = str(os.getpid())
    library = os.environ['GPU_PROGRESS_LIBRARY']
    key = (worker_pid, library)
    if key not in _WORKER_TOKENS:
        _WORKER_TOKENS[key] = get_gpu_progress_process_token(library, worker_pid)
    worker_token = _WORKER_TOKENS[key]
    document = json.loads(path.read_text(encoding='utf-8'))
    if any(document.get(key) != value for key, value in identity.items()):
        raise ValueError('GPU progress belongs to a different queue invocation')
    samples = document.get('samples', [])
    if (document.get('phase') != phase or document.get('total') != total
            or document.get('worker_pid') != worker_pid
            or document.get('worker_token') != worker_token):
        samples = []
    if samples:
        previous = samples[-1]['completed']
        if completed < previous:
            raise ValueError('GPU progress counter regressed within one phase')
        if completed == previous:
            return
    samples = [*samples, {'completed': completed, 'monotonic': time.monotonic()}][-32:]
    save_gpu_progress(path, {**identity, 'worker_pid': worker_pid, 'worker_token': worker_token,
                            'phase': phase, 'total': total, 'samples': samples})


def get_gpu_progress_estimate(document, expected_identity):
    """Return measured per-unit spread, requiring current live-owner identity."""
    if any(document.get(key) != value for key, value in expected_identity.items()):
        raise ValueError('Progress owner identity does not match the live job')
    phase = document.get('phase')
    if not isinstance(phase, str) or not phase.strip():
        raise ValueError('No named progress phase')
    total = document.get('total')
    samples = document.get('samples')
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError('Not enough current-run unit measurements')
    rates = []
    previous = None
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError('Malformed GPU progress sample')
        completed, timestamp = sample.get('completed'), sample.get('monotonic')
        validate_gpu_progress_counter(completed, total)
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or timestamp < 0):
            raise ValueError('GPU progress timestamp must be finite monotonic time')
        if previous is not None:
            delta = completed - previous['completed']
            elapsed = timestamp - previous['monotonic']
            if delta <= 0 or elapsed <= 0:
                raise ValueError('GPU progress observations must advance time and work')
            rates.append(elapsed / delta)
        previous = sample
    remaining = total - previous['completed']
    average = ((samples[-1]['monotonic'] - samples[0]['monotonic'])
               / (samples[-1]['completed'] - samples[0]['completed']))
    if not all(math.isfinite(value) for value in (average, remaining * average,
                                                 remaining * min(rates), remaining * max(rates))):
        raise ValueError('GPU progress rate or remaining time overflowed')
    return {'phase': phase, 'completed': previous['completed'], 'total': total,
            'remaining': remaining, 'seconds': remaining * average,
            'seconds_low': remaining * min(rates), 'seconds_high': remaining * max(rates),
            'seconds_per_unit': average, 'measured_intervals': len(rates),
            'last_progress_monotonic': previous['monotonic']}


def ensure_queue_progress(parent_pid, wrapper, job):
    """Initialize a fresh report after verified queue admission, returning env."""
    root = Path(wrapper).resolve().parent
    library = root / 'run_chains/lib/gpu_pending.sh'
    token = get_gpu_progress_process_token(library, parent_pid)
    if not token:
        raise OSError('Queue progress has no live wrapper birth identity')
    log = Path(os.environ.get('GPU_QLOG', root / 'ab_test_runtime/logs/gpu_jobq.log'))
    directory = Path(os.environ.get('GPU_PROGRESS_DIR', log.parent / 'progress'))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{parent_pid}.json'
    identity = {'run_id': uuid.uuid4().hex, 'owner_pid': str(parent_pid),
                'owner_token': token, 'job': job}
    save_gpu_progress(path, {**identity, 'samples': []})
    return dict(zip(('GPU_PROGRESS_RUN_ID', 'GPU_PROGRESS_OWNER_PID',
                     'GPU_PROGRESS_OWNER_TOKEN', 'GPU_PROGRESS_JOB'), identity.values()),
                GPU_PROGRESS_FILE=str(path), GPU_PROGRESS_LIBRARY=str(library))


def get_queue_eta_message(directory, job, owners):
    """Read current-owner telemetry without touching reports or queue history."""
    documents = []
    for pid, token in owners:
        path = Path(directory) / f'{pid}.json'
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(document, dict):
                continue
            identity = {'owner_pid': str(pid), 'owner_token': token, 'job': job}
            if any(document.get(key) != value for key, value in identity.items()):
                continue
            if not isinstance(document.get('run_id'), str) or not document['run_id']:
                continue
            worker_pid = document.get('worker_pid')
            if not isinstance(worker_pid, str) or not worker_pid.isdecimal():
                continue
            library = Path(__file__).resolve().parent.parent / 'run_chains/lib/gpu_pending.sh'
            if document.get('worker_token') != get_gpu_progress_process_token(library, worker_pid):
                continue
            documents.append((document, {**identity, 'run_id': document['run_id']}))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            continue
    if len(documents) != 1:
        return 'ETA: unavailable (no unique current-owner progress report)'
    try:
        estimate = get_gpu_progress_estimate(*documents[0])
    except (ValueError, TypeError, KeyError) as error:
        return f'ETA: unavailable ({error})'
    age = time.monotonic() - estimate['last_progress_monotonic']
    if age < 0:
        return 'ETA: unavailable (progress timestamp is in the future)'
    if not estimate['remaining']:
        return 'ETA: work units complete; remaining cleanup time unavailable'
    low, high = estimate['seconds_low'], estimate['seconds_high']
    now = datetime.now(ZoneInfo('America/Chicago'))
    try:
        finishes = [(now + timedelta(seconds=seconds)).strftime('%b %d %I:%M:%S %p %Z')
                    for seconds in (low, high)]
    except OverflowError:
        return 'ETA: unavailable (measured estimate exceeds calendar range)'
    return (f"ETA for {estimate['phase']}: {low:.1f}–{high:.1f}s remaining; "
            f"estimated completion {finishes[0]}–{finishes[1]}; "
            f"{estimate['completed']}/{estimate['total']} units, "
            f"measured {estimate['seconds_per_unit']:.2f}s/unit from "
            f"{estimate['measured_intervals']} current-run intervals; "
            f"last progress {age:.1f}s ago (assumes work continues at observed rates)")


if __name__ == '__main__':
    import sys
    directory, job = sys.argv[1:3]
    owners = [argument.split('=', 1) for argument in sys.argv[3:]]
    print(get_queue_eta_message(directory, job, owners))
