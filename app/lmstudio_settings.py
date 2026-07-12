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
import os
import re
import shlex
import shutil
import subprocess
from collections import namedtuple
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

from utils import run_rocm_smi_json, atomic_json_write


@dataclass
class ApplyResult:
    """Result of an apply_*_lmstudio_settings call. Only the remote variant (a
    resolved Thunder reload) populates base_url/log_kind/target; local applies
    leave them None. A named result lets both variants be unpacked identically
    without trailing positional dead slots.

    - base_url: the freshly computed Thunder URL whenever resolution succeeded
      (regardless of whether verify did), else None.
    - log_kind: which diagnostic-log bucket a failure belongs to
      ("thunder_resolve" / "optimize" / "optimize_verify"), or None on success.
    - target: the resolved Thunder target dict (uuid/ip/ssh_port/...) used for
      the reload, so callers can record last_synced without reverse-parsing the
      URL. None for the literal-alias and local paths.
    """
    ok: bool
    message: str
    base_url: Optional[str] = None
    log_kind: Optional[str] = None
    target: Optional[dict] = None


# What a (verify-ok?, freshly-resolved url) pair means for the two distinct
# outputs the heal sites need: what to PERSIST to config vs. what a live client
# should ADOPT this run. Returned by decide_healed_urls so the optimize route
# and ensure_ideal_settings can't drift on that policy (Rule 15).
HealUrls = namedtuple("HealUrls", ["persist_url", "adopt_url"])

# ensure_ideal_settings' heal outcome handed to its callers: the url to use for
# this run's client (adopt_url), the url to persist (persist_url, None when
# unchanged), and the resolved Thunder target for last_synced (None locally).
HealOutcome = namedtuple("HealOutcome", ["adopt_url", "persist_url", "target"])

IDEAL_SETTINGS = {"context_length": 8192, "parallel": 1, "gpu": "max"}
DEFAULT_SETTINGS = {"context_length": 4096, "parallel": 4, "gpu": "max"}

# "Best settings" for a remote LM Studio on a big cloud GPU (e.g. Thunder A6000,
# 48GB): a large context with headroom under the model's max, vs LM Studio's
# small defaults. Applied over SSH since the forwarded /v1 port can't set these.
REMOTE_IDEAL_SETTINGS = {"context_length": 98304, "parallel": 2}
REMOTE_DEFAULT_SETTINGS = {"context_length": 4096, "parallel": 4}

# Port LM Studio's HTTP server listens on (and that the Thunder instance must
# forward) on a resolved Thunder target. Defaults to LM Studio's own default
# (1234, also llm_bench.py's --base-url default) and is overridable via the
# THUNDER_LMS_PORT env var. `tnr status --json` exposes no port-mapping field
# we can read, so deriving the forwarded port dynamically is deferred until it
# does - this constant keeps the single assumed value in one named place.
try:
    THUNDER_LMS_PORT = int(os.environ.get("THUNDER_LMS_PORT", "1234"))
except ValueError:
    # A malformed env value must not crash every importer of this module (app.py
    # imports it at startup). Name the offending setting and fall back.
    print(f"WARNING: invalid THUNDER_LMS_PORT={os.environ.get('THUNDER_LMS_PORT')!r}; using 1234")
    THUNDER_LMS_PORT = 1234

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")


def is_local_llm_endpoint(base_url):
    """Return True if base_url points at a local LM Studio (so the local `lms`
    CLI and the local GPU VRAM watchdog are meaningful). Returns False for a
    remote endpoint (e.g. LM Studio on a Thunder Compute instance), where the
    model is loaded/managed elsewhere and local `lms`/nvidia-smi calls are moot.
    """
    if not base_url:
        return True
    return (urlparse(base_url).hostname or "").lower() in _LOCAL_HOSTS


def is_remote_llm(llm_mode, base_url):
    """Single source of truth for "is this LLM endpoint remote?" - use this
    everywhere instead of checking is_local_llm_endpoint alone, which misses
    an explicit llm_mode="remote" pointed at a URL that happens to resolve as
    local (or vice versa after a save_config edge case where llm_mode and the
    active base_url have drifted out of sync).
    """
    return llm_mode == "remote" or not is_local_llm_endpoint(base_url)


def get_base_url_config_sections(is_remote):
    """Config sections that should receive a healed base_url: always
    'llm_remote', plus 'llm' when the active endpoint is remote. Single source
    so the three persist sites (review_script.py, find_nicknames.py, app.py's
    optimize route) can't drift on which sections to update.
    """
    return ["llm_remote", "llm"] if is_remote else ["llm_remote"]


def persist_healed_base_url(config, config_path, is_remote, base_url, target=None):
    """Write base_url into every config section that should track it (see
    get_base_url_config_sections) and atomically save config to config_path.

    The self-heal sites (review_script.py, find_nicknames.py, and app.py's
    optimize route) all need this identical write; keeping it here is the
    single source so they can't drift on which sections get updated or how the
    file is written. Mutates the passed-in config dict (the caller's own local
    copy) then writes it - a load/mutate/save of one local value, not a
    cross-function output parameter.

    `target`: when given (the resolved Thunder target dict for the heal that
    produced base_url), also records llm_remote.last_synced with the structured
    uuid/ip/ssh_port straight from the target - the single writer of that field,
    so no caller has to reverse-parse them out of the URL string. Omit it (None)
    to update base_url only (the literal-alias / verify-failed cases).
    """
    from datetime import datetime
    for section in get_base_url_config_sections(is_remote):
        config.setdefault(section, {})["base_url"] = base_url
    if target is not None:
        config.setdefault("llm_remote", {})["last_synced"] = {
            "uuid": target.get("uuid"),
            "ip": target.get("ip"),
            "ssh_port": target.get("ssh_port"),
            "base_url": base_url,
            "timestamp": datetime.now().isoformat(),
        }
    atomic_json_write(config, config_path)


def decide_healed_urls(ok, resolved_base_url, cached_base_url):
    """The single policy for what a (verify-ok?, freshly-resolved url) pair means,
    shared by app.py's optimize route and ensure_ideal_settings so the two can't
    drift (Rule 15). Returns HealUrls(persist_url, adopt_url):

    - persist_url: the url to write to config, or None to leave config alone.
      A freshly-resolved url that differs from the cached one is persisted even
      when verify failed - it's the new truth (the old instance/forward is gone),
      and the next run re-resolves/heals from it. None when nothing changed.
    - adopt_url: the url a live client should use THIS run. Only a verify-OK url
      is adopted; on failure the caller keeps the cached/old url rather than
      switching this run onto an endpoint that isn't reachable yet.
    """
    persist_url = resolved_base_url if (resolved_base_url and resolved_base_url != cached_base_url) else None
    # Adopt only a verify-OK url that actually resolved. A successful apply that
    # produced no url (every local heal, and the literal-alias remote heal) must
    # NOT switch the live client onto None — that silently redirects local runs to
    # the Ollama default and crashes remote runs in resolve_client_base_url.
    adopt_url = resolved_base_url if (ok and resolved_base_url) else cached_base_url
    return HealUrls(persist_url, adopt_url)


def resolve_client_base_url(base_url, is_remote):
    """Resolve the base_url an OpenAI-compatible client should connect to.
    Single source so the LLM subprocess scripts (find_nicknames.py,
    review_script.py, generate_script.py) can't drift on the empty-url policy.

    Raises ValueError when is_remote but no base_url resolved - a remote run with
    an empty url must fail loudly, NOT silently fall back to a local endpoint
    (that's how a remote job ends up hitting a local Ollama on :11434). Returns
    base_url or the local default only for the local case.
    """
    if is_remote and not base_url:
        raise ValueError("remote LLM selected but no base_url resolved "
                         "(refusing to fall back to a local endpoint)")
    return base_url or "http://localhost:11434/v1"


def find_lms_binary():
    """Return the path to the `lms` CLI, or None if it isn't available."""
    return shutil.which("lms")


def _validate_ssh_alias(ssh_alias):
    """Raise OSError if ssh_alias is empty or could be parsed by `ssh` as an
    option rather than a literal hostname (e.g. starts with '-'). Raises
    OSError (not ValueError) specifically because every SSH-driving caller in
    this module already catches OSError for connection failures.
    """
    if not ssh_alias or ssh_alias.startswith("-"):
        raise OSError(f"Invalid SSH host alias: {ssh_alias!r}")


_TNR_ALIAS_RE = re.compile(r"^tnr-(\d+)$")


def resolve_thunder_target(ssh_alias):
    """If ssh_alias looks like 'tnr-<id>' (the alias `tnr connect <id>`
    creates in ~/.ssh/config), resolve the CURRENT live connection info for
    that Thunder instance via `tnr status --json` instead of trusting the
    possibly-stale ~/.ssh/config entry it points at - that file only updates
    when `tnr connect` is run again, which always drops into an interactive
    shell (there's no non-interactive "just refresh the config" mode).

    Returns (target, error):
    - (dict, None) on success - dict has "instance_id", "uuid", "ip",
      "ssh_port", "http_port" (THUNDER_LMS_PORT - LM Studio's default server
      port unless overridden via env), and "key_path"
      (~/.thunder/keys/<uuid> - confirmed present for every instance ever
      created).
    - (None, None) if ssh_alias doesn't match the tnr-<id> pattern at all -
      caller should fall back to using ssh_alias as a literal SSH alias,
      completely unchanged from today's behavior (generic, non-Thunder
      remote endpoints are never affected by this function).
    - (None, (instance_id, detail)) if it DID look like a Thunder alias but
      resolution itself failed (tnr missing, bad JSON, instance not
      running) - detail is the raw diagnostic text for the caller to log.

    Never raises.
    """
    match = _TNR_ALIAS_RE.match(ssh_alias or "")
    if not match:
        return None, None
    instance_id = match.group(1)

    tnr_path = shutil.which("tnr")
    if not tnr_path:
        return None, (instance_id, "`tnr` CLI not found on PATH")

    try:
        result = subprocess.run([tnr_path, "status", "--json"],
                                 capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, (instance_id, f"`tnr status --json` failed to run: {e}")

    if result.returncode != 0:
        return None, (instance_id,
                       f"`tnr status --json` exited {result.returncode}\n"
                       f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    try:
        instances = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        return None, (instance_id,
                       f"could not parse `tnr status --json` output: {e}\n"
                       f"raw stdout:\n{result.stdout}")

    if not isinstance(instances, list):
        return None, (instance_id,
                       f"unexpected `tnr status --json` shape (expected a list, got "
                       f"{type(instances).__name__}):\n{result.stdout}")

    for inst in instances:
        if isinstance(inst, dict) and str(inst.get("id")) == instance_id:
            uuid = inst.get("uuid")
            return {
                "instance_id": instance_id,
                "uuid": uuid,
                "ip": inst.get("ip"),
                "ssh_port": inst.get("port"),
                "http_port": THUNDER_LMS_PORT,
                "key_path": os.path.expanduser(f"~/.thunder/keys/{uuid}"),
            }, None

    return None, (instance_id,
                  f"no running instance with id '{instance_id}' in "
                  f"`tnr status --json` output:\n{result.stdout}")


def _resolve_ssh_target(ssh_alias):
    """Best-effort, error-swallowing wrapper used by the read-only/status
    SSH helpers (get_remote_lmstudio_status, get_remote_gpu_name_and_backend):
    returns a Thunder target dict if ssh_alias resolves live, else ssh_alias
    unchanged (literal-alias fallback), collapsing a resolve failure into the
    same fallback as a non-Thunder alias. Never raises.

    NOT a single dispatch point for the whole module:
    apply_remote_lmstudio_settings deliberately calls resolve_thunder_target
    directly (not this) so it can surface the resolve error to the user
    instead of silently falling back to the literal alias. ensure_ideal_settings
    resolves once and threads the result into both kinds of helper.
    """
    target, _resolve_error = resolve_thunder_target(ssh_alias)
    return target if target is not None else ssh_alias


def _verify_remote_endpoint(base_url, model_name=None, timeout=10):
    """GET {base_url}/models to confirm the forwarded HTTPS endpoint is
    actually reachable - SSH/lms succeeding doesn't guarantee Thunder's port
    forward has finished propagating (this is the exact failure mode found
    live: SSH-driven reload succeeded while the public endpoint still showed
    "Nothing running here"). When model_name is given, also confirm it appears
    in the /models listing - a 200 with an empty/other model list means the
    forward is up but the model we just (re)loaded isn't being served yet, so
    a request against it would still fail. Returns (ok, detail). Never raises.
    """
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        if model_name:
            try:
                ids = {m.get("id") for m in (resp.json().get("data") or [])}
            except ValueError:
                return False, f"non-JSON /models response: {resp.text[:200]}"
            if model_name not in ids:
                return False, (f"endpoint reachable but model {model_name!r} not in "
                               f"served models {sorted(i for i in ids if i)}")
        return True, None
    except requests.RequestException as e:
        return False, str(e)


# The biggest real prompt the LLM workloads build is nickname discovery's
# co-occurrence chunk (observed ~14k tokens). A served context >= this proves the
# runtime didn't silently fall back to LM Studio's 4096 default - the failure mode
# behind the nickname n_keep>=n_ctx error - while staying well under the requested
# remote context (98304) so a correctly-loaded model never false-fails.
_REMOTE_VERIFY_NEED_TOKENS = 16384


def _configured_remote_hosts():
    """Hosts of the remote LM Studio endpoints the user has explicitly configured
    in config.json (llm_remote/llm base_url). Probing these is allowed even when
    they aren't Thunder — the SSRF guard only needs to block hosts the user never
    configured, not the user's own chosen endpoint."""
    hosts = set()
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return hosts
    if isinstance(cfg, dict):
        for section in ("llm_remote", "llm"):
            sec = cfg.get(section)
            if isinstance(sec, dict):
                host = (urlparse(sec.get("base_url") or "").hostname or "").lower()
                if host:
                    hosts.add(host)
    return hosts


def _is_thunder_host(host):
    """A Thunder Compute port-forward host (thundercompute.net / *.thundercompute.net).
    Single source for the Thunder-host check, shared with app.py (Rule 15)."""
    host = (host or "").lower()
    return host == "thundercompute.net" or host.endswith(".thundercompute.net")


def _is_verifiable_probe_host(base_url):
    """True only for hosts this app is allowed to send the context probe to:
    local LM Studio, a Thunder Compute port-forward (*.thundercompute.net), or a
    remote endpoint the user has explicitly configured in config.json. Gates
    _verify_served_context so a drifted/tampered base_url can't turn the probe
    into an SSRF against an arbitrary host the user never configured (the
    LLM-client path is already gated; this raw POST must be too)."""
    host = (urlparse(base_url or "").hostname or "").lower()
    if host in _LOCAL_HOSTS or _is_thunder_host(host):
        return True
    return host in _configured_remote_hosts()


def _verify_served_context(base_url, model_name, need_tokens=_REMOTE_VERIFY_NEED_TOKENS, timeout=60):
    """Confirm the model actually SERVES a context >= need_tokens by sending a
    need_tokens-sized probe through the public /v1 endpoint with max_tokens=1.

    `lms ps`'s reported contextLength has been observed to overstate the real
    n_ctx (a JIT/eviction reload at the 4096 default while the registry still
    advertised the configured value), so this checks the truth the way the
    workload will hit it instead of trusting the status flag. Returns
    (ok, detail). Never raises.
    """
    if not base_url:
        # A None/empty base_url would reach base_url.rstrip() below and raise
        # AttributeError (the ""-host also passes the local-host gate) — guard it
        # so the "Never raises" contract holds when llm_mode/base_url have drifted.
        return False, "no base_url configured"
    if not _is_verifiable_probe_host(base_url):
        return False, f"refusing to probe non-local/non-Thunder host: {base_url!r}"
    filler = "the harbor lights fog pier coat wind truth liar calm sky bell buoy water cold "
    approx_chars = int(need_tokens * 3.5)
    prompt = (filler * (approx_chars // len(filler) + 1))[:approx_chars]
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={"model": model_name,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 1, "temperature": 0},
            timeout=timeout)
        if resp.status_code == 200:
            return True, None
        body = resp.text[:300]
        m = re.search(r"n_ctx:\s*(\d+)", body)
        if m:
            return False, f"served context only {m.group(1)} tokens (need >= {need_tokens})"
        return False, f"HTTP {resp.status_code}: {body}"
    except requests.RequestException as e:
        return False, str(e)


def _validate_thunder_target(target):
    """Raise OSError if a resolved Thunder target dict is missing a required
    field - mirrors _validate_ssh_alias's contract (raises OSError, which
    every SSH-driving caller in this module already catches). uuid is required
    too: it builds both the key_path and the public thundercompute.net URL, so
    a missing one yields a broken endpoint rather than an honest failure."""
    if (not target.get("ip") or not target.get("ssh_port")
            or not target.get("key_path") or not target.get("uuid")):
        raise OSError(f"Incomplete Thunder SSH target: {target!r}")


def _ssh_run(ssh_target, remote_cmd, timeout, connect_timeout=10):
    """Run remote_cmd via `bash -lc`, returning the CompletedProcess.

    ssh_target is either a literal ~/.ssh/config alias (str, today's
    behavior, unchanged) or a resolved Thunder target dict from
    resolve_thunder_target/_resolve_ssh_target - in which case this connects
    directly with -i/-p, bypassing ~/.ssh/config entirely. That's what makes
    the Thunder path immune to a stale `tnr connect` cache.

    Shared by every remote helper below instead of each one re-building its
    own ssh argv list. Pre-quoting remote_cmd into a single argv element
    (rather than passing "bash", "-lc", remote_cmd as 3 separate argv
    elements) is required: ssh joins all args after the host with bare
    spaces and hands the result to the remote shell as one line, so 3 separate
    elements would let that join split remote_cmd's own `;`-separated
    statements apart, breaking any command after the first.
    """
    if isinstance(ssh_target, dict):
        _validate_thunder_target(ssh_target)
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={connect_timeout}",
                "-i", ssh_target["key_path"], "-p", str(ssh_target["ssh_port"]),
                f"ubuntu@{ssh_target['ip']}", "bash -lc " + shlex.quote(remote_cmd)]
    else:
        _validate_ssh_alias(ssh_target)
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
                ssh_target, "bash -lc " + shlex.quote(remote_cmd)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


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
        return {"available": True, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    for m in models:
        if m.get("identifier") == model_name or m.get("modelKey") == model_name:
            context_length = m.get("contextLength")
            parallel = m.get("parallel")
            optimized = (context_length == ideal_settings["context_length"]
                         and parallel == ideal_settings["parallel"])
            return {"available": True, "loaded": True, "context_length": context_length,
                    "parallel": parallel, "optimized": optimized}

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
    except (subprocess.TimeoutExpired, OSError):
        return {"available": True, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    return _parse_lms_ps_output(result.stdout, model_name, IDEAL_SETTINGS)


def get_remote_lmstudio_status(ssh_alias, model_name, timeout=20, resolved_target=None):
    """Like get_lmstudio_status, but for a remote LM Studio reached over SSH
    (ssh_alias e.g. "tnr-0", from config.json's llm_remote_ssh).

    Result dict: {available, loaded, context_length, parallel, optimized} -
    "optimized" compares against REMOTE_IDEAL_SETTINGS, mirroring
    get_lmstudio_status's local IDEAL_SETTINGS comparison. Best-effort: never
    raises, returns available=False on any SSH/parse failure or if ssh_alias
    isn't configured.

    `resolved_target`: an already-resolved target (the dict-or-alias that
    _resolve_ssh_target would return) when the caller resolved once and is
    threading it in (ensure_ideal_settings) - avoids re-running
    `tnr status --json` for every status check. Left None means resolve here.
    """
    if not ssh_alias:
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    ssh_target = resolved_target if resolved_target is not None else _resolve_ssh_target(ssh_alias)
    try:
        result = _ssh_run(ssh_target, "lms ps --json", timeout=timeout, connect_timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    if result.returncode != 0:
        # ssh exits non-zero (e.g. 255 on auth failure / connection refused) with
        # empty stdout, which _parse_lms_ps_output would misread as available=True.
        # Report unreachable so callers surface it instead of "loaded, not optimized".
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    return _parse_lms_ps_output(result.stdout, model_name, REMOTE_IDEAL_SETTINGS)


def _gpu_name_from_probes(run):
    """Shared logic for get_gpu_name_and_backend/get_remote_gpu_name_and_backend.

    `run` is a callable(argv_list) -> CompletedProcess-like object with
    .returncode/.stdout, so the same probe logic works for a local
    subprocess.run and an SSH-wrapped one.

    Takes the LAST non-empty output line, not the first: a remote login
    shell (bash -lc) prints a decorative banner ahead of any command's real
    output (confirmed live - 4 lines of box-drawing before the actual
    "NVIDIA RTX A6000"), so the real answer is always the most recent line,
    never the first. Harmless for the local (banner-free) case too, since
    there's only one line of real output there either way.
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
    """Return (gpu_name, backend) for the local machine, backend in
    ("cuda", "rocm", None). No torch dependency (same subprocess-based style
    as review_script.py's get_vram_usage) so this works even in environments
    without torch installed. Prefers rocm-smi's JSON output (more reliable
    than scraping --showproductname's text) before falling back to the
    shared text-scraping probe.
    """
    name_data = run_rocm_smi_json(["--showproductname"], rocm_smi_path="/opt/rocm/bin/rocm-smi", timeout=2)
    if name_data:
        for card_data in name_data.values():
            if isinstance(card_data, dict):
                name = card_data.get("Card Series") or card_data.get("Card Model")
                if name and name != "N/A":
                    return name, "rocm"

    return _gpu_name_from_probes(lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5))


def get_remote_gpu_name_and_backend(ssh_alias, resolved_target=None):
    """Same as get_gpu_name_and_backend, but probes a remote host over SSH.

    `resolved_target`: an already-resolved target (the dict-or-alias that
    _resolve_ssh_target returns) when the caller resolved once and is threading
    it in - same pattern as get_remote_lmstudio_status. Left None means resolve
    here, but only ONCE: _gpu_name_from_probes runs up to two probes
    (nvidia-smi, then rocm-smi), and resolving per probe would re-run
    `tnr status --json` for each. Resolve up front and reuse for every probe.
    """
    if not ssh_alias:
        return None, None

    ssh_target = resolved_target if resolved_target is not None else _resolve_ssh_target(ssh_alias)

    def _run(argv):
        remote_cmd = " ".join(shlex.quote(a) for a in argv)
        return _ssh_run(ssh_target, remote_cmd, timeout=15, connect_timeout=10)

    return _gpu_name_from_probes(_run)


def apply_lmstudio_settings(model_name, ideal=True, ttl=3600):
    """Reload model_name with either the VRAM-safe (ideal) or default settings.

    Best-effort: returns an ApplyResult; local mode never resolves a Thunder URL,
    log kind, or target, so those fields stay None (the result type is shared
    with apply_remote_lmstudio_settings so callers unpack both identically).
    Never raises.
    """
    lms = find_lms_binary()
    if not lms:
        return ApplyResult(False, "lms CLI not found on PATH")

    settings = IDEAL_SETTINGS if ideal else DEFAULT_SETTINGS

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
    except (OSError, subprocess.TimeoutExpired):
        # subprocess.run without check=True never raises CalledProcessError, and
        # FileNotFoundError is already an OSError — so only these two can occur.
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
    except (OSError, subprocess.TimeoutExpired) as e:
        return ApplyResult(False, f"Failed to run lms load: {e}")

    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "lms load failed"
        if unload_failed:
            msg += (" (unloading the previously-loaded model also failed - "
                    "it may still be running with different settings)")
        return ApplyResult(False, msg)

    label = "VRAM-safe" if ideal else "default"
    return ApplyResult(True, f"Reloaded {model_name} with {label} settings")


def apply_remote_lmstudio_settings(ssh_alias, model_name, ideal=True, resolved=None):
    """Reload model_name on a remote LM Studio host over SSH.

    If ssh_alias looks like 'tnr-<id>', resolves the CURRENT live Thunder
    instance via resolve_thunder_target and connects directly - this also
    starts the LM Studio server itself (--bind 0.0.0.0, required for
    Thunder's port-forward to see it; confirmed idempotent and
    self-correcting even if already running on the wrong bind) and verifies
    the public HTTPS endpoint is reachable afterward, since SSH succeeding
    doesn't guarantee that. Any other ssh_alias is used as a literal
    ~/.ssh/config alias exactly as before - no server-start, no resolution,
    no behavior change, so generic (non-Thunder) remote endpoints are
    unaffected.

    Returns an ApplyResult: base_url is the freshly computed Thunder URL
    whenever resolution succeeded (regardless of whether the rest of the call
    did), else None; log_kind names the diagnostic-log bucket of a failure
    ("thunder_resolve" / "optimize" / "optimize_verify", None on success);
    target is the resolved Thunder target dict (None for the literal-alias
    path) so the caller records last_synced without reverse-parsing the URL.

    Never raises. Unlike apply_lmstudio_settings, this intentionally does
    not pass `--ttl` - that's today's existing remote semantics, not an
    oversight.

    `resolved`: an already-fetched resolve_thunder_target() result
    (target, resolve_error) when the caller resolved once and is threading it
    in (ensure_ideal_settings) - avoids re-running `tnr status --json`. Left
    None (the user-facing optimize route) means resolve here, preserving that
    path's exact error handling.
    """
    settings = REMOTE_IDEAL_SETTINGS if ideal else REMOTE_DEFAULT_SETTINGS
    # model_name is shlex.quote()'d (not hand-wrapped in '...') since it comes
    # from user-editable config with no character restrictions - a bare
    # f"'{model_name}'" would let an embedded single quote break out of the
    # intended argument and inject additional shell commands once bash -lc
    # evaluates remote_cmd on the remote host.
    quoted_model = shlex.quote(model_name)

    target, resolve_error = resolve_thunder_target(ssh_alias) if resolved is None else resolved
    if target is None and resolve_error is not None:
        instance_id, detail = resolve_error
        return ApplyResult(False,
                           f"Could not resolve Thunder instance '{instance_id}': {detail.splitlines()[0]}",
                           log_kind="thunder_resolve")

    # Shared unload+reload tail. The Thunder branch additionally (re)starts the
    # server bound to 0.0.0.0 first, so Thunder's port-forward can see it.
    reload_cmd = (
        f"lms unload {quoted_model} >/dev/null 2>&1; "
        f"lms load {quoted_model} --context-length {settings['context_length']} "
        f"--parallel {settings['parallel']} --gpu max --identifier {quoted_model} -y"
    )
    if target is not None:
        ssh_target = target
        where = f"{target['ip']}:{target['ssh_port']} (uuid={target['uuid']})"
        remote_cmd = (
            f"lms server start --port {target['http_port']} --bind 0.0.0.0 --cors >/dev/null 2>&1; "
            + reload_cmd
        )
    else:
        ssh_target = ssh_alias
        where = ssh_alias
        remote_cmd = reload_cmd

    try:
        result = _ssh_run(ssh_target, remote_cmd, timeout=200, connect_timeout=15)
    except (subprocess.TimeoutExpired, OSError) as e:
        return ApplyResult(False, f"SSH to '{where}' failed: {e}", log_kind="optimize")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"ssh exited {result.returncode}"
        return ApplyResult(False, f"[{where}] {detail}", log_kind="optimize")

    label = f"best ({REMOTE_IDEAL_SETTINGS['context_length']} ctx)" if ideal else "default"

    if target is None:
        return ApplyResult(True, f"Reloaded {model_name} on '{ssh_alias}' with {label} settings")

    new_base_url = f"https://{target['uuid']}-{target['http_port']}.thundercompute.net/v1"
    verified, verify_detail = _verify_remote_endpoint(new_base_url, model_name=model_name)
    if not verified:
        return ApplyResult(False,
                           f"Reloaded {model_name} on Thunder instance '{target['instance_id']}' "
                           f"({target['uuid']}), but the public endpoint {new_base_url} is still "
                           f"unreachable: {verify_detail}",
                           base_url=new_base_url, log_kind="optimize_verify", target=target)

    # /models being reachable doesn't prove the model honors the context we just
    # asked it to load - lms ps has been seen to report the configured value while
    # the runtime silently fell back to 4096. Probe the real served context so an
    # apply only reports success when the settings actually took effect (fail loud).
    if ideal:
        ctx_ok, ctx_detail = _verify_served_context(new_base_url, model_name)
        if not ctx_ok:
            return ApplyResult(False,
                               f"Reloaded {model_name} on Thunder instance '{target['instance_id']}' "
                               f"({target['uuid']}), but it is not serving the requested context: "
                               f"{ctx_detail}. The runtime likely fell back to its default - "
                               f"try Optimize again.",
                               base_url=new_base_url, log_kind="optimize_verify", target=target)

    return ApplyResult(True,
                       f"Reloaded {model_name} on Thunder instance '{target['instance_id']}' with {label} settings",
                       base_url=new_base_url, target=target)


def get_current_status(llm_mode, base_url, model_name, ssh_alias=None):
    """Fetch live status (local or remote) with no self-heal side effect -
    just the is_remote_llm dispatch shared by ensure_ideal_settings's own
    status checks, the /api/lmstudio/status route, and any later
    re-verification that doesn't want to trigger a reload.
    """
    if is_remote_llm(llm_mode, base_url):
        return get_remote_lmstudio_status(ssh_alias, model_name)
    return get_lmstudio_status(model_name)


def ensure_ideal_settings(llm_mode, base_url, model_name, ssh_alias=None):
    """Self-heal LM Studio's load settings (local or remote) toward the ideal
    config (VRAM-safe locally, large-context remotely), then return the FRESH
    post-heal status so callers can size chunking/context decisions and the
    concurrency benchmark off live truth instead of each re-fetching it.

    Shared by review_script.py and find_nicknames.py instead of each
    hand-rolling its own copy of this branch.

    Returns (is_remote, status, message, heal). status always has the
    {available, loaded, context_length, parallel, optimized} shape. `heal` is a
    HealOutcome(adopt_url, persist_url, target):
    - adopt_url: the URL this run's live client should use - the input base_url
      unless a Thunder reload verified end-to-end and produced a different URL
      (never silently substitutes a not-yet-reachable URL for a working one).
    - persist_url: the URL to write to config (None when unchanged); follows the
      shared decide_healed_urls keep-persist policy so callers persist
      consistently with the optimize route.
    - target: the resolved Thunder target dict to record in last_synced on a
      successful heal, else None. Never raises - every call it makes is itself
      best-effort/non-raising.
    """
    is_remote = is_remote_llm(llm_mode, base_url)

    if is_remote and not ssh_alias:
        return (True, get_remote_lmstudio_status(None, model_name),
                "Remote LLM endpoint - no SSH alias configured, cannot verify/apply ideal settings.",
                HealOutcome(base_url, None, None))

    if is_remote:
        # Resolve the Thunder instance ONCE here and thread the result through
        # both the status checks and the apply, instead of each re-running
        # `tnr status --json` (this branch otherwise resolves 3x per run-start:
        # two get_status calls + one apply). status_target mirrors
        # _resolve_ssh_target's "target dict if live, else literal alias"; the
        # apply gets the full (target, resolve_error) tuple so it keeps its
        # exact thunder_resolve error reporting.
        resolved = resolve_thunder_target(ssh_alias)
        status_target = resolved[0] if resolved[0] is not None else ssh_alias
        get_status = lambda: get_remote_lmstudio_status(ssh_alias, model_name, resolved_target=status_target)
        apply_settings = lambda: apply_remote_lmstudio_settings(ssh_alias, model_name, ideal=True, resolved=resolved)
        label = "Remote LM Studio"
        ok_warning = ("Proceeding with whatever is currently loaded, which may truncate "
                      "responses or fail outright if the context is too small.")
    else:
        get_status = lambda: get_current_status(llm_mode, base_url, model_name, ssh_alias)
        apply_settings = lambda: apply_lmstudio_settings(model_name, ideal=True)
        label = "LM Studio"
        ok_warning = ("The model may be running with a higher 'parallel'/context-length "
                      "configuration, which uses more VRAM per request and increases the "
                      "risk of an out-of-memory crash. The VRAM watchdog below will still "
                      "pause batches if usage gets too high, but if you hit OOM, restart "
                      "LM Studio and re-run.")

    status = get_status()
    already_ideal = status["loaded"] and status["optimized"]
    if is_remote and already_ideal:
        # lms ps's "optimized" has been observed to lie (it reported context=98304
        # while the runtime actually served n_ctx=4096 after a JIT/eviction reload),
        # which let the corrective reload be skipped and broke nickname discovery.
        # Confirm the served context for real before trusting it.
        ctx_ok, _ = _verify_served_context(base_url, model_name)
        already_ideal = ctx_ok
    if already_ideal:
        return (is_remote, status,
                f"{label}: {model_name} already loaded with ideal settings (context verified).",
                HealOutcome(base_url, None, None))

    res = apply_settings()
    # decide_healed_urls is the single shared policy (Rule 15), also used by the
    # optimize route: persist_url is what to write to config (the new truth even
    # if verify failed - the next run re-resolves from it), adopt_url is what
    # THIS run's live client uses (only a verify-OK URL; on failure we keep the
    # prior base_url rather than switch onto a not-yet-reachable endpoint).
    # target is recorded in last_synced only on a successful heal.
    persist_url, adopt_url = decide_healed_urls(res.ok, res.base_url, base_url)
    heal = HealOutcome(adopt_url, persist_url, res.target if res.ok else None)
    status = get_status()
    if res.ok:
        return is_remote, status, f"{label}: {res.message}", heal
    # `lms ps`'s "optimized" flag has been observed to lie (it advertised the
    # configured context while the runtime served n_ctx=4096 after a JIT reload),
    # so on remote confirm the served context for real before continuing — the
    # same guard the already-ideal path above uses.
    ctx_ok = (not is_remote) or _verify_served_context(base_url, model_name)[0]
    if status["loaded"] and status["optimized"] and ctx_ok:
        return (is_remote, status,
                f"{label}: could not reload ({res.message}), but {model_name} is "
                f"already loaded with ideal settings - continuing.",
                heal)
    return (is_remote, status,
            f"{label}: WARNING - could not apply ideal settings ({res.message}). {ok_warning}",
            heal)
