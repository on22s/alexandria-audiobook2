"""Helpers for applying VRAM-safe LM Studio load settings via the `lms` CLI.

Background: a long batch_review run keeps the LLM loaded with a large KV
cache. Loading the model with a high `parallel` value multiplies that KV
cache and can exhaust GPU VRAM, crashing the display server (see the
VRAM watchdog in review_script.py for the runtime safety net). These
helpers let both the CLI script and the web UI force/inspect the
VRAM-safe load configuration (context 8192, parallel 1, full GPU offload).
"""

import json
import shutil
import subprocess

IDEAL_SETTINGS = {"context_length": 8192, "parallel": 1, "gpu": "max"}
DEFAULT_SETTINGS = {"context_length": 4096, "parallel": 4, "gpu": "max"}


def find_lms_binary():
    """Return the path to the `lms` CLI, or None if it isn't available."""
    return shutil.which("lms")


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
        models = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {"available": True, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    if not isinstance(models, list):
        return {"available": True, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False}

    for m in models:
        if m.get("identifier") == model_name or m.get("modelKey") == model_name:
            context_length = m.get("contextLength")
            parallel = m.get("parallel")
            optimized = (context_length == IDEAL_SETTINGS["context_length"]
                          and parallel == IDEAL_SETTINGS["parallel"])
            return {"available": True, "loaded": True,
                    "context_length": context_length, "parallel": parallel,
                    "optimized": optimized}

    return {"available": True, "loaded": False, "context_length": None,
            "parallel": None, "optimized": False}


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
