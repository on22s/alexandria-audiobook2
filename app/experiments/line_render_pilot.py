"""Owned fixed-line render pilots: #1545 (best of N), #1546 (numbers), #1435 (window seams),
#1434 (verifier short and authored-repetition lines).

Same owned, prepared-text render path as question_punctuation_ab.py (#1519):
fresh process, explicit local settings, absolute LoRA adapter paths, explicit
seeds, a NEW output folder, actual submitted text and artifact hashes checked
per clip. Runs are not resumed. Synthesis windows are disabled unless
`--windows-budget` is given; it enables them for local LoRA only, in the run's
own config copy, with the default seam policy.

`numbers` lines are synthetic. `alice` lines are narration from Alice's
Adventures in Wonderland (public domain). `windows` passages are from Alice and
The Picture of Dorian Gray (public domain); each splits exactly once at a
150-character budget, at the seam kind its id names (s=sentence, c=clause,
p=paragraph). `verify` lines are short Alice-style or synthetic lines; `numbers2` lines are synthetic. This captures clips only; selection,
ASR scoring and listening happen afterwards and are not validated here.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
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
from speech_text import get_speech_normalization

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
WINDOWS = [
    ("s1", "They all sat down at once, in a large ring, with the Mouse in the middle. Alice kept her eyes anxiously fixed on it, for she felt sure she would catch a bad cold if she did not get dry very soon."),
    ("s2", "The Mouse gave a sudden leap out of the water, and seemed to quiver all over with fright. \u201cOh, I beg your pardon!\u201d cried Alice hastily, afraid that she had hurt the poor animal\u2019s feelings."),
    ("s3", "A grasshopper began to chirrup by the wall, and like a blue thread a long thin dragon-fly floated past on its brown gauze wings. Lord Henry felt as if he could hear Basil Hallward\u2019s heart beating, and wondered what was coming."),
    ("s4", "I choose my friends for their good looks, my acquaintances for their good characters, and my enemies for their good intellects. A man cannot be too careful in the choice of his enemies."),
    ("c1", "The rabbit-hole went straight on like a tunnel for some way, and then dipped suddenly down, so suddenly that Alice had not a moment to think about stopping herself before she found herself falling down a very deep well."),
    ("c2", "There were doors all round the hall, but they were all locked; and when Alice had been all the way down one side and up the other, trying every door, she walked sadly down the middle, wondering how she was ever to get out again."),
    ("c3", "Alice thought the whole thing very absurd, but they all looked so grave that she did not dare to laugh; and, as she could not think of anything to say, she simply bowed, and took the thimble, looking as solemn as she could."),
    ("c4", "Alice laughed so much at this, that she had to run back into the wood for fear of their hearing her; and when she next peeped out the Fish-Footman was gone, and the other was sitting on the ground near the door, staring stupidly up into the sky."),
    ("p1", "And Alice was so much frightened that she ran off at once in the direction it pointed to, without trying to explain the mistake it had made.\n\n\u201cHe took me for his housemaid,\u201d she said to herself as she ran."),
    ("p2", "\u201cYou\u2019ll get used to it in time,\u201d said the Caterpillar; and it put the hookah into its mouth and began smoking again.\n\nThis time Alice waited patiently until it chose to speak again."),
    ("p3", "He spoke very slowly, and the words seemed wrung out of him almost against his will.\n\n\u201cWhat nonsense you talk!\u201d said Lord Henry, smiling, and taking Hallward by the arm, he almost led him into the house."),
    ("p4", "\u201cIt is quite finished,\u201d he cried at last, and stooping down he wrote his name in long vermilion letters on the left-hand corner of the canvas.\n\nLord Henry came over and examined the picture."),
]
WINDOW_SEAMS = {"s": "sentence", "c": "clause", "p": "paragraph"}
# #1434 verifier cases the other sets lack: short utterances (v, under six words)
# and repetition that is written in the text (r), which must not be flagged.
VERIFY = [
    ("v01", "Yes."),
    ("v02", "No, sir."),
    ("v03", "Oh!"),
    ("v04", "Who are you?"),
    ("v05", "Come back!"),
    ("v06", "Never mind."),
    ("v07", "Hold your tongue!"),
    ("v08", "Not at all."),
    ("r01", "No, no, no, no!"),
    ("r02", "Off with her head! Off with her head!"),
    ("r03", "Faster! Faster!"),
    ("r04", "Twinkle, twinkle, little bat! How I wonder what you're at!"),
    ("r05", "I am very, very, very sorry."),
    ("r06", "Beautiful, beautiful Soup!"),
    ("r07", "Wake up, wake up, wake up!"),
    ("r08", "Oh dear! Oh dear! I shall be too late!"),
]
# #1546 round 2: the three conventions misread in round 1 (cents, dash ranges,
# slash dates), several lines each, all synthetic.
NUMBERS2 = [
    ("m1", "It cost $3.25 at the market."),
    ("m2", "He still owed her $12.80."),
    ("m3", "The fare was only $0.75."),
    ("m4", "She found $2.05 in her coat pocket."),
    ("m5", "A loaf of bread cost $1.10 that year."),
    ("m6", "The ticket was \u00a33.50 each way."),
    ("g1", "Read chapters 3-5 tonight."),
    ("g2", "The meeting runs 9-11 tomorrow morning."),
    ("g3", "He lived there from 1890-1902."),
    ("g4", "See pages 45-48 for the map."),
    ("g5", "Children aged 6-12 may enter."),
    ("g6", "Questions 1-10 are the easy ones."),
    ("d1", "On 3/14/1879 he was born."),
    ("d2", "The note was dated 7/4/1776."),
    ("d3", "By 11/11/1918 the war was over."),
    ("d4", "She wrote to him on 1/2/1900."),
    ("d5", "The ship sailed on 6/30/1912."),
]
LINE_SETS = {"numbers": NUMBERS, "alice": ALICE, "windows": WINDOWS, "verify": VERIFY,
             "numbers2": NUMBERS2}


def get_window_settings(budget):
    """Requested synthesis-window settings for local LoRA, default seam policy."""
    from synthesis_settings import get_synthesis_window_settings
    if type(budget) is not int or budget < 1:
        raise ValueError("Choose a positive integer window budget")
    return get_synthesis_window_settings({'enabled': True, 'budgets': {'local.lora': {'max_chars': budget}}})


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
        raise ValueError("Disable synthesis windows in the config; use --windows-budget instead")
    budget = getattr(args, 'windows_budget', None)
    if budget is not None:
        config = copy.deepcopy(config)
        config['tts']['synthesis_windows'] = get_window_settings(budget)
        config_raw = json.dumps(config, indent=2).encode('utf-8')
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


def render_exact(engine, text, voice, path, provenance_kind='local_lora_api'):
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
                or observed.get('provenance') != provenance_kind
                or observed.get('audio', {}).get('sha256') != file_sha256(path)):
            raise ValueError("Pilot clip lacks matching actual submitted-input evidence")
        ensure_audio_generation_active()
        return observed


def get_peak_memory_reset():
    """Return a reader of peak device memory since this call, or None without a GPU."""
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    torch.cuda.reset_peak_memory_stats()
    return torch.cuda.max_memory_allocated


def render_pilot_lines(engine, entries, seeds, lines, out, identity, line_set, windows_budget=None,
                       prepare_text=False):
    from audio_generation_owner import ensure_audio_generation_active, ensure_audio_generation_publication

    settings = copy.deepcopy(engine.get_model_settings())
    expected = None if windows_budget is None else get_window_settings(windows_budget)
    if settings.get('mode') != 'local' or get_enabled_synthesis_window_settings(settings.get('synthesis_windows')) != expected:
        raise ValueError("The admitted engine must be local with exactly the requested synthesis windows")
    provenance_kind = 'local_lora_api' if expected is None else 'composed_windows_api_inputs'
    entries = freeze_question_voices(entries)
    rows = []
    for line_id, requested in lines:
        # --prepare-text submits what the app's own path would: the line after
        # get_speech_normalization (#1546). Without it the line is sent as written.
        text = get_speech_normalization(requested)['text'] if prepare_text else requested
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
                plan = None
                if expected is not None:
                    plan = [{'start': w.start, 'end': w.end, 'boundary': w.boundary}
                            for w in engine._get_synthesis_window_plan(text, 'lora').windows]
                peak = get_peak_memory_reset()
                started = time.monotonic()
                observed = render_exact(engine, text, dict(entry, seed=str(seed)), wav,
                                        'local_lora_api' if plan is None or len(plan) == 1 else provenance_kind)
                seconds = round(time.monotonic() - started, 3)
                if engine.get_model_settings() != settings:
                    raise ValueError("Engine settings changed during rendering")
                rows.append({'clip_id': clip_id, 'line_set': line_set, 'line_id': line_id,
                    'speaker': speaker, 'adapter_id': entry.get('adapter_id'),
                    'adapter_generation_sha256': entry['adapter_generation_sha256'],
                    'seed': seed, 'requested_text': requested, 'prepared_text': prepare_text,
                    'submitted_text': observed['text'], 'speech_input': observed,
                    'wav': str(wav), 'wav_sha256': observed['audio']['sha256'],
                    'window_plan': plan, 'render_seconds': seconds,
                    'peak_device_bytes': None if peak is None else int(peak())})
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
    parser.add_argument('--windows-budget', type=int, default=None,
                        help='enable synthesis windows for local LoRA at this character budget (#1435)')
    parser.add_argument('--prepare-text', action='store_true',
                        help="submit each line after the app's speech preparation, as production does")
    parser.add_argument('--date-order', choices=('mdy', 'dmy'), default=None,
                        help="this run's book order for ambiguous slash dates (#1546)")
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
        if args.date_order:
            # The run's own book state; the live book is never touched.
            (data / 'state.json').write_text(json.dumps({'speech_date_order': args.date_order}))
        atomic_json_write({'status': 'incomplete', 'identity': identity, 'provenance': identity['provenance'], 'rows': []}, str(out / 'rows.json'))
        os.environ['ALEXANDRIA_DATA_DIR'] = str(data)
        import core
        document = run_owned_audio_experiment(core, lambda engine: render_pilot_lines(
            engine, entries, seeds, lines, out, identity, args.set, args.windows_budget,
            args.prepare_text), out / 'run')
        if get_experiment_source_identity(root) != {key: identity[key] for key in ('head', 'tree')}:
            raise ValueError('Experiment source changed during rendering')
    except (OSError, ValueError, RuntimeError, HTTPException) as error:
        parser.error(str(error))
    print(json.dumps({'status': document['status'], 'clips': len(document['rows']), 'out': str(out),
                      'qualification': document['qualification']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
