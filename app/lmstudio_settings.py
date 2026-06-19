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
import shlex
import shutil
import subprocess
from urllib.parse import urlparse

from utils import run_rocm_smi_json

IDEAL_SETTINGS = {"context_length": 8192, "parallel": 1, "gpu": "max"}
DEFAULT_SETTINGS = {"context_length": 4096, "parallel": 4, "gpu": "max"}

# "Best settings" for a remote LM Studio on a big cloud GPU (e.g. Thunder A6000,
# 48GB): a large context with headroom under the model's max, vs LM Studio's
# small defaults. Applied over SSH since the forwarded /v1 port can't set these.
REMOTE_IDEAL_SETTINGS = {"context_length": 98304, "parallel": 2}
REMOTE_DEFAULT_SETTINGS = {"context_length": 4096, "parallel": 4}

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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return {"available": True, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    return _parse_lms_ps_output(result.stdout, model_name, IDEAL_SETTINGS)


def get_remote_lmstudio_status(ssh_alias, model_name, timeout=20):
    """Like get_lmstudio_status, but for a remote LM Studio reached over SSH
    (ssh_alias e.g. "tnr-0", from config.json's llm_remote_ssh).

    Result dict: {available, loaded, context_length, parallel, optimized} -
    "optimized" compares against REMOTE_IDEAL_SETTINGS, mirroring
    get_lmstudio_status's local IDEAL_SETTINGS comparison. Best-effort: never
    raises, returns available=False on any SSH/parse failure or if ssh_alias
    isn't configured.
    """
    if not ssh_alias:
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    try:
        result = _ssh_run(ssh_alias, "lms ps --json", timeout=timeout, connect_timeout=10)
    except (subprocess.TimeoutExpired, OSError):
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
    return True, f"Reloaded {model_name} with {label} settings"


def apply_remote_lmstudio_settings(ssh_alias, model_name, ideal=True):
    """Reload model_name on a remote LM Studio host via SSH (`lms` over `tnr-N`).

    Returns (success, message). The forwarded OpenAI /v1 port can't change load
    settings, so we drive the remote `lms` CLI directly. Never raises. Unlike
    apply_lmstudio_settings, this intentionally does not pass `--ttl` - that's
    today's existing remote semantics, not an oversight.
    """
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
    return True, f"Reloaded {model_name} on '{ssh_alias}' with {label} settings"


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

    Returns (is_remote, status, message). status always has the
    {available, loaded, context_length, parallel, optimized} shape. Never
    raises - every call it makes is itself best-effort/non-raising.
    """
    is_remote = is_remote_llm(llm_mode, base_url)

    if is_remote and not ssh_alias:
        return (True, get_remote_lmstudio_status(None, model_name),
                "Remote LLM endpoint - no SSH alias configured, cannot verify/apply ideal settings.")

    if is_remote:
        get_status = lambda: get_current_status(llm_mode, base_url, model_name, ssh_alias)
        apply_settings = lambda: apply_remote_lmstudio_settings(ssh_alias, model_name, ideal=True)
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
    if status["loaded"] and status["optimized"]:
        return is_remote, status, f"{label}: {model_name} already loaded with ideal settings."

    ok, msg = apply_settings()
    status = get_status()
    if ok:
        return is_remote, status, f"{label}: {msg}"
    if status["loaded"] and status["optimized"]:
        return (is_remote, status,
                f"{label}: could not reload ({msg}), but {model_name} is "
                f"already loaded with ideal settings - continuing.")
    return is_remote, status, f"{label}: WARNING - could not apply ideal settings ({msg}). {ok_warning}"
