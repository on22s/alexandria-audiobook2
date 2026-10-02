"""Provider-specific options for OpenAI-compatible LLM endpoints.

The application sends its own decoding options on each completion.  A provider
may additionally require headers or body keys (for example a reasoning switch).
Keep that configuration in one wrapper so every generation path gets the same
profile settings, while a call's explicit options still take precedence.
"""

import os
from copy import deepcopy
import random
import re
import threading
import time
from types import SimpleNamespace

import httpx
from openai import OpenAI


_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def is_api_key_reference(configured):
    """True for `env:NAME` / `${NAME}` - a pointer, not a secret, so the setup
    page may show it back to the user unredacted."""
    value = str(configured or "").strip()
    return value.startswith("env:") or bool(_ENV_REF.match(value))


def resolve_api_key(configured):
    """-> the API key to send, from a profile's `api_key` value.

    `env:NAME` and `${NAME}` read the environment so the secret never sits in
    config.json (which the UI now redacts but still stores); an empty value
    falls back to OPENAI_API_KEY; anything else - "local", "sk-..." - is the
    key itself. Missing environment variables resolve to "" rather than to
    the literal reference, so a typo shows up as an auth error, not as a key.
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
        for key in _SAMPLING_KEYS:
            out.pop(key, None)
    return out


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
        return self._completions.create(*args, **adapt_request_for_reasoning_model(kwargs))

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
                from lmstudio_settings import get_current_status, is_remote_llm
                config = self._secondary_config
                try:
                    status = get_current_status(
                        self._pair[1][2], config.get("base_url", ""), self.secondary_model,
                        ssh_alias=self._ssh_alias, api_key=resolve_api_key(config.get("api_key")))
                except Exception as error:
                    status = {"available": False, "loaded": False,
                              "runtime_error": f"{type(error).__name__}: {error}"}
                context = (status.get("context_length") if status.get("available")
                           and status.get("loaded") else None)
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
    return FailoverClient(primary, active_llm_config.get("model_name", ""),
                          secondary, other.get("model_name", ""),
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


def manual_llm_dir(data_dir):
    return os.path.join(data_dir, MANUAL_DIR_NAME)


def is_manual_request_owner_alive(pending):
    """Whether a queued manual request still has a process to receive it."""
    pid = pending.get("owner_pid") if isinstance(pending, dict) else None
    if not isinstance(pid, int) or pid < 1:
        return False  # a legacy request cannot be tied to a live owner
    if pid == os.getpid():
        owner_thread = pending.get("owner_thread")
        return any(thread.ident == owner_thread and thread.is_alive()
                   for thread in threading.enumerate())
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
        from utils import atomic_json_write, file_lock, safe_load_json
        os.makedirs(self.dir, exist_ok=True)
        self.sequence += 1
        request = {
            "id": uuid.uuid4().hex, "sequence": self.sequence, "created": time.time(),
            "owner_pid": os.getpid(), "owner_thread": threading.get_ident(),
            "model": kwargs.get("model"), "messages": kwargs.get("messages") or [],
            "params": {**{key: value for key, value in kwargs.items()
                          if key not in ("model", "messages", "timeout",
                                         "extra_headers", "extra_query")},
                       "temperature": kwargs.get("temperature"),
                       "max_tokens": kwargs.get("max_tokens"),
                       "json_schema": bool(kwargs.get("response_format"))},
        }
        # The one visible prompt is a queue slot shared by all clients. Hold
        # the short file lock only while claiming it; human replies can take
        # hours, so the lock must not span the wait.
        while True:
            with file_lock(self.pending_path):
                pending = safe_load_json(self.pending_path, None)
                if os.path.exists(self.pending_path) and (
                        not isinstance(pending, dict) or not pending.get("id") or
                        not is_manual_request_owner_alive(pending)):
                    os.remove(self.pending_path)
                if not os.path.exists(self.pending_path):
                    try:
                        os.remove(self.response_path)
                    except FileNotFoundError:
                        pass
                    atomic_json_write(request, self.pending_path)
                    break
            time.sleep(self.POLL_SECONDS)
        while True:
            try:
                with open(self.response_path, "r", encoding="utf-8") as handle:
                    response = json.load(handle)
            except (FileNotFoundError, ValueError):
                time.sleep(self.POLL_SECONDS)
                continue
            if not isinstance(response, dict):
                print("Warning: manual LLM response must be a JSON object; waiting for a valid reply.")
                os.remove(self.response_path)
                continue
            if response.get("id") != request["id"]:
                # a reply to an earlier request that arrived late; not ours
                os.remove(self.response_path)
                continue
            break
        with file_lock(self.pending_path):
            # Only the request owner may release this queue slot.
            current = safe_load_json(self.pending_path, None)
            if isinstance(current, dict) and current.get("id") == request["id"]:
                try:
                    os.remove(self.response_path)
                except FileNotFoundError:
                    pass
                os.remove(self.pending_path)
        content = str(response.get("content") or "")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, reasoning_content=None),
                                     finish_reason="stop")],
            usage=None, model=request["model"])


class _ManualChat:
    def __init__(self, client):
        self.completions = SimpleNamespace(create=client.create)


def make_llm_client(llm_config, timeout, respect_profile_timeout=True):
    """Create an OpenAI-compatible client with the profile's provider options."""
    llm_config = llm_config or {}
    if llm_config.get("transport") == "manual":
        from utils import get_runtime_data_dir
        return ManualClient(get_runtime_data_dir(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    client = OpenAI(
        base_url=llm_config.get("base_url", "http://localhost:11434/v1"),
        api_key=resolve_api_key(llm_config.get("api_key", "local")),
        timeout=(get_profile_timeout(llm_config, timeout)
                 if respect_profile_timeout else timeout),
        default_headers=get_provider_headers(llm_config) or None,
    )
    return ConfiguredOpenAI(
        client, get_provider_extra_body(llm_config),
        request_interval_seconds=llm_config.get("request_interval_seconds", 0))
