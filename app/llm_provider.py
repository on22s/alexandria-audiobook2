"""Provider-specific options for OpenAI-compatible LLM endpoints.

The application sends its own decoding options on each completion.  A provider
may additionally require headers or body keys (for example a reasoning switch).
Keep that configuration in one wrapper so every generation path gets the same
profile settings, while a call's explicit options still take precedence.
"""

import logging
import os
from copy import deepcopy
import random
import re
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx2
from openai import OpenAI as _SDKOpenAI
from lmstudio_settings import is_local_llm_endpoint
from manual_llm_context import (ManualRequestCancelled, apply_manual_cancellation,
                                ensure_manual_request_active)


_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_OPENAI_API_HOST = "api.openai.com"


class MissingAPIKeyError(ValueError):
    """A non-local endpoint has no configured key, so no bearer token may be
    invented for it. OPENAI_API_KEY belongs only to the official OpenAI host.
    """


class ResolvedCredential(str):
    """A value already resolved from its reference; transports must not re-parse it.

    An opaque resolved value may itself start with `env:` (a real secret can
    contain those characters). Parsing it again would silently replace that
    credential with another environment lookup, so resolved values are tagged.
    """


_DETERMINISTIC_ERROR_STATUSES = frozenset({400, 401, 403, 404, 405, 410, 413, 422})


class _DeterministicErrorOpenAI(_SDKOpenAI):
    """SDK transport retries are for transient failures only.

    The SDK honours a provider's ``x-should-retry: true`` even on deterministic
    4xx responses, which would repeat a bad request before the app ever
    classifies it. Invalid requests, auth/model mistakes and missing methods or
    resources are terminal here; 408/409/429, 5xx, timeouts and every other
    transient condition keep their normal recovery. ``classify_llm_error`` uses
    the same status set for the app-level retry decision (Rule 15).
    """

    def _should_retry(self, response):
        if getattr(response, "status_code", None) in _DETERMINISTIC_ERROR_STATUSES:
            return False
        return super()._should_retry(response)


# One client constructor for the whole app, so the retry policy above cannot be
# bypassed by a caller reaching for the bare SDK class. Tests patch this name.
OpenAI = _DeterministicErrorOpenAI


def is_api_key_reference(configured):
    """True for `env:NAME` / `${NAME}` - a pointer, not a secret, so the setup
    page may show it back to the user unredacted."""
    value = str(configured or "").strip()
    return value.startswith("env:") or bool(_ENV_REF.match(value))


def is_official_openai_endpoint(base_url):
    """True only for the official OpenAI HTTPS API origin.

    Used to decide where an implicit OPENAI_API_KEY may be sent. Anything else
    - another hosted provider, a proxy, a look-alike host, a URL with
    credentials or a non-default port - must carry its own explicit key.
    """
    parsed = urlparse(str(base_url or ""))
    if (parsed.hostname or "").lower().removesuffix(".") != _OPENAI_API_HOST:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and port in (None, 443)
            and parsed.path.rstrip("/") in ("", "/v1")
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment)


def resolve_api_key(configured, base_url=None):
    """-> the API key to send, from a profile's `api_key` and `base_url`.

    `env:NAME` and `${NAME}` read the environment so the secret never sits in
    config.json (which the UI now redacts but still stores); an explicit
    "local" or "sk-..." value is the key itself. Missing environment variables
    resolve to "" rather than to the literal reference, so a typo shows up as
    an auth error, not as a key.

    An EMPTY value only inherits OPENAI_API_KEY on the official OpenAI
    endpoint. Local endpoints keep the harmless "local" placeholder, and any
    other host refuses to start rather than sending a real OpenAI key to a
    third party. Every caller passes the endpoint the key will be used against.
    """
    value = str(configured or "").strip()
    if value.startswith("env:"):
        return os.environ.get(value[4:].strip(), "")
    match = _ENV_REF.match(value)
    if match:
        return os.environ.get(match.group(1), "")
    if value:
        return value
    if is_official_openai_endpoint(base_url):
        return os.environ.get("OPENAI_API_KEY", "local")
    if is_local_llm_endpoint(base_url):
        return "local"
    host = (urlparse(str(base_url or "")).hostname or str(base_url or "")).lower()
    raise MissingAPIKeyError(
        f"No API key configured for {host}. Set api_key in the profile "
        "(OPENAI_API_KEY is only used for the official OpenAI endpoint).")


def resolve_historical_api_key(configured):
    """The pre-fix key resolution, for local fingerprint matching ONLY.

    Historical receipts and checkpoints hashed an empty key against the ambient
    OPENAI_API_KEY regardless of endpoint. Reproducing that value lets an
    unchanged run reuse its evidence after an upgrade; it must never build a
    request, because the ambient key may belong to a different provider.
    """
    value = str(configured or "").strip()
    if value.startswith("env:"):
        return os.environ.get(value[4:].strip(), "")
    match = _ENV_REF.match(value)
    if match:
        return os.environ.get(match.group(1), "")
    if not value:
        return os.environ.get("OPENAI_API_KEY", "local")
    return value


def _raw_authorization_headers(llm_config):
    """Non-blank Authorization headers, regardless of name spelling."""
    headers = (llm_config or {}).get("provider_headers") or {}
    return [(name, str(value).strip()) for name, value in headers.items()
            if str(name).lower() == "authorization" and str(value).strip()]


def is_authorization_header_unambiguous(llm_config):
    """True when header presence and spelling cannot change the wire identity.

    Historical transports sent the raw header mapping. A present-but-empty
    Authorization or two case spellings of the name may therefore have produced
    a different request than today's single canonical header, so such profiles
    cannot prove an unchanged dispatch for legacy receipt/report matching.
    """
    headers = (llm_config or {}).get("provider_headers") or {}
    names = [name for name in headers if str(name).lower() == "authorization"]
    if len(names) > 1:
        return False
    if not names:
        return True
    value = headers[names[0]]
    return isinstance(value, str) and bool(value.strip())


def resolve_authorization_value(value):
    """Resolve one credential value; an unset env reference is None, not text.

    Bare legacy header values are returned verbatim. `env:NAME` / `${NAME}` are
    references: when the variable is unset the answer is authoritatively no
    credential, never the literal reference sent on the wire or used as a token.
    """
    if value is None or not isinstance(value, str):
        return value
    if isinstance(value, ResolvedCredential):
        return value
    text = value.strip()
    if text.startswith("env:"):
        return os.environ.get(text[4:].strip()) or None
    match = _ENV_REF.match(text)
    if match:
        return os.environ.get(match.group(1)) or None
    return text or None


def get_explicit_authorization(llm_config):
    """The one resolved Authorization header value (names case-insensitive).

    None means the profile has no usable explicit Authorization header. Probes
    and the SDK must use the resolved value rather than deriving a bearer from
    an unrelated api_key. Conflicting case spellings are ambiguous about what
    the transport sends, so they are rejected here rather than resolved
    differently by each caller.
    """
    raw = _raw_authorization_headers(llm_config)
    values = {resolved for _, value in raw
              if (resolved := resolve_authorization_value(value)) is not None}
    if len(values) > 1:
        raise ValueError("ambiguous Authorization header: conflicting case spellings")
    return next(iter(values)) if values else None


def has_unresolved_authorization_reference(llm_config):
    """True when a profile's Authorization header is still an env reference.

    Historical transports sent that literal reference on the wire while the
    modern resolver sends the referenced value, so such a profile cannot prove
    historical/wire equivalence for legacy evidence matching.
    """
    headers = (llm_config or {}).get("provider_headers") or {}
    for name, value in headers.items():
        if str(name).lower() != "authorization":
            continue
        text = str(value or "").strip()
        if text.startswith("env:") or _ENV_REF.match(text):
            return True
    return False


def resolve_probe_api_key(llm_config):
    """The resolved key a status probe may use, or None when intentionally absent."""
    try:
        return resolve_client_api_key(llm_config) or None
    except MissingAPIKeyError:
        return None


def has_explicit_authorization_header(llm_config):
    """True when a profile explicitly configures a nonempty Authorization header.

    Header names are case-insensitive, so `authorization` and `Authorization`
    are one credential. Such a header authenticates the request itself: the SDK
    still needs an api_key argument, but it must be a harmless placeholder and
    no unrelated environment key may be attached.
    """
    return get_explicit_authorization(llm_config) is not None


def resolve_client_api_key(llm_config):
    """The api_key the HTTP client for this profile actually uses.

    Single source for client construction, receipts and benchmark identity. The
    order matters: a Manual profile has no SDK key, and an explicit nonempty
    Authorization header authenticates the request on its own, so both return a
    harmless placeholder BEFORE any api_key lookup. That lookup would otherwise
    resolve (and record in a fingerprint) a credential the request never uses.
    An OMITTED key keeps the legacy "local" placeholder; other values go through
    resolve_api_key. MissingAPIKeyError still propagates for a genuinely keyless
    hosted profile - the real client must refuse it too.
    """
    llm_config = llm_config or {}
    if llm_config.get("transport") == "manual":
        return ResolvedCredential("manual")
    if has_explicit_authorization_header(llm_config):
        return ResolvedCredential("local")
    configured = llm_config.get("api_key", "local")
    resolved = resolve_api_key(configured, llm_config.get("base_url", ""))
    return ResolvedCredential(resolved) if resolved else ""


def get_effective_authorization(llm_config):
    """The exact Authorization header value this profile sends, or None.

    One answer for the SDK, the native runtime probe and the adapter probe: an
    explicit header wins verbatim; otherwise the effective SDK key is sent as a
    bearer. Manual sends no HTTP at all, so it has no wire credential even when
    a retained URL or headers remain in the profile. Never store this in a
    receipt or fingerprint - it is a credential.
    """
    llm_config = llm_config or {}
    if llm_config.get("transport") == "manual":
        return None
    explicit = get_explicit_authorization(llm_config)
    if explicit is not None:
        return ResolvedCredential(explicit)
    key = resolve_client_api_key(llm_config)
    return ResolvedCredential(f"Bearer {key}") if key else None


def is_openai_reasoning_model(model_name):
    """GPT-5 / o-series names use the reasoning request shape (below)."""
    name = str(model_name or "").strip().lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


_SAMPLING_KEYS = ("temperature", "top_p", "presence_penalty", "frequency_penalty")


def adapt_request_for_reasoning_model(kwargs):
    """Rewrite one chat.completions.create call for an OpenAI reasoning model.

    Those models reject `max_tokens` (they want `max_completion_tokens`) and,
    whenever reasoning is on, reject the sampling controls too; local servers
    and non-reasoning OpenAI models keep every parameter. Explicit effort in
    extra_body wins over the typed parameter, as it does in the SDK's body.
    Omitted effort preserves sampling only for documented non-reasoning defaults.
    Returns a new dict; the caller's is untouched.
    """
    if not is_openai_reasoning_model(kwargs.get("model")):
        return kwargs
    out = dict(kwargs)
    if "max_tokens" in out:
        max_tokens = out.pop("max_tokens")
        if "max_completion_tokens" not in out:
            out["max_completion_tokens"] = max_tokens
    effort = str((out.get("extra_body") or {}).get(
        "reasoning_effort", out.get("reasoning_effort")) or "").strip().lower()
    if not effort:
        # Standard GPT-5.1/5.2/5.4 aliases and snapshots default to none.
        # Other recognized reasoning models may reason by default; omit their
        # sampling controls without changing the requested reasoning effort.
        model = str(out.get("model") or "").strip().lower()
        effort = "none" if re.fullmatch(
            r"gpt-5\.(?:1|2|4)(?:-\d{4}-\d{2}-\d{2})?", model) else "medium"
    if effort != "none":
        if "extra_body" in out and out["extra_body"] is not None:
            out["extra_body"] = dict(out["extra_body"])
        for key in _SAMPLING_KEYS:
            out.pop(key, None)
            if out.get("extra_body") is not None:
                out["extra_body"].pop(key, None)
    return out


def get_provider_headers(llm_config):
    """Return the copied headers the SDK sends, with one canonical credential.

    Case variants of Authorization are the same header to an HTTP transport.
    Collapsing them here - using the shared resolver - keeps the SDK, adapter
    probes and status probes on the same value instead of relying on dict order.
    """
    headers = {name: value for name, value in ((llm_config or {}).get("provider_headers") or {}).items()
               if str(name).lower() != "authorization"}
    authorization = get_explicit_authorization(llm_config)
    if authorization is not None:
        headers["Authorization"] = authorization
    return headers


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
    body = getattr(error, "body", None)
    details = body.get("error", body) if isinstance(body, dict) else {}
    code = details.get("code") if isinstance(details, dict) else None
    refusal_codes = ("content_filter", "content_policy_violation", "safety_violation", "refusal")
    if isinstance(details, dict) and any(details.get(field) in refusal_codes for field in ("code", "type")):
        return {"category": "content_policy", "status_code": status_code, "retryable": False}
    if status_code in (400, 413) and (code == "context_length_exceeded" or any(
            token in lowered for token in ("context window exceeded", "maximum context length",
                                            "exceeds the context window"))):
        return {"category": "context_budget", "status_code": status_code, "retryable": False}
    if status_code == 429:
        return {"category": "rate_limited", "status_code": status_code, "retryable": True}
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return {"category": "server_error", "status_code": status_code, "retryable": True}
    if status_code in (401, 403):
        return {"category": "auth", "status_code": status_code, "retryable": False}
    if status_code == 404:
        return {"category": "not_found", "status_code": status_code, "retryable": False}
    if status_code in (400, 405, 410, 413, 422):
        # A malformed request, wrong method, gone resource, oversized body or
        # schema refusal will fail identically on a retry. Keep the established
        # api_error category but stop retrying; 408/409/429/5xx stay retryable.
        return {"category": "api_error", "status_code": status_code, "retryable": False}
    error_type = type(error).__name__.lower()
    if "timeout" in error_type:
        return {"category": "timeout", "status_code": status_code, "retryable": True}
    if "connection" in error_type:
        return {"category": "connection_error", "status_code": status_code, "retryable": True}
    if any(token in lowered for token in ("safety", "policy", "content filter", "nsfw")):
        return {"category": "content_policy", "status_code": status_code, "retryable": False}
    if "timeout" in lowered:
        return {"category": "timeout", "status_code": status_code, "retryable": True}
    if "connect" in lowered:
        return {"category": "connection_error", "status_code": status_code, "retryable": True}
    return {"category": "api_error", "status_code": status_code, "retryable": True}


_LOCAL_REQUEST_FAILURE_CATEGORIES = frozenset({
    "context_budget", "profile_changed", "request_admission",
})


def is_failover_eligible(error_details):
    """Whether an exhausted error may switch to the other profile.

    Local sizing and dispatch/config failures are not the provider's fault:
    re-sending the whole book to a second (possibly billed) endpoint cannot
    help, so they stop at the primary. Provider refusals and auth/model errors
    remain eligible - a different provider may accept the same request.
    """
    return error_details.get("category") not in _LOCAL_REQUEST_FAILURE_CATEGORIES


def get_retry_delay(retry_initial_delay_seconds, retry_multiplier,
                    retry_max_delay_seconds, retry_number, jitter=0.0, rng=random):
    """Return a bounded exponential delay for a numbered retry (starting at one).

    `jitter` is the fraction of the delay randomised either side (0.2 = +-20%)
    so concurrent workers retrying a rate-limited provider do not all come
    back in the same instant; 0 keeps the exact exponential value.
    """
    delay = min(retry_max_delay_seconds,
                retry_initial_delay_seconds * retry_multiplier ** max(0, retry_number - 1))
    if jitter:
        delay = min(retry_max_delay_seconds, delay * rng.uniform(1 - jitter, 1 + jitter))
    return delay


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
    def __init__(self, completions, provider_extra_body, pacer, limits):
        self._completions = completions
        self._provider_extra_body = provider_extra_body
        self._pacer = pacer
        self._limits = limits

    def create(self, *args, **kwargs):
        self._pacer.wait()
        provider_extra_body = {
            key: value for key, value in self._provider_extra_body.items()
            if key not in kwargs and not (
                key in ("max_tokens", "max_completion_tokens")
                and any(name in kwargs for name in ("max_tokens", "max_completion_tokens")))
        }
        kwargs["extra_body"] = merge_provider_extra_body(
            provider_extra_body, kwargs.get("extra_body"))
        from llm_request_budget import get_budgeted_request
        kwargs = get_budgeted_request(kwargs, self._limits or {})
        return self._completions.create(*args, **adapt_request_for_reasoning_model(kwargs))

    def __getattr__(self, name):
        return getattr(self._completions, name)


class _ConfiguredChat:
    def __init__(self, chat, provider_extra_body, pacer, limits):
        self._chat = chat
        self.completions = _ConfiguredCompletions(chat.completions, provider_extra_body, pacer, limits)

    def __getattr__(self, name):
        return getattr(self._chat, name)


class ConfiguredOpenAI:
    """Proxy an OpenAI client while applying one profile's completion defaults."""
    def __init__(self, client, provider_extra_body, request_interval_seconds=0,
                 pacer=None, limits=None):
        self._client = client
        self._provider_extra_body = dict(provider_extra_body or {})
        self._pacer = pacer or _RequestPacer(request_interval_seconds)
        self._limits = limits
        self.chat = _ConfiguredChat(client.chat, self._provider_extra_body, self._pacer, limits)

    def with_options(self, **kwargs):
        """Return a configured timeout/retry variant without losing body defaults."""
        return ConfiguredOpenAI(
            self._client.with_options(**kwargs), self._provider_extra_body,
            pacer=self._pacer, limits=self._limits)

    def __getattr__(self, name):
        return getattr(self._client, name)


def get_profile_timeout(llm_config, default_timeout):
    """Build the HTTP timeout for one profile, falling back to the app default."""
    llm_config = llm_config or {}
    request_timeout = llm_config.get("request_timeout_seconds") or default_timeout
    connect_timeout = llm_config.get("connect_timeout_seconds")
    if connect_timeout is None:
        return request_timeout
    return httpx2.Timeout(request_timeout, connect=connect_timeout)


class RunProfileChanged(RuntimeError):
    """A request must be replanned against the now-serving runtime."""


class RunRequestAdmissionError(RuntimeError):
    """The serving runtime failed the caller's admission safety check."""


class FailoverClient:
    """Two configured clients, one active. `failover()` switches to the second
    for the rest of the process and every later request goes there, with the
    second profile's model substituted for the one the caller named - the
    caller only knows the primary's model name.

    Sticky on purpose: a provider that is rate-limiting or refusing will keep
    doing so for the next chunk, and flapping between profiles would send the
    same book to both. One switch, logged, for the run."""

    def __init__(self, primary, primary_model, secondary, secondary_model,
                 primary_label="primary", secondary_label="secondary",
                 secondary_config=None, ssh_alias=None):
        self._pair = [(primary, primary_model, primary_label),
                      (secondary, secondary_model, secondary_label)]
        self._dispatch_state = {"active": 0}
        self._dispatch_lock = threading.Lock()
        self._secondary_config = deepcopy(secondary_config)
        self._ssh_alias = ssh_alias
        self._runtime_state = {"profile": None, "semaphore": None}
        self._runtime_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed_clients = set()
        self.chat = _FailoverChat(self)

    def get_active_profile_index(self):
        return self._dispatch_state["active"]

    def get_profile_base_url(self, index):
        return str(getattr(self._pair[index][0], "base_url", ""))

    @property
    def switched(self):
        return self.get_active_profile_index() == 1

    @property
    def secondary_model(self):
        return self._pair[1][1]

    @property
    def active_model(self):
        return self._pair[self.get_active_profile_index()][1]

    def failover(self, error_details):
        """Switch to the secondary. Returns True when a switch happened, False
        when this client is already on the secondary (nothing left to try)."""
        with self._dispatch_lock:
            if self.switched:
                return False
            self._dispatch_state["active"] = 1
            _, model, label = self._pair[1]
            print(f"[FAILOVER] switched to the {label} profile ({model}) after "
                  f"{error_details.get('category')} (HTTP {error_details.get('status_code')}); "
                  "it serves the rest of this run.", flush=True)
            return True

    def ensure_active_runtime_profile(self):
        """Read the secondary runtime once when it starts serving this run."""
        if not self.switched or self._secondary_config is None:
            return None
        with self._runtime_lock:
            if self._runtime_state["profile"] is None:
                config = self._secondary_config
                if config.get("transport") == "manual":
                    # A manual secondary has no HTTP runtime to interrogate.
                    # Never probe a saved endpoint on its behalf: use declared
                    # limits and one slot so human answers stay serial.
                    from llm_request_budget import get_request_limits
                    context = get_request_limits(config, None)["context_length"]
                    if not isinstance(context, int) or isinstance(context, bool) or context < 1:
                        context = 4096
                    self._runtime_state["semaphore"] = threading.BoundedSemaphore(1)
                    self._runtime_state["profile"] = {
                        "config": config,
                        "status": {"available": False, "loaded": False,
                                   "manual": True, "runtime": "manual"},
                        "context_length": context, "is_remote": False,
                    }
                    return deepcopy(self._runtime_state["profile"])
                from lmstudio_settings import get_current_status, is_remote_llm
                try:
                    status = get_current_status(
                        self._pair[1][2], config.get("base_url", ""), self.secondary_model,
                        ssh_alias=self._ssh_alias,
                        api_key=resolve_client_api_key(config),
                        authorization=get_effective_authorization(config),
                        llm_config=config)
                except Exception as error:
                    status = {"available": False, "loaded": False,
                              "runtime_error": f"{type(error).__name__}: {error}"}
                context = (status.get("context_length") if status.get("available")
                           and status.get("loaded") else None)
                from llm_request_budget import get_request_limits
                context = get_request_limits(config, context)["context_length"]
                if not isinstance(context, int) or isinstance(context, bool) or context < 1:
                    context = 4096
                    print("[FAILOVER] secondary context could not be verified; "
                          "using a conservative 4096-token budget, not the primary context.",
                          flush=True)
                parallel = status.get("parallel")
                if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
                    parallel = 1
                self._runtime_state["semaphore"] = threading.BoundedSemaphore(parallel)
                self._runtime_state["profile"] = {"config": config, "status": status,
                                                   "context_length": context,
                                                   "is_remote": is_remote_llm(self._pair[1][2], config.get("base_url", ""))}
            return deepcopy(self._runtime_state["profile"])

    def close(self):
        """Close both owned clients, retaining failed closes for another attempt."""
        errors = []
        with self._close_lock:
            for client, _, _ in self._pair:
                identity = id(client)
                if identity in self._closed_clients:
                    continue
                try:
                    client.close()
                except Exception as error:
                    errors.append(error)
                else:
                    self._closed_clients.add(identity)
        if errors:
            raise errors[0]

    def _client(self):
        return self._pair[self.get_active_profile_index()][0]

    def with_options(self, **kwargs):
        p, pm, pl = self._pair[0]
        s, sm, sl = self._pair[1]
        clone = FailoverClient(p.with_options(**kwargs), pm, s.with_options(**kwargs), sm, pl, sl,
                               self._secondary_config, self._ssh_alias)
        clone._runtime_state = self._runtime_state
        clone._runtime_lock = self._runtime_lock
        clone._dispatch_state = self._dispatch_state
        clone._dispatch_lock = self._dispatch_lock
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
        planned_index = kwargs.pop("_run_profile_index", None)
        admission = kwargs.pop("_request_admission", None)
        with self._owner._dispatch_lock:
            index = self._owner.get_active_profile_index()
            if planned_index is not None and planned_index != index:
                raise RunProfileChanged("Serving profile changed after request planning")
            target, model, _ = self._owner._pair[index]
        # Capture the dispatch target under the switch lock. A request already
        # admitted to the primary may finish there; it cannot migrate unnoticed.
        if index == 1 and "model" in kwargs:
            kwargs["model"] = model
        profile = self._owner.ensure_active_runtime_profile() if index == 1 else None
        if profile is None:
            return target.chat.completions.create(*args, **kwargs)
        with self._owner._runtime_state["semaphore"]:
            if admission is not None and not admission(profile):
                raise RunRequestAdmissionError("Secondary runtime failed request admission")
            return target.chat.completions.create(*args, **kwargs)

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
    try:
        secondary = make_llm_client(other, timeout)
    except BaseException:
        primary.close()
        raise
    return FailoverClient(primary, str(active_llm_config.get("model_name", "")).strip(),
                          secondary, str(other.get("model_name", "")).strip(),
                          primary_label=mode,
                          secondary_label="remote" if mode == "local" else "local",
                          secondary_config=other, ssh_alias=config.get("llm_remote_ssh"))


def get_run_fingerprint_identity(fingerprint):
    """Compare configured model identity independently of execution history."""
    identity = deepcopy(fingerprint)
    if isinstance(identity, dict) and isinstance(identity.get("model_binding"), dict):
        binding = identity["model_binding"]
        if isinstance(binding.get("failover_used"), bool):
            binding.pop("failover_used")
    return identity


def get_run_model_binding(client, primary_model, previous=None):
    """Read model identity and sticky failover, including validated resumed work."""
    is_failover = isinstance(client, FailoverClient)
    return {
        "primary_model": primary_model,
        "failover_model": client.secondary_model if is_failover else None,
        "failover_used": (bool(is_failover and client.switched)
                          or bool(previous and previous.get("failover_used") is True)),
    }


MANUAL_DIR_NAME = "manual_llm"
# A child that runs with a private ALEXANDRIA_DATA_DIR (the book test, #1920)
# still asks its manual-transport questions where the page reads and answers them.
MANUAL_DATA_DIR_ENV = "ALEXANDRIA_MANUAL_LLM_DATA_DIR"
_manual_request_owners = {}
_manual_request_owners_lock = threading.Lock()


def manual_llm_dir(data_dir):
    return os.path.join(data_dir, MANUAL_DIR_NAME)


def read_manual_pending_locked(path):
    """Read an occupied slot strictly while the caller holds its file lock."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as handle:
            pending = json.load(handle)
        if not isinstance(pending, dict) or not isinstance(pending.get("id"), str) or not pending["id"]:
            raise ValueError("Invalid manual LLM pending request")
        return pending
    except FileNotFoundError:
        return None
    except OSError as error:
        from core import AppError
        raise AppError(503, "The pending manual LLM request could not be read. Try again.",
                       key="error.manual_llm.pending_read_failed") from error
    except ValueError as error:
        from core import AppError
        raise AppError(503, "The manual LLM request file manual_llm/pending.json in the data directory is corrupt. "
                       "Cancel the manual request (or restart the app), then delete or rename "
                       "this file before trying again.", key="error.manual_llm.pending_corrupt") from error


def get_manual_llm_data_dir():
    """The data directory whose manual_llm/ the page serves: an explicit parent
    directory for isolated children, otherwise this process's data directory.

    Any child started with a private ALEXANDRIA_DATA_DIR must also set
    MANUAL_DATA_DIR_ENV to the app's data directory, or a Manual LLM's requests
    land where no page reads them and the run waits forever (#1920)."""
    from utils import get_runtime_data_dir
    configured = os.environ.get(MANUAL_DATA_DIR_ENV, "").strip()
    if configured:
        # The same admitted spelling the server uses for manual_llm_dir(DATA_DIR).
        from direct_path_admission import get_direct_spelling
        return get_direct_spelling(configured)
    return get_runtime_data_dir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_manual_process_identity(pid):
    """Read a kernel birth identity, never just a reusable process number."""
    import sys
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.WaitForSingleObject.restype = ctypes.c_uint32
        api.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.GetProcessTimes.restype = ctypes.c_int
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
        handle = api.OpenProcess(0x101000, False, pid)  # SYNCHRONIZE + QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return None
            raise OSError(error, "Cannot inspect manual LLM request owner")
        try:
            status = api.WaitForSingleObject(handle, 0)
            if status == 0:
                return None
            if status != 258:
                raise OSError(ctypes.get_last_error(), "Cannot query manual LLM request owner")
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise OSError(ctypes.get_last_error(), "Cannot read manual LLM owner creation time")
            return "windows:" + str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        from subprocess_ownership import get_process_identity
        try:
            return "linux:" + get_process_identity(pid)
        except (FileNotFoundError, ProcessLookupError):
            return None
    import subprocess
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, timeout=5)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode or not result.stdout.strip():
        raise OSError("Cannot read manual LLM owner creation time")
    return "posix:" + result.stdout.strip()


def is_manual_request_owner_alive(pending):
    """Whether a queued manual request still has a process to receive it."""
    pid = pending.get("owner_pid") if isinstance(pending, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return False  # a legacy request cannot be tied to a live owner
    if pid == os.getpid():
        request_id = pending.get("id")
        if not isinstance(request_id, str):
            return False
        with _manual_request_owners_lock:
            owner = _manual_request_owners.get(request_id)
        return bool(owner is not None and owner.ident == pending.get("owner_thread")
                    and owner.is_alive())
    identity = pending.get("owner_process_identity")
    if not isinstance(identity, str) or not identity:
        return False
    return get_manual_process_identity(pid) == identity


class ManualClient:
    """The user is the model (issue #593).

    Same surface the pipelines use - client.chat.completions.create(...) ->
    choices[0].message.content - but no HTTP: each request is written to
    <data_dir>/manual_llm/pending.json, the call blocks until a
    response.json with the same id appears (the Script tab's Submit, or a
    user's own script through /api/manual_llm/response), and the reply is
    returned as the model's content. Nothing about validation, retries or
    checkpoints changes: a bad paste is a rejected attempt, and the retry
    prompt becomes the next pending request. No timeout on purpose - the run
    waits for a person; Cancel kills the run and clears the files."""

    POLL_SECONDS = 0.5

    def __init__(self, data_dir):
        self.dir = manual_llm_dir(data_dir)
        self.sequence = 0
        self.chat = _ManualChat(self)

    @property
    def pending_path(self):
        return os.path.join(self.dir, "pending.json")

    @property
    def response_path(self):
        return os.path.join(self.dir, "response.json")

    def with_options(self, **_kwargs):
        return self

    def close(self):
        pass

    def create(self, **kwargs):
        import json
        import uuid
        from types import SimpleNamespace
        from utils import atomic_json_write, file_lock
        os.makedirs(self.dir, exist_ok=True)
        self.sequence += 1
        request = {
            "id": uuid.uuid4().hex, "sequence": self.sequence, "created": time.time(),
            "owner_pid": os.getpid(), "owner_thread": threading.get_ident(),
            "owner_process_identity": get_manual_process_identity(os.getpid()),
            "model": kwargs.get("model"), "messages": kwargs.get("messages") or [],
            "params": {**{key: value for key, value in kwargs.items()
                          if key not in ("model", "messages", "timeout",
                                         "extra_headers", "extra_query")},
                       "temperature": kwargs.get("temperature"),
                       "max_tokens": kwargs.get("max_tokens"),
                       "json_schema": bool(kwargs.get("response_format"))},
        }
        with _manual_request_owners_lock:
            _manual_request_owners[request["id"]] = threading.current_thread()
        claimed = False
        request_failed = False
        try:
            # The one visible prompt is a queue slot shared by all clients. Hold
            # the short file lock only while claiming it; human replies can take
            # hours, so the lock must not span the wait.
            while True:
                ensure_manual_request_active()
                with file_lock(self.pending_path):
                    ensure_manual_request_active()
                    pending = read_manual_pending_locked(self.pending_path)
                    if pending is None or not is_manual_request_owner_alive(pending):
                        try:
                            os.remove(self.response_path)
                        except FileNotFoundError:
                            pass
                        atomic_json_write(request, self.pending_path, allow_nonatomic_fallback=False)
                        claimed = True
                        break
                time.sleep(self.POLL_SECONDS)
            while True:
                ensure_manual_request_active()
                with file_lock(self.pending_path):
                    try:
                        with open(self.response_path, "r", encoding="utf-8") as handle:
                            response = json.load(handle)
                    except (FileNotFoundError, ValueError):
                        pass
                    else:
                        if not isinstance(response, dict):
                            print("Warning: manual LLM response must be a JSON object; waiting for a valid reply.")
                            os.remove(self.response_path)
                        elif response.get("id") != request["id"]:
                            os.remove(self.response_path)
                        else:
                            break
                time.sleep(self.POLL_SECONDS)
            content = str(response.get("content") or "")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content, reasoning_content=None),
                                         finish_reason="stop")],
                usage=None, model=request["model"])
        except BaseException:
            request_failed = True
            raise
        finally:
            with _manual_request_owners_lock:
                _manual_request_owners.pop(request["id"], None)
            if claimed:
                from core import AppError, logger
                from review_startup import emit_diagnostic_log
                with file_lock(self.pending_path):
                    # Only the request owner may release this queue slot.
                    try:
                        current = read_manual_pending_locked(self.pending_path)
                    except AppError as error:
                        emit_diagnostic_log(logger.name, logging.ERROR,
                                            "Manual LLM request %s could not retire its pending slot (%s).",
                                            request["id"], error.message_key)
                        if not request_failed:
                            raise
                    else:
                        if current is not None and current.get("id") == request["id"]:
                            try:
                                os.remove(self.response_path)
                            except FileNotFoundError:
                                pass
                            os.remove(self.pending_path)


class _ManualChat:
    def __init__(self, client):
        self.completions = SimpleNamespace(create=client.create)


def make_llm_client(llm_config, timeout, respect_profile_timeout=True):
    """Create an OpenAI-compatible client with the profile's provider options."""
    llm_config = llm_config or {}
    # Declared limits only: no runtime probe here. Construction stays pure and
    # cacheable on config alone; detected context reaches each call through
    # its params (ensure_run_request_params / context_length) instead.
    from llm_request_budget import get_request_limits
    limits = get_request_limits(llm_config)
    limits = limits if any(limits.values()) else None
    if llm_config.get("transport") == "manual":
        client = ManualClient(get_manual_llm_data_dir())
        return ConfiguredOpenAI(client, get_provider_extra_body(llm_config), limits=limits) if limits else client
    from lmstudio_settings import normalize_llm_endpoint
    endpoint = normalize_llm_endpoint(llm_config.get("base_url"))
    client = OpenAI(
        base_url=endpoint or llm_config.get("base_url", "http://localhost:11434/v1"),
        api_key=resolve_client_api_key(llm_config),
        timeout=(get_profile_timeout(llm_config, timeout)
                 if respect_profile_timeout else timeout),
        default_headers=get_provider_headers(llm_config) or None,
    )
    return ConfiguredOpenAI(
        client, get_provider_extra_body(llm_config),
        request_interval_seconds=llm_config.get("request_interval_seconds", 0), limits=limits)
