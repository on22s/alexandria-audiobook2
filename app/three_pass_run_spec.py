"""Pure canonical run-spec resolution shared by admission and recovery.

No I/O, no global state: every input an admission path needs is passed
explicitly, so the same call can be made by the router, the worker and tests.
"""
import hashlib
import json
from urllib.parse import urlsplit

from generate_script import LLMGenParams, get_failover_request_policy
from three_pass_generate import (get_segment_chunk_digests, get_segment_chunk_records,
                                 three_pass_fingerprint)


_SECRET_MARKERS = ('api_key', 'apikey', 'secret', 'password',
                   'authorization', 'credential', 'header', 'extra_body')
_SECRET_EXACT = frozenset({'auth', 'token', 'api-key', 'x-api-key', 'apikey'})
_SAFE_LLM_KEYS = frozenset({
    'model_name', 'base_url', 'context_length', 'max_output_tokens',
    'max_input_tokens', 'transport', 'reasoning_effort', 'structured_output',
    'retry_initial_delay_seconds', 'retry_multiplier', 'retry_max_delay_seconds',
    'retry_jitter', 'api_retry_limit', 'on_api_exhaustion', 'llm_mode',
})


def _is_secret_key(key):
    lowered = str(key).lower()
    return lowered in _SECRET_EXACT or any(marker in lowered for marker in _SECRET_MARKERS)


def safe_endpoint(url):
    """Bind the configured route without storing userinfo or query text."""
    if not isinstance(url, str):
        return url
    endpoint = urlsplit(url.strip())
    # Gateways can give credentials any parameter name. Compare the whole
    # query by fingerprint and keep this identifier out of the transport.
    fingerprint = (hashlib.sha256(endpoint.query.encode('utf-8')).hexdigest()
                   if endpoint.query else None)
    return endpoint._replace(netloc=endpoint.netloc.rpartition('@')[2],
                             query='', fragment=('query-sha256=' + fingerprint
                                                 if fingerprint else '')).geturl()


def get_provider_request_digest(primary, secondary):
    """Bind the SDK's request policy without persisting header or body values."""
    from llm_provider import get_provider_extra_body, get_provider_headers
    from llm_request_budget import get_request_limits

    def overrides(profile):
        profile = profile or {}
        return {key: value for key, value in {
            'body': get_provider_extra_body(profile),
            'headers': sorted(((name.lower(), value)
                               for name, value in get_provider_headers(profile).items()),
                              key=lambda item: item[0]),
            'limits': {key: value for key, value in get_request_limits(profile).items()
                       if value is not None},
            'transport': profile.get('transport') if profile.get('transport') != 'http' else None,
            'request_timeout_seconds': profile.get('request_timeout_seconds'),
            'connect_timeout_seconds': profile.get('connect_timeout_seconds'),
            'request_interval_seconds': profile.get('request_interval_seconds'),
        }.items() if value}

    request_options = [overrides(primary), overrides(secondary)]
    if secondary:
        request_options[1]['failover_policy'] = get_failover_request_policy(secondary)
    return spec_digest(request_options) if any(request_options) else None


def redact_secrets(value):
    """Drop credential-bearing fields before a spec is persisted anywhere."""
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()
                if not _is_secret_key(key)}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value


def canonical_spec(*, prepared_text, model_name, chunk_size, params, options,
                   settings=None, llm_profile=None, prompts=None, binding=None,
                   cast_sha256=None, keep_scope='line', overrides=None,
                   source_path=None, source_sha256=None, book_token=None, run_id=None,
                   fingerprint=None, secondary_model=None, secondary_endpoint=None,
                   attribution=None, context_rescue=None, provider_request_sha256=None,
                   legacy_runtime=False):
    """Return the complete resolved spec, worker chunking and exact fingerprint."""
    resolved_params = params if params is not None else LLMGenParams()
    records = get_segment_chunk_records(prepared_text, chunk_size, resolved_params)
    if fingerprint is None:
        fingerprint = three_pass_fingerprint(
            prepared_text, model_name or '', chunk_size, resolved_params,
            cast_sha256=cast_sha256, keep_scope=keep_scope)
    param_fields = {key: value for key, value in vars(resolved_params).items()
                    if isinstance(value, (str, int, float, bool, type(None), list, dict))
                    and not _is_secret_key(key)}
    safe_profile = {key: value for key, value in (llm_profile or {}).items()
                    if key in _SAFE_LLM_KEYS}
    if 'base_url' in safe_profile:
        safe_profile['base_url'] = safe_endpoint(safe_profile['base_url'])
    spec = redact_secrets({
        'model_name': model_name or '', 'chunk_size': chunk_size,
        'params': param_fields, 'options': dict(options or {}),
        'settings': settings or {}, 'llm_profile': safe_profile,

        'prompts': prompts or {}, 'model_binding': binding or {},
        'cast_sha256': cast_sha256, 'keep_scope': keep_scope,
        'secondary_model': secondary_model,
        'secondary_endpoint': safe_endpoint(secondary_endpoint),
        'attribution': attribution or {},
        **({'provider_request_sha256': provider_request_sha256} if provider_request_sha256 else {}),
        **({'legacy_runtime': True} if legacy_runtime else {}),
        **({'context_rescue': context_rescue} if context_rescue else {}),
        'overrides': overrides or {}, 'source_path': source_path,
        'source_sha256': source_sha256, 'book_token': book_token, 'run_id': run_id,
        'chunk_count': len(records),
        'chunk_digests': get_segment_chunk_digests(records),
    })
    return {'spec': spec, 'fingerprint': fingerprint,
            'chunk_count': len(records), 'chunk_records': records}


def spec_digest(spec):
    return hashlib.sha256(json.dumps(
        spec, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()
