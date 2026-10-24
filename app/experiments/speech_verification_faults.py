"""#1434 qualification: false positives and missed defects of the finished-take verifier.

Runs the unchanged production `verify_finished_speech` (Small.en primary, distinct
Large-v3 confirmation, CPU) over:

- controls: real renders from line_render_pilot.py, each checked against the exact
  text its render proof says was sent to the model;
- faults: copies of some controls with a known defect cut or spliced into the PCM
  (`make_fault`), so the right answer is known by construction.

A control is a false positive when the verdict is not `no_content_candidate`; a
fault is missed when it is. Designed faults are not human-listened truth, and real
model failures (mumbled or skipped words in a render) can look different from a
cut. Each worker gets its own ASR scratch parent so workers do not contend for the
one per-user lease; nothing else in the verifier path is changed. Resumable: rows
already in results.jsonl are skipped.
"""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

import numpy as np
import soundfile as sf

CLEAR = 'no_content_candidate'
RUNS = ('alice', 'numbers', 'w1435_U', 'w1435_W', 'verify')
LONG_FAULTS = ('tail_cut', 'head_cut', 'tail_repeat', 'mid_repeat')
SHORT_FAULTS = ('short_double', 'short_half')


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


def get_rows(run_dir):
    data = json.loads((Path(run_dir) / 'rows.json').read_text())
    for row in data['rows']:
        proof = row['speech_input']
        if proof['audio']['sha256'] != row['wav_sha256']:
            raise ValueError(f"{row['clip_id']}: render proof does not match its WAV")
        yield row


def get_cases(pilot_root, derived_dir, fault_seeds, runs=RUNS):
    """Every clip is a control; clips with a seed in `fault_seeds` also seed faults."""
    cases = []
    derived_dir.mkdir(parents=True, exist_ok=True)
    for run in runs:
        for row in get_rows(Path(pilot_root) / run):
            text = row['speech_input']['text']
            group = row['line_id'][0] if run == 'verify' else run
            cases.append(dict(case_id=f"{row['clip_id']}__control", run=run, group=group,
                              fault='control', expect='clean', text=text, wav=row['wav']))
            if row['seed'] not in fault_seeds or run in ('numbers', 'w1435_W'):
                continue
            kinds = SHORT_FAULTS if group == 'v' else () if group == 'r' else LONG_FAULTS
            pcm, rate = sf.read(row['wav'], dtype='int16')
            if pcm.ndim != 1:
                raise ValueError(f"{row['clip_id']}: expected mono render")
            for kind in kinds:
                path = derived_dir / f"{row['clip_id']}__{kind}.wav"
                if not path.exists():
                    sf.write(str(path), make_fault(pcm, kind), rate, subtype='PCM_16')
                cases.append(dict(case_id=f"{row['clip_id']}__{kind}", run=run, group=group,
                                  fault=kind, expect='defect', text=text, wav=str(path)))
    return cases


def _verify(job):
    case, root, scratch = job
    import cpu_asr_scratch
    cpu_asr_scratch.get_cpu_asr_scratch_parent = lambda: Path(scratch)
    from speech_verification import verify_finished_speech
    from speech_verification_runtime import get_verification_runtime
    report = verify_finished_speech(case['wav'], case['text'], 'en',
        get_verification_runtime(root), get_verification_runtime(root, confirmation=True))
    out = dict(case, status=report['status'], seconds=round(report['seconds'], 2),
               duration=report.get('duration'), reason=report.get('reason'),
               primary=report.get('primary', {}).get('transcript'),
               confirmation=report.get('confirmation', {}).get('transcript'),
               corroborated=len(report.get('corroborated_regions') or ()))
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
    cases = get_cases(args.pilot_root, out / 'derived', {int(s) for s in args.fault_seeds.split(',')},
                      args.runs.split(','))
    results = out / 'results.jsonl'
    done = set()
    if results.exists():
        done = {json.loads(line)['case_id'] for line in results.read_text().splitlines() if line}
    todo = [c for c in cases if c['case_id'] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f'{len(cases)} cases, {len(done)} done, {len(todo)} to run', flush=True)
    scratch = [out / f'scratch{i}' for i in range(args.workers)]
    for path in scratch:
        path.mkdir(parents=True, exist_ok=True)
    jobs = [(c, args.asr_root, str(scratch[i % args.workers])) for i, c in enumerate(todo)]
    with multiprocessing.Pool(args.workers) as pool, results.open('a') as fh:
        for i, row in enumerate(pool.imap_unordered(_verify_owned, jobs), 1):
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
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
