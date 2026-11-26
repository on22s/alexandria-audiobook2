"""Helpers for applying VRAM-safe LM Studio load settings via the `lms` CLI.

Background: a long batch_review run keeps the LLM loaded with a large KV
cache. Loading the model with a high `parallel` value multiplies that KV
cache and can exhaust GPU VRAM, crashing the display server (see the
VRAM watchdog in review_script.py for the runtime safety net). These
helpers let both the CLI script and the web UI force/inspect the
VRAM-safe load configuration (context 8192, parallel 1, full GPU offload).

Also covers the remote case: a LM Studio on a Thunder Compute GPU instance,
managed over SSH instead of the local `lms` CLI (see apply_remote_lmstudio_settings).
"""

import json
import hashlib
import ipaddress
import re
import shlex
import shutil
import subprocess
import threading
import time
from weakref import WeakValueDictionary
from copy import deepcopy
from urllib.parse import urlparse

from diagnostics import sanitize_url_for_display
from utils import run_rocm_smi_json
from lmstudio_endpoint import get_lmstudio_endpoint_status, open_no_redirect

# Distinguishes an omitted legacy argument from an explicitly resolved None:
# None is authoritative ("send no credential"), omission is just old-call shape.
_STATUS_UNSET = object()

IDEAL_SETTINGS = {"context_length": 8192, "parallel": 1, "gpu": "max"}
DEFAULT_SETTINGS = {"context_length": 4096, "parallel": 4, "gpu": "max"}

# Profiles are enabled only after the exact model/profile has passed a real
# concurrent near-limit run on this machine. Unknown models and unreadable GPU
# metrics retain IDEAL_SETTINGS rather than extrapolating from another model.
_VERIFIED_LOCAL_PROFILES = {
    "gemma-4-e4b-uncensored-hauhaucs-aggressive": {
        "context_length": 32768,
        "parallel": 2,
        "model_vram_bytes": int(8.50 * 1024 ** 3),
        # Measured context growth was smaller; 16 KiB/token deliberately
        # overestimates it so transient backend workspaces remain covered.
        "bytes_per_extra_context_token": 16 * 1024,
    },
    # A 9B (6.55 GB on disk) that leaves ample room for a full 32768 context on
    # a ~16 GB card - added for the script-gen A/B so it runs at the same
    # context as gemma instead of the 8192 fallback the 27B is stuck at. The
    # get_safe_local_settings VRAM guard still gates the actual load, so an
    # over-tight moment falls back to 8192 rather than risking OOM.
    "qwen3.5-9b-uncensored-hauhaucs-aggressive": {
        "context_length": 32768,
        "parallel": 1,
        "model_vram_bytes": int(7.20 * 1024 ** 3),
        "bytes_per_extra_context_token": 16 * 1024,
    },
    # Both ministrals measured 2026-07-24 on this card (15.92 GiB total):
    # ~8.8 GiB of weights, but ~160 KiB/token of KV cache - ten times the
    # gemma/qwen figure, so context is what constrains them, not weights.
    # 32768 was measured and left only ~1.5 GiB, under the 2 GiB reserve;
    # 16384 projects to ~11.9 GiB used with ~4.0 GiB spare. Kept at 16384
    # rather than the 24576 that also fits, because this is a load-time
    # measurement rather than the sustained near-limit run this table wants.
    "ministral-3-14b-instruct-2512": {
        "context_length": 16384,
        "parallel": 1,
        "model_vram_bytes": int(8.90 * 1024 ** 3),
        # Measured 158.90 KiB/token; rounded up to cover transient workspaces.
        "bytes_per_extra_context_token": 176 * 1024,
    },
    # Dense 14B/15B pairs measured 2026-07-26 on this card (15.92 GiB total,
    # 2.25 GiB resident at idle). Both cost ~160 KiB per context token, ten
    # times the compressed-KV models above, so 32768 leaves under 1 GiB of
    # headroom and is refused; 16384 matches ministral and keeps ~2.5-3 GiB.
    "microsoft/phi-4": {
        "context_length": 16384,
        "parallel": 1,
        "model_vram_bytes": int(9.91 * 1024 ** 3),
        # Measured 159.95 KiB/token; rounded up to cover transient workspaces.
        "bytes_per_extra_context_token": 176 * 1024,
    },
    "qwen/qwen3-14b": {
        "context_length": 16384,
        "parallel": 1,
        "model_vram_bytes": int(9.33 * 1024 ** 3),
        # Measured 159.78 KiB/token; rounded up as above.
        "bytes_per_extra_context_token": 176 * 1024,
    },
    # mistralai/magistral-small is deliberately absent. Its weights alone take
    # 13.51 GiB, leaving 0.16 GiB at the 8192 minimum - below any usable
    # reserve. It cannot run here without weakening the VRAM guard, so it gets
    # no profile and falls back to the conservative default rather than being
    # made to fit.
    "ministral-3-14b-instruct-2512-absolute-heresy-i1": {
        "context_length": 16384,
        "parallel": 1,
        "model_vram_bytes": int(8.80 * 1024 ** 3),
        # Measured 163.05 KiB/token; rounded up to cover transient workspaces.
        "bytes_per_extra_context_token": 176 * 1024,
    },
    # gemma-4-12b-coder-fable5-composer2.5-v1 (13.45 GiB) and
    # qwen3.6-27b-uncensored-hauhaucs-aggressive (12.67 GiB) are deliberately
    # absent: both exceed the 2 GiB reserve at the 8192 baseline already
    # (0.53 and 1.33 GiB spare), so no context setting makes them safe here.
    # They stay on the conservative fallback.
}
_LOCAL_VRAM_RESERVE_BYTES = 2 * 1024 ** 3

# "Best settings" for a remote LM Studio on a big cloud GPU (e.g. Thunder A6000,
# 48GB): a large context with headroom under the model's max, vs LM Studio's
# small defaults. Applied over SSH since the forwarded /v1 port can't set these.
REMOTE_IDEAL_SETTINGS = {"context_length": 98304, "parallel": 2}
REMOTE_DEFAULT_SETTINGS = {"context_length": 4096, "parallel": 4}


class TokenBudgetError(ValueError):
    """The prompt and safety reserve do not fit the verified context."""


def get_effective_max_tokens(fallback, context_length=None, messages=None,
                             hard_max=None, reserve=512, scale_to_context=True,
                             max_input_tokens=None):
    """Return a context-safe completion budget for a production LLM call.

    ``fallback`` is the conservative allowance used when live model context is
    unknown.  A verified larger context may raise it, but never beyond the
    call-specific ``hard_max`` or the space left after prompt and reserve.
    Character-based prompt estimation is deliberately conservative because
    this helper must work without loading a model-specific tokenizer.
    """
    fallback = max(1, int(fallback))
    hard_max = max(1, int(hard_max or fallback))
    fallback = min(fallback, hard_max)
    from llm_request_budget import get_prompt_token_estimate
    prompt_tokens = get_prompt_token_estimate(messages)
    if max_input_tokens is not None and prompt_tokens > max_input_tokens:
        raise TokenBudgetError(
            f"Prompt estimate ({prompt_tokens}) exceeds the input ceiling "
            f"({max_input_tokens}). Reduce the input or adjust the profile input limit.")
    if not context_length:
        return min(fallback, hard_max)
    context_length = max(1, int(context_length))
    available = context_length - prompt_tokens - max(0, int(reserve))
    if available < 1:
        raise TokenBudgetError(
            f"Prompt estimate ({prompt_tokens}) plus reserve ({reserve}) exceeds "
            f"the loaded context ({context_length}). Reduce the input or review the profile/server context settings.")
    scaled_target = max(fallback, context_length // 4) if scale_to_context else fallback
    return max(1, min(hard_max, scaled_target, available))


def get_next_retry_max_tokens(current, retry_reason, hard_max, multiplier=1.5):
    """Return the next requested completion budget for evidence of truncation."""
    current = max(1, int(current))
    hard_max = max(1, int(hard_max or current))
    if retry_reason not in {"token_truncated", "incomplete_output"}:
        return min(current, hard_max)
    increased = max(current + 1, int(current * multiplier + 0.5))
    return min(increased, hard_max)

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")  # nosec B104 - host allowlist, never bind()


def is_local_llm_endpoint(base_url):
    """Recognize local address spellings, without asserting runtime ownership.

    An unparseable URL (a stored Manual profile may carry one) is not a local
    endpoint; classification must not raise on it.
    """
    if not base_url:
        return True
    try:
        host = (urlparse(base_url).hostname or "").lower().removesuffix(".")
    except ValueError:
        return False
    if host in _LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def normalize_llm_endpoint(base_url):
    """Trim a saved HTTP endpoint for dialing, or None when it is not dialable.

    One answer for the failover predicate and the runtime client (Rule 15): a
    padded legacy URL stays usable, a malformed port is refused rather than
    handed to the client to fail obscurely later.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    candidate = base_url.strip()
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            return None
        # urlparse strips tabs/newlines from the netloc before exposing it, so
        # check the raw authority as written rather than the parsed one.
        scheme_at = candidate.find("://")
        authority = candidate[scheme_at + 3:].split("/", 1)[0] if scheme_at >= 0 else candidate
        if any(character.isspace() or ord(character) < 33 for character in authority):
            return None
        # A parser-valid host can still carry an unusable explicit port.
        if parsed.port == 0:
            return None
    except ValueError:
        return None
    return candidate


def is_usable_llm_endpoint(base_url):
    """Whether a saved HTTP profile has an endpoint a client can dial."""
    return normalize_llm_endpoint(base_url) is not None


def is_billed_http_profile(profile):
    """Whether this profile sends HTTP requests to a (possibly billed) host."""
    return (isinstance(profile, dict) and profile.get("transport") != "manual"
            and is_usable_llm_endpoint(profile.get("base_url")))


def is_remote_llm(llm_mode, base_url):
    """Single source of truth for "is this LLM endpoint remote?" - use this
    everywhere instead of checking is_local_llm_endpoint alone, which misses
    an explicit llm_mode="remote" pointed at a URL that happens to resolve as
    local (or vice versa after a save_config edge case where llm_mode and the
    active base_url have drifted out of sync).
    """
    return llm_mode == "remote" or not is_local_llm_endpoint(base_url)


def get_active_llm_config(config):
    """Single source of truth for "which LLM block is this run using?".

    `config["llm"]` is a MIRROR, not a source. `/api/config` picks the active
    profile from the `llm_mode` toggle and copies it into `llm` for consumers
    to read, refusing to save when the two would disagree. Anything that writes
    config.json outside that endpoint - a benchmark caching its concurrency
    into `llm_local`, a hand edit, a script - updates one and not the other,
    and then `llm` is stale while the toggle is right.

    That is not hypothetical. On 2026-08-06 a run dialled a dead endpoint for
    an hour while a working server sat idle, because it read the mirror.

    So the profile named by `llm_mode` wins, and `llm` is the fallback for
    configs old enough to predate the toggle. Two call sites had written this
    rule out by hand, in two different spellings, each with a comment citing
    Rule 15 - which is how you can tell the rule needed a home.

    NOT every read of a profile is this question. `benchmark_runner`'s
    `_get_llm_benchmark_target` resolves an explicitly NAMED target so it can
    measure the local and remote endpoints independently of the toggle. That
    is a different decision and deliberately does not use this function.
    """
    block = get_saved_llm_profile(config, require_complete=True)
    if not block:
        return {}
    from llm_request_budget import get_inherited_profile_limits
    return get_inherited_profile_limits(block, config.get("generation") or {})


_PROFILE_CREDENTIAL_KEYS = frozenset(("api_key", "provider_headers", "provider_extra_body"))
BENCHMARK_IDENTITY_KEY = "benchmark_identity"
BENCHMARK_IDENTITY_VERSION = 1
_BENCHMARK_DIGEST_PATTERN = re.compile(r"^v[0-9]+:[0-9a-f]{64}$")
_BENCHMARK_SSH_ALIAS = object()
BENCHMARK_CACHE_KEYS = frozenset(("concurrency", "concurrency_for", "concurrency_environment",
                                  "concurrency_revision", BENCHMARK_IDENTITY_KEY))
# A scheduling hint, not a request setting: changing it must not retire a
# measurement of the endpoint's own throughput.
_BENCHMARK_IDENTITY_EXCLUDED = _PROFILE_CREDENTIAL_KEYS | BENCHMARK_CACHE_KEYS | frozenset(("on_this_gpu",))


def get_profile_neutral_values():
    """Schema defaults for every LLMConfig field.

    A profile that passed through Setup carries every field, including its
    defaults. Comparing against these keeps a normalized blank stub from
    looking like a configured profile.
    """
    from config_settings import LLMConfig
    neutral = {}
    for name, field in LLMConfig.model_fields.items():
        if field.is_required():
            neutral[name] = "" if field.annotation is str else None
        else:
            neutral[name] = field.get_default(call_default_factory=True)
    return neutral


def get_profile_settings(block):
    """Profile fields describing a configuration, not credentials or cache."""
    if not isinstance(block, dict):
        return {}
    excluded = _PROFILE_CREDENTIAL_KEYS | BENCHMARK_CACHE_KEYS
    return {key: value for key, value in block.items() if key not in excluded}


def is_profile_configured(block):
    """Whether a saved LLM profile carries a real, non-default setting.

    Credentials alone never make a profile usable, and a normalized blank
    stub (every schema field at its default) or a cache-only slot must not
    shadow a complete legacy `llm` profile. Manual transport is intentional
    on its own and needs no URL; partial HTTP profiles (endpoint-, model- or
    adapter-only) predate Setup and stay valid as the active profile, which
    Setup fills in before a run.
    """
    settings = get_profile_settings(block)
    if settings.get("transport") == "manual":
        return True
    # attribution_adapter lives outside LLMConfig, so it is not in the neutral
    # map. A configured adapter still makes the profile complete: the required
    # serving check must run even when the named slot holds nothing else.
    spec = settings.get("attribution_adapter")
    if isinstance(spec, dict) and str(spec.get("path") or "").strip():
        return True
    neutral = get_profile_neutral_values()
    return any(key in neutral and value != neutral[key] for key, value in settings.items())


def is_executable_failover_profile(block):
    """Whether the secondary can actually serve a run if the primary fails.

    The primary may be partial because Setup completes it; a failover target
    is dialled as saved, so an HTTP secondary needs both an endpoint and a
    model. Manual transport stays opt-in and serial, without a URL.
    """
    if not is_profile_configured(block):
        return False
    if block.get("transport") == "manual":
        return True
    return (is_usable_llm_endpoint(block.get("base_url"))
            and bool(str(block.get("model_name") or "").strip()))


def get_benchmark_identity(config, section, *, profile=None, ssh_alias=_BENCHMARK_SSH_ALIAS,
                           llm_mode=None):
    """Nonsecret identity of the requests a concurrency measurement belongs to.

    One answer for the benchmark writer, reader and Setup retention (Rule 15).
    It is derived from the effective run profile - the one the client is bound
    to - not from a later edit of the on-disk section: provider body options
    are folded into their effective form (typed reasoning_effort merged into
    the custom body), global token limits are inherited, and the management
    SSH alias is included whenever the endpoint is remote rather than only
    when the toggle says "remote" (a hosted legacy profile still runs with
    llm_mode=local). Credentials, cache metadata and the scheduling hint are
    excluded. Missing fields take schema defaults, so a compact legacy
    profile compares equal to the same settings normalized by Setup.

    `profile` lets the caller pass an already-bound run profile; when absent
    the section's own config block is used and global limits are inherited.
    A bound profile already carries the ceiling captured when its client was
    built, so inheriting again here would describe a later config edit rather
    than the requests that client actually sends (including an absent ceiling).
    `ssh_alias` overrides the management alias for the same reason, and
    `llm_mode` overrides the toggle: a Setup switch after the client was bound
    must not change the remote classification of that client's requests.
    """
    config = config if isinstance(config, dict) else {}
    bound = profile is not None
    if not bound:
        profile = config.get(section)
    if not isinstance(profile, dict):
        return None
    from llm_provider import get_provider_extra_body
    if bound:
        effective = profile
    else:
        from llm_request_budget import get_inherited_profile_limits
        effective = get_inherited_profile_limits(profile, config.get("generation") or {})
    neutral = get_profile_neutral_values()
    identity = {key: neutral[key] for key in neutral if key not in _BENCHMARK_IDENTITY_EXCLUDED}
    identity.pop("reasoning_effort", None)
    identity.update({key: value for key, value in effective.items() if key in identity})
    # Fold after the raw profile copy: the effective body, not the raw JSON,
    # is what the bound client sends, and a body-only spelling of the same
    # option must therefore compare equal to the typed setting.
    identity["provider_extra_body"] = get_provider_extra_body(effective)
    if ssh_alias is _BENCHMARK_SSH_ALIAS:
        ssh_alias = config.get("llm_remote_ssh")
    mode = llm_mode if llm_mode is not None else (config.get("llm_mode") or "local")
    if is_remote_llm(mode, identity.get("base_url", "")):
        identity["ssh_alias"] = ssh_alias
    return identity


def _canonical_identity_value(value):
    """Normalize integral floats without collapsing distinct large integers.

    Casting every number to float made adjacent provider-body seeds either side
    of 2^53 produce the same receipt. Integers keep their full precision; an
    integral float (a JSON round-trip of an int default) folds back to an int.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {key: _canonical_identity_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_identity_value(item) for item in value]
    return value


def get_benchmark_identity_key(config, section, *, profile=None, ssh_alias=_BENCHMARK_SSH_ALIAS,
                               llm_mode=None):
    """A versioned digest of the effective request identity.

    The canonical JSON is hashed rather than persisted: a raw identity carried
    provider body values (and can carry future ones) into config.json, so a
    retired value stayed on disk long after the setting that produced it was
    changed. The version makes a future identity-schema change explicit rather
    than silently reinterpreting old receipts.
    """
    identity = get_benchmark_identity(config, section, profile=profile, ssh_alias=ssh_alias,
                                      llm_mode=llm_mode)
    if identity is None:
        return None
    canonical = json.dumps({key: _canonical_identity_value(value) for key, value in identity.items()},
                           sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"v{BENCHMARK_IDENTITY_VERSION}:{digest}"


def is_benchmark_digest(value):
    """Whether a stored receipt identity is a generated digest, not raw JSON.

    Pre-digest receipts carried provider body values verbatim; reusing one as
    an identity key would keep a retired value on disk after a request edit.
    """
    return isinstance(value, str) and bool(_BENCHMARK_DIGEST_PATTERN.fullmatch(value))


def json_values_equal(left, right):
    """JSON equality that never confuses a boolean with a number.

    Python treats True and 1 as equal, which would let an old `{"flag":true}`
    receipt vouch for a current `{"flag":1}` request. Only numeric scalars may
    compare across int/float (a JSON round-trip spelling); every other type
    must match before its value is considered.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return (left.keys() == right.keys()
                and all(json_values_equal(left[key], right[key]) for key in left))
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (len(left) == len(right)
                and all(json_values_equal(a, b) for a, b in zip(left, right)))
    return left == right


def predigest_identity_matches(raw, config, section):
    """Whether an old receipt's plain identity describes this exact request.

    A receipt whose provider body changed before Setup opened must be dropped,
    not given the new request's digest; only an identity that still matches
    the saved section can be relabelled and reused.
    """
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return False
    current = get_benchmark_identity(config, section)
    return (isinstance(stored, dict) and isinstance(current, dict)
            and json_values_equal(_canonical_identity_value(stored),
                                       _canonical_identity_value(current)))


def get_benchmark_destination(profile):
    """The endpoint::model a stored measurement was actually taken against.

    The environment records the destination at measurement time; a named slot
    may since have been hydrated to a neutral stub by Setup, so its current
    base_url/model_name alone cannot qualify a fresh receipt.
    """
    profile = profile if isinstance(profile, dict) else {}
    environment = profile.get("concurrency_environment")
    if isinstance(environment, dict):
        base_url = environment.get("base_url")
        model_name = environment.get("model_name")
        if base_url and model_name:
            return f"{base_url}::{model_name}"
    return f"{profile.get('base_url', '')}::{profile.get('model_name', '')}"


def is_qualified_concurrency_cache(profile):
    """Whether a profile carries a real measurement bound to its own endpoint."""
    if not isinstance(profile, dict):
        return False
    concurrency = profile.get("concurrency")
    if not (isinstance(concurrency, int) and not isinstance(concurrency, bool) and concurrency > 0):
        return False
    return (profile.get("concurrency_for") == get_benchmark_destination(profile)
            and has_valid_benchmark_environment(profile))


def has_valid_benchmark_environment(profile):
    """Whether a stored entry carries real measured-environment evidence.

    An empty or malformed environment is not evidence: a neutral named slot
    must not displace a qualified measurement just because it has a cache
    field. The version and the measured endpoint are the minimum schema.
    """
    environment = profile.get("concurrency_environment") if isinstance(profile, dict) else None
    if not isinstance(environment, dict) or not environment:
        return False
    version = environment.get("version")
    base_url = environment.get("base_url")
    return (isinstance(version, int) and not isinstance(version, bool)
            and isinstance(base_url, str) and bool(base_url.strip()))


def get_benchmark_target_slot(config):
    """The named slot the benchmark reads for this config's active endpoint.

    The slot follows the endpoint, not the toggle: a hosted profile saved in
    llm_local with llm_mode=local is still dialled remotely and measured into
    llm_remote. Migration must land evidence there for the next run to reuse.
    """
    config = config if isinstance(config, dict) else {}
    active = get_saved_llm_profile(config, require_complete=True) or config.get("llm") or {}
    base_url = active.get("base_url", "") if isinstance(active, dict) else ""
    return "llm_remote" if is_remote_llm(config.get("llm_mode") or "local", base_url) else "llm_local"


def get_document_with_retained_cache(document, existing):
    """Return a save document carrying still-valid measured evidence.

    A measurement belongs to the requests that produced it, so a qualified
    entry with real environment evidence is kept for the matching effective
    identity. The legacy `llm` mirror is one such source, not a separate
    cache: its entry is also placed in the slot the benchmark actually reads
    (endpoint-based, so a hosted legacy profile with llm_mode=local migrates
    into llm_remote). A blank or key-only named slot with an invalid
    environment cannot displace qualified legacy evidence. Credentials and
    every other posted field come from `document` unchanged.
    """
    result = dict(document)
    before_map = existing if isinstance(existing, dict) else {}
    qualified = {}
    # Named slots first: a structurally valid named receipt is the authority
    # for its own identity. The mirror is only a fallback source, never a way
    # to overwrite newer named evidence for the same endpoint. A receipt is
    # identified by its stored digest, not by a digest recomputed from the
    # surrounding profile, which a later edit can change without the receipt
    # itself changing. A pre-digest receipt gets a fresh nonsecret one.
    for section in ("llm_local", "llm_remote", "llm"):
        before = before_map.get(section)
        if not is_qualified_concurrency_cache(before) or not has_valid_benchmark_environment(before):
            continue
        stored = before.get(BENCHMARK_IDENTITY_KEY)
        if is_benchmark_digest(stored):
            key = stored
        elif stored is None or predigest_identity_matches(stored, before_map, section):
            key = get_benchmark_identity_key(before_map, section)
        else:
            # Stale or malformed plain identity: drop it rather than relabel
            # evidence from a different request as the active one's receipt.
            continue
        if key is None:
            continue
        entry = {name: before[name] for name in BENCHMARK_CACHE_KEYS if name in before}
        entry[BENCHMARK_IDENTITY_KEY] = key
        if key not in qualified:
            qualified[key] = entry
    for section in ("llm", "llm_local", "llm_remote"):
        after = result.get(section)
        if not isinstance(after, dict):
            continue
        entry = qualified.get(get_benchmark_identity_key(result, section))
        if entry:
            result[section] = {**after, **entry}
    target = get_benchmark_target_slot(result)
    after = result.get(target)
    # The mirror is the active proposed request. Only a receipt for that exact
    # request may land in the benchmark's slot, whether it came from the legacy
    # mirror or a named slot hydrated to a neutral stub. A retired receipt from
    # a body/model/mode edit must not be carried forward.
    proposed_key = get_benchmark_identity_key(result, "llm")
    receipt = qualified.get(proposed_key) if proposed_key is not None else None
    if receipt and isinstance(after, dict) and not is_qualified_concurrency_cache(after):
        result[target] = {**after, **receipt}
    return result


def get_saved_llm_profile(config, *, require_complete=False):
    """The named profile exactly as saved, without inherited global limits.

    Settings round-trips (the Setup migration, secret restoration) and
    credential lookup need the raw profile, even when it is partial: a saved
    key or header still belongs to that endpoint. Run resolution passes
    `require_complete` so a partial primary cannot shadow a complete legacy
    `llm` profile and silently dial the wrong endpoint.
    """
    if not isinstance(config, dict):
        return {}
    mode = config.get("llm_mode") or "local"
    block = config.get(f"llm_{mode}")
    if not isinstance(block, dict) or (require_complete and not is_profile_configured(block)):
        block = config.get("llm")
    return block if isinstance(block, dict) else {}


def get_hydrated_profile_source(config, section):
    """The persisted profile GET used to fill `section` before the form posts.

    The active named slot is filled from the complete legacy mirror when it is
    not itself configured; an inactive slot keeps its own saved values. Preview
    uses this same decision to establish a baseline instead of guessing from
    equal endpoints or bodies.
    """
    config = config if isinstance(config, dict) else {}
    mode = config.get("llm_mode") or "local"
    if section == f"llm_{mode}":
        block = config.get(section)
        if not isinstance(block, dict) or not is_profile_configured(block):
            return "llm" if isinstance(config.get("llm"), dict) else section
    return section


def get_failover_llm_config(config):
    """The profile a run switches to when `llm_failover` is on: whichever of
    llm_local / llm_remote is NOT active. {} when failover is off or the other
    profile is not configured, so callers never half-fail over."""
    if not isinstance(config, dict) or not config.get("llm_failover"):
        return {}
    other = "remote" if (config.get("llm_mode") or "local") == "local" else "local"
    block = config.get(f"llm_{other}")
    if not is_executable_failover_profile(block):
        return {}
    from llm_request_budget import get_inherited_profile_limits
    return get_inherited_profile_limits(block, config.get("generation") or {})


def is_active_llm_remote(config):
    """Single source of truth for "will this config's runs go to a remote
    (billed) endpoint?".

    `is_remote_llm` answers "is THIS endpoint remote?", which still leaves the
    caller to have chosen the right endpoint - and choosing the wrong one is
    how the Setup page's cost warning came to be computed from the stale `llm`
    mirror (#2007): with llm_mode=local, a mirror still pointing at localhost
    while the active `llm_local` was a billed hosted endpoint, the prompt was
    skipped and the run then dialled the billed endpoint through
    `get_active_llm_config`. Resolving the active profile here means a caller
    cannot pass the mirror by accident.
    """
    config = config if isinstance(config, dict) else {}
    active = get_active_llm_config(config)
    if not is_billed_http_profile(active):
        return False
    return is_remote_llm(config.get("llm_mode") or "local", active.get("base_url", ""))


def find_lms_binary():
    """Return the path to the `lms` CLI, or None if it isn't available."""
    return shutil.which("lms")


def get_local_gpu_info():
    """Return identity and memory from one unambiguous local GPU probe."""
    data = run_rocm_smi_json(["--showmeminfo", "vram", "--showproductname"],
                             rocm_smi_path="/opt/rocm/bin/rocm-smi", timeout=2)
    if data:
        if len(data) != 1:
            return None
        card = next(iter(data.values()))
        if not isinstance(card, dict):
            return None
        name = card.get("Card Series") or card.get("Card Model")
        backend = "rocm"
        try:
            total = int(card["VRAM Total Memory (B)"])
            used = int(card["VRAM Total Used Memory (B)"])
        except (KeyError, TypeError, ValueError):
            total, used = None, None
    else:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if result.returncode != 0 or len(lines) != 1:
                return None
            name, total_text, used_text = lines[0].rsplit(",", 2)
            backend = "cuda"
            try:
                total = int(total_text.strip()) * 1024 ** 2
                used = int(used_text.strip()) * 1024 ** 2
            except ValueError:
                total, used = None, None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    name = name.strip() if isinstance(name, str) else None
    if not name or name == "N/A":
        return None
    if total is None or total <= 0 or used is None or not 0 <= used <= total:
        total, used = None, None
    return {"name": name, "backend": backend, "total": total, "used": used}


def get_local_vram_bytes():
    """Return memory only when the local device selection is unambiguous."""
    info = get_local_gpu_info()
    return (info["total"], info["used"]) if info and info["total"] is not None else None


def get_safe_local_settings(model_name, model_loaded, vram_bytes=None):
    """Select a verified profile from live VRAM, otherwise return the fallback."""
    fallback = dict(IDEAL_SETTINGS)
    fallback["reason"] = "conservative fallback"
    profile = _VERIFIED_LOCAL_PROFILES.get(model_name)
    memory = vram_bytes if vram_bytes is not None else get_local_vram_bytes()
    if not profile or not memory:
        fallback["reason"] = ("model has no verified dynamic profile" if not profile
                              else "GPU memory could not be measured")
        return fallback

    total, used = memory
    baseline = used if model_loaded else used + profile["model_vram_bytes"]
    extra_tokens = max(0, profile["context_length"] - IDEAL_SETTINGS["context_length"])
    projected = baseline + extra_tokens * profile["bytes_per_extra_context_token"]
    if projected + _LOCAL_VRAM_RESERVE_BYTES > total:
        fallback["reason"] = "live VRAM lacks the 2 GiB safety reserve"
        return fallback
    return {"context_length": profile["context_length"],
            "parallel": profile["parallel"], "gpu": "max",
            "reason": "verified profile fits live VRAM with 2 GiB reserved"}


def _validate_ssh_alias(ssh_alias):
    """Raise OSError if ssh_alias is empty or could be parsed by `ssh` as an
    option rather than a literal hostname (e.g. starts with '-'). Raises
    OSError (not ValueError) specifically because every SSH-driving caller in
    this module already catches OSError for connection failures.
    """
    if not ssh_alias or ssh_alias.startswith("-"):
        raise OSError(f"Invalid SSH host alias: {ssh_alias!r}")


def _ssh_run(ssh_alias, remote_cmd, timeout, connect_timeout=10):
    """Run remote_cmd on ssh_alias via `bash -lc`, returning the CompletedProcess.

    Shared by every remote helper below instead of each one re-building its
    own ssh argv list. Pre-quoting remote_cmd into a single argv element
    (rather than passing "bash", "-lc", remote_cmd as 3 separate argv
    elements) is required: ssh joins all args after the host with bare
    spaces and hands the result to the remote shell as one line, so 3 separate
    elements would let that join split remote_cmd's own `;`-separated
    statements apart, breaking any command after the first.
    """
    _validate_ssh_alias(ssh_alias)
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
         ssh_alias, "bash -lc " + shlex.quote(remote_cmd)],
        capture_output=True, text=True, timeout=timeout,
    )


def is_lmstudio_status_optimized(status, ideal_settings):
    """Compare a loaded status against the selected safe settings."""
    return (bool(status["loaded"])
            and status["context_length"] == ideal_settings["context_length"]
            and status["parallel"] == ideal_settings["parallel"])


def _parse_lms_ps_output(stdout, model_name, ideal_settings):
    """Parse `lms ps --json` output and return the status dict for model_name.

    Result dict: {available, loaded, context_length, parallel, optimized}.
    Tries json.loads on the whole string first (the local, no-login-shell
    case always looks like this); only on failure falls back to scanning
    non-empty lines from the end, since a remote login shell (bash -lc)
    prints a decorative banner ahead of the real JSON output - this fallback
    keeps the local path's behavior identical while making the remote path
    robust to one stray trailing non-JSON line too.
    """
    models = None
    try:
        models = json.loads(stdout or "")
    except json.JSONDecodeError:
        for line in reversed([l for l in (stdout or "").splitlines() if l.strip()]):
            try:
                models = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if not isinstance(models, list):
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    for m in models:
        if not isinstance(m, dict):
            continue
        if m.get("identifier") == model_name or m.get("modelKey") == model_name:
            context_length = m.get("contextLength")
            parallel = m.get("parallel")
            status = {"available": True, "loaded": True, "context_length": context_length,
                      "parallel": parallel}
            status["optimized"] = is_lmstudio_status_optimized(status, ideal_settings)
            return status

    return {"available": True, "loaded": False, "context_length": None,
            "parallel": None, "optimized": False}


def get_lmstudio_status(model_name):
    """Return current load status for model_name via `lms ps --json`.

    Result dict: {available, loaded, context_length, parallel, optimized}
    - available: whether the `lms` CLI could be found/run
    - loaded: whether the model is currently loaded
    - optimized: whether the loaded settings match IDEAL_SETTINGS
    """
    lms = find_lms_binary()
    if not lms:
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    try:
        result = subprocess.run([lms, "ps", "--json"], capture_output=True,
                                 text=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    if result.returncode != 0:
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}
    status = _parse_lms_ps_output(result.stdout, model_name, IDEAL_SETTINGS)
    ideal = get_safe_local_settings(model_name, status["loaded"])
    status["optimized"] = is_lmstudio_status_optimized(status, ideal)
    status.update({"ideal_context_length": ideal["context_length"],
                   "ideal_parallel": ideal["parallel"],
                   "settings_reason": ideal["reason"]})
    return status


def _remote_server_bound(ssh_alias, port, timeout=10):
    """Read whether this port has a network wildcard listener, not loopback.

    Recognize IPv4/IPv6 and ss's abbreviated wildcard spelling. This observes
    a listener, not end-to-end tunnel reachability or IPv6 dual-stack policy.
    SSH failure remains unknown (None); this function never starts anything.
    """
    try:
        result = _ssh_run(ssh_alias, "ss -tlnp", timeout=timeout, connect_timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields and fields[0] in ("tcp", "tcp6"):
            fields = fields[1:]
        if len(fields) < 5 or fields[0] != "LISTEN":
            continue
        host, separator, listener_port = fields[3].rpartition(":")
        if (separator and listener_port == str(port)
                and host in ("0.0.0.0", "*", "::", "[::]")):  # nosec B104 - netstat output, never bind()
            return True
    return False


def ensure_remote_server_running(ssh_alias, port, timeout=30):
    """Start LM Studio's server bound to 0.0.0.0:<port> on ssh_alias if it
    isn't already - `apply_remote_lmstudio_settings`'s `lms load` only
    loads a model into an already-running server; it never starts the
    server itself, so a stopped or localhost-only-bound server "succeeds"
    while staying unreachable through the forwarded tunnel.

    Returns (success, message). Never raises.
    """
    if isinstance(port, str):
        digits = port.strip().lstrip("0")
        if re.fullmatch(r"[0-9]+", port.strip()) and len(digits) <= 5:
            port = int(digits or "0")
    if type(port) is not int or not 1 <= port <= 65535:
        return False, "Invalid server port: expected an integer from 1 to 65535"
    if _remote_server_bound(ssh_alias, port, timeout=10):
        return True, f"Server already bound on a network wildcard at port {port}"
    remote_cmd = f"lms server start --port {port} --bind 0.0.0.0"
    try:
        result = _ssh_run(ssh_alias, remote_cmd, timeout=timeout, connect_timeout=15)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"SSH to '{ssh_alias}' failed: {e}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or result.stdout.strip()
                       or f"ssh exited {result.returncode}")
    return True, f"Started server on 0.0.0.0:{port}"


def get_remote_lmstudio_status(ssh_alias, model_name, timeout=20, port=None):
    """Like get_lmstudio_status, but for a remote LM Studio reached over SSH
    (ssh_alias e.g. "tnr-0", from config.json's llm_remote_ssh).

    Result dict: {available, loaded, context_length, parallel, optimized} -
    "optimized" compares against REMOTE_IDEAL_SETTINGS, mirroring
    get_lmstudio_status's local IDEAL_SETTINGS comparison. Best-effort: never
    raises, returns available=False on any SSH/parse failure or if ssh_alias
    isn't configured.

    `loaded: True` here only means `lms ps` sees the model in memory - it
    does NOT mean the HTTP server is reachable (a server bound to
    127.0.0.1 or not running at all still reports a loaded model). Pass
    `port` to also get a `server_reachable` field; omitted by default so
    existing callers that don't have a port handy are unaffected.
    """
    if not ssh_alias:
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    try:
        result = _ssh_run(ssh_alias, "lms ps --json", timeout=timeout, connect_timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    if result.returncode != 0:
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}
    status = _parse_lms_ps_output(result.stdout, model_name, REMOTE_IDEAL_SETTINGS)
    if port is not None:
        status["server_reachable"] = _remote_server_bound(ssh_alias, port, timeout=timeout)
    return status


_remote_status_cache = {}  # native/runtime identity tuples -> (timestamp, status_dict)
_local_status_cache_epoch = 0
_remote_status_cache_epoch = 0
_remote_status_alias_epochs = WeakValueDictionary()
_REMOTE_STATUS_CACHE_TTL = 10  # seconds - shorter than the UI's 30s poll interval
_remote_status_cache_lock = threading.Lock()  # protects _remote_status_cache itself (fast dict ops only)
_remote_status_key_locks = WeakValueDictionary()  # waiting/fetching callers own strong references

def ensure_remote_status_key_lock(key):
    """Retain one lock while callers are waiting for or fetching this key."""
    with _remote_status_cache_lock:
        lock = _remote_status_key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _remote_status_key_locks[key] = lock
        return lock


def is_local_status_cache_key(key):
    return len(key) == 5 and key[-1] == "local-ui"


class _RemoteStatusAliasEpoch:
    pass


def _status_cache_epoch_locked(key):
    """Capture an alias token retained only while its probes are in flight."""
    if is_local_status_cache_key(key):
        return _local_status_cache_epoch
    alias = key[0]
    token = _remote_status_alias_epochs.get(alias)
    if token is None:
        token = _RemoteStatusAliasEpoch()
        _remote_status_alias_epochs[alias] = token
    return _remote_status_cache_epoch, token


def _prune_expired_remote_status_cache_locked(now):
    """Remove expired observations; callers hold the cache mutex."""
    local_now = time.monotonic()
    expired = [key for key, (timestamp, _) in _remote_status_cache.items()
               if (local_now if is_local_status_cache_key(key) else now) - timestamp >= _REMOTE_STATUS_CACHE_TTL]
    for key in expired:
        del _remote_status_cache[key]


def ensure_remote_status_cache_entry(key):
    """Prune expired observations and return an independent live entry."""
    with _remote_status_cache_lock:
        _prune_expired_remote_status_cache_locked(time.time())
        return deepcopy(_remote_status_cache.get(key))


def save_remote_status_cache(key, status, cache_epoch=None):
    """Store a copied observation without retaining expired other keys."""
    save_runtime_status_cache(key, status, cache_epoch)


def get_remote_lmstudio_status_cached(ssh_alias, model_name, timeout=20):
    """Like get_remote_lmstudio_status, but reuses a result younger than
    _REMOTE_STATUS_CACHE_TTL seconds instead of making a fresh SSH round-trip.

    Multiple browser tabs each poll independently every 30s; without this,
    each poll across every open tab triggers its own live SSH call to the
    remote host just to refresh a status badge. This caps it to at most one
    SSH call per TTL window regardless of how many tabs are open.

    Keyed by (ssh_alias, model_name) - not just ssh_alias - so switching the
    configured model doesn't return a stale status computed for the
    previous one. The actual SSH call runs under a per-key lock (not the
    global _remote_status_cache_lock, which only ever guards the dict's own
    get/set) so concurrent requests for the SAME key block on one real SSH
    call, while requests for a DIFFERENT key aren't held up behind someone
    else's slow round-trip.
    """
    key = (ssh_alias, model_name)
    cached = ensure_remote_status_cache_entry(key)
    if cached:
        return cached[1]

    with ensure_remote_status_key_lock(key):
        cached = ensure_remote_status_cache_entry(key)
        if cached:
            return cached[1]
        with _remote_status_cache_lock:
            epoch = _status_cache_epoch_locked(key)
        status = get_remote_lmstudio_status(ssh_alias, model_name, timeout=timeout)
        save_remote_status_cache(key, status, epoch)
        return deepcopy(status)


def get_remote_runtime_status_cached(base_url, ssh_alias, model_name, api_key=None,
                                     authorization=None):
    """Preserve the existing remote runtime-cache API."""
    return get_runtime_status_cached("remote", base_url, ssh_alias, model_name, api_key,
                                     authorization)


def save_runtime_status_cache(key, status, local_epoch):
    """Reject an observation that overlapped a settings invalidation."""
    snapshot = deepcopy(status)
    with _remote_status_cache_lock:
        epoch = _status_cache_epoch_locked(key)
        if local_epoch is not None and local_epoch != epoch:
            return
        now = time.time()
        _prune_expired_remote_status_cache_locked(now)
        timestamp = time.monotonic() if is_local_status_cache_key(key) else now
        _remote_status_cache[key] = (timestamp, snapshot)


def invalidate_local_status_cache():
    """Retire local UI observations, including in-flight results."""
    global _local_status_cache_epoch
    with _remote_status_cache_lock:
        _local_status_cache_epoch += 1
        for key in list(_remote_status_cache):
            if is_local_status_cache_key(key):
                del _remote_status_cache[key]


def get_runtime_status_cached(llm_mode, base_url, ssh_alias, model_name, api_key=None,
                              authorization=None):
    """Cache an explicitly requested UI runtime observation, including `/props`.

    The credential is part of the cache identity: a profile authenticating by
    a non-Bearer header must not inherit a probe made with an unrelated api_key.
    Only a digest is stored, never the header value itself.
    """
    from llm_provider import resolve_authorization_value
    api_key = resolve_authorization_value(api_key)
    authorization = resolve_authorization_value(authorization)
    remote = is_remote_llm(llm_mode, base_url)
    key = (ssh_alias, model_name, base_url,
           hashlib.sha256(repr((type(api_key).__name__, api_key,
                                type(authorization).__name__, authorization)).encode()).hexdigest()
           if (api_key is not None or authorization is not None) else None)
    if not remote:
        key = (None, *key[1:], "local-ui")
    cached = ensure_remote_status_cache_entry(key)
    if cached:
        return cached[1]
    with ensure_remote_status_key_lock(key):
        cached = ensure_remote_status_cache_entry(key)
        if cached:
            return cached[1]
        with _remote_status_cache_lock:
            epoch = _status_cache_epoch_locked(key)
        status = get_current_status(llm_mode, base_url, model_name, ssh_alias, api_key=api_key,
                                    authorization=authorization)
        save_runtime_status_cache(key, status, epoch)
        return deepcopy(status)


def invalidate_remote_status_cache(ssh_alias=None):
    """Drop cached remote status so the next poll makes a fresh SSH call.

    Call this after any action that changes what 'lms ps' would report
    (e.g. apply_remote_lmstudio_settings) - otherwise a poll within the TTL
    window can show pre-change status right after a successful change.
    ssh_alias=None clears every cached entry; pass a specific alias to
    clear just that one.
    """
    global _local_status_cache_epoch, _remote_status_cache_epoch
    with _remote_status_cache_lock:
        if ssh_alias is None:
            _remote_status_cache_epoch += 1
            _remote_status_alias_epochs.clear()
            _local_status_cache_epoch += 1
            _remote_status_cache.clear()
        else:
            _remote_status_alias_epochs.pop(ssh_alias, None)
            for key in [k for k in _remote_status_cache if k[0] == ssh_alias]:
                del _remote_status_cache[key]


def _gpu_name_from_probes(run):
    """SSH-compatible diagnostic logic for get_remote_gpu_name_and_backend.

    `run` is a callable(argv_list) -> CompletedProcess-like object with
    .returncode/.stdout, so the same probe logic works for a local
    subprocess.run and an SSH-wrapped one.

    Takes the LAST non-empty output line, not the first: a remote login
    shell (bash -lc) prints a decorative banner ahead of any command's real
    output (confirmed live - 4 lines of box-drawing before the actual
    "NVIDIA RTX A6000"), so the real answer is always the most recent line,
    never the first. Local memory/profile selection uses get_local_gpu_info
    instead; this remote diagnostic does not size local profiles.
    """
    try:
        result = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        lines = [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
        name = lines[-1] if lines else ""
        if result.returncode == 0 and name:
            return name, "cuda"
    except (OSError, IndexError, subprocess.TimeoutExpired):
        pass

    try:
        result = run(["rocm-smi", "--showproductname"])
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout:
        # rocm-smi's JSON mode is handled by run_rocm_smi_json for local calls;
        # for the shared SSH-compatible path we just scan the plain-text output.
        for line in result.stdout.splitlines():
            if "Card series" in line or "Card model" in line:
                name = line.split(":", 1)[-1].strip()
                if name:
                    return name, "rocm"
    return None, None


def get_gpu_name_and_backend():
    """Return the identity selected by the same local policy as VRAM sizing."""
    info = get_local_gpu_info()
    return (info["name"], info["backend"]) if info else (None, None)


def get_remote_gpu_name_and_backend(ssh_alias):
    """Same as get_gpu_name_and_backend, but probes a remote host over SSH."""
    if not ssh_alias:
        return None, None

    def _run(argv):
        remote_cmd = " ".join(shlex.quote(a) for a in argv)
        return _ssh_run(ssh_alias, remote_cmd, timeout=15, connect_timeout=10)

    return _gpu_name_from_probes(_run)


def apply_lmstudio_settings(model_name, ideal=True, ttl=3600):
    """Reload model_name with either the VRAM-safe (ideal) or default settings.

    Best-effort: returns (success, message). Never raises.
    """
    lms = find_lms_binary()
    if not lms:
        return False, "lms CLI not found on PATH"

    if ideal:
        status = get_lmstudio_status(model_name)
        settings = get_safe_local_settings(model_name, status["loaded"])
    else:
        settings = DEFAULT_SETTINGS

    invalidate_local_status_cache()
    try:
        # `lms load` refuses to load if a model is already loaded under the same
        # identifier, so drop any existing instance first. If unload fails (e.g.
        # the model is busy), the load below will likely fail too - remember that
        # so the failure message can explain why the old settings may still be
        # active instead of just reporting the load error in isolation.
        unload_failed = False
        try:
            unload_result = subprocess.run([lms, "unload", model_name], capture_output=True,
                                            text=True, timeout=60)
            unload_failed = unload_result.returncode != 0
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
            unload_failed = True

        try:
            result = subprocess.run(
                [lms, "load", model_name,
                 "--context-length", str(settings["context_length"]),
                 "--parallel", str(settings["parallel"]),
                 "--gpu", settings["gpu"],
                 "--identifier", model_name,
                 "--ttl", str(ttl),
                 "-y"],
                capture_output=True, text=True, timeout=180
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
            return False, f"Failed to run lms load: {e}"

        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip() or "lms load failed"
            if unload_failed:
                msg += (" (unloading the previously-loaded model also failed - "
                        "it may still be running with different settings)")
            return False, msg

        label = "VRAM-safe" if ideal else "default"
        detail = (f" ({settings['context_length']} context, parallel {settings['parallel']}; "
                  f"{settings.get('reason', 'fixed profile')})")
        return True, f"Reloaded {model_name} with {label} settings{detail}"

    finally:
        invalidate_local_status_cache()


def apply_remote_lmstudio_settings(ssh_alias, model_name, ideal=True, port=1234):
    """Reload model_name on a remote LM Studio host via SSH (`lms` over `tnr-N`).

    Returns (success, message). The forwarded OpenAI /v1 port can't change load
    settings, so we drive the remote `lms` CLI directly. Never raises. Unlike
    apply_lmstudio_settings, this intentionally does not pass `--ttl` - that's
    today's existing remote semantics, not an oversight.

    Ensures the server itself is bound to 0.0.0.0:<port> before loading -
    `lms load` only loads a model into an already-running server, so without
    this a stopped or localhost-only-bound server used to "succeed" here
    while staying unreachable through the forwarded tunnel.
    """
    server_ok, server_msg = ensure_remote_server_running(ssh_alias, port)
    if not server_ok:
        return False, f"Could not start remote server: {server_msg}"
    settings = REMOTE_IDEAL_SETTINGS if ideal else REMOTE_DEFAULT_SETTINGS
    # model_name is shlex.quote()'d (not hand-wrapped in '...') since it comes
    # from user-editable config with no character restrictions - a bare
    # f"'{model_name}'" would let an embedded single quote break out of the
    # intended argument and inject additional shell commands once bash -lc
    # evaluates remote_cmd on the remote host.
    quoted_model = shlex.quote(model_name)
    remote_cmd = (
        f"lms unload {quoted_model} >/dev/null 2>&1; "
        f"lms load {quoted_model} --context-length {settings['context_length']} "
        f"--parallel {settings['parallel']} --gpu max --identifier {quoted_model} -y"
    )
    try:
        result = _ssh_run(ssh_alias, remote_cmd, timeout=200, connect_timeout=15)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"SSH to '{ssh_alias}' failed: {e}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or result.stdout.strip()
                       or f"ssh exited {result.returncode}")
    label = f"best ({REMOTE_IDEAL_SETTINGS['context_length']} ctx)" if ideal else "default"
    invalidate_remote_status_cache(ssh_alias)
    return True, f"Reloaded {model_name} on '{ssh_alias}' with {label} settings"


# /props probe misses, scoped to origin + effective credential: a server that
# answers one profile may refuse another, and the digest must never be the
# credential itself. See get_llama_cpp_status.
PROPS_MISS_TTL_SECONDS = 600
PROPS_MAX_BYTES = 1 << 20
_props_miss = {}
_props_miss_lock = threading.Lock()


def _props_miss_key(origin, api_key=None, authorization=None):
    """One cache scope per origin and wire Authorization, never plaintext."""
    credential = (authorization if authorization else
                  (f'Bearer {api_key}' if api_key else None))
    digest = hashlib.sha256(repr((type(credential).__name__, credential)).encode()).hexdigest()
    return (origin, digest)


def get_llama_cpp_status(base_url, model_name, timeout=5, api_key=None, authorization=None):
    """-> a status dict if `base_url` is llama.cpp, else None.

    WHY THIS EXISTS. `get_lmstudio_status` asks `lms ps`, which is the right
    instrument for LM Studio and the wrong one for anything else. Against the
    llama.cpp server this project actually runs it reported

        {"available": true, "loaded": false, "context_length": null,
         "parallel": null, "optimized": false}

    for a model answering in 0.2 seconds - reading "LM Studio does not know
    about this" as "the model is not loaded". The Setup tab has therefore shown
    an unloaded, unoptimized model on every single load.

    llama.cpp answers /props, which LM Studio does not serve, and that response
    carries the truth this was guessing at: the served alias, the real context
    length, and the slot count. So the honest answer is available rather than a
    third "not applicable" - a llama.cpp server knows exactly what it has
    loaded, it was simply never asked.

    Returns None - not a status - when /props does not answer, so an LM Studio
    endpoint falls through to the LM Studio path untouched.
    """
    import urllib.error
    import urllib.request

    root = base_url.rsplit("/v1", 1)[0].rstrip("/")
    # An endpoint that is not llama.cpp stays not-llama.cpp: the Setup tab
    # polls status every ~30 s per open tab, and against an OpenAI-compatible
    # gateway every poll was a fresh GET /props answered 404 - hundreds in a
    # row in one user's proxy log (2026-09-17). Remember the miss per URL for
    # a while instead of asking again on every poll.
    from llm_provider import resolve_authorization_value
    api_key = resolve_authorization_value(api_key)
    authorization = resolve_authorization_value(authorization)
    miss_key = _props_miss_key(root, api_key, authorization)
    with _props_miss_lock:
        missed_at = _props_miss.get(miss_key)
    if missed_at is not None and time.monotonic() - missed_at < PROPS_MISS_TTL_SECONDS:
        return None
    # Same credential precedence as the LM Studio endpoint probe: an explicit
    # Authorization header wins verbatim, otherwise a bearer comes from api_key.
    headers = {}
    if authorization:
        headers['Authorization'] = authorization
    elif api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    try:
        props_request = urllib.request.Request(root + "/props", headers=headers)
        with open_no_redirect(props_request, timeout=timeout) as response:
            payload = response.read(PROPS_MAX_BYTES + 1)
            if len(payload) > PROPS_MAX_BYTES:
                return None
            props = json.loads(payload)
    except urllib.error.HTTPError as exc:
        # Cache an unsupported route, not a loading server, rate limit, auth
        # failure or gateway outage. Those do not identify the runtime.
        if exc.code in (404, 405, 501):
            with _props_miss_lock:
                _props_miss[miss_key] = time.monotonic()
        return None
    except Exception:                                       # noqa: BLE001
        return None
    if not isinstance(props, dict) or "default_generation_settings" not in props:
        with _props_miss_lock:
            _props_miss[miss_key] = time.monotonic()
        return None
    with _props_miss_lock:
        _props_miss.pop(miss_key, None)

    generation = props.get("default_generation_settings") or {}
    context = generation.get("n_ctx")
    slots = props.get("total_slots")
    alias = props.get("model_alias")
    # A served alias that is not the configured model is a REAL mismatch and
    # must not be reported as "loaded" - that is how a run gets scored against
    # a model nobody meant to use.
    expected = {model_name, str(model_name).rsplit("/", 1)[-1]}
    return {
        "available": True,
        "loaded": bool(context) and alias in expected,
        "context_length": context,
        "parallel": slots,
        # LM Studio's notion of "optimized" is its own load settings; llama.cpp
        # is configured at launch and has no equivalent, so None means "not a
        # question that applies here" rather than "no".
        "optimized": None,
        "runtime": "llama.cpp",
        "server_alias": alias,
        "build": props.get("build_info"),
        "model_path": props.get("model_path"),
        "model_ftype": props.get("model_ftype"),
        "reasoning_format": (generation.get("params") or {}).get("reasoning_format"),
        "ideal_context_length": context,
        "ideal_parallel": slots,
        "settings_reason": "llama.cpp is configured at launch, not reloaded",
    }


def get_lmstudio_management_binding(base_url, ssh_alias=None):
    """Verify a direct native endpoint against its configured CLI server."""
    try:
        url = urlparse(base_url)
        port = url.port or (443 if url.scheme == "https" else 80)
        if url.scheme not in ("http", "https") or url.path.rstrip("/") not in ("", "/v1"):
            return False, None, "proxied or invalid API root has no verified CLI binding"
        if ssh_alias:
            _validate_ssh_alias(ssh_alias)
            if is_local_llm_endpoint(base_url):
                return False, None, "forwarded loopback API has no verified SSH transport binding"
            config = subprocess.run(["ssh", "-G", ssh_alias], capture_output=True,
                                    text=True, timeout=5)
            proxies = [line.split(None, 1)[1].strip() for line in config.stdout.splitlines()
                       if line.lower().startswith("proxycommand ")]
            if any(proxy != "none" for proxy in proxies):
                return False, None, "custom SSH proxy transport has no verified direct API binding"
            hosts = [line.split(None, 1)[1].strip() for line in config.stdout.splitlines()
                     if line.lower().startswith("hostname ")]
            if (config.returncode != 0 or len(hosts) != 1
                    or hosts[0].lower().removesuffix(".") != (url.hostname or "").lower().removesuffix(".")):
                return False, None, "SSH destination does not match API hostname"
            result = _ssh_run(ssh_alias, "lms server status --json --quiet", timeout=10)
        else:
            if not is_local_llm_endpoint(base_url):
                return False, None, "nonlocal API has no management host"
            binary = find_lms_binary()
            if not binary:
                return False, None, "local lms CLI unavailable"
            result = subprocess.run([binary, "server", "status", "--json", "--quiet"],
                                    capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return False, None, "CLI server status failed"
        try:
            parsed = json.loads(result.stdout)
            document = parsed if isinstance(parsed, dict) else None
        except ValueError:
            document = None
        for line in reversed(result.stdout.splitlines()) if document is None else []:
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict):
                    document = candidate
                    break
            except ValueError:
                continue
        if (not document or document.get("running") is not True
                or type(document.get("port")) is not int or document["port"] != port):
            return False, None, "CLI server port does not match API endpoint"
        return True, port, "native API and direct CLI server binding verified"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, None, "CLI management binding could not be verified"


def get_manual_runtime_status(llm_config):
    """Declared-limit serial status for Manual transport, with no HTTP at all."""
    from llm_request_budget import get_request_limits
    limits = get_request_limits(llm_config or {})
    return {"runtime": "manual", "available": False, "loaded": False,
            "context_length": limits.get("context_length"), "parallel": 1,
            "optimized": None, "management_verified": False,
            "management_reason": "Manual transport has no runtime to configure"}


def get_current_status(llm_mode, base_url, model_name, ssh_alias=None, use_cache=False,
                       api_key=_STATUS_UNSET, cache_local=False,
                       authorization=_STATUS_UNSET, llm_config=None):
    """Read runtime without reloads; cache_local opts the UI into bounded caching.

    Existing local worker reads stay live even when use_cache=True. An explicit
    `authorization` is the profile's effective credential and is used verbatim
    by the native endpoint probe (a profile may authenticate by header rather
    than by api_key).
    """
    from llm_provider import resolve_authorization_value
    api_key = (None if api_key is _STATUS_UNSET
               else resolve_authorization_value(api_key))
    authorization = (None if authorization is _STATUS_UNSET
                     else resolve_authorization_value(authorization))
    if (llm_config or {}).get("transport") == "manual":
        return get_manual_runtime_status(llm_config)
    remote = is_remote_llm(llm_mode, base_url)
    if use_cache and (remote or cache_local):
        return get_runtime_status_cached(llm_mode, base_url, ssh_alias, model_name,
                                         api_key, authorization)
    native = get_llama_cpp_status(base_url, model_name, api_key=api_key,
                                  authorization=authorization)
    if native is not None:
        return native
    endpoint = get_lmstudio_endpoint_status(base_url, model_name, api_key,
                                            authorization=authorization)
    if endpoint is None:
        return {"runtime": "unknown", "available": False, "loaded": False,
                "context_length": None, "parallel": None, "optimized": False,
                "management_verified": False,
                "management_reason": "endpoint native runtime could not be verified"}
    if remote and not ssh_alias:
        binding = (False, None, "remote API has no SSH management host")
    else:
        binding = get_lmstudio_management_binding(base_url, ssh_alias if remote else None)
    verified, port, reason = binding
    if verified:
        status = (get_remote_lmstudio_status(ssh_alias, model_name) if remote
                  else get_lmstudio_status(model_name))
    else:
        status = endpoint
    return {**status, "runtime": "lmstudio", "management_verified": verified,
            "management_port": port, "management_reason": reason}


def get_planned_ideal_settings(llm_mode, base_url, model_name, ssh_alias=None, api_key=None,
                               authorization=None):
    """Return the settings a subsequent ``ensure_ideal_settings`` will target.

    This is a pure sizing helper for preflight UIs: it reports the verified
    target without reloading a model. Runtime callers must still call
    ``ensure_ideal_settings`` before dispatching work and size against its
    fresh post-heal status.

    ``context_known`` is False only when the context is the conservative
    fallback for a runtime that neither reported one nor can be healed to one.
    """
    status = get_current_status(llm_mode, base_url, model_name, ssh_alias, api_key=api_key,
                                authorization=authorization)
    if status.get("runtime") == "llama.cpp" or not status.get("management_verified"):
        reported = status.get("ideal_context_length") or status.get("context_length")
        return {
            "context_length": reported or IDEAL_SETTINGS["context_length"],
            "context_known": bool(reported),
            "parallel": status.get("ideal_parallel") or status.get("parallel") or 1,
            "settings_reason": status.get("settings_reason") or "current settings or conservative fallback",
        }
    if is_remote_llm(llm_mode, base_url):
        return {"context_length": REMOTE_IDEAL_SETTINGS["context_length"],
                "context_known": True,
                "parallel": REMOTE_IDEAL_SETTINGS["parallel"],
                "settings_reason": "remote ideal profile"}
    return {
        "context_length": (status.get("ideal_context_length")
                           or IDEAL_SETTINGS["context_length"]),
        "context_known": True,
        "parallel": status.get("ideal_parallel") or IDEAL_SETTINGS["parallel"],
        "settings_reason": status.get("settings_reason") or "conservative fallback",
    }


def get_planned_request_context(config):
    """-> {context, known, parallel}: the one pre-run answer to "what context
    will this run size against?" for admission and preflight alike (Rule 15).

    Planned (post-heal) runtime, narrowed by the active profile's declared
    context. A fallback guess never narrows a declaration; when neither is
    known, `known` is False and `context` is that conservative guess, usable
    for sizing but not as evidence that a larger request fits.
    """
    from llm_provider import get_effective_authorization
    from llm_request_budget import get_request_limits
    llm = get_active_llm_config(config)
    if llm.get("transport") == "manual":
        # Manual transport has no runtime to read; only a declaration is known.
        planned = {"context_length": IDEAL_SETTINGS["context_length"],
                   "context_known": False, "parallel": 1}
    else:
        planned = get_planned_ideal_settings(
            config.get("llm_mode", "local"), llm.get("base_url", ""),
            llm.get("model_name", ""), config.get("llm_remote_ssh"),
            api_key=llm.get("api_key"), authorization=get_effective_authorization(llm))
    detected = planned["context_length"] if planned.get("context_known") else None
    context = get_request_limits(llm, detected)["context_length"]
    return {"context": int(context or planned.get("context_length") or 0),
            "known": bool(context),
            "parallel": max(1, int(planned.get("parallel") or 1))}


def ensure_ideal_settings(llm_mode, base_url, model_name, ssh_alias=None, api_key=None,
                          authorization=None, llm_config=None):
    """Self-heal LM Studio's load settings (local or remote) toward the ideal
    config (VRAM-safe locally, large-context remotely), then return the FRESH
    post-heal status so callers can size chunking/context decisions and the
    concurrency benchmark off live truth instead of each re-fetching it.

    Shared by review_script.py and find_nicknames.py instead of each
    hand-rolling its own copy of this branch.

    Returns (is_remote, status, message). status always has the
    {available, loaded, context_length, parallel, optimized} shape. Never
    raises - every call it makes is itself best-effort/non-raising.
    """
    if (llm_config or {}).get("transport") == "manual":
        return (False, get_manual_runtime_status(llm_config),
                "Manual transport: serial execution with declared limits; no runtime to configure.")

    is_remote = is_remote_llm(llm_mode, base_url)

    initial_status = get_current_status(
        llm_mode, base_url, model_name, ssh_alias, api_key=api_key,
        authorization=authorization)
    if initial_status.get("runtime") == "llama.cpp":
        return (is_remote, initial_status,
                "llama.cpp server at %s - context %s, %s slot(s), fixed at "
                "launch. No LM Studio settings to apply."
                % (sanitize_url_for_display(base_url),
                   initial_status.get("context_length"),
                   initial_status.get("parallel")))

    if not initial_status.get("management_verified"):
        return is_remote, initial_status, "Settings were not applied: " + initial_status.get(
            "management_reason", "endpoint management ownership is unverified")

    if is_remote and not ssh_alias:
        return (True, initial_status,
                "Remote LLM endpoint - no SSH alias configured, cannot verify/apply ideal settings.")

    if is_remote:
        get_status = lambda: get_current_status(llm_mode, base_url, model_name, ssh_alias, api_key=api_key, authorization=authorization)
        apply_settings = lambda: apply_remote_lmstudio_settings(ssh_alias, model_name, ideal=True, port=initial_status["management_port"])
        label = "Remote LM Studio"
        ok_warning = ("Proceeding with whatever is currently loaded, which may truncate "
                      "responses or fail outright if the context is too small.")
    else:
        # ASK WHOSE SERVER THIS IS BEFORE CONFIGURING IT. A local endpoint is
        # not necessarily LM Studio - this project runs llama.cpp on 8090 for
        # every chain - and `lms` cannot configure a server it does not own.
        # Every unseen-book run on 2026-08-19 therefore logged "could not apply
        # ideal settings ... increases the risk of an out-of-memory crash",
        # which is both alarming and inapplicable: llama.cpp fixes its context
        # and slot count at launch, so there is nothing to apply and no risk to
        # warn about. get_llama_cpp_status already answers this question and
        # returns None for anything that is not llama.cpp (Rule 15) - the local
        # branch simply never asked it.
        get_status = lambda: get_current_status(llm_mode, base_url, model_name, ssh_alias, api_key=api_key, authorization=authorization)
        apply_settings = lambda: apply_lmstudio_settings(model_name, ideal=True)
        label = "LM Studio"
        ok_warning = ("The model may be running with a higher 'parallel'/context-length "
                      "configuration, which uses more VRAM per request and increases the "
                      "risk of an out-of-memory crash. The VRAM watchdog below will still "
                      "pause batches if usage gets too high, but if you hit OOM, restart "
                      "LM Studio and re-run.")

    status = initial_status
    if status["loaded"] and status["optimized"]:
        return is_remote, status, f"{label}: {model_name} already loaded with ideal settings."

    ok, msg = apply_settings()
    status = get_status()
    if ok:
        if not (status.get("management_verified") and status.get("loaded") and status.get("optimized")):
            return is_remote, status, f"{label}: CLI reported reload, but fresh status did not verify ideal settings. {status.get('management_reason', '')}"
        return is_remote, status, f"{label}: {msg}"
    if status["loaded"] and status["optimized"]:
        return (is_remote, status,
                f"{label}: could not reload ({msg}), but {model_name} is "
                f"already loaded with ideal settings - continuing.")
    return is_remote, status, f"{label}: WARNING - could not apply ideal settings ({msg}). {ok_warning}"
