"""#1434 qualification: false positives and missed defects of the finished-take verifier.

Runs the unchanged production `verify_finished_speech` (Small.en primary, distinct
Large-v3 confirmation, CPU) over:

- controls: real renders from line_render_pilot.py, each checked against the exact
  text its render proof says was sent to the model;
- faults: copies of some controls with a known defect cut or spliced into the PCM
  (`make_fault`), so the right answer is known by construction.

A control flag is any verdict other than `no_content_candidate`; without listening
it is unresolved and is not counted as a confirmed false positive. A fault is
missed when the verdict is `no_content_candidate`. Designed faults are not
human-listened truth, and real model failures (mumbled or skipped words in a
render) can look different from a cut.

Every case is bound to the bytes it is verified on: a control's WAV must hash to
what its render proof names, and a fault is planned from that control, hashed in a
private staging directory, and only published after resume validation. A result is
published only when the verifier's returned audio identity matches the planned
proof and its actual runtime identity matches the planned content identity.

Resumable: a row already in results.jsonl is reused only when its (run, case_id)
and its text, fault, input hash/size and verifier runtime all still match; a
recorded attempt must also carry a valid status, finite seconds and a verifier
result. An unavailable attempt is retained as such when resuming; it is not
counted as successful ASR or a clean control. Anything else stops the run without changing prior derived files.

The archived 334-row results.jsonl predates this input/runtime binding. It remains
historical evidence, not a resumable checkpoint; resuming into it is refused
rather than accepting rows with missing bindings.
"""
import argparse
import hashlib
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

import numpy as np
import soundfile as sf

CLEAR = 'no_content_candidate'
RUNS = ('alice', 'numbers', 'w1435_U', 'w1435_W', 'verify')
LONG_FAULTS = ('tail_cut', 'head_cut', 'tail_repeat', 'mid_repeat')
SHORT_FAULTS = ('short_double', 'short_half')
FAULT_KINDS = ('control',) + LONG_FAULTS + SHORT_FAULTS
ALLOWED_STATUSES = (CLEAR, 'review', 'corroborated_content_difference',
                    'review_confirmation_unavailable', 'unavailable',
                    'review_short_acoustic_unavailable')
BINDING = ('text', 'fault', 'expect', 'input_sha256', 'input_size', 'runtime')
SAFE_COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
MAX_ROWS_BYTES = 32 * 1024 ** 2
MAX_RESULTS_BYTES = 64 * 1024 ** 2
MAX_TEXT_CHARS = 100000


def make_fault(pcm, kind):
    """Return a new mono PCM array with one designed defect; `pcm` is not changed."""
    n = len(pcm)
    if kind == 'tail_cut':          # last 45% of the take missing
        return pcm[:int(n * 0.55)].copy()
    if kind == 'head_cut':          # first 35% missing
        return pcm[int(n * 0.35):].copy()
    if kind == 'tail_repeat':       # last 40% spoken twice
        return np.concatenate([pcm, pcm[int(n * 0.6):]])
    if kind == 'mid_repeat':        # 40-60% span spoken twice in place
        a, b = int(n * 0.4), int(n * 0.6)
        return np.concatenate([pcm[:b], pcm[a:b], pcm[b:]])
    if kind == 'short_double':      # whole short line said twice
        return np.concatenate([pcm, pcm])
    if kind == 'short_half':        # second half of a short line missing
        return pcm[:int(n * 0.5)].copy()
    raise ValueError(f'unknown fault {kind}')


def _is_sha(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def get_sha256(path):
    """Plain whole-file hash for tests and row proofs; admission uses the bounded helper."""
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024 ** 2), b''):
            digest.update(block)
    return digest.hexdigest()


def _validate_row(row):
    """Validate one render-row shape and bounded metadata before reading its WAV."""
    if not isinstance(row, dict):
        raise ValueError('rows.json entries must be objects')
    for field in ('clip_id', 'line_id'):
        value = row.get(field)
        if not isinstance(value, str) or not SAFE_COMPONENT.fullmatch(value):
            raise ValueError(f'{field} is not a safe component: {value!r}')
    if type(row.get('seed')) is not int:
        raise ValueError(f"{row.get('clip_id')}: seed must be an integer")
    wav = row.get('wav')
    if not isinstance(wav, str) or len(wav) > 4096:
        raise ValueError(f"{row.get('clip_id')}: wav path is missing or too long")
    if not _is_sha(row.get('wav_sha256')):
        raise ValueError(f"{row.get('clip_id')}: wav_sha256 is not a sha256")
    speech = row.get('speech_input')
    if not isinstance(speech, dict) or not isinstance(speech.get('text'), str):
        raise ValueError(f"{row.get('clip_id')}: speech_input.text is missing")
    if not speech['text'] or len(speech['text']) > MAX_TEXT_CHARS:
        raise ValueError(f"{row.get('clip_id')}: speech_input.text is empty or too long")
    audio = speech.get('audio')
    if not isinstance(audio, dict) or not _is_sha(audio.get('sha256')):
        raise ValueError(f"{row.get('clip_id')}: speech_input audio proof is missing")


def _read_bounded_bytes(path, limit, *, missing=False):
    # This helper holds one nonblocking regular descriptor through the bounded
    # read and checks its identity afterwards; never reopen an admitted path.
    from project_scope_files import capture_scope_file
    path = Path(path).absolute()
    if missing and not path.parent.exists():
        return b''
    info, raw = capture_scope_file(path.parent.resolve(), path.name,
                                   limit=limit, include_bytes=True)
    if not info['exists']:
        if missing:
            return b''
        raise FileNotFoundError(path)
    return raw


def _read_bounded_json(path, limit):
    from json_metadata import get_unique_json_object
    raw = _read_bounded_bytes(path, limit)
    try:
        return json.loads(raw, object_pairs_hook=get_unique_json_object,
                          parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'{path} is not valid JSON') from error


def _reject_constant(value):
    raise ValueError('Nonfinite JSON values are not supported')


def _read_control(path, expected_sha, expected_size):
    from word_timing import MAX_AUDIO_BYTES
    raw = _read_bounded_bytes(path, MAX_AUDIO_BYTES)
    if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('Control changed while planning or publishing')
    # Native decoding receives only this bounded immutable snapshot. Compressed
    # files cannot use a small container to request an unbounded decoded array.
    with sf.SoundFile(io.BytesIO(raw)) as audio:
        if (audio.channels != 1 or not 0 < audio.samplerate <= 192000
                or not 0 < audio.frames <= MAX_AUDIO_BYTES // 2):
            raise ValueError('Expected bounded nonempty mono render')
        pcm = audio.read(audio.frames, dtype='int16')
        if len(pcm) != audio.frames:
            raise ValueError('Control decode ended before its declared frame count')
        return pcm, audio.samplerate


def get_rows(run_dir):
    """Rows whose WAV on disk is the audio their render proof names."""
    from project_scope_files import capture_scope_file
    from speech_verification_compare import get_comparison_tokens
    from word_timing import MAX_AUDIO_BYTES
    root = Path(run_dir).resolve()
    data = _read_bounded_json(root / 'rows.json', MAX_ROWS_BYTES)
    if not isinstance(data, dict) or not isinstance(data.get('rows'), list):
        raise ValueError(f'{run_dir}/rows.json has no rows list')
    seen = set()
    for row in data['rows']:
        _validate_row(row)
        if row['clip_id'] in seen:
            raise ValueError('Duplicate clip identity in render rows')
        seen.add(row['clip_id'])
        if not get_comparison_tokens(row['speech_input']['text']):
            raise ValueError('Control text has no verifier tokens')
        proof = row['speech_input']['audio']['sha256']
        if proof != row['wav_sha256']:
            raise ValueError(f"{row['clip_id']}: render proof does not match its WAV")
        try:
            relative = Path(row['wav']).absolute().relative_to(root)
        except ValueError as error:
            raise ValueError('Control WAV must stay inside its pilot run') from error
        actual, _ = capture_scope_file(root, relative.as_posix(), limit=MAX_AUDIO_BYTES)
        if not actual['exists'] or actual['sha256'] != proof:
            raise ValueError(f"{row['clip_id']}: WAV on disk is not the audio its render proof names")
        yield dict(row, input_sha256=actual['sha256'], input_size=actual['size'])


def _get_derived_root(root):
    # User-selected ancestors may have OS aliases (/var on macOS), but the
    # generated directory entry itself must remain plain.
    requested = Path(root).absolute()
    plain = requested.parent.resolve() / requested.name
    if plain.is_symlink():
        raise ValueError('Derived evidence directory must not be an alias')
    return plain


def _confined_path(root, run, clip_id, kind):
    """Build one derived filename from safe components only."""
    for value in (run, clip_id):
        if not isinstance(value, str) or not SAFE_COMPONENT.fullmatch(value):
            raise ValueError(f'unsafe run/clip component: {value!r}')
    if kind not in LONG_FAULTS + SHORT_FAULTS:
        raise ValueError(f'unknown fault kind: {kind!r}')
    name = f'{run}__{clip_id}__{kind}.wav'
    if len(name) > 200 or not SAFE_COMPONENT.fullmatch(name):
        raise ValueError(f'derived filename is not a safe component: {name!r}')
    base = _get_derived_root(root)
    path = Path(os.path.abspath(base / name))
    if path.parent != base:
        raise ValueError(f'derived path escaped its directory: {path}')
    return path


def _planned_fault_identity(pcm, rate):
    """Hash the exact WAV bytes a pending fault would publish, using private staging."""
    from speech_verification_runtime import get_verifier_file_identity
    from word_timing import MAX_AUDIO_BYTES
    header = io.BytesIO()
    sf.write(header, np.empty(0, dtype=np.int16), rate, format='WAV', subtype='PCM_16')
    if pcm.nbytes + header.getbuffer().nbytes > MAX_AUDIO_BYTES:
        raise ValueError('Designed fault exceeds the verifier WAV byte limit')
    with tempfile.TemporaryDirectory(prefix='.fault-plan-') as directory:
        path = Path(directory) / 'fault.wav'
        sf.write(str(path), pcm, rate, subtype='PCM_16')
        info = get_verifier_file_identity(path, MAX_AUDIO_BYTES, lambda: False)
    return info['sha256'], info['size']


def get_cases(pilot_root, derived_dir, fault_seeds, runs=RUNS):
    """Every clip is a control; clips with a seed in `fault_seeds` also seed faults.

    No derived file is written here. Fault PCM and identities are planned in
    private staging so a rejected resume cannot overwrite prior evidence.
    """
    cases = []
    if len(set(runs)) != len(runs):
        raise ValueError('Duplicate pilot runs are not supported')
    for run in runs:
        if run not in RUNS:
            raise ValueError(f'unknown run: {run!r}')
        for row in get_rows(Path(pilot_root) / run):
            text = row['speech_input']['text']
            group = row['line_id'][0] if run == 'verify' else run
            cases.append(dict(case_id=f"{row['clip_id']}__control", run=run, group=group,
                              fault='control', expect='clean', text=text, wav=row['wav'],
                              input_sha256=row['input_sha256'], input_size=row['input_size']))
            if row['seed'] not in fault_seeds or run in ('numbers', 'w1435_W'):
                continue
            kinds = SHORT_FAULTS if group == 'v' else () if group == 'r' else LONG_FAULTS
            if not kinds:
                continue
            pcm, rate = _read_control(row['wav'], row['input_sha256'], row['input_size'])
            for kind in kinds:
                fault_pcm = make_fault(pcm, kind)
                try:
                    sha, size = _planned_fault_identity(fault_pcm, rate)
                except ValueError as error:
                    raise ValueError(f"{run}/{row['clip_id']}/{kind}: {error}") from error
                path = _confined_path(derived_dir, run, row['clip_id'], kind)
                cases.append(dict(case_id=f"{row['clip_id']}__{kind}", run=run, group=group,
                                  fault=kind, expect='defect', text=text, wav=str(path),
                                  input_sha256=sha, input_size=size,
                                  source_sha256=row['input_sha256'],
                                  _source_wav=row['wav'], _source_size=row['input_size']))
    return cases


def publish_cases(cases, derived_dir, done=()):
    """Publish pending fault WAVs after resume validation; verify done evidence in place."""
    from speech_verification_runtime import get_verifier_file_identity
    from project_scope_files import capture_scope_file
    from word_timing import MAX_AUDIO_BYTES
    root = _get_derived_root(derived_dir)
    root.mkdir(parents=True, exist_ok=True)
    # Validate every completed artifact before replacing any pending artifact.
    for case in cases:
        if case['fault'] == 'control':
            continue
        path = Path(case['wav'])
        if path.parent != root.absolute() and path.parent.resolve() != root.resolve():
            raise ValueError(f"{case['case_id']}: derived path is outside {root}")
        if (case['run'], case['case_id']) in done:
            info, _ = capture_scope_file(root, path.name, limit=MAX_AUDIO_BYTES)
            if not info['exists'] or info['sha256'] != case['input_sha256'] or info['size'] != case['input_size']:
                raise ValueError(f"{case['case_id']}: prior derived evidence changed; refusing to overwrite")
    for case in cases:
        if case['fault'] == 'control' or (case['run'], case['case_id']) in done:
            continue
        path = Path(case['wav'])
        pcm, rate = _read_control(case['_source_wav'], case['source_sha256'], case['_source_size'])
        fault_pcm = make_fault(pcm, case['fault'])
        staged = root / f'.{path.name}.{uuid.uuid4().hex}.tmp'
        try:
            sf.write(str(staged), fault_pcm, rate, format='WAV', subtype='PCM_16')
            info = get_verifier_file_identity(staged, MAX_AUDIO_BYTES, lambda: False)
            if info['sha256'] != case['input_sha256'] or info['size'] != case['input_size']:
                raise ValueError(f"{case['case_id']}: staged fault differs from its plan")
            os.replace(staged, path)
        finally:
            if staged.exists():
                staged.unlink()


def get_runtime_identity(root):
    """The verifier's content identity, including decoder, libraries, adapter code and options."""
    from speech_verification_runtime import get_verification_runtime, get_verifier_identity
    def one(confirmation):
        role = 'confirmation' if confirmation else 'primary'
        return get_verifier_identity(get_verification_runtime(root, confirmation=confirmation),
                                     role, lambda: False)
    return {'primary': one(False), 'confirmation': one(True)}


def _validate_result_row(row, location):
    """Validate the completed-result schema and finite bounds without touching the file."""
    if not isinstance(row, dict):
        raise ValueError(f'{location}: result row is not an object')
    if row.get('run') not in RUNS:
        raise ValueError(f'{location}: unknown run {row.get("run")!r}')
    case_id = row.get('case_id')
    if not isinstance(case_id, str) or not SAFE_COMPONENT.fullmatch(case_id):
        raise ValueError(f'{location}: unsafe or missing case_id')
    if row.get('fault') not in FAULT_KINDS:
        raise ValueError(f'{location}: unknown fault {row.get("fault")!r}')
    if row.get('expect') not in ('clean', 'defect'):
        raise ValueError(f'{location}: unknown expectation {row.get("expect")!r}')
    text = row.get('text')
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError(f'{location}: text is missing or too long')
    if row.get('status') not in ALLOWED_STATUSES:
        raise ValueError(f'{location}: invalid or missing status')
    seconds = row.get('seconds')
    if (type(seconds) not in (int, float) or isinstance(seconds, bool)
            or not math.isfinite(seconds) or not 0 <= seconds <= 86400):
        raise ValueError(f'{location}: seconds is missing or out of bounds')
    if row['status'] != 'unavailable' and not isinstance(row.get('primary'), str):
        raise ValueError(f'{location}: completed ASR requires a primary transcript')
    if row['status'] in ('unavailable', 'review_confirmation_unavailable') and not isinstance(row.get('reason'), str):
        raise ValueError(f'{location}: unavailable verification requires a reason')
    if row['status'] == 'corroborated_content_difference' and (
            not isinstance(row.get('confirmation'), str)
            or type(row.get('corroborated')) is not int or row['corroborated'] <= 0):
        raise ValueError(f'{location}: corroborated verdict lacks confirmation evidence')
    if row['status'] != 'unavailable' or row.get('duration') is not None:
        duration = row.get('duration')
        if (type(duration) not in (int, float) or isinstance(duration, bool)
                or not math.isfinite(duration) or not 0 <= duration <= 86400):
            raise ValueError(f'{location}: duration is out of bounds')
    if 'corroborated' in row:
        corroborated = row['corroborated']
        if type(corroborated) is not int or corroborated < 0:
            raise ValueError(f'{location}: corroborated count is invalid')
    for field in ('primary', 'confirmation', 'reason'):
        if field in row and row[field] is not None and not isinstance(row[field], str):
            raise ValueError(f'{location}: {field} is not text')
    if not isinstance(row.get('primary'), str) and not isinstance(row.get('reason'), str):
        raise ValueError(f'{location}: no verifier result')
    if not isinstance(row.get('wav'), str) or not row['wav'] or len(row['wav']) > 4096:
        raise ValueError(f'{location}: wav path is missing or too long')
    if 'input_sha256' in row and not _is_sha(row['input_sha256']):
        raise ValueError(f'{location}: input_sha256 is not a sha256')
    if 'input_size' in row and (type(row['input_size']) is not int or row['input_size'] <= 0):
        raise ValueError(f'{location}: input_size is invalid')
    if 'runtime' in row and not isinstance(row['runtime'], dict):
        raise ValueError(f'{location}: runtime is not an object')


def _read_results_lines(results):
    raw = _read_bounded_bytes(results, MAX_RESULTS_BYTES, missing=True)
    return raw.decode('utf-8').splitlines()


def get_reusable_results(results, cases):
    """-> {(run, case_id)} already done for exactly these inputs; raises on anything else."""
    by_key = {(c['run'], c['case_id']): c for c in cases}
    done = set()
    for number, line in enumerate(_read_results_lines(Path(results)), 1):
        if not line:
            continue
        try:
            from json_metadata import get_unique_json_object
            row = json.loads(line, object_pairs_hook=get_unique_json_object,
                             parse_constant=_reject_constant)
        except json.JSONDecodeError as error:
            raise ValueError(f'{results}:{number}: invalid JSON') from error
        _validate_result_row(row, f'{results}:{number}')
        key = (row.get('run'), row.get('case_id'))
        if key in done:
            raise ValueError(f'{results}:{number}: {key} appears twice')
        case = by_key.get(key)
        if case is None or any(row.get(field) != case.get(field) for field in BINDING):
            raise ValueError(f'{results}:{number}: {key} was run on other inputs or another '
                             'verifier; use a new --out-dir instead of resuming into this one')
        done.add(key)
    return done


def _verify(job):
    case, root, scratch = job
    import cpu_asr_scratch
    cpu_asr_scratch.get_cpu_asr_scratch_parent = lambda: Path(scratch)
    from speech_verification import verify_finished_speech
    from speech_verification_runtime import get_verification_runtime
    report = verify_finished_speech(case['wav'], case['text'], 'en',
        get_verification_runtime(root), get_verification_runtime(root, confirmation=True))
    audio = report.get('audio') or {}
    if (audio.get('sha256') != case['input_sha256'] or audio.get('size') != case['input_size']):
        raise ValueError(f"{case['case_id']}: verifier consumed different audio than its planned proof")
    planned = case['runtime']
    primary = (report.get('primary') or {}).get('identity')
    if primary is None:
        if report.get('status') != 'unavailable':
            raise ValueError(f"{case['case_id']}: verifier did not report its actual primary identity")
    elif primary != planned.get('primary'):
        raise ValueError(f"{case['case_id']}: primary verifier identity changed before publishing")
    confirmation = report.get('confirmation')
    if confirmation and confirmation.get('identity') != planned.get('confirmation'):
        raise ValueError(f"{case['case_id']}: confirmation verifier identity changed before publishing")
    out = {key: value for key, value in case.items() if not key.startswith('_')}
    out.update(status=report['status'], seconds=round(report['seconds'], 2),
               duration=report.get('duration'), reason=report.get('reason'),
               primary=(report.get('primary') or {}).get('transcript'),
               confirmation=(report.get('confirmation') or {}).get('transcript'),
               corroborated=len(report.get('corroborated_regions') or ()))
    _validate_result_row(out, case['case_id'])
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pilot-root', required=True, help='folder holding the pilot run folders')
    parser.add_argument('--asr-root', required=True, help='checkout holding whisper.cpp and models/')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--fault-seeds', default='15451,14351,14341')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--runs', default=','.join(RUNS))
    parser.add_argument('--limit', type=int, default=0, help='run only the first N pending cases')
    args = parser.parse_args(argv)
    out = Path(args.out_dir)
    derived = out / 'derived'
    cases = get_cases(args.pilot_root, derived, {int(s) for s in args.fault_seeds.split(',')},
                      args.runs.split(','))
    runtime = get_runtime_identity(args.asr_root)
    cases = [dict(c, runtime=runtime) for c in cases]
    results = out / 'results.jsonl'
    done = get_reusable_results(results, cases)
    publish_cases(cases, derived, done)
    todo = [c for c in cases if (c['run'], c['case_id']) not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f'{len(cases)} cases, {len(done)} done, {len(todo)} to run', flush=True)
    scratch = [out / f'scratch{i}' for i in range(args.workers)]
    for path in scratch:
        path.mkdir(parents=True, exist_ok=True)
    jobs = [({key: value for key, value in c.items() if not key.startswith('_')},
             args.asr_root, str(scratch[i % args.workers])) for i, c in enumerate(todo)]
    by_key = {(c['run'], c['case_id']): c for c in todo}
    with multiprocessing.Pool(args.workers) as pool, results.open('ab') as fh:
        for i, row in enumerate(pool.imap_unordered(_verify_owned, jobs), 1):
            _validate_result_row(row, 'worker result')
            case = by_key.get((row['run'], row['case_id']))
            if case is None or any(row.get(field) != case.get(field) for field in BINDING):
                raise ValueError('Worker result does not match its planned case')
            encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
            if fh.tell() + len(encoded) > MAX_RESULTS_BYTES:
                raise ValueError('Results checkpoint exceeds its byte limit; prior rows were kept')
            fh.write(encoded)
            fh.flush()
            print(i, len(todo), row['case_id'], row['status'], row['seconds'], flush=True)


def _verify_owned(job):
    # Jobs are dealt round-robin to scratch parents, but a pool can hand two jobs with
    # the same parent to different workers; take that parent's lock file first.
    import fcntl
    lock = Path(job[2]) / '.harness.lock'
    with open(lock, 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return _verify(job)


if __name__ == '__main__':
    main()
