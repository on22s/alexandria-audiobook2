"""Exploratory ECAPA controls; same-name groups are NOT speaker-identity ground truth."""
import argparse
import json
import math
import random
import statistics
import time

from casting_catalog import get_casting_catalog
from core import _norm_name
from voice_reference import _speaker_similarities, get_reference_audio_path


def get_probe_summary(scores):
    if (not scores or len(scores) % 3 or any(not isinstance(value, (float, int))
            or isinstance(value, bool) or not math.isfinite(value) for value in scores)):
        raise ValueError('Expected complete finite scores in candidate/random/contrast triples')
    candidates, randoms, contrasts = scores[::3], scores[1::3], scores[2::3]
    return {'groups': len(candidates), 'above_random': sum(a > b for a, b in zip(candidates, randoms)),
            'above_contrast': sum(a > b for a, b in zip(candidates, contrasts)),
            'mean_candidate': statistics.mean(candidates), 'mean_random': statistics.mean(randoms),
            'mean_contrast': statistics.mean(contrasts), 'validated_identity_separation': False,
            'limitation': 'Names and description genders are not verified speaker identities; these are exploratory controls only.'}


def run_probe(designed_dir, limit=15):
    voices = [voice for voice in get_casting_catalog(designed_dir)['voices'] if voice['audio_present']]
    groups = {}
    for voice in voices:
        groups.setdefault(_norm_name(voice['name']), []).append(voice)
    generator = random.Random(1532)
    pairs = []
    for name in sorted(groups):
        group = groups[name]
        if len(group) < 2:
            continue
        anchor, candidate = group[:2]
        gender = anchor['tags']['gender']
        others = [voice for voice in voices if _norm_name(voice['name']) != name]
        contrasts = [voice for voice in others if gender in ('Male', 'Female')
                     and voice['tags']['gender'] in ('Male', 'Female') and voice['tags']['gender'] != gender]
        if not others or not contrasts:
            continue
        matches = [candidate, generator.choice(others), generator.choice(contrasts)]
        def resolve(voice):
            return get_reference_audio_path(voice['preview_url'].removeprefix('/designed_voices/'), designed_dir)
        pairs.extend((resolve(anchor), resolve(match)) for match in matches)
        if len(pairs) >= limit * 3:
            break
    started = time.monotonic()
    scores = _speaker_similarities(pairs, dataset_root=designed_dir)
    if scores is None or len(scores) != len(pairs):
        raise RuntimeError('ECAPA did not return complete control scores')
    return {**get_probe_summary(scores), 'seconds': time.monotonic() - started, 'scores': scores}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('designed_dir')
    args = parser.parse_args()
    print(json.dumps(run_probe(args.designed_dir)))
