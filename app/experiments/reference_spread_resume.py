"""Publish and verify completion receipts for the cloud clone-only resume chain."""
import argparse
import json
import math
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'app'))

from audio_validation import GeneratedAudioError, validate_generated_audio
from experiments.provenance import file_sha256
from experiments.reference_spread_compare import get_usable_reference_score
from utils import atomic_json_write


def get_resolved_path(repo, path):
    return (Path(repo) / path).resolve()


def get_reference_inputs(repo, build_path):
    repo = Path(repo).resolve()
    build = json.loads(Path(build_path).read_text())
    rows = build['test']
    if not isinstance(rows, list) or not rows:
        raise ValueError('build needs nonempty test rows')
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not row['id'] or row['id'] in ids):
            raise ValueError('build test IDs must be nonempty and unique')
        ids.add(row['id'])
        for key in ('book', 'text', 'human_wav'):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError('build test row needs ' + key)
        if (not isinstance(row.get('seconds'), (int, float))
                or isinstance(row['seconds'], bool) or not math.isfinite(row['seconds'])
                or row['seconds'] <= 0):
            raise ValueError('build needs positive finite human durations')
    paths = [Path(build_path).resolve(), repo / 'app/config.json',
             get_resolved_path(repo, build['ref_sample'])]
    paths.extend(get_resolved_path(repo, row['human_wav']) for row in rows)
    paths.extend(repo / path for path in (
        'app/experiments/ljspeech_generate.py', 'app/experiments/ljspeech_score.py',
        'app/experiments/generation.py', 'app/tts.py', 'app/audio_validation.py'))
    return {'schema': 1, 'seed': 1234, 'arms': ['clone'], 'limit': 0,
            'files': {str(path): file_sha256(path) for path in paths}}


def get_reference_outputs(repo, build_path, generated_path, score_path):
    repo = Path(repo).resolve()
    build = json.loads(Path(build_path).read_text())
    generated = json.loads(Path(generated_path).read_text())
    score = json.loads(Path(score_path).read_text())
    expected = {row['id']: row for row in build['test']}
    if (generated.get('arms') != ['clone'] or type(generated.get('seed')) is not int
            or generated['seed'] != 1234 or generated.get('failures') != []
            or generated.get('reference_id') != build.get('ref_source_id')
            or score.get('arms') != ['clone'] or score.get('ecapa_error') is not None
            or get_resolved_path(repo, score['source']) != Path(generated_path).resolve()):
        raise ValueError('generation/scoring settings or source do not match')
    files = [Path(generated_path).resolve(), Path(score_path).resolve()]
    for document, stage in ((generated, 'generate'), (score, 'score')):
        rows = document.get('rows')
        if not isinstance(rows, list) or len(rows) != len(expected):
            raise ValueError(stage + ' does not cover every expected row')
        seen = set()
        for row in rows:
            source = expected.get(row['id'])
            if source is None or row['id'] in seen:
                raise ValueError(stage + ' has unexpected or duplicate rows')
            seen.add(row['id'])
            if row['book'] != source['book'] or row['human_seconds'] != source['seconds']:
                raise ValueError(stage + ' row does not match the build')
            if stage == 'generate':
                if row['text'] != source['text'] or row['human_wav'] != source['human_wav']:
                    raise ValueError('generated row does not match source text/audio')
                wav = get_resolved_path(repo, row['clone_wav'])
                validate_generated_audio(str(wav), 'resume clone audio')
                files.append(wav)
            else:
                value = row['clone']['ecapa']
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or not -1 <= value <= 1):
                    raise ValueError('score row needs finite ECAPA in [-1, 1]')
    mean, count = get_usable_reference_score(score_path)
    if (count != len(expected) or not math.isclose(mean,
            sum(row['clone']['ecapa'] for row in score['rows']) / len(expected),
            rel_tol=1e-9, abs_tol=1e-9)):
        raise ValueError('score summary does not match its rows')
    return {str(path): file_sha256(path) for path in files}


def get_reference_generation_identity(repo, generated_path):
    generated = json.loads(Path(generated_path).read_text())
    paths = [Path(generated_path).resolve()]
    paths.extend(get_resolved_path(repo, row['clone_wav']) for row in generated['rows'])
    return {str(path): file_sha256(path) for path in paths}


def save_reference_begin(repo, build_path, receipt):
    atomic_json_write({'inputs': get_reference_inputs(repo, build_path)}, str(receipt) + '.pending')


def save_reference_score_begin(repo, build_path, generated_path, receipt):
    pending = json.loads(Path(str(receipt) + '.pending').read_text())
    if pending['inputs'] != get_reference_inputs(repo, build_path):
        raise ValueError('inputs changed during generation')
    atomic_json_write({'inputs': pending['inputs'],
                      'generation': get_reference_generation_identity(repo, generated_path)},
                     str(receipt) + '.pending')


def save_reference_completion(repo, build_path, generated_path, score_path, receipt):
    before = json.loads(Path(str(receipt) + '.pending').read_text())
    current = get_reference_inputs(repo, build_path)
    if (before['inputs'] != current
            or before['generation'] != get_reference_generation_identity(repo, generated_path)):
        raise ValueError('inputs or generation changed while the arm was running')
    outputs = get_reference_outputs(repo, build_path, generated_path, score_path)
    atomic_json_write({'inputs': current, 'outputs': outputs}, str(receipt))
    Path(str(receipt) + '.pending').unlink()


def get_completed_reference_arm(repo, build_path, generated_path, score_path, receipt):
    stored = json.loads(Path(receipt).read_text())
    current = get_reference_inputs(repo, build_path)
    outputs = get_reference_outputs(repo, build_path, generated_path, score_path)
    if stored != {'inputs': current, 'outputs': outputs}:
        raise ValueError('receipt input/output identities no longer match')
    return stored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('check', 'begin', 'score-begin', 'finish'))
    parser.add_argument('--repo', default=str(REPO))
    parser.add_argument('--build', required=True)
    parser.add_argument('--generated', required=True)
    parser.add_argument('--score', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    try:
        if args.mode == 'begin':
            save_reference_begin(args.repo, args.build, args.receipt)
        elif args.mode == 'score-begin':
            save_reference_score_begin(args.repo, args.build, args.generated, args.receipt)
        elif args.mode == 'finish':
            save_reference_completion(args.repo, args.build, args.generated, args.score, args.receipt)
        else:
            get_completed_reference_arm(args.repo, args.build, args.generated, args.score, args.receipt)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, GeneratedAudioError) as exc:
        print('reference resume: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
