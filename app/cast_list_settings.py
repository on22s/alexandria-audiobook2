"""Shared, strict controls for experimental cast discovery (#1931)."""
DEFAULT_SOURCE_CHARS = 30000
DEFAULT_MAX_TOKENS = 8000
MAX_WINDOW_CHARS = 1200000


def get_cast_list_settings(config, *, incremental=None, source_chars=None, max_tokens=None):
    generation = config.get('generation') or {}
    enabled = generation.get('cast_list_incremental', False) if incremental is None else incremental
    chars = generation.get('cast_list_source_chars', DEFAULT_SOURCE_CHARS) if source_chars is None else source_chars
    tokens = generation.get('cast_list_max_tokens', DEFAULT_MAX_TOKENS) if max_tokens is None else max_tokens
    if type(enabled) is not bool:
        raise ValueError('Incremental cast must be a boolean')
    if type(chars) is not int or not 1 <= chars <= MAX_WINDOW_CHARS:
        raise ValueError('Cast source characters must be an integer from 1 to 1200000')
    if type(tokens) is not int or tokens <= 0:
        raise ValueError('Cast output tokens must be a positive integer')
    return {'incremental': enabled, 'source_chars': chars, 'max_tokens': tokens}


def get_cast_list_cli_args(settings):
    return ['--incremental' if settings['incremental'] else '--no-incremental',
            '--source-chars', str(settings['source_chars']), '--max-tokens', str(settings['max_tokens'])]
