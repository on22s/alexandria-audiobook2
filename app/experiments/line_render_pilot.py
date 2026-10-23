"""Owned fixed-line render pilots for #1545 (best of N takes) and #1546 (number reading).

Same owned, prepared-text render path as question_punctuation_ab.py (#1519):
fresh process, explicit local settings, absolute LoRA adapter paths, explicit
seeds, synthesis windows disabled, a NEW output folder, actual submitted text
and artifact hashes checked per clip. Runs are not resumed.

`numbers` lines are synthetic. `alice` lines are narration from Alice's
Adventures in Wonderland (public domain). This captures clips only; selection,
ASR scoring and listening happen afterwards and are not validated here.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from fastapi import HTTPException

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import atomic_json_write
from adapter_publication import save_adapter_publication_bytes
from experiments.provenance import file_sha256, provenance
from experiments.run_dialogue_continuity import (_get_input,
    get_continuity_output_path, get_experiment_source_identity, run_owned_audio_experiment)
from synthesis_settings import get_enabled_synthesis_window_settings

NUMBERS = [
    ("n01", "The letter was dated the 4th of July, 1865."),
    ("n02", "She paid $4.50 for the ribbon and 75 cents for the thread."),
    ("n03", "Chapter XII begins on page 247."),
    ("n04", "The train leaves at 7:45 and arrives at 10:15."),
    ("n05", "He was born in 1902 and died in 1987."),
    ("n06", "Only 3 of the 21 boats came back."),
    ("n07", "The bill came to \u00a312 and 6 shillings."),
    ("n08", "It was the 1st, 2nd and 3rd time she had asked."),
    ("n09", "King George III signed it in 1776."),
    ("n10", "The room was 12 feet by 15 feet."),
    ("n11", "They walked 2.5 miles before noon."),
    ("n12", "The population had grown to 1,250,000 by 1900."),
    ("n13", "Meet me at 221B Baker Street at 9 p.m."),
    ("n14", "The temperature fell to -5 degrees overnight."),
    ("n15", "Prices rose by 15% in the 1920s."),
    ("n16", "Volume II, Book 3, Chapter 14."),
    ("n17", "He finished 23rd out of 100 runners."),
    ("n18", "The war lasted from 1914 to 1918."),
    ("n19", "On 12/25/1890 the snow was deep."),
    ("n20", "She was 16 years old in the summer of '68."),
    ("n21", "The ship weighed 46,000 tons."),
    ("n22", "Louis XIV ruled for 72 years."),
    ("n23", "It cost $1,000,000 to build."),
    ("n24", "Read pages 10-12 before Monday."),
    ("n25", "The clock struck 3 at half past 2."),
    ("n26", "Only 1/3 of the flour was left."),
    ("n27", "The 19th century ended in 1900."),
    ("n28", "Henry VIII had 6 wives."),
    ("n29", "The road is 1.5 km long."),
    ("n30", "In 1066 the battle was fought at Hastings."),
]
ALICE = [
    ("a01", "This time Alice waited patiently until it chose to speak again."),
    ("a02", "While she was looking at the place where it had been, it suddenly appeared again."),
    ("a03", "There was certainly too much of it in the air."),
    ("a04", "The Mouse gave a sudden leap out of the water, and seemed to quiver all over with fright."),
    ("a05", "Alice led the way, and the whole party swam to the shore."),
    ("a06", "There was nothing else to do, so Alice soon began talking again."),
    ("a07", "Once more she found herself in the long hall, and close to the little glass table."),
    ("a08", "He had been looking at Alice for some time with great curiosity, and this was his first speech."),
    ("a09", "Alice was not much surprised at this, she was getting so used to queer things happening."),
    ("a10", "I almost think I can remember feeling a little different."),
    ("a11", "They very soon came upon a Gryphon, lying fast asleep in the sun."),
    ("a12", "So she set the little creature down, and felt quite relieved to see it trot away quietly into the wood."),
    ("a13", "She was walking by the White Rabbit, who was peeping anxiously into her face."),
    ("a14", "However, she did not like to be rude, so she bore it as well as she could."),
    ("a15", "There was a sound of many footsteps, and Alice looked round, eager to see the Queen."),
    ("a16", "Alice looked up, and there stood the Queen in front of them, with her arms folded, frowning like a thunderstorm."),
    ("a17", "The twelve jurors were all writing very busily on slates."),
    ("a18", "He looked at Alice, and tried to speak, but for a minute or two sobs choked his voice."),
    ("a19", "The Cat seemed to think that there was enough of it now in sight, and no more of it appeared."),
    ("a20", "Now, if you only kept on good terms with him, he would do almost anything you liked with the clock."),
]
LINE_SETS = {"numbers": NUMBERS, "alice": ALICE}


def get_voice_entries(raw, speakers):
    from tts import voice_category

    if not isinstance(raw, dict):
        raise ValueError("Voice configuration must be an object")
    config = raw.get("characters") if isinstance(raw.get("characters"), dict) else raw
    entries = {}
    for speaker in speakers:
        entry = config.get(speaker)
        if not isinstance(entry, dict) or voice_category(entry) != "lora":
            raise ValueError(f"{speaker!r} is not a LoRA voice")
        path = entry.get("adapter_path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("Declare an absolute adapter_path for each experiment voice")
        entries[speaker] = copy.deepcopy(entry)
    return entries


def get_question_inputs(args):
    voices_raw, config_raw = map(_get_input, (args.voice_config, args.config))
    voices, config = json.loads(voices_raw), json.loads(config_raw)
    speakers = [s.strip() for s in args.speakers.split(",") if s.strip()]
    seeds = [int(s) for s in args.seeds.split(",")]
    if not speakers or len(speakers) != len(set(speakers)):
        raise ValueError("Choose nonempty, unique speakers")
    if not seeds or len(seeds) != len(set(seeds)) or any(not 0 <= seed < 2**31 for seed in seeds):
        raise ValueError("Choose unique explicit seeds from 0 through 2147483647")
    if args.set not in LINE_SETS:
        raise ValueError("Choose a known line set")
    lines = LINE_SETS[args.set]
    if not 0 <= args.limit <= len(lines):
        raise ValueError("limit must be between zero and the number of fixture lines")
    if not isinstance(config, dict) or not isinstance(config.get("tts"), dict) or config['tts'].get('mode') != 'local':
        raise ValueError("This experiment requires explicit local TTS settings")
    if get_enabled_synthesis_window_settings(config['tts'].get('synthesis_windows')) is not None:
        raise ValueError("Disable synthesis windows for the single-request punctuation comparison")
    entries = get_voice_entries(voices, speakers)
    return config_raw, voices_raw, entries, seeds, lines[:args.limit] if args.limit else lines


def freeze_question_voices(entries):
    from adapter_checkpoint_transaction import ensure_adapter_generation_snapshot
    from audio_generation_owner import get_audio_generation_claim_id
    from voice_manifest import get_resolved_adapter_path

    if get_audio_generation_claim_id() is None:
        raise ValueError("Pilot rendering requires an owned audio task")
    frozen = {}
    for speaker, entry in entries.items():
        path = get_resolved_adapter_path(entry['adapter_path'])
        with ensure_adapter_generation_snapshot(path) as (_snapshot, generation):
            expected = entry.get('adapter_generation_sha256')
            if expected is not None and expected != generation:
                raise ValueError("Adapter changed since experiment input admission")
            frozen[speaker] = {**entry, 'adapter_path': path, 'adapter_generation_sha256': generation}
    return frozen


def render_exact(engine, text, voice, path):
    """Owned prepared-text submission; fresh artifacts only, with observed proof."""
    from audio_generation_owner import ensure_audio_generation_active, get_audio_generation_claim_id
    from audio_validation import validate_generated_audio
    from synthesis_input import ensure_speech_input_capture, get_captured_speech_input

    if get_audio_generation_claim_id() is None:
        raise ValueError("Pilot rendering requires an owned audio task")
    ensure_audio_generation_active()
    if os.path.lexists(path):
        raise FileExistsError("Pilot output already exists; choose a fresh run")
    with ensure_speech_input_capture():
        if engine._generate_lora_voice_prepared(text, "", copy.deepcopy(voice), str(path)) is not True:
            raise RuntimeError("Pilot generation did not finish")
        ensure_audio_generation_active()
        validate_generated_audio(str(path), "render pilot")
        observed = get_captured_speech_input(str(path))
        if (not isinstance(observed, dict) or observed.get('text') != text
                or observed.get('provenance') != 'local_lora_api'
                or observed.get('audio', {}).get('sha256') != file_sha256(path)):
            raise ValueError("Pilot clip lacks matching actual submitted-input evidence")
        ensure_audio_generation_active()
        return observed


def render_pilot_lines(engine, entries, seeds, lines, out, identity, line_set):
    from audio_generation_owner import ensure_audio_generation_active, ensure_audio_generation_publication

    settings = copy.deepcopy(engine.get_model_settings())
    if settings.get('mode') != 'local' or get_enabled_synthesis_window_settings(settings.get('synthesis_windows')) is not None:
        raise ValueError("The admitted engine must be local with synthesis windows disabled")
    entries = freeze_question_voices(entries)
    rows = []
    for line_id, text in lines:
        for speaker, entry in entries.items():
            # Speaker labels remain metadata, never filesystem path components.
            speaker_id = hashlib.sha256(speaker.encode('utf-8')).hexdigest()[:12]
            for seed in seeds:
                ensure_audio_generation_active()
                if engine.get_model_settings() != settings:
                    raise ValueError("Engine settings changed during the pilot")
                clip_id = f"{line_set}_{line_id}_{speaker_id}_{seed}"
                wav = out / 'wav' / (clip_id + '.wav')
                if wav.resolve().parent != (out / 'wav').resolve():
                    raise ValueError("Pilot output escaped its run directory")
                observed = render_exact(engine, text, dict(entry, seed=str(seed)), wav)
                if engine.get_model_settings() != settings:
                    raise ValueError("Engine settings changed during rendering")
                rows.append({'clip_id': clip_id, 'line_set': line_set, 'line_id': line_id,
                    'speaker': speaker, 'adapter_id': entry.get('adapter_id'),
                    'adapter_generation_sha256': entry['adapter_generation_sha256'],
                    'seed': seed, 'requested_text': text,
                    'submitted_text': observed['text'], 'speech_input': observed,
                    'wav': str(wav), 'wav_sha256': observed['audio']['sha256']})
                atomic_json_write({'status': 'incomplete', 'identity': identity, 'provenance': identity['provenance'],
                    'model_settings': settings, 'rows': rows}, str(out / 'rows.json'))
    document = {'status': 'captured', 'identity': identity, 'provenance': identity['provenance'], 'model_settings': settings, 'rows': rows,
                'qualification': 'exact submitted inputs captured; selection, ASR and listening effects unvalidated'}
    manifest = ''.join(json.dumps({'id': row['clip_id'], 'wav': row['wav'],
        'text': row['submitted_text'], 'wav_sha256': row['wav_sha256']}) + '\n' for row in rows)
    with ensure_audio_generation_publication():
        if os.path.lexists(out / 'manifest.jsonl'):
            raise FileExistsError('Pilot manifest already exists')
        save_adapter_publication_bytes(str(out / 'manifest.jsonl'), manifest.encode('utf-8'))
        atomic_json_write(document, str(out / 'rows.json'))
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice-config', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--set', required=True, choices=sorted(LINE_SETS))
    parser.add_argument('--speakers', default='NARRATOR')
    parser.add_argument('--seeds', default='1234')
    parser.add_argument('--limit', type=int, default=0, help='0 = all lines in the set')
    parser.add_argument('--out-dir', required=True)
    args = parser.parse_args(argv)
    try:
        if 'core' in sys.modules or os.environ.get('ALEXANDRIA_DATA_DIR'):
            raise ValueError('Run in a fresh process without a data directory override')
        config_raw, voices_raw, entries, seeds, lines = get_question_inputs(args)
        root = Path(REPO).resolve()
        identity = {**get_experiment_source_identity(root), 'config_sha256': hashlib.sha256(config_raw).hexdigest(),
                    'voices_sha256': hashlib.sha256(voices_raw).hexdigest(), 'provenance': provenance(__file__, args)}
        out = get_continuity_output_path(root, args.out_dir)
        out.mkdir(mode=0o700, exist_ok=False)
        (out / 'wav').mkdir(); data = out / 'data'; data.mkdir()
        (data / 'config.json').write_bytes(config_raw)
        atomic_json_write({'status': 'incomplete', 'identity': identity, 'provenance': identity['provenance'], 'rows': []}, str(out / 'rows.json'))
        os.environ['ALEXANDRIA_DATA_DIR'] = str(data)
        import core
        document = run_owned_audio_experiment(core, lambda engine: render_pilot_lines(
            engine, entries, seeds, lines, out, identity, args.set), out / 'run')
        if get_experiment_source_identity(root) != {key: identity[key] for key in ('head', 'tree')}:
            raise ValueError('Experiment source changed during rendering')
    except (OSError, ValueError, RuntimeError, HTTPException) as error:
        parser.error(str(error))
    print(json.dumps({'status': document['status'], 'clips': len(document['rows']), 'out': str(out),
                      'qualification': document['qualification']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
