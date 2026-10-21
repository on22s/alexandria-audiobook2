"""Explicit synthesis-window settings; no backend budget is enabled by default."""
from dataclasses import asdict, fields

from synthesis_audio import SynthesisJoinPolicy
from synthesis_windows import MAX_WINDOW_SOURCE_CHARS
from tts_capabilities import get_tts_capability_modes


def get_synthesis_budget_keys():
    return tuple(f'{mode}.{category}' for mode, capabilities in get_tts_capability_modes().items()
                 for category in capabilities['voice_categories'])


def get_synthesis_window_settings(value=None):
    """Validate/copy saved controls without loading a model or changing a project."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'enabled', 'budgets', 'seams'}:
        raise ValueError('Invalid synthesis window settings')
    enabled = value.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('Synthesis window enabled must be a boolean')
    requested = value.get('budgets', {})
    allowed = get_synthesis_budget_keys()
    if not isinstance(requested, dict) or set(requested) - set(allowed):
        raise ValueError('Synthesis window budgets must use supported backend/voice categories')
    budgets = {}
    for key in allowed:
        if key not in requested:
            continue
        budget = requested[key]
        if not isinstance(budget, dict) or set(budget) - {'max_chars', 'max_words'}:
            raise ValueError('A synthesis budget needs explicit character and optional word limits')
        chars, words = budget.get('max_chars'), budget.get('max_words')
        if type(chars) is not int or not 1 <= chars <= MAX_WINDOW_SOURCE_CHARS:
            raise ValueError('Synthesis character limits must be positive bounded integers')
        if words is not None and (type(words) is not int or not 1 <= words <= 100000):
            raise ValueError('Synthesis word limits must be positive bounded integers')
        budgets[key] = dict(max_chars=chars, max_words=words)
    if enabled and not budgets:
        raise ValueError('Enable synthesis windows only with at least one explicit backend budget')
    seams = value.get('seams', {})
    if not isinstance(seams, dict) or set(seams) - {field.name for field in fields(SynthesisJoinPolicy)}:
        raise ValueError('Invalid internal synthesis seam settings')
    policy = SynthesisJoinPolicy(**seams)
    policy.validate()
    seams = asdict(policy)
    if seams['quiet_threshold_db'] is not None:
        seams['quiet_threshold_db'] = float(seams['quiet_threshold_db'])
    return dict(enabled=enabled, budgets=budgets, seams=seams)


def get_enabled_synthesis_window_settings(value=None):
    """Omit disabled controls from model identity to preserve legacy evidence."""
    settings = get_synthesis_window_settings(value)
    return settings if settings['enabled'] else None
