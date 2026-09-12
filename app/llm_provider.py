"""Provider-specific options for OpenAI-compatible LLM endpoints.

The application sends its own decoding options on each completion.  A provider
may additionally require headers or body keys (for example a reasoning switch).
Keep that configuration in one wrapper so every generation path gets the same
profile settings, while a call's explicit options still take precedence.
"""

import threading
import time

import httpx
from openai import OpenAI


def get_provider_headers(llm_config):
    """Return a copied, string-only header mapping from one LLM profile."""
    headers = (llm_config or {}).get("provider_headers") or {}
    return dict(headers)


def get_provider_extra_body(llm_config):
    """Return a copied provider request-body mapping from one LLM profile.

    The profile's `reasoning_effort` setting is folded in here - the ONE place
    profile-level request options are assembled - so every caller that builds
    a client through make_llm_client sends it. An explicit `reasoning_effort`
    key in the custom request-body JSON wins over the setting: the JSON is the
    escape hatch for values the dropdown does not offer.
    """
    llm_config = llm_config or {}
    extra_body = dict(llm_config.get("provider_extra_body") or {})
    effort = llm_config.get("reasoning_effort")
    if effort and "reasoning_effort" not in extra_body:
        extra_body["reasoning_effort"] = effort
    return extra_body


def merge_provider_extra_body(provider_extra_body, request_extra_body):
    """Merge provider defaults with a request's explicit body options.

    Decoding settings selected by a generation/review run are more specific
    than profile defaults, so they intentionally win on duplicate keys.
    """
    merged = dict(provider_extra_body or {})
    merged.update(request_extra_body or {})
    return merged


def classify_llm_error(error):
    """Classify a failed provider request for retry policy and recovery logs."""
    status_code = getattr(error, "status_code", None)
    message = str(error)
    lowered = message.lower()
    if any(token in lowered for token in ("safety", "policy", "content filter", "nsfw")):
        return {"category": "content_policy", "status_code": status_code, "retryable": False}
    if status_code == 429:
        return {"category": "rate_limited", "status_code": status_code, "retryable": True}
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return {"category": "server_error", "status_code": status_code, "retryable": True}
    error_type = type(error).__name__.lower()
    if "timeout" in error_type or "timeout" in lowered:
        return {"category": "timeout", "status_code": status_code, "retryable": True}
    if "connection" in error_type or "connect" in lowered:
        return {"category": "connection_error", "status_code": status_code, "retryable": True}
    return {"category": "api_error", "status_code": status_code, "retryable": True}


def get_retry_delay(retry_initial_delay_seconds, retry_multiplier,
                    retry_max_delay_seconds, retry_number):
    """Return a bounded exponential delay for a numbered retry (starting at one)."""
    return min(retry_max_delay_seconds,
               retry_initial_delay_seconds * retry_multiplier ** max(0, retry_number - 1))


class _RequestPacer:
    """Serialize request starts to honor one profile's minimum interval."""
    def __init__(self, interval_seconds):
        self._interval_seconds = interval_seconds
        self._next_start = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if not self._interval_seconds:
            return
        with self._lock:
            delay = self._next_start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_start = time.monotonic() + self._interval_seconds


class _ConfiguredCompletions:
    def __init__(self, completions, provider_extra_body, pacer):
        self._completions = completions
        self._provider_extra_body = provider_extra_body
        self._pacer = pacer

    def create(self, *args, **kwargs):
        self._pacer.wait()
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
    def __init__(self, chat, provider_extra_body, pacer):
        self._chat = chat
        self.completions = _ConfiguredCompletions(chat.completions, provider_extra_body, pacer)

    def __getattr__(self, name):
        return getattr(self._chat, name)


class ConfiguredOpenAI:
    """Proxy an OpenAI client while applying one profile's completion defaults."""
    def __init__(self, client, provider_extra_body, request_interval_seconds=0,
                 pacer=None):
        self._client = client
        self._provider_extra_body = dict(provider_extra_body or {})
        self._pacer = pacer or _RequestPacer(request_interval_seconds)
        self.chat = _ConfiguredChat(client.chat, self._provider_extra_body, self._pacer)

    def with_options(self, **kwargs):
        """Return a configured timeout/retry variant without losing body defaults."""
        return ConfiguredOpenAI(
            self._client.with_options(**kwargs), self._provider_extra_body,
            pacer=self._pacer)

    def __getattr__(self, name):
        return getattr(self._client, name)


def get_profile_timeout(llm_config, default_timeout):
    """Build the HTTP timeout for one profile, falling back to the app default."""
    llm_config = llm_config or {}
    request_timeout = llm_config.get("request_timeout_seconds") or default_timeout
    connect_timeout = llm_config.get("connect_timeout_seconds")
    if connect_timeout is None:
        return request_timeout
    return httpx.Timeout(request_timeout, connect=connect_timeout)


class FailoverClient:
    """Two configured clients, one active. `failover()` switches to the second
    for the rest of the process and every later request goes there, with the
    second profile's model substituted for the one the caller named - the
    caller only knows the primary's model name.

    Sticky on purpose: a provider that is rate-limiting or refusing will keep
    doing so for the next chunk, and flapping between profiles would send the
    same book to both. One switch, logged, for the run."""

    def __init__(self, primary, primary_model, secondary, secondary_model,
                 primary_label="primary", secondary_label="secondary"):
        self._pair = [(primary, primary_model, primary_label),
                      (secondary, secondary_model, secondary_label)]
        self._active = 0
        self.chat = _FailoverChat(self)

    @property
    def switched(self):
        return self._active == 1

    @property
    def active_model(self):
        return self._pair[self._active][1]

    def failover(self, error_details):
        """Switch to the secondary. Returns True when a switch happened, False
        when this client is already on the secondary (nothing left to try)."""
        if self.switched:
            return False
        self._active = 1
        _, model, label = self._pair[1]
        print(f"[FAILOVER] switched to the {label} profile ({model}) after "
              f"{error_details.get('category')} (HTTP {error_details.get('status_code')}); "
              "it serves the rest of this run.", flush=True)
        return True

    def _client(self):
        return self._pair[self._active][0]

    def with_options(self, **kwargs):
        p, pm, pl = self._pair[0]
        s, sm, sl = self._pair[1]
        clone = FailoverClient(p.with_options(**kwargs), pm, s.with_options(**kwargs), sm, pl, sl)
        clone._active = self._active
        return clone

    def __getattr__(self, name):
        return getattr(self._client(), name)


class _FailoverChat:
    def __init__(self, owner):
        self._owner = owner
        self.completions = _FailoverCompletions(owner)

    def __getattr__(self, name):
        return getattr(self._owner._client().chat, name)


class _FailoverCompletions:
    def __init__(self, owner):
        self._owner = owner

    def create(self, *args, **kwargs):
        if self._owner.switched and "model" in kwargs:
            kwargs["model"] = self._owner.active_model
        return self._owner._client().chat.completions.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._owner._client().chat.completions, name)


def make_run_client(config, active_llm_config, timeout):
    """The client a generation run should use: the active profile's, wrapped
    with the failover profile's when `llm_failover` is on and the other profile
    is configured (lmstudio_settings.get_failover_llm_config decides which)."""
    from lmstudio_settings import get_failover_llm_config
    primary = make_llm_client(active_llm_config, timeout)
    other = get_failover_llm_config(config)
    if not other:
        return primary
    mode = (config.get("llm_mode") or "local")
    return FailoverClient(primary, active_llm_config.get("model_name", ""),
                          make_llm_client(other, timeout), other.get("model_name", ""),
                          primary_label=mode,
                          secondary_label="remote" if mode == "local" else "local")


def make_llm_client(llm_config, timeout, respect_profile_timeout=True):
    """Create an OpenAI-compatible client with the profile's provider options."""
    llm_config = llm_config or {}
    client = OpenAI(
        base_url=llm_config.get("base_url", "http://localhost:11434/v1"),
        api_key=llm_config.get("api_key", "local"),
        timeout=(get_profile_timeout(llm_config, timeout)
                 if respect_profile_timeout else timeout),
        default_headers=get_provider_headers(llm_config) or None,
    )
    return ConfiguredOpenAI(
        client, get_provider_extra_body(llm_config),
        request_interval_seconds=llm_config.get("request_interval_seconds", 0))
