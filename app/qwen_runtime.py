"""Qwen3-ForcedAligner runtime discovery and owned worker runs (#1844).

The Qwen stack lives in a separate interpreter (qwen-env/) because it
needs transformers 4.57.6 while the main app pins 4.57.3. Everything
here is lazy: this module never imports torch or qwen_asr. Capability
probing runs a bounded owned subprocess that only reports import
presence and CUDA devices — it never loads the model, never performs
inference, and never downloads anything (unknown stays not-ready).
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from subprocess_ownership import (run_owned_capture, start_owned_subprocess,
                                  stop_owned_subprocess)

MODEL_NAME = "Qwen3-ForcedAligner-0.6B"
DEFAULT_PROBE_TIMEOUT_SECONDS = 120
DEFAULT_RUN_TIMEOUT_SECONDS = 7200
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_JOB_BYTES = 64 * 1024 * 1024

_PROBE_SOURCE = (
    "import json, sys\n"
    "out = {'ready': False, 'torch': False, 'qwen_asr': False, "
    "'qwen_asr_version': None, 'torch_version': None, 'devices': [], "
    "'cpu_fallback': False, 'reason': None}\n"
    "# qwen_asr presence first: importlib.util.find_spec never imports torch.\n"
    "import importlib.util\n"
    "if not importlib.util.find_spec('qwen_asr'):\n"
    "    out['reason'] = 'missing module: qwen_asr (install it in the qwen-env interpreter)'\n"
    "    print(json.dumps(out)); sys.exit(0)\n"
    "try:\n"
    "    from importlib import metadata as _metadata\n"
    "except Exception:\n"
    "    out['reason'] = 'unknown Python package metadata support'\n"
    "    print(json.dumps(out)); sys.exit(0)\n"
    "try:\n"
    "    out['qwen_asr_version'] = _metadata.version('qwen-asr')\n"
    "except Exception:\n"
    "    out['reason'] = 'unknown qwen-asr package metadata; refusing to identify it'\n"
    "    print(json.dumps(out)); sys.exit(0)\n"
    "if not out['qwen_asr_version'] or out['qwen_asr_version'] == 'unknown':\n"
    "    out['reason'] = 'unknown qwen-asr package metadata; refusing to identify it'\n"
    "    print(json.dumps(out)); sys.exit(0)\n"
    "try:\n"
    "    import torch\n"
    "except Exception as exc:\n"
    "    out['reason'] = 'missing module: %s' % getattr(exc, 'name', exc)\n"
    "    print(json.dumps(out)); sys.exit(0)\n"
    "out['torch'] = True\n"
    "try:\n"
    "    out['torch_version'] = _metadata.version('torch')\n"
    "except Exception:\n"
    "    out['torch_version'] = None\n"
    "out['qwen_asr'] = True\n"
    "out['cpu_fallback'] = True\n"
    "try:\n"
    "    if torch.cuda.is_available():\n"
    "        for i in range(torch.cuda.device_count()):\n"
    "            major, minor = torch.cuda.get_device_capability(i)\n"
    "            try:\n"
    "                free, total = torch.cuda.mem_get_info(i)\n"
    "            except Exception:\n"
    "                free, total = 0, 0\n"
    "            out['devices'].append({'device': 'cuda:%d' % i,\n"
    "                'name': torch.cuda.get_device_name(i),\n"
    "                'capability': [major, minor],\n"
    "                'free_bytes': int(free or 0),\n"
    "                'total_bytes': int(total or 0)})\n"
    "except Exception:\n"
    "    pass\n"
    "out['ready'] = True\n"
    "print(json.dumps(out))\n"
)

# A complete local install. Everything the worker's from_pretrained +
# processor will touch: config (model type gate), tokenizer, processor,
# weights. The inventory check is file-presence + bounds only — no
# inference and no network.
_REQUIRED_MODEL_FILES = (
    "config.json", "generation_config.json", "tokenizer_config.json",
    "vocab.json", "merges.txt", "preprocessor_config.json",
    "chat_template.json",
)
_MAX_MODEL_FILES = 2048
_MAX_MODEL_TOTAL_BYTES = 8 * 1024**3
_MAX_MODEL_FILE_BYTES = 8 * 1024**3
_MIN_MODEL_FILE_BYTES = 1
EXPECTED_MODEL_TYPE = "qwen3_asr"


def get_qwen_python_candidates(root):
    """Interpreter candidates for the Qwen stack, best first."""
    candidates = []
    env_python = os.environ.get("ALEXANDRIA_QWEN_PYTHON")
    if env_python:
        candidates.append(env_python)
    else:
        base = Path(root) / "qwen-env"
        if os.name == "nt":
            candidates.append(str(base / "Scripts" / "python.exe"))
        else:
            candidates.append(str(base / "bin" / "python"))
        candidates.append(sys.executable)
    return [c for c in candidates if isinstance(c, str) and c and os.path.isfile(c)]


def get_qwen_model_candidates(root):
    """Local model directory candidates; no hub ids are resolved here."""
    candidates = []
    env_model = os.environ.get("ALEXANDRIA_QWEN_MODEL")
    if env_model:
        candidates.append(env_model)
    else:
        candidates.append(str(Path(root) / "models" / MODEL_NAME))
    return [c for c in candidates if isinstance(c, str) and c and os.path.isdir(c)]


def get_qwen_model_digest(model_dir):
    """Full streamed file digest (weights included) of a model directory."""
    return get_qwen_model_file_digest(model_dir)


def get_qwen_model_file_digest(model_dir, cancel_check=None):
    """Cancel-aware streamed digest over every file in the model dir.

    Weights gain by size; tiny config-only directories cannot collide
    with a replaced model. Bounded in files and total bytes; unknown
    errors never fall back to a matching digest.
    """
    if not isinstance(model_dir, str) or not os.path.isdir(model_dir):
        raise ValueError("Qwen model directory is unavailable; refusing to identify it.")
    root = Path(model_dir)
    files = []
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        if cancel_check is not None and cancel_check():
            raise ValueError("Provider identity capture cancelled.")
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            if name.startswith((".", "_")):
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                raise ValueError("Qwen model directory contains a symlink; refusing to identify it.")
            if not full.is_file():
                continue
            rel = os.path.join(rel_dir, name) if rel_dir != "." else name
            size = full.stat().st_size
            if size < _MIN_MODEL_FILE_BYTES or size > _MAX_MODEL_FILE_BYTES:
                raise ValueError("Qwen model file %s exceeds its bounds." % rel)
            total += size
            if total > _MAX_MODEL_TOTAL_BYTES:
                raise ValueError("Qwen model directory exceeds its digest bound.")
            files.append((rel.replace(os.sep, "/"), int(size)))
    if len(files) > _MAX_MODEL_FILES:
        raise ValueError("Qwen model directory has too many files.")
    if not files:
        raise ValueError("Qwen model directory holds no files.")
    files.sort()
    digest = hashlib.sha256()
    for rel, size in files:
        if cancel_check is not None and cancel_check():
            raise ValueError("Provider identity capture cancelled.")
        digest.update(rel.encode("utf-8"))
        digest.update(str(size).encode("ascii"))
        digest.update(b"\x00")
        remaining = size
        with (root / rel).open("rb") as stream:
            while remaining > 0:
                if cancel_check is not None and cancel_check():
                    raise ValueError("Provider identity capture cancelled.")
                block = stream.read(min(1024**2, remaining))
                if not block:
                    raise ValueError("Qwen model file %s shrank during identity capture." % rel)
                digest.update(block)
                remaining -= len(block)
    return digest.hexdigest()


def get_qwen_provider_digest(model_dir, runtime_identity=None, cancel_check=None):
    """Bounded provider identity: model files + interpreter/runtime.

    Captured once at admission; the caller recomputes the same way to
    revalidate before the worker starts, before publication, and at
    download time. No caching; mtime plays no role.
    """
    inner = get_qwen_model_file_digest(model_dir, cancel_check=cancel_check)
    if not isinstance(runtime_identity, dict):
        raise ValueError("Runtime identity is required for the provider digest.")
    version = runtime_identity.get("qwen_asr_version")
    torch_version = runtime_identity.get("torch_version")
    device = runtime_identity.get("device")
    dtype = runtime_identity.get("dtype")
    if not isinstance(version, str) or version in ("", "unknown"):
        raise ValueError("qwen-asr version metadata is unknown; refusing provider identity.")
    outer = hashlib.sha256()
    outer.update(inner.encode("ascii"))
    outer.update(str(version).encode("utf-8"))
    outer.update(str(torch_version or "unknown-torch").encode("utf-8"))
    outer.update(str(device).encode("utf-8"))
    outer.update(str(dtype).encode("utf-8"))
    identity = runtime_identity.get("identity") if isinstance(runtime_identity, dict) else None
    if isinstance(identity, dict) and isinstance(identity.get("runtime_digest"), str):
        outer.update(identity["runtime_digest"].encode("ascii"))
    elif isinstance(identity, dict):
        body = {"python": identity.get("python"),
                "python_version": identity.get("python_version"),
                "executable_sha256": identity.get("executable_sha256"),
                "executable_bytes": identity.get("executable_bytes"),
                "packages": identity.get("packages") or {}}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False)
        outer.update(canonical.encode("utf-8"))
    else:
        raise ValueError("Qwen runtime identity is required for the provider digest.")
    return outer.hexdigest()


def get_model_issues(model_dir):
    """Completeness issues for a local model directory (R1 gate)."""
    if not isinstance(model_dir, str) or not os.path.isdir(model_dir):
        return ["Qwen model directory is unavailable."]
    issues = []
    root = Path(model_dir)
    missing = [name for name in _REQUIRED_MODEL_FILES if not (root / name).is_file()]
    for name in missing:
        issues.append("missing model file: %s" % name)
    weights = [p for p in root.glob("model*.safetensors")]
    if not weights:
        issues.append("missing model weights (model.safetensors)")
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = None
        issues.append("model config.json is unreadable.")
    if isinstance(config, dict):
        if config.get("model_type") != EXPECTED_MODEL_TYPE:
            issues.append("model config model_type is not %r." % EXPECTED_MODEL_TYPE)
        languages = config.get("support_languages")
        if not isinstance(languages, list) or not languages:
            issues.append("model config lists no support languages.")
        else:
            from qwen_align import SUPPORTED_ALIGNER_LANGUAGES
            supported = set(SUPPORTED_ALIGNER_LANGUAGES)
            known = [lang for lang in languages if lang in supported]
            if not known:
                issues.append("model config languages are unsupported by the forced aligner.")
        return issues, languages
    return issues, None


def get_qwen_capability(root, cancel_check=None, timeout=DEFAULT_PROBE_TIMEOUT_SECONDS,
                        probe_command=None, runtime_identity=None):
    """Bounded capability: complete model + interpreter + devices, never inference.

    Returns stop-and-report info only. Any failure (incomplete model,
    missing interpreter, import error, timeout, malformed probe output)
    leaves ``available`` False with a human ``reason``.
    """
    if cancel_check is not None and cancel_check():
        return {"available": False, "reason": "Capability probe cancelled.",
                "python": None, "model_dir": None, "devices": [],
                "languages": [], "qwen_asr_version": None,
                "cpu_fallback": False, "provider_digest": None}
    pythons = get_qwen_python_candidates(root)
    models = get_qwen_model_candidates(root)
    if not pythons:
        return _not_ready("No Qwen interpreter found. Install it per "
                          "docs/readalong.md (qwen-env + requirements-qwen.txt) "
                          "or set ALEXANDRIA_QWEN_PYTHON.", None, models[0] if models else None)
    if not models:
        return _not_ready("Qwen model is not provisioned locally. Download it once "
                          "with the hf CLI (see docs/readalong.md) or set "
                          "ALEXANDRIA_QWEN_MODEL.", pythons[0], None)
    python, model_dir = pythons[0], models[0]
    issues, languages = get_model_issues(model_dir)
    if issues:
        return _not_ready("Qwen model installation is incomplete: %s. Download the "
                          "model once with the hf CLI (see docs/readalong.md); "
                          "config-only directories are not a model."
                          % "; ".join(issues[:6]), python, model_dir)
    command = probe_command if probe_command is not None else [python, "-c", _PROBE_SOURCE]
    try:
        completed = run_owned_capture(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _not_ready("Qwen capability probe timed out.", python, model_dir)
    except (OSError, ValueError) as error:
        return _not_ready("Qwen capability probe failed: %s" % error, python, model_dir)
    if completed.returncode != 0:
        return _not_ready("Qwen capability probe failed with exit code %s."
                          % completed.returncode, python, model_dir)
    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _not_ready("Qwen capability probe returned malformed output.", python, model_dir)
    if not isinstance(payload, dict) or payload.get("ready") is not True:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        return _not_ready("Qwen runtime not ready: %s" % (reason or "unknown reason."),
                          python, model_dir)
    version = payload.get("qwen_asr_version")
    if not isinstance(version, str) or version in ("", "unknown"):
        return _not_ready("Qwen runtime not ready: unknown qwen-asr package metadata.", python, model_dir)
    devices = payload.get("devices")
    if not isinstance(devices, list):
        return _not_ready("Qwen capability probe returned malformed devices.", python, model_dir)
    normalized = []
    for device in devices:
        if isinstance(device, str):
            name = device.upper()
            if not name.startswith("CUDA:") or not name[5:].isdigit():
                return _not_ready("Qwen capability probe returned an unknown device: %r" % device,
                                  python, model_dir)
            normalized.append(device)
        elif isinstance(device, dict) and isinstance(device.get("device"), str) \
                and device["device"].upper().startswith("CUDA:"):
            entry = dict(device)
            if not isinstance(entry.get("capability"), list) or len(entry["capability"]) != 2:
                entry["capability"] = [0, 0]
            normalized.append(entry)
        else:
            return _not_ready("Qwen capability probe returned an unknown device: %r" % device,
                              python, model_dir)
    try:
        device_choice, dtype = get_qwen_device_selection(normalized)
    except ValueError as error:
        return _not_ready(str(error), python, model_dir)
    identity = get_qwen_runtime_identity(python)
    provider = get_qwen_provider_digest(
        model_dir,
        runtime_identity={"qwen_asr_version": version,
                          "torch_version": payload.get("torch_version"),
                          "device": device_choice, "dtype": dtype,
                          "identity": identity})
    supported = set(languages) if isinstance(languages, list) else set()
    from qwen_align import SUPPORTED_ALIGNER_LANGUAGES
    ordered = [lang for lang in SUPPORTED_ALIGNER_LANGUAGES if lang in supported]
    if not ordered:
        ordered = [lang for lang in (languages or []) if isinstance(lang, str) and lang]
    return {"available": True, "python": python, "model_dir": model_dir,
            "devices": normalized, "device": device_choice, "dtype": dtype,
            "languages": ordered, "qwen_asr_version": version,
            "torch_version": payload.get("torch_version"),
            "cpu_fallback": bool(payload.get("cpu_fallback")),
            "provider_digest": provider, "reason": None}


def _not_ready(reason, python, model_dir):
    return {"available": False, "python": python, "model_dir": model_dir,
            "devices": [], "languages": [], "qwen_asr_version": None,
            "cpu_fallback": False, "provider_digest": None, "reason": reason}


MIN_FREE_VRAM_BYTES = int(2.5 * 1024**3)

_RUNTIME_PACKAGES = ("qwen-asr", "torch", "transformers")

_IDENTITY_SOURCE = (
    "import json, sys\n"
    "out = {'python_version': sys.version.split()[0], 'packages': {}}\n"
    "try:\n"
    "    from importlib import metadata as _metadata\n"
    "except Exception:\n"
    "    print(json.dumps(out)); sys.exit(0)\n"
    "for name in ('qwen-asr', 'torch', 'transformers'):\n"
    "    try:\n"
    "        out['packages'][name] = _metadata.version(name)\n"
    "    except Exception:\n"
    "        out['packages'][name] = None\n"
    "print(json.dumps(out))\n"
)


def get_qwen_runtime_identity(python_path, cancel_check=None, timeout=60):
    """Live selected-runtime identity for a specific interpreter.

    Runs a stdlib-only identity query with the selected interpreter
    (importlib.metadata + sys.version; never torch, never inference)
    and streams a digest of the interpreter file itself. Does not look
    at the SDK-level capability GPU probe.
    """
    if cancel_check is not None and cancel_check():
        raise ValueError("Qwen runtime identity capture cancelled.")
    if not isinstance(python_path, str) or not python_path or not os.path.isfile(python_path):
        raise ValueError("Qwen interpreter is unavailable; refusing to identify it.")
    canonical = str(Path(python_path).resolve())
    if canonical != python_path:
        path_effective = canonical
    else:
        path_effective = python_path
    exe = Path(canonical)
    if not exe.is_file():
        raise ValueError("Qwen interpreter is unavailable; refusing to identify it.")
    try:
        completed = run_owned_capture(
            [canonical, "-c", _IDENTITY_SOURCE], timeout=timeout)
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise ValueError("Qwen runtime identity capture failed: %s" % error) from error
    if completed.returncode != 0:
        raise ValueError("Qwen runtime identity capture failed with exit code %s."
                         % completed.returncode)
    try:
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Qwen runtime identity capture returned malformed output.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("python_version"), str):
        raise ValueError("Qwen runtime identity capture returned malformed output.")
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("Qwen runtime identity capture returned malformed packages.")
    normalized = {_runtime_package_name(key): (value if isinstance(value, str) else None)
                  for key, value in packages.items()}
    exe_sha256 = hashlib.sha256()
    total = 0
    with exe.open("rb") as stream:
        while True:
            if cancel_check is not None and cancel_check():
                raise ValueError("Qwen runtime identity capture cancelled.")
            block = stream.read(1024**2)
            if not block:
                break
            exe_sha256.update(block)
            total += len(block)
    if total <= 0:
        raise ValueError("Qwen interpreter is empty; refusing to identify it.")
    identity = {"python": canonical, "python_prefix": os.path.dirname(canonical),
                "python_version": payload["python_version"],
                "executable_sha256": exe_sha256.hexdigest(),
                "executable_bytes": int(total),
                "packages": normalized}
    identity["runtime_digest"] = get_qwen_runtime_digest(identity)
    return identity


def _runtime_package_name(key):
    return str(key).lower()


def get_qwen_runtime_digest(identity):
    """Canonical digest of a captured runtime identity."""
    if not isinstance(identity, dict):
        raise ValueError("Qwen runtime identity is unavailable.")
    body = {
        "python": identity.get("python"),
        "python_version": identity.get("python_version"),
        "executable_sha256": identity.get("executable_sha256"),
        "executable_bytes": identity.get("executable_bytes"),
        "packages": dict(sorted((identity.get("packages") or {}).items())),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_qwen_device_selection(devices):
    """Choose (device, dtype) from normalized probe devices.

    Donor policy (ReadAloudMaker, adapted): a device needs 2.5 GB of
    free VRAM; the most capable device wins; BF16 needs capability
    >= 8 (Ampere), older cards use FP16, CPU always FP32. No free-VRAM
    guess is ever treated as free==total for dict entries: unknown free
    memory is ineligible in auto selection. String entries are UNKNOWN
    metadata (our own legacy fixture, not a real probe): ineligible.
    """
    candidates = []
    for device in devices or []:
        if isinstance(device, dict) and isinstance(device.get("device"), str):
            entry = dict(device)
            entry.setdefault("capability", [0, 0])
            entry.setdefault("free_bytes", 0)
            entry.setdefault("total_bytes", 0)
            candidates.append(entry)
        # String entries (or malformed metadata) are unknown probe data, not
        # a ready GPU claim: ineligible for auto-selection.
    usable = [c for c in candidates
              if c.get("free_bytes", 0) and c["free_bytes"] >= MIN_FREE_VRAM_BYTES]
    if not usable:
        return "cpu", "float32"
    best = max(usable, key=lambda c: (c["capability"][0], c["capability"][1],
                                      c["free_bytes"], c["total_bytes"]))
    dtype = "bfloat16" if best["capability"][0] >= 8 else "float16"
    return best["device"], dtype


def get_qwen_device_policy(mode, device, capability):
    """Single dispatch for timing mode + device (Rule 15).

    Pure: never probes, never loads a model. The caller pins the result
    before admission so a worker cannot later upgrade CPU to GPU.
    """
    if mode not in ("auto", "qwen", "chunk"):
        raise ValueError("Qwen timing mode must be auto, qwen or chunk.")
    if not isinstance(device, str) or not device.strip():
        raise ValueError("Qwen device must be auto, cpu or cuda:N.")
    device = device.strip()
    if mode == "chunk":
        return {"timing_mode": "chunk", "device": "cpu", "cpu_only": True,
                "reason": "Chunk timing selected; low-resource CPU path."}
    if not isinstance(capability, dict) or not capability.get("available"):
        if mode == "auto":
            return {"timing_mode": "chunk", "device": "cpu", "cpu_only": True,
                    "reason": "Qwen not ready (%s); using chunk timing."
                              % ((capability or {}).get("reason") or "unknown reason")}
        raise ValueError(
            "Qwen alignment is not ready: %s Install the isolated qwen-env and "
            "provision the model per docs/readalong.md, or choose chunk timing."
            % ((capability or {}).get("reason") or "unknown reason"))
    devices = capability.get("devices") or []
    device_names = [d.get("device") or "" for d in devices
                    if isinstance(d, dict)]
    if mode == "auto":
        chosen, dtype = get_qwen_device_selection(devices)
        return {"timing_mode": "qwen", "device": chosen, "dtype": dtype,
                "cpu_only": chosen == "cpu",
                "reason": ("Qwen ready; using %s (%s)." % (chosen, dtype))}
    if device == "cpu":
        if not capability.get("cpu_fallback"):
            raise ValueError("Qwen CPU fallback is unavailable in this runtime.")
        return {"timing_mode": "qwen", "device": "cpu", "dtype": "float32",
                "cpu_only": True, "reason": "Qwen CPU selected."}
    if device == "auto":
        chosen, dtype = get_qwen_device_selection(devices)
        return {"timing_mode": "qwen", "device": chosen, "dtype": dtype,
                "cpu_only": chosen == "cpu",
                "reason": "Qwen ready; using %s (%s)." % (chosen, dtype)}
    if device in device_names:
        chosen, dtype = get_qwen_device_selection(
            [d for d in devices if isinstance(d, dict) and d.get("device") == device])
        if chosen != device:
            raise ValueError(
                "Device %r does not report enough free VRAM (%d GB free required). "
                "Free memory or choose CPU."
                % (device, MIN_FREE_VRAM_BYTES // 1024**3))
        return {"timing_mode": "qwen", "device": device, "dtype": dtype,
                "cpu_only": False, "reason": "Qwen GPU selected."}
    raise ValueError(
        "Device %r is unavailable here; real devices: %s"
        % (device, ", ".join(d for d in device_names if d) or "none (CPU only)"))


def get_qwen_worker_command(python):
    """Owned worker command in the Qwen interpreter."""
    worker = Path(__file__).resolve().parent / "qwen_align_worker.py"
    return [python, str(worker)]


def run_qwen_alignment(command, job_path, result_path, cancel_check,
                       timeout=DEFAULT_RUN_TIMEOUT_SECONDS,
                       max_result_bytes=MAX_RESULT_BYTES, task_lease_fd=None,
                       worker_env=None):
    """Run the Qwen worker as an owned subprocess. Returns tokens by row id.

    Mirrors word_timing.run_timing_command: cancel is polled every
    0.1s, the child is terminated and reaped by the owner on
    cancel/timeout, and the worker's own error text is preserved.
    The child environment forces offline (HF_HUB_OFFLINE=1) so a build
    can never fetch missing weights or reach credentials.
    """
    if not isinstance(job_path, (str, Path)) or not Path(job_path).is_file():
        raise ValueError("Qwen alignment job is missing.")
    if Path(job_path).stat().st_size > MAX_JOB_BYTES:
        raise ValueError("Qwen alignment job exceeds its size limit.")
    if cancel_check():
        raise ValueError("Qwen alignment cancelled.")
    if Path(result_path).exists():
        Path(result_path).unlink()
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"
    if worker_env:
        env.update(worker_env)
    full_command = [str(part) for part in command] + [str(job_path), str(result_path)]
    process = None
    started = time.monotonic()
    try:
        from subprocess_ownership import get_subprocess_lease_options
        options = get_subprocess_lease_options(env, task_lease_fd)
        process = start_owned_subprocess(
            full_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env, **options)
        while True:
            if cancel_check():
                raise ValueError("Qwen alignment cancelled.")
            if time.monotonic() - started >= timeout:
                raise ValueError("Qwen alignment timed out.")
            try:
                status = process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if cancel_check():
            raise ValueError("Qwen alignment cancelled.")
        if status:
            message = _read_worker_error(result_path)
            if message:
                raise ValueError("Qwen alignment failed: %s" % message)
            raise ValueError("Qwen alignment worker failed with exit code %s." % status)
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    stop_owned_subprocess(process, timeout=1)
            finally:
                control = getattr(process, "_alexandria_control", None)
                if control is not None:
                    control.close()
    return _read_worker_tokens(result_path, max_result_bytes)


def _read_worker_tokens(result_path, max_result_bytes):
    path = Path(result_path)
    if not path.is_file():
        raise ValueError("Qwen alignment produced no result file.")
    with path.open("rb") as stream:
        data = stream.read(max_result_bytes + 1)
    if len(data) > max_result_bytes:
        raise ValueError("Qwen alignment result exceeds its size limit.")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Qwen alignment result is malformed.") from error
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        message = doc.get("error") if isinstance(doc, dict) else None
        raise ValueError("Qwen alignment failed: %s" % message if message
                         else "Qwen alignment failed.")
    tokens = doc.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("Qwen alignment result tokens are malformed.")
    return tokens


def _read_worker_error(result_path):
    try:
        with Path(result_path).open("rb") as stream:
            data = stream.read(1024**2 + 1)
        doc = json.loads(data.decode("utf-8"))
        message = doc.get("error") if isinstance(doc, dict) else None
        return message if isinstance(message, str) and message else None
    except Exception:
        return None
