import argparse
import hashlib
import json
import math
from pathlib import Path
import numpy as np


def get_numeric_comparison(values):
    names = {precision + '_' + arm for precision in ('fp32', 'bf16')
             for arm in ('base', 'merged', 'unmerged')}
    if set(values) != names:
        raise ValueError('Numeric evidence must contain all six precision/arm arrays')
    arrays = {name: np.asarray(value, dtype=np.float32) for name, value in values.items()}
    if any(value.shape != (6, 3072) or not np.isfinite(value).all()
           for value in arrays.values()):
        raise ValueError('Numeric evidence requires six finite 3072-logit rows per arm')
    records = []
    for text_id in range(6):
        reference = arrays['fp32_merged'][text_id] - arrays['fp32_base'][text_id]
        reference_norm = float(np.linalg.norm(reference))
        if not math.isfinite(reference_norm) or reference_norm <= 0:
            raise ValueError('The fp32 merged-minus-base reference effect is unavailable')
        for arm in ('merged', 'unmerged'):
            candidate = arrays['bf16_' + arm][text_id] - arrays['bf16_base'][text_id]
            candidate_norm = float(np.linalg.norm(candidate))
            if not math.isfinite(candidate_norm) or candidate_norm <= 0:
                raise ValueError('The bf16 adapter effect is unavailable')
            record = {'text_id': text_id, 'arm': arm,
                      'retention': candidate_norm / reference_norm,
                      'cosine': float(reference @ candidate) / (reference_norm * candidate_norm),
                      'relative_error': float(np.linalg.norm(candidate - reference)) / reference_norm}
            if not all(math.isfinite(record[key]) for key in ('retention', 'cosine', 'relative_error')):
                raise ValueError('Numeric comparison is non-finite')
            records.append(record)
    errors = {arm: [row['relative_error'] for row in records if row['arm'] == arm]
              for arm in ('merged', 'unmerged')}
    medians = {arm: float(np.median(values)) for arm, values in errors.items()}
    gates = {'lower_median_relative_error': medians['unmerged'] < medians['merged'],
             'no_line_more_than_20_percent_worse': all(
                 unmerged <= 1.2 * merged for unmerged, merged in
                 zip(errors['unmerged'], errors['merged']))}
    return {'records': records, 'median_relative_l2_error': medians,
            'gates': gates, 'qualified': all(gates.values())}


def get_archived_comparison(directory):
    directory = Path(directory)
    stored = json.loads((directory / 'results.json').read_text(encoding='utf-8'))
    archive = directory / 'numeric-logits.npz'
    expected = stored['numeric_replay']['input_sha256']
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError('Numeric archive digest does not match the recorded evidence')
    with np.load(archive, allow_pickle=False) as data:
        comparison = get_numeric_comparison({name: data[name] for name in data.files})
    for observed, recorded in zip(comparison['records'], stored['numeric']['records']):
        if any(observed[key] != recorded[key] for key in ('text_id', 'arm')):
            raise ValueError('Numeric evidence ordering changed')
        for key in ('retention', 'cosine', 'relative_error'):
            if not math.isclose(observed[key], recorded[key], rel_tol=1e-5, abs_tol=1e-6):
                raise ValueError('Numeric comparison does not reproduce the recorded ' + key)
    if len(stored['numeric']['records']) != len(comparison['records']):
        raise ValueError('Numeric comparison record count changed')
    for arm, observed in comparison['median_relative_l2_error'].items():
        if not math.isclose(observed, stored['numeric']['median_relative_l2_error'][arm],
                            rel_tol=1e-5, abs_tol=1e-6):
            raise ValueError('Numeric median does not reproduce recorded evidence')
    return comparison


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Replay the bounded ROCm numeric gate on CPU, without Torch or model assets.')
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args()
    result = get_archived_comparison(arguments.directory)
    print(json.dumps(result, indent=2, allow_nan=False))
    raise SystemExit(0 if result['qualified'] else 1)
