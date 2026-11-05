import math
import numpy as np


def get_edit_distance(expected, observed):
    previous = list(range(len(observed) + 1))
    for index, token in enumerate(expected, 1):
        current = [index]
        for other_index, other in enumerate(observed, 1):
            current.append(min(current[-1] + 1, previous[other_index] + 1,
                               previous[other_index - 1] + (token != other)))
        previous = current
    return previous[-1]


def get_unit_vector(values):
    vector = np.asarray(values, dtype=np.float64)
    norm = np.linalg.norm(vector)
    if vector.ndim != 1 or not np.isfinite(vector).all() or not math.isfinite(norm) or norm <= 0:
        raise ValueError('Unavailable speaker embedding')
    return vector / norm


def get_identity_separation(arms):
    records = []
    for text_id in range(6):
        centers = {}
        for arm in ('no_lora', 'unmerged'):
            training = [get_unit_vector(row['speaker_embedding']) for row in arms[arm]['samples']
                        if row['text_id'] != text_id]
            assert len(training) == 10, 'missing or duplicate training rows'
            centers[arm] = get_unit_vector(np.mean(training, axis=0))
        for arm in ('no_lora', 'unmerged'):
            heldout = [row for row in arms[arm]['samples'] if row['text_id'] == text_id]
            assert len(heldout) == 2, 'missing heldout rows'
            for row in heldout:
                features = get_unit_vector(row['speaker_embedding'])
                difference = float(features @ (centers['unmerged'] - centers['no_lora']))
                prediction = 'unmerged' if difference > 0 else 'no_lora'
                signed_margin = difference if arm == 'unmerged' else -difference
                records.append({'text_id': text_id, 'seed': row['seed'], 'actual': arm,
                                'predicted': prediction, 'signed_margin': signed_margin,
                                'correct': prediction == arm})
    accuracy = sum(row['correct'] for row in records) / len(records)
    margin = sum(row['signed_margin'] for row in records) / len(records)
    return {'method': 'native_qwen_speaker_encoder_leave_one_text_out_nearest_centroid',
            'trials': len(records), 'balanced_accuracy': accuracy, 'mean_signed_margin': margin,
            'qualified_to_separate_serving_conditions': accuracy >= .75 and margin > 0,
            'not_claimed': 'perceptual voice identity, superiority, or generalization beyond these texts',
            'records': records}


def run_controls():
    assert get_edit_distance(['cat', 'dog'], ['cat', 'dog']) == 0
    assert get_edit_distance(['cat', 'dog'], ['dog', 'cat']) == 2
    assert get_edit_distance(['cat', 'dog'], []) == 2
    assert get_edit_distance(['cat', 'dog'], ['cat']) == 1
    assert get_edit_distance(['cat'], ['cat', 'cat']) == 1
    assert abs(float(get_unit_vector([1, 2]) @ get_unit_vector([1, 2])) - 1) < 1e-12
    for invalid in ([0, 0], [float('nan'), 1]):
        try:
            get_unit_vector(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid embedding was accepted')
    return {'controls': 8, 'failed': 0, 'skipped': 0}
