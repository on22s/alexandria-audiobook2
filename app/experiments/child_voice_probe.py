"""Render four child voice candidates for listening; never publish to the catalog."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from utils import atomic_json_write

SAMPLE = 'I found a little blue stone beside the river. Can we keep it and show everyone when we get home?'
HELD_OUT = 'The lantern was still shining when I opened the door. I called to my friend, but nobody answered.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    from experiments.gpu_guard import require_free_gpu
    from experiments.provenance import provenance, file_sha256
    require_free_gpu('child voice probe')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ['ALEXANDRIA_DATA_DIR'] = str(out)
    from tts import TTSEngine
    from experiments.generation import render
    import soundfile as sf
    import numpy as np
    config = json.loads(Path(args.config).read_text())
    engine = TTSEngine(config)
    source = provenance(__file__, args)
    rows = []
    for gender in ('male', 'female'):
        for age, years in (('young_child','4'),('child','9')):
            description = (f'A natural English-speaking {years}-year-old {"boy" if gender=="male" else "girl"}. '
                'A clear youthful speaking voice with age-appropriate resonance and articulation. '
                'Calm everyday speech, without exaggerated squeaking, cartoon acting or baby talk.')
            started = time.monotonic()
            reference, rate = engine.generate_voice_design(description, SAMPLE, seed=733)
            # A held-out text through the actual clone path tests reusability.
            entry = {'type':'clone','ref_audio':reference,'ref_text':SAMPLE,'seed':'733'}
            destination = out/f'{gender}_{age}_held_out.wav'
            render(engine, HELD_OUT, '', 'CANDIDATE', {'CANDIDATE':entry}, entry, str(destination))
            clips = []
            for path in (Path(reference), destination):
                samples, sample_rate = sf.read(path)
                if not np.isfinite(samples).all() or not len(samples) or not np.max(np.abs(samples))>0:
                    raise ValueError(f'Invalid candidate audio: {path}')
                clips.append({'path':str(path),'sha256':file_sha256(path),
                              'duration_seconds':round(len(samples)/sample_rate,3),'sample_rate':sample_rate})
            rows.append({'target_gender':gender,'target_age_group':age,'description':description,
                         'reference_text':SAMPLE,'held_out_text':HELD_OUT,'seed':733,'clips':clips,
                         'elapsed_seconds':round(time.monotonic()-started,3),'listening_verdict':'pending'})
            atomic_json_write({'provenance':source,'candidates':rows,
                'notice':'Targets are design requests, not measured age/gender. Listening is required before catalog publication.'}, str(out/'candidates.json'))
            print(gender,age,rows[-1]['elapsed_seconds'],flush=True)


if __name__ == '__main__':
    main()
