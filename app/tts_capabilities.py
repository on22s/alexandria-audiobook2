"""The verified AA2 dispatch contract, not a guess about remote model internals."""
_BACKENDS = {
    'local': {
        'voice_categories': ('custom', 'clone', 'lora', 'design', 'ensemble'),
        'controls': {'server': False, 'external_timeout': False, 'device': True, 'local_options': True},
        'description': 'Local synthesis supports custom, cloned, LoRA, designed and ensemble voices. Ensemble members must each be valid.',
        'language_note': 'The language setting is passed to the local Qwen3-TTS model.',
    },
    'external': {
        'voice_categories': ('custom', 'clone', 'ensemble'),
        'controls': {'server': True, 'external_timeout': True, 'device': False, 'local_options': False},
        'description': 'External synthesis supports custom and cloned voices, plus ensembles of supported members. LoRA and designed voices require local mode.',
        'language_note': 'The language setting is sent to the server; its supported languages are not verified here.',
    },
}
_VOICE_TYPES = {'custom': 'custom', 'clone': 'clone', 'lora': 'lora',
                'builtin_lora': 'lora', 'design': 'design', 'ensemble': 'ensemble'}


def get_tts_backend_capabilities(mode):
    if mode not in _BACKENDS:
        raise ValueError(f'Unknown TTS mode: {mode}')
    source = _BACKENDS[mode]
    return {
        'voice_categories': list(source['voice_categories']),
        'voice_types': {kind: category in source['voice_categories'] for kind, category in _VOICE_TYPES.items()},
        'controls': dict(source['controls']),
        'description': source['description'],
        'language_note': source['language_note'],
        'output_mode': 'buffered',
        'speed_control': False,
    }


def get_tts_capability_modes():
    return {mode: get_tts_backend_capabilities(mode) for mode in _BACKENDS}
