"""Validate llama.cpp's read-only loaded-model and adapter identity responses."""
import json
import os
import sys


def is_expected_adapter_path(served, expected, resolve_local=False):
    """Compare full identities; only a local server may use this filesystem."""
    if not isinstance(served, str) or not served or not isinstance(expected, str) or not expected:
        return False
    if resolve_local:
        return os.path.realpath(served) == os.path.realpath(expected)
    return served == expected


def is_expected_llama_server(models, props, adapters, model, adapter, alias,
                             context=None, parallel=None, reasoning_off=False):
    if not isinstance(models, dict) or not isinstance(props, dict) or not isinstance(adapters, list):
        return False
    entries = models.get('data')
    if (not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict)
            or entries[0].get('id') != alias or not isinstance(entries[0].get('meta'), dict)):
        return False
    served_model = props.get('model_path')
    if (not isinstance(served_model, str) or not served_model
            or os.path.realpath(served_model) != os.path.realpath(model)
            or props.get('is_sleeping') is True):
        return False
    if context is not None or parallel is not None or reasoning_off:
        generation = props.get('default_generation_settings')
        if not isinstance(generation, dict):
            return False
        if context is not None and (type(generation.get('n_ctx')) is not int or generation['n_ctx'] != context):
            return False
        if parallel is not None and (type(props.get('total_slots')) is not int or props['total_slots'] != parallel):
            return False
        params = generation.get('params') or {}
        if reasoning_off and (not isinstance(params, dict) or params.get('reasoning_format') not in (None, 'none')):
            return False
    if not adapter:
        return not adapters
    if len(adapters) != 1 or not isinstance(adapters[0], dict):
        return False
    loaded = adapters[0]
    path, scale = loaded.get('path'), loaded.get('scale')
    return (is_expected_adapter_path(path, adapter, resolve_local=True)
            and not isinstance(scale, bool) and isinstance(scale, (int, float)) and scale == 1)


if __name__ == '__main__':
    try:
        responses = sys.stdin.buffer.read().split(b'\0')
        if len(sys.argv) not in (4, 7) or len(responses) != 4 or responses[-1]:
            raise ValueError('Expected three identity responses and requested model/adapter/alias')
        models, props, adapters = [json.loads(value) for value in responses[:3]]
        requested = sys.argv[1:4]
        if sys.argv[1] == '--default-alias':
            # This launcher requests no alias; still validate the sole served
            # model, its physical path and adapter through the common reader.
            entries = models.get('data') if isinstance(models, dict) else None
            alias = (entries[0].get('id') if isinstance(entries, list)
                     and len(entries) == 1 and isinstance(entries[0], dict) else None)
            if not isinstance(alias, str) or not alias:
                raise ValueError('Missing served model alias')
            requested = [sys.argv[2], sys.argv[3], alias]
        settings = {}
        if len(sys.argv) == 7:
            context, parallel = int(sys.argv[4]), int(sys.argv[5])
            if context < 1 or parallel < 1 or sys.argv[6] not in ('0', '1'):
                raise ValueError('Invalid requested runtime settings')
            settings = dict(context=context, parallel=parallel, reasoning_off=sys.argv[6] == '0')
        ready = is_expected_llama_server(models, props, adapters, *requested, **settings)
    except (ValueError, TypeError, UnicodeError):
        ready = False
    sys.exit(0 if ready else 1)
