"""Owned baseline/deterministic pair capture; no audible qualification implied.

The controller must hold AA2's audio task and supply its engine and frozen voice
configuration. Each destination is new; this module never reuses saved takes.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from adapter_publication import save_adapter_publication_bytes
from audio_generation_owner import (ensure_audio_generation_active,
    ensure_audio_generation_publication, get_audio_generation_claim_id)
from audio_validation import GeneratedAudioError, validate_generated_audio
from experiments.plan_dialogue_continuity import get_continuity_experiment_packet
from project_scope_files import capture_scope_file
from synthesis_recovery_identity import get_recovery_digest
from utils import file_lock
from word_timing import MAX_AUDIO_BYTES


def get_continuity_voice_inputs(voice_config, speaker, row_index):
    """Materialize the same selected mapping that both renders will receive."""
    from tts import (get_chunk_voice_inputs, get_resolved_speaker,
                     resolve_narrator_voice_config, voice_config_for_chunk)
    if not isinstance(voice_config, dict):
        raise ValueError('Continuity voices must be an object')
    voices = copy.deepcopy(voice_config)
    voices = voice_config_for_chunk(voices, speaker, row_index)
    resolved = get_resolved_speaker(speaker, voices)
    voices = voice_config_for_chunk(resolve_narrator_voice_config(resolved, voices), resolved, row_index)
    inputs = get_chunk_voice_inputs(voices, {'speaker': resolved}, row_index)
    seeds = {name: entry['seed'] for name, entry in inputs.items() if entry['type'] != 'ensemble'}
    if not seeds or any(type(seed) is not int or not 0 <= seed < 2**31 for seed in seeds.values()):
        raise ValueError('Continuity requires an explicit nonnegative seed for every effective voice')
    return voices, inputs, seeds


def save_continuity_render_pair(engine, raw_source, packet, row_index, voice_config, destination):
    """Render exactly two arms, publishing a complete new pair or nothing.

    The receipt captures requested and actually submitted text separately.
    Native serving/model identity, alignment, memory, drift and listening are
    still controller qualification responsibilities, never inferred here.
    """
    if get_audio_generation_claim_id() is None:
        raise ValueError('Continuity rendering requires an owned audio task')
    ensure_audio_generation_active()
    expected = get_continuity_experiment_packet(raw_source)
    if packet != expected:
        raise ValueError('Continuity source or loaded planner changed')
    rows = expected['plan']['rows']
    if type(row_index) is not int or not 0 <= row_index < len(rows):
        raise ValueError('Invalid continuity row index')
    row = rows[row_index]
    voices, voice_inputs, seeds = get_continuity_voice_inputs(voice_config, row['speaker'], row_index)
    # Validate serializability before inference; identity does not authorize reuse.
    voice_digest = get_recovery_digest(voices)
    destination = Path(destination)
    with file_lock(str(destination)):
        if os.path.lexists(destination):
            raise FileExistsError('Choose a new continuity pair destination')
        with tempfile.TemporaryDirectory(prefix='.continuity-', dir=destination.parent) as stage:
            stage = Path(stage)
            arms = {}
            for arm in ('baseline', 'deterministic'):
                ensure_audio_generation_active()
                request = row[arm]
                path = stage / (arm + '.wav')
                started = time.monotonic()
                result = engine.generate_voice_result(request['text'], request['instruct'],
                    row['speaker'], copy.deepcopy(voices), str(path), capture_input=True)
                ensure_audio_generation_active()
                if not result.success:
                    raise GeneratedAudioError('Continuity arm generation failed')
                validate_generated_audio(str(path), 'continuity ' + arm)
                facts, _ = capture_scope_file(stage, path.name, limit=MAX_AUDIO_BYTES)
                audio = {'sha256': facts['sha256'], 'size': facts['size']}
                captured = result.speech_input
                if (not isinstance(captured, dict) or captured.get('audio') != audio
                        or not isinstance(captured.get('text'), str) or not captured['text']):
                    raise GeneratedAudioError('Continuity arm has no matching submitted-input capture')
                arms[arm] = {'request': copy.deepcopy(request), 'audio': audio,
                    'speech_input': copy.deepcopy(captured),
                    'synthesis_receipt': copy.deepcopy(result.synthesis_receipt),
                    'elapsed_seconds': time.monotonic() - started}
            receipt = {'version': 1, 'source_sha256': hashlib.sha256(raw_source).hexdigest(),
                'planner_loaded_code_sha256': expected['planner_loaded_code_sha256'],
                'row_index': row_index, 'uid': row['uid'], 'speaker': row['speaker'],
                'voice_config_digest': voice_digest, 'effective_voice_inputs': voice_inputs, 'seeds': seeds,
                'model_settings': engine.get_model_settings(), 'arms': arms,
                'qualification': 'captured_only; alignment, memory, drift and listening remain unqualified'}
            encoded = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2,
                                  allow_nan=False) + '\n').encode('utf-8')
            save_adapter_publication_bytes(str(stage / 'receipt.json'), encoded)
            with ensure_audio_generation_publication():
                if os.path.lexists(destination):
                    raise FileExistsError('Continuity pair destination appeared during rendering')
                os.rename(stage, destination)
            return receipt
