"""Provider-specific options for OpenAI-compatible LLM endpoints.

The application sends its own decoding options on each completion.  A provider
may additionally require headers or body keys (for example a reasoning switch).
Keep that configuration in one wrapper so every generation path gets the same
profile settings, while a call's explicit options still take precedence.
"""

from openai import OpenAI


def get_provider_headers(llm_config):
    """Return a copied, string-only header mapping from one LLM profile."""
    headers = (llm_config or {}).get("provider_headers") or {}
    return dict(headers)


def get_provider_extra_body(llm_config):
    """Return a copied provider request-body mapping from one LLM profile."""
    extra_body = (llm_config or {}).get("provider_extra_body") or {}
    return dict(extra_body)


def merge_provider_extra_body(provider_extra_body, request_extra_body):
    """Merge provider defaults with a request's explicit body options.

    Decoding settings selected by a generation/review run are more specific
    than profile defaults, so they intentionally win on duplicate keys.
    """
    merged = dict(provider_extra_body or {})
    merged.update(request_extra_body or {})
    return merged


class _ConfiguredCompletions:
    def __init__(self, completions, provider_extra_body):
        self._completions = completions
        self._provider_extra_body = provider_extra_body

    def create(self, *args, **kwargs):
        provider_extra_body = {
            key: value for key, value in self._provider_extra_body.items()
            if key not in kwargs
        }
        kwargs["extra_body"] = merge_provider_extra_body(
            provider_extra_body, kwargs.get("extra_body"))
        return self._completions.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._completions, name)


class _ConfiguredChat:
    def __init__(self, chat, provider_extra_body):
        self._chat = chat
        self.completions = _ConfiguredCompletions(chat.completions, provider_extra_body)

    def __getattr__(self, name):
        return getattr(self._chat, name)


class ConfiguredOpenAI:
    """Proxy an OpenAI client while applying one profile's completion defaults."""
    def __init__(self, client, provider_extra_body):
        self._client = client
        self._provider_extra_body = dict(provider_extra_body or {})
        self.chat = _ConfiguredChat(client.chat, self._provider_extra_body)

    def with_options(self, **kwargs):
        """Return a configured timeout/retry variant without losing body defaults."""
        return ConfiguredOpenAI(
            self._client.with_options(**kwargs), self._provider_extra_body)

    def __getattr__(self, name):
        return getattr(self._client, name)


def make_llm_client(llm_config, timeout):
    """Create an OpenAI-compatible client with the profile's provider options."""
    llm_config = llm_config or {}
    client = OpenAI(
        base_url=llm_config.get("base_url", "http://localhost:11434/v1"),
        api_key=llm_config.get("api_key", "local"),
        timeout=timeout,
        default_headers=get_provider_headers(llm_config) or None,
    )
    return ConfiguredOpenAI(client, get_provider_extra_body(llm_config))
