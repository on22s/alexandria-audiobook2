"""Bounded, credential-safe summaries of supported configuration changes."""
from typing import Literal, get_args, get_origin

from fastapi.encoders import jsonable_encoder

from config_settings import AppConfig, GenerationConfig, LLMConfig, PromptConfig, TTSConfig

_ABSENT = object()
_MODELS = {'llm_local': LLMConfig, 'llm_remote': LLMConfig, 'tts': TTSConfig,
           'generation': GenerationConfig, 'prompts': PromptConfig}
_SCOPES = {'tts': 'Audio generation', 'generation': 'Script generation', 'prompts': 'Prompts'}
_LABELS = {'api_key': 'API credential', 'base_url': 'Model endpoint', 'model_name': 'Model',
           'max_input_tokens': 'Input ceiling (tokens)', 'max_output_tokens': 'Output ceiling (tokens)',
           'context_length': 'Total context budget (tokens)', 'provider_headers': 'Custom headers',
           'provider_extra_body': 'Custom request body', 'llm_mode': 'Active model location',
           'llm_failover': 'Automatic model failover', 'llm_remote_ssh': 'Remote management connection',
           'prompt_presets': 'Saved attribution presets'}
_VISIBLE_TEXT = {'model_name', 'device', 'language', 'attribution_preset', 'pass1_preset', 'pass3_preset'}


def get_literal_values(annotation):
    if get_origin(annotation) is Literal:
        return set(get_args(annotation))
    return {value for member in get_args(annotation) for value in get_literal_values(member)}


def get_preview_value(value, key, section='', allowed=frozenset()):
    if value is _ABSENT:
        return 'Not explicitly saved'
    if value is None:
        return 'Blank / inherited'
    if key in {'api_key', 'llm_remote_ssh', 'base_url', 'url'}:
        return 'Configured (value hidden)' if value else 'Empty'
    if key in {'provider_headers', 'provider_extra_body'}:
        return f'{len(value)} entries (contents hidden)' if isinstance(value, dict) else 'Contents hidden'
    if key == 'external_urls':
        return f'{len(value)} endpoints (values hidden)' if isinstance(value, list) else 'Values hidden'
    if type(value) is bool:
        return 'On' if value else 'Off'
    if type(value) in (int, float):
        return str(int(value)) if type(value) is float and value.is_integer() else str(value)
    if isinstance(value, str):
        if key in _VISIBLE_TEXT or value in allowed:
            return value[:120] + ('…' if len(value) > 120 else '')
        return f'Text: {len(value)} characters (contents hidden)'
    if isinstance(value, list):
        if value and all(type(item) in (int, float) for item in value):
            return ', '.join(map(str, value[:8])) + (f' … ({len(value)} values)' if len(value) > 8 else '')
        return f'{len(value)} items (contents hidden)'
    return 'Contents hidden'


def get_config_change_preview(existing, proposal, get_local_profile, *, exists):
    """Read schema defaults; never change the supplied document or model."""
    saved = jsonable_encoder(existing.data)
    after = proposal.model_dump(mode='json')
    changes, defaults = [], []
    unmanaged = len(set(saved) - set(AppConfig.model_fields))

    def append_row(section, key, old, new, default=_ABSENT, absent=False, field=None):
        before = default if old is _ABSENT else old
        allowed = get_literal_values(field.annotation)
        row = {'path': f'{section}.{key}' if section else key,
               'label': _LABELS.get(key, key.removeprefix('three_pass_').replace('_', ' ').capitalize()),
               'scope': (('Local model' if section == 'llm_local' else 'Remote model')
                         + (' · active after Save' if section == 'llm_' + proposal.llm_mode else ' · inactive after Save'))
                        if section in {'llm_local', 'llm_remote'} else _SCOPES.get(section, 'Setup'),
               'before': get_preview_value(before, key, section, allowed), 'after': get_preview_value(new, key, section, allowed),
               'was_default': absent and default is not _ABSENT}
        if before != new:
            changes.append(row)
        elif absent and new is not _ABSENT:
            defaults.append(row)

    for section, model in _MODELS.items():
        old = get_local_profile(saved) if section == 'llm_local' else saved.get(section)
        new = after.get(section)
        if old is None and new is None:
            continue
        old = old if isinstance(old, dict) else {}
        new = new if isinstance(new, dict) else {}
        unmanaged += len(set(old) - set(model.model_fields))
        for key, field in model.model_fields.items():
            default = _ABSENT if field.is_required() else field.get_default(call_default_factory=True)
            append_row(section, key, old.get(key, _ABSENT), new.get(key, _ABSENT),
                       default, key not in old, field)
    for key in ('llm_mode', 'llm_failover', 'llm_remote_ssh', 'prompt_presets'):
        field = AppConfig.model_fields[key]
        default = _ABSENT if field.is_required() else field.get_default(call_default_factory=True)
        append_row('', key, saved.get(key, _ABSENT), after[key], default, key not in saved, field)
    return {'changes': changes, 'defaults': defaults, 'active_after': proposal.llm_mode,
            'baseline': 'missing' if not exists else 'damaged' if existing.needs_backup else 'read',
            'warning_count': len(existing.warnings), 'unmanaged_fields': unmanaged,
            'mirror_alignment': saved.get('llm') != after['llm'],
            'notice': ('Preview compares supported settings, including default values and URL normalization. '
                       'Save may also fill missing defaults and align the active model copy. '
                       'This is a snapshot; preview again if the saved settings change.'),
            'timing': ('Save writes configuration and resets the cached TTS engine for its next use. '
                       'Future work uses saved settings; running work may keep settings it already loaded. '
                       'Preview does not apply settings or contact a model.')}
