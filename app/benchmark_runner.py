"""Production-backed benchmark stage adapters."""

import hashlib
import os
import time
import json
import base64
import copy
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from openai import OpenAI

from benchmark_core import load_resumable_benchmark_report, save_benchmark_report
from benchmark_fixtures import _hash_entries, get_normalized_source_chunks
from chunk_quality import validate_chunk_quality
from config_settings import load_app_config
from generate_script import LLMGenParams, process_chunk
from lmstudio_settings import get_lmstudio_status, get_remote_lmstudio_status
from utils import is_path_inside
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from review_script import check_text_loss, diff_entries, review_batch
import generate_personas
from find_nicknames import find_nicknames


def _validate_tts_fixture(fixture, root_dir=None):
    if fixture.get("voice_type") == "design":
        keys = ("voice_type", "text", "description", "seed")
    elif fixture.get("voice_type", "custom") == "clone":
        keys = ("voice_type", "text", "speaker", "seed", "ref_audio",
                "ref_audio_sha256", "ref_text")
    elif fixture.get("voice_type") == "lora":
        keys = ("voice_type", "text", "instruct", "speaker", "seed",
                "adapter_path", "adapter_artifact_sha256")
    else:
        keys = ("text", "instruct", "speaker", "voice", "seed")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    if fixture.get("voice_type") == "clone" and root_dir:
        path = os.path.abspath(os.path.join(root_dir, fixture["ref_audio"]))
        if not is_path_inside(path, root_dir) or not os.path.isfile(path):
            raise ValueError("clone reference audio is outside the project or missing")
        with open(path, "rb") as ref_file:
            if hashlib.sha256(ref_file.read()).hexdigest() != fixture["ref_audio_sha256"]:
                raise ValueError("clone reference audio hash changed")
    if fixture.get("voice_type") == "lora" and root_dir:
        adapter_path = os.path.abspath(os.path.join(root_dir, fixture["adapter_path"]))
        if not is_path_inside(adapter_path, root_dir) or not os.path.isdir(adapter_path):
            raise ValueError("LoRA adapter is outside the project or missing")
        for filename, expected in fixture["adapter_artifact_sha256"].items():
            artifact_path = os.path.join(adapter_path, filename)
            if not os.path.isfile(artifact_path):
                raise ValueError(f"LoRA adapter is missing {filename}")
            with open(artifact_path, "rb") as artifact_file:
                if hashlib.sha256(artifact_file.read()).hexdigest() != expected:
                    raise ValueError(f"LoRA adapter artifact hash changed: {filename}")
    return content


def _validate_lora_training_fixture(fixture, root_dir):
    keys = ("dataset_path", "metadata_sha256", "sample_count", "audio_sha256",
            "epochs", "seed", "lr", "lora_r", "lora_alpha", "grad_accum", "language")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    dataset_path = os.path.abspath(os.path.join(root_dir, fixture["dataset_path"]))
    if not is_path_inside(dataset_path, root_dir):
        raise ValueError("training dataset is outside the project")
    metadata_path = os.path.join(dataset_path, "metadata.jsonl")
    with open(metadata_path, "rb") as metadata_file:
        if hashlib.sha256(metadata_file.read()).hexdigest() != fixture["metadata_sha256"]:
            raise ValueError("training metadata hash changed")
    for relative_path, expected in fixture["audio_sha256"].items():
        audio_path = os.path.abspath(os.path.join(dataset_path, relative_path))
        if not is_path_inside(audio_path, dataset_path) or not os.path.isfile(audio_path):
            raise ValueError("training audio is outside the dataset or missing")
        with open(audio_path, "rb") as audio_file:
            if hashlib.sha256(audio_file.read()).hexdigest() != expected:
                raise ValueError(f"training audio hash changed: {relative_path}")
    return content


def _validate_preparer_fixture(fixture, root_dir):
    keys = ("audio_path", "audio_sha256", "limit", "language", "model_revision")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    audio_path = os.path.abspath(os.path.join(root_dir, fixture["audio_path"]))
    if not is_path_inside(audio_path, root_dir) or not os.path.isfile(audio_path):
        raise ValueError("preparer audio is outside the project or missing")
    with open(audio_path, "rb") as audio_file:
        if hashlib.sha256(audio_file.read()).hexdigest() != fixture["audio_sha256"]:
            raise ValueError("preparer audio hash changed")
    return content


def _validate_dedup_fixture(fixture, root_dir):
    keys = ("dataset_path", "metadata_sha256", "samples_per_volume",
            "audio_sha256", "model_id", "seed")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    dataset_path = os.path.abspath(os.path.join(root_dir, fixture["dataset_path"]))
    if not is_path_inside(dataset_path, root_dir):
        raise ValueError("dedup source dataset is outside the project")
    metadata_path = os.path.join(dataset_path, "metadata.jsonl")
    with open(metadata_path, "rb") as metadata_file:
        if hashlib.sha256(metadata_file.read()).hexdigest() != fixture["metadata_sha256"]:
            raise ValueError("dedup metadata hash changed")
    for relative_path, expected in fixture["audio_sha256"].items():
        audio_path = os.path.abspath(os.path.join(dataset_path, relative_path))
        if not is_path_inside(audio_path, dataset_path) or not os.path.isfile(audio_path):
            raise ValueError("dedup audio is outside the dataset or missing")
        with open(audio_path, "rb") as audio_file:
            if hashlib.sha256(audio_file.read()).hexdigest() != expected:
                raise ValueError(f"dedup audio hash changed: {relative_path}")
    return content


def _validate_profiling_fixture(fixture, root_dir):
    keys = ("zip_path", "zip_sha256", "model_path", "model_sha256",
            "dataset_id", "seed")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    for path_key, hash_key in (("zip_path", "zip_sha256"),
                               ("model_path", "model_sha256")):
        path = os.path.abspath(os.path.join(root_dir, fixture[path_key]))
        if not is_path_inside(path, root_dir) or not os.path.isfile(path):
            raise ValueError(f"profiling {path_key} is outside the project or missing")
        with open(path, "rb") as source_file:
            if hashlib.sha256(source_file.read()).hexdigest() != fixture[hash_key]:
                raise ValueError(f"profiling {path_key} hash changed")
    return content


def _run_tts_worker(payload, target, settings, root_dir, output_dir, ssh_alias):
    if target == "local":
        worker_payload = payload
    else:
        worker_payload = _stage_remote_tts_assets(payload, root_dir, ssh_alias)
    encoded = base64.b64encode(json.dumps(worker_payload).encode("utf-8")).decode("ascii")
    if target == "local":
        command = [sys.executable, os.path.join(root_dir, "app", "tts_benchmark.py"),
                   "--payload", encoded, "--output-dir", output_dir]
    else:
        remote_root = settings.get("remote_root")
        remote_python = settings.get("remote_python")
        if not remote_root or not remote_python or not ssh_alias:
            raise ValueError("Thunder TTS requires remote_root, remote_python, and SSH alias")
        remote_command = " ".join(shlex.quote(part) for part in [
            remote_python, os.path.join(remote_root, "app", "tts_benchmark.py"),
            "--payload", encoded, "--output-dir", output_dir])
        command = ["ssh", ssh_alias, remote_command]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600,
                            check=False)
    marker = "TTS_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        detail = result.stderr.strip() or result.stdout.strip() or "TTS worker failed"
        raise RuntimeError(detail[-2000:])
    return json.loads(lines[-1][len(marker):])


def _stage_remote_tts_assets(payload, root_dir, ssh_alias):
    """Copy hash-verified runtime assets to a stable remote temp directory."""
    if not ssh_alias:
        raise ValueError("Thunder SSH alias is required to stage TTS assets")
    staged = copy.deepcopy(payload)
    fixtures = staged.get("fixtures", [])
    clone_fixtures = [fixture for fixture in fixtures if fixture.get("voice_type") == "clone"]
    lora_fixtures = [fixture for fixture in fixtures if fixture.get("voice_type") == "lora"]
    if not clone_fixtures and not lora_fixtures:
        return staged
    remote_dir = "/tmp/alexandria-tts-benchmark-assets"
    mkdir = subprocess.run(["ssh", ssh_alias, "mkdir", "-p", remote_dir],
                           capture_output=True, text=True, timeout=30, check=False)
    if mkdir.returncode:
        raise RuntimeError(mkdir.stderr.strip() or "could not create remote asset directory")
    copied = set()
    for fixture in clone_fixtures:
        digest = fixture["ref_audio_sha256"]
        source = os.path.abspath(os.path.join(root_dir, fixture["ref_audio"]))
        remote_path = f"{remote_dir}/{digest}.wav"
        if digest not in copied:
            transfer = subprocess.run(
                ["scp", source, f"{ssh_alias}:{remote_path}"], capture_output=True,
                text=True, timeout=120, check=False)
            if transfer.returncode:
                raise RuntimeError(transfer.stderr.strip() or "clone reference transfer failed")
            copied.add(digest)
        fixture["ref_audio"] = remote_path
    staged_adapters = set()
    for fixture in lora_fixtures:
        hashes = fixture["adapter_artifact_sha256"]
        adapter_digest = _hash_entries(hashes)
        remote_adapter = f"{remote_dir}/lora-{adapter_digest}"
        if adapter_digest not in staged_adapters:
            mkdir = subprocess.run(["ssh", ssh_alias, "mkdir", "-p", remote_adapter],
                                   capture_output=True, text=True, timeout=30, check=False)
            if mkdir.returncode:
                raise RuntimeError(mkdir.stderr.strip() or "could not create remote adapter directory")
            local_adapter = os.path.abspath(os.path.join(root_dir, fixture["adapter_path"]))
            for filename in hashes:
                transfer = subprocess.run(
                    ["scp", os.path.join(local_adapter, filename),
                     f"{ssh_alias}:{remote_adapter}/{filename}"], capture_output=True,
                    text=True, timeout=300, check=False)
                if transfer.returncode:
                    raise RuntimeError(transfer.stderr.strip() or "LoRA adapter transfer failed")
            staged_adapters.add(adapter_digest)
        fixture["adapter_path"] = remote_adapter
    return staged


def run_tts_generation_benchmark(manifest, environment, report_path, state,
                                 config_path, root_dir):
    """Run the same production CustomVoice worker locally or through SSH."""
    if manifest["stage"] != "tts_generation" or len(manifest["targets"]) != 1:
        raise ValueError("TTS runs require exactly one target")
    target = manifest["targets"][0]
    settings = manifest.get("settings") or {}
    fixtures = []
    for fixture in manifest["fixtures"]:
        _validate_tts_fixture(fixture, root_dir)
        fixtures.append(dict(fixture))
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"])
                 for case in report.get("cases", [])}
    pending_fixtures = []
    for fixture in fixtures:
        missing = [repetition for repetition in range(1, manifest["repetitions"] + 1)
                   if (fixture["id"], repetition) not in completed]
        if missing:
            pending = dict(fixture)
            pending["repetition_numbers"] = missing
            pending_fixtures.append(pending)
    if not pending_fixtures:
        for task in state["tasks"]:
            task["status"] = "done"
        state["status"] = "complete"
        return report
    if state.get("cancel"):
        state["status"] = "cancelled"
        return report
    tts_config = dict(config.get("tts") or {})
    tts_config["max_new_tokens"] = settings.get(
        "max_new_tokens", tts_config.get("max_new_tokens", 2048))
    payload = {"tts": tts_config, "fixtures": pending_fixtures,
               "repetitions": manifest["repetitions"]}
    output_dir = (os.path.join(os.path.dirname(report_path), "audio",
                               os.path.splitext(os.path.basename(report_path))[0])
                  if target == "local" else settings.get("remote_output_dir",
                                                         "/tmp/alexandria-tts-benchmark"))
    cases = _run_tts_worker(payload, target, settings, root_dir, output_dir,
                            (config.get("llm_remote_ssh") or "").strip())
    thresholds = manifest.get("quality_thresholds") or {}
    for case in cases:
        metrics = case.get("metrics") or {}
        quality = bool(case["status"] == "passed"
                       and metrics.get("duration_seconds", 0) >= thresholds.get("min_duration_seconds", 0.1)
                       and metrics.get("silence_ratio", 1) <= thresholds.get("max_silence_ratio", 0.98)
                       and metrics.get("clipping_ratio", 1) <= thresholds.get("max_clipping_ratio", 0.01))
        case["quality"] = {"passed": quality}
        case["status"] = "passed" if quality else "failed"
        report["cases"].append(case)
        save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _run_lora_training_worker(fixture, target, settings, root_dir, ssh_alias):
    worker_fixture = copy.deepcopy(fixture)
    if target == "local":
        worker_fixture["root_dir"] = root_dir
        python_executable = sys.executable
        train_script = os.path.join(root_dir, "app", "train_lora.py")
        output_root = "/tmp/alexandria-lora-training-local"
        worker_script = os.path.join(root_dir, "app", "lora_training_benchmark.py")
        command_prefix = []
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder training requires remote_root, remote_python, and SSH alias")
        remote_source = f"/tmp/alexandria-lora-training-{fixture['sha256']}"
        mkdir = subprocess.run(["ssh", ssh_alias, "mkdir", "-p", remote_source],
                               capture_output=True, text=True, timeout=30, check=False)
        if mkdir.returncode:
            raise RuntimeError(mkdir.stderr.strip() or "could not create remote training fixture")
        source_dir = os.path.join(root_dir, fixture["dataset_path"])
        files = ["metadata.jsonl", *fixture["audio_sha256"].keys()]
        for relative_path in files:
            remote_path = os.path.join(remote_source, relative_path)
            subprocess.run(["ssh", ssh_alias, "mkdir", "-p", os.path.dirname(remote_path)],
                           capture_output=True, text=True, timeout=30, check=True)
            transfer = subprocess.run(["scp", os.path.join(source_dir, relative_path),
                                       f"{ssh_alias}:{remote_path}"], capture_output=True,
                                      text=True, timeout=300, check=False)
            if transfer.returncode:
                raise RuntimeError(transfer.stderr.strip() or "training fixture transfer failed")
        worker_fixture.update({"root_dir": "/tmp", "dataset_path": os.path.basename(remote_source)})
        output_root = "/tmp/alexandria-lora-training-output"
        worker_script = os.path.join(remote_root, "app", "lora_training_benchmark.py")
        train_script = os.path.join(remote_root, "app", "train_lora.py")
        command_prefix = ["ssh", ssh_alias]
    payload = {"fixture": worker_fixture, "python": python_executable,
               "train_script": train_script, "output_root": output_root}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    worker_command = [python_executable, worker_script, "--payload", encoded]
    command = worker_command if not command_prefix else [*command_prefix,
        " ".join(shlex.quote(part) for part in worker_command)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=7200, check=False)
    marker = "LORA_TRAINING_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "training worker failed")[-4000:])
    return json.loads(lines[-1][len(marker):])


def run_lora_training_benchmark(manifest, environment, report_path, state,
                                config_path, root_dir):
    """Run production LoRA training calibration locally or on Thunder."""
    target = manifest["targets"][0]
    for fixture in manifest["fixtures"]:
        _validate_lora_training_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report.get("cases", [])}
    for fixture in manifest["fixtures"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_lora_training_worker(
                fixture, target, manifest.get("settings") or {}, root_dir,
                (config.get("llm_remote_ssh") or "").strip())
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _run_preparer_worker(fixture, target, settings, root_dir, ssh_alias):
    worker_fixture = copy.deepcopy(fixture)
    if target == "local":
        python_executable = settings.get("local_python")
        if not python_executable:
            raise ValueError("local preparer benchmark requires local_python")
        worker_fixture["root_dir"] = root_dir
        preparer_script = os.path.join(root_dir, "alexandria_preparer_rocm_compatible.py")
        worker_script = os.path.join(root_dir, "app", "preparer_benchmark.py")
        command_prefix = []
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder preparer requires remote_root, remote_python, and SSH alias")
        remote_audio = f"/tmp/alexandria-preparer-{fixture['audio_sha256']}.wav"
        transfer = subprocess.run(
            ["scp", os.path.join(root_dir, fixture["audio_path"]),
             f"{ssh_alias}:{remote_audio}"], capture_output=True, text=True,
            timeout=300, check=False)
        if transfer.returncode:
            raise RuntimeError(transfer.stderr.strip() or "preparer audio transfer failed")
        worker_fixture.update({"root_dir": "/", "audio_path": remote_audio.lstrip("/")})
        preparer_script = os.path.join(remote_root, "alexandria_preparer_rocm_compatible.py")
        worker_script = os.path.join(remote_root, "app", "preparer_benchmark.py")
        command_prefix = ["ssh", ssh_alias]
    payload = {"fixture": worker_fixture, "python": python_executable,
               "preparer_script": preparer_script}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    worker_command = [python_executable, worker_script, "--payload", encoded]
    command = worker_command if not command_prefix else [*command_prefix,
        " ".join(shlex.quote(part) for part in worker_command)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600, check=False)
    marker = "PREPARER_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "preparer worker failed")[-4000:])
    return json.loads(lines[-1][len(marker):])


def run_preparer_benchmark(manifest, environment, report_path, state,
                           config_path, root_dir):
    """Run the preparer's production ASR phase locally or on Thunder."""
    target = manifest["targets"][0]
    for fixture in manifest["fixtures"]:
        _validate_preparer_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report.get("cases", [])}
    for fixture in manifest["fixtures"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_preparer_worker(
                fixture, target, manifest.get("settings") or {}, root_dir,
                (config.get("llm_remote_ssh") or "").strip())
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _run_dedup_worker(fixture, target, settings, root_dir, ssh_alias):
    worker_fixture = copy.deepcopy(fixture)
    if target == "local":
        python_executable = settings.get("local_python")
        if not python_executable:
            raise ValueError("local dedup benchmark requires local_python")
        worker_fixture["root_dir"] = root_dir
        analysis_script = os.path.join(root_dir, "voice_analysis.py")
        worker_script = os.path.join(root_dir, "app", "dedup_benchmark.py")
        command_prefix = []
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder dedup requires remote_root, remote_python, and SSH alias")
        remote_source = f"/tmp/alexandria-dedup-{fixture['sha256']}"
        source_dir = os.path.join(root_dir, fixture["dataset_path"])
        files = ["metadata.jsonl", *fixture["audio_sha256"].keys()]
        for relative_path in files:
            remote_path = os.path.join(remote_source, relative_path)
            mkdir = subprocess.run(["ssh", ssh_alias, "mkdir", "-p",
                                    os.path.dirname(remote_path)], capture_output=True,
                                   text=True, timeout=30, check=False)
            if mkdir.returncode:
                raise RuntimeError(mkdir.stderr.strip() or "could not create remote dedup fixture")
            transfer = subprocess.run(["scp", os.path.join(source_dir, relative_path),
                                       f"{ssh_alias}:{remote_path}"], capture_output=True,
                                      text=True, timeout=300, check=False)
            if transfer.returncode:
                raise RuntimeError(transfer.stderr.strip() or "dedup fixture transfer failed")
        worker_fixture.update({"root_dir": "/tmp", "dataset_path": os.path.basename(remote_source)})
        analysis_script = os.path.join(remote_root, "voice_analysis.py")
        worker_script = os.path.join(remote_root, "app", "dedup_benchmark.py")
        command_prefix = ["ssh", ssh_alias]
    payload = {"fixture": worker_fixture, "python": python_executable,
               "analysis_script": analysis_script}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    worker_command = [python_executable, worker_script, "--payload", encoded]
    command = worker_command if not command_prefix else [*command_prefix,
        " ".join(shlex.quote(part) for part in worker_command)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600, check=False)
    marker = "DEDUP_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "dedup worker failed")[-4000:])
    return json.loads(lines[-1][len(marker):])


def run_dedup_benchmark(manifest, environment, report_path, state,
                        config_path, root_dir):
    """Run production Voice Lab dedup locally or on Thunder."""
    target = manifest["targets"][0]
    for fixture in manifest["fixtures"]:
        _validate_dedup_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report.get("cases", [])}
    for fixture in manifest["fixtures"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_dedup_worker(
                fixture, target, manifest.get("settings") or {}, root_dir,
                (config.get("llm_remote_ssh") or "").strip())
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _run_profiling_worker(fixture, target, settings, root_dir, ssh_alias):
    zip_path = os.path.join(root_dir, fixture["zip_path"])
    if target == "local":
        python_executable = settings.get("local_python")
        worker_root = root_dir
        worker_zip = zip_path
        model_path = os.path.join(root_dir, fixture["model_path"])
        worker_script = os.path.join(root_dir, "app", "profiling_benchmark.py")
        prefix = []
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        model_path = settings.get("remote_model_path")
        if not remote_root or not python_executable or not model_path or not ssh_alias:
            raise ValueError("Thunder profiling requires remote_root, remote_python, remote_model_path, and SSH alias")
        worker_root = remote_root
        worker_zip = f"/tmp/alexandria-profiling-{fixture['zip_sha256']}.zip"
        transfer = subprocess.run(["scp", zip_path, f"{ssh_alias}:{worker_zip}"],
                                  capture_output=True, text=True, timeout=300, check=False)
        if transfer.returncode:
            raise RuntimeError(transfer.stderr.strip() or "profiling fixture transfer failed")
        verify = subprocess.run(["ssh", ssh_alias, "sha256sum", model_path],
                                capture_output=True, text=True, timeout=300, check=False)
        observed = verify.stdout.strip().split()[0] if verify.returncode == 0 and verify.stdout.strip() else ""
        if observed != fixture["model_sha256"]:
            raise ValueError("Thunder profiling model hash does not match the fixture")
        worker_script = os.path.join(remote_root, "app", "profiling_benchmark.py")
        prefix = ["ssh", ssh_alias]
    if not python_executable:
        raise ValueError("profiling benchmark requires a Python executable")
    payload = {"fixture": fixture, "root_dir": worker_root,
               "zip_path": worker_zip, "model_path": model_path}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    worker_command = [python_executable, worker_script, "--payload", encoded]
    command = worker_command if not prefix else [*prefix,
        " ".join(shlex.quote(part) for part in worker_command)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=3600, check=False)
    marker = "PROFILING_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or
                            "profiling worker failed")[-4000:])
    return json.loads(lines[-1][len(marker):])


def run_profiling_benchmark(manifest, environment, report_path, state,
                            config_path, root_dir):
    """Run production Voice Lab acoustics and GGUF description generation."""
    target = manifest["targets"][0]
    for fixture in manifest["fixtures"]:
        _validate_profiling_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    for fixture in manifest["fixtures"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_profiling_worker(
                fixture, target, manifest.get("settings") or {}, root_dir,
                (config.get("llm_remote_ssh") or "").strip())
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _run_naming_worker(fixture, target, settings, root_dir, ssh_alias):
    if _hash_entries({"entries": fixture["entries"]}) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    if target == "local":
        python_executable = settings.get("local_python") or sys.executable
        script = os.path.join(root_dir, "name_voices.py")
        worker = os.path.join(root_dir, "app", "naming_benchmark.py")
        prefix = []
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python") or "python3"
        if not remote_root or not ssh_alias:
            raise ValueError("Thunder naming requires remote_root and SSH alias")
        script = os.path.join(remote_root, "name_voices.py")
        worker = os.path.join(remote_root, "app", "naming_benchmark.py")
        prefix = ["ssh", ssh_alias]
    payload = {"fixture": fixture, "python": python_executable, "script": script}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    worker_command = [python_executable, worker, "--payload", encoded]
    command = worker_command if not prefix else [*prefix,
        " ".join(shlex.quote(part) for part in worker_command)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=120, check=False)
    marker = "NAMING_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or
                            "naming worker failed")[-4000:])
    return json.loads(lines[-1][len(marker):])


def run_naming_benchmark(manifest, environment, report_path, state,
                         config_path, root_dir):
    """Run deterministic production Voice Lab naming locally or remotely."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    for fixture in manifest["fixtures"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_naming_worker(
                fixture, target, manifest.get("settings") or {}, root_dir,
                (config.get("llm_remote_ssh") or "").strip())
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _validate_persona_fixture(fixture):
    content = {key: fixture[key] for key in ("entries", "speakers", "batch_size")}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    return content


def _run_persona_case(fixture, client, model_name, context_length):
    """Run production advanced persona LLM phases without duplicating TTS."""
    _validate_persona_fixture(fixture)
    entries = fixture["entries"]
    speakers = fixture["speakers"]
    samples = {speaker: [entry["text"] for entry in entries
                         if entry["speaker"] == speaker] for speaker in speakers}
    captures = {}
    voice_config = {}
    discovery_calls = 0
    compile_calls = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="alexandria-persona-") as root:
        ref_dir = os.path.join(root, "refs")
        os.makedirs(ref_dir)
        for batch_number, (batch_start, batch) in enumerate(
                generate_personas._batch_entries(entries, fixture["batch_size"]), 1):
            prompt = generate_personas._build_batch_discovery_prompt(
                batch_start, batch, speakers)
            characters = generate_personas._discover_batch_characters(
                client, model_name, prompt, batch, batch_number, context_length)
            discovery_calls += 1
            generate_personas._write_batch_character_refs(
                ref_dir, characters, speakers, batch_number)

        original_save = generate_personas._save_generated_preview
        original_sleep = generate_personas.time.sleep
        def capture_preview(_root, _engine, _config, speaker, description, ref_text):
            captures[speaker] = {"description": description, "ref_text": ref_text}
            return True
        try:
            generate_personas._save_generated_preview = capture_preview
            generate_personas.time.sleep = lambda _seconds: None
            for speaker in speakers:
                generate_personas._compile_persona(
                    client, model_name, None, voice_config, root, ref_dir, speaker,
                    samples, generate_personas.PERSONA_SYSTEM_PROMPT,
                    generate_personas.PERSONA_ADVANCED_PROMPT, context_length)
                compile_calls += 1
        finally:
            generate_personas._save_generated_preview = original_save
            generate_personas.time.sleep = original_sleep
        refs = {speaker: generate_personas._load_character_ref(ref_dir, speaker)
                for speaker in speakers}
    complete = all(captures.get(speaker, {}).get("description")
                   and captures[speaker].get("ref_text")
                   and refs[speaker].get("observations") for speaker in speakers)
    return {"status": "passed" if complete else "failed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "discovery_calls": discovery_calls, "compile_calls": compile_calls,
            "personas": captures,
            "quality": {"passed": complete,
                        "speaker_coverage": sum(speaker in captures for speaker in speakers) / len(speakers),
                        "evidence_coverage": sum(bool(refs[speaker].get("observations"))
                                                 for speaker in speakers) / len(speakers)}}


def run_persona_generation_benchmark(manifest, environment, report_path, state,
                                     config_path, root_dir):
    """Run advanced persona discovery and compilation against configured LLM."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, target)
    client = OpenAI(base_url=llm["base_url"], api_key=llm.get("api_key", "local"))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    for fixture in manifest["fixtures"]:
        _validate_persona_fixture(fixture)
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_persona_case(
                fixture, client, llm["model_name"], status.get("context_length"))
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _validate_nickname_fixture(fixture):
    content = {key: fixture[key] for key in
               ("entries", "expected_aliases", "existing_aliases")}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    return content


def run_nickname_detection_benchmark(manifest, environment, report_path, state,
                                     config_path, root_dir):
    """Run production context-aware alias discovery against configured LLM."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, target)
    client = OpenAI(base_url=llm["base_url"], api_key=llm.get("api_key", "local"))
    context_length = status.get("context_length") or 4096
    concurrency = status.get("parallel") or 1
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    for fixture in manifest["fixtures"]:
        _validate_nickname_fixture(fixture)
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            started = time.monotonic()
            aliases, evidence = find_nicknames(
                client, llm["model_name"], fixture["entries"],
                existing_aliases=fixture["existing_aliases"],
                context_length=context_length, concurrency=concurrency)
            expected = fixture["expected_aliases"]
            correct = sum(aliases.get(key) == value for key, value in expected.items())
            precision = correct / len(aliases) if aliases else 0.0
            recall = correct / len(expected)
            evidence_by_label = {str(key).strip().lower(): value
                                 for key, value in evidence.items()}
            evidence_coverage = sum(bool(evidence_by_label.get(key.lower()))
                                    for key in expected) / len(expected)
            quality = {"passed": precision == 1.0 and recall == 1.0
                       and evidence_coverage == 1.0,
                       "precision": precision, "recall": recall,
                       "evidence_coverage": evidence_coverage}
            report["cases"].append({
                "fixture_id": fixture["id"], "repetition": repetition,
                "status": "passed" if quality["passed"] else "failed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "context_length": context_length, "concurrency": concurrency,
                "aliases": aliases, "evidence": evidence, "quality": quality})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _validate_export_fixture(fixture, root_dir):
    content = {key: fixture[key] for key in
               ("chunks", "audio_sha256", "per_chunk_chapters")}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    for relative_path, expected in fixture["audio_sha256"].items():
        path = os.path.abspath(os.path.join(root_dir, relative_path))
        if not is_path_inside(path, root_dir) or not os.path.isfile(path):
            raise ValueError("export audio is outside the project or missing")
        with open(path, "rb") as audio_file:
            if hashlib.sha256(audio_file.read()).hexdigest() != expected:
                raise ValueError(f"export audio hash changed: {relative_path}")


def _run_export_worker(stage, fixture, target, settings, root_dir, ssh_alias):
    _validate_export_fixture(fixture, root_dir)
    if target == "local":
        python_executable = settings.get("local_python") or sys.executable
        source_root = root_dir
        worker = os.path.join(root_dir, "app", "export_benchmark.py")
        prefix = []
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder export requires remote_root, remote_python, and SSH alias")
        source_root = f"/tmp/alexandria-export-{fixture['sha256']}"
        for relative_path in fixture["audio_sha256"]:
            remote_path = os.path.join(source_root, relative_path)
            subprocess.run(["ssh", ssh_alias, "mkdir", "-p", os.path.dirname(remote_path)],
                           capture_output=True, text=True, timeout=30, check=True)
            subprocess.run(["scp", os.path.join(root_dir, relative_path),
                            f"{ssh_alias}:{remote_path}"], capture_output=True,
                           text=True, timeout=300, check=True)
        worker = os.path.join(remote_root, "app", "export_benchmark.py")
        prefix = ["ssh", ssh_alias]
    payload = {"stage": stage, "fixture": fixture, "source_root": source_root}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    worker_command = [python_executable, worker, "--payload", encoded]
    command = worker_command if not prefix else [*prefix,
        " ".join(shlex.quote(part) for part in worker_command)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=900, check=False)
    marker = "EXPORT_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or
                            "export worker failed")[-4000:])
    return json.loads(lines[-1][len(marker):])


def run_export_benchmark(manifest, environment, report_path, state,
                         config_path, root_dir):
    """Run Audacity or M4B production exports locally or on Thunder."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    for fixture in manifest["fixtures"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_export_worker(
                manifest["stage"], fixture, target, manifest.get("settings") or {},
                root_dir, (config.get("llm_remote_ssh") or "").strip())
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _run_dataset_builder_worker(fixture, target, settings, root_dir, ssh_alias,
                                tts_config):
    content = {key: fixture[key] for key in
               ("description", "samples", "global_seed", "seeds")}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    if target == "local":
        python_executable = settings.get("local_python") or sys.executable
        worker = os.path.join(root_dir, "app", "dataset_builder_benchmark.py")
        prefix = []
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder Dataset Builder requires remote_root, remote_python, and SSH alias")
        worker = os.path.join(remote_root, "app", "dataset_builder_benchmark.py")
        prefix = ["ssh", ssh_alias]
    payload = {"fixture": fixture, "tts": tts_config}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    worker_command = [python_executable, worker, "--payload", encoded]
    command = worker_command if not prefix else [*prefix,
        " ".join(shlex.quote(part) for part in worker_command)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=3600, check=False)
    marker = "DATASET_BUILDER_BENCHMARK_RESULT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or
                            "Dataset Builder worker failed")[-4000:])
    return json.loads(lines[-1][len(marker):])


def run_dataset_builder_benchmark(manifest, environment, report_path, state,
                                  config_path, root_dir):
    """Run the production Dataset Builder batch route locally or on Thunder."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    for fixture in manifest["fixtures"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            result = _run_dataset_builder_worker(
                fixture, target, manifest.get("settings") or {}, root_dir,
                (config.get("llm_remote_ssh") or "").strip(), config.get("tts") or {})
            report["cases"].append({"fixture_id": fixture["id"],
                                    "repetition": repetition, **result})
            save_benchmark_report(report_path, report)
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"
    return report


def _load_text_fixture(fixture, uploads_dir):
    path = os.path.abspath(fixture.get("path") or "")
    if not path or not is_path_inside(path, uploads_dir) or not os.path.isfile(path):
        raise ValueError(f"fixture {fixture.get('id')} must be a file inside uploads")
    with open(path, "rb") as fixture_file:
        raw = fixture_file.read()
    digest = hashlib.sha256(raw).hexdigest()
    if fixture.get("chunk_number") is not None:
        if digest != fixture.get("source_sha256"):
            raise ValueError(f"fixture {fixture['id']} source hash changed")
        chunks = get_normalized_source_chunks(raw, fixture.get("chunk_size", 6000))
        chunk_number = fixture["chunk_number"]
        if not isinstance(chunk_number, int) or not 1 <= chunk_number <= len(chunks):
            raise ValueError(f"fixture {fixture['id']} chunk_number is out of range")
        text = chunks[chunk_number - 1]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != fixture["sha256"]:
            raise ValueError(f"fixture {fixture['id']} chunk hash changed")
        return text
    if digest != fixture["sha256"]:
        raise ValueError(f"fixture {fixture['id']} hash changed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"fixture {fixture['id']} is not UTF-8 text") from exc
    if not text.strip():
        raise ValueError(f"fixture {fixture['id']} is empty")
    return text


def _measure_llm_network_rtt(client):
    """Baseline round-trip time against the same base_url a benchmark case
    will call, using the cheapest request the OpenAI-compatible API exposes.

    A case's elapsed_seconds is call-latency-plus-compute, not pure compute
    - for the "thunder" target that latency includes an internet round trip
    through the SSH-forwarded HTTPS tunnel. Most visible on short calls
    (e.g. nickname detection's sub-2-second totals), where a single RTT can
    double the reported time. Returns None if the probe itself fails, so a
    transient failure here doesn't block the real benchmark case.
    """
    started = time.monotonic()
    try:
        client.models.list()
    except Exception:
        return None
    return round(time.monotonic() - started, 3)


def _get_llm_benchmark_target(config, target):
    if target == "local":
        llm = config.get("llm_local") or config.get("llm") or {}
        status = get_lmstudio_status(llm.get("model_name"))
    elif target == "thunder":
        llm = config.get("llm_remote") or {}
        remote_port = urlparse(llm.get("base_url") or "").port or 1234
        status = get_remote_lmstudio_status(
            (config.get("llm_remote_ssh") or "").strip(), llm.get("model_name"),
            port=remote_port)
    else:
        raise ValueError(f"unsupported LLM benchmark target: {target}")
    if not llm.get("base_url") or not llm.get("model_name"):
        raise ValueError(f"{target} LLM endpoint is not configured")
    if not status.get("available") or not status.get("loaded"):
        raise ValueError(f"{target} LM Studio model is not ready")
    if status.get("server_reachable") is False:
        raise ValueError(
            f"{target} LM Studio model is loaded but its server isn't reachable "
            "on the forwarded port (likely bound to 127.0.0.1 instead of 0.0.0.0)")
    return llm, status


def run_script_generation_benchmark(manifest, environment, report_path, state,
                                    config_path, uploads_dir):
    """Run local script-generation cases and persist after every repetition."""
    if manifest["stage"] != "script_generation" or len(manifest["targets"]) != 1:
        raise ValueError("script-generation runs require exactly one target")
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, target)
    generation = config.get("generation") or {}
    prompts = config.get("prompts") or {}
    model_name = llm.get("model_name")
    params = LLMGenParams(
        system_prompt=prompts.get("system_prompt"),
        user_prompt_template=prompts.get("user_prompt"),
        max_tokens=generation.get("max_tokens", 4096),
        temperature=generation.get("temperature", 0.6),
        top_p=generation.get("top_p", 0.8), top_k=generation.get("top_k"),
        min_p=generation.get("min_p"),
        presence_penalty=generation.get("presence_penalty", 0.0),
        banned_tokens=generation.get("banned_tokens", []),
        context_length=status.get("context_length"))
    client = OpenAI(base_url=llm.get("base_url"), api_key=llm.get("api_key", "local"))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    completed = {(case["fixture_id"], case["repetition"])
                 for case in report.get("cases", [])}
    max_retries = manifest.get("settings", {}).get("max_retries", 0)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("script-generation max_retries must be a non-negative integer")
    for fixture_index, fixture in enumerate(manifest["fixtures"]):
        state["current_task_idx"] = fixture_index
        state["tasks"][fixture_index]["status"] = "running"
        text = _load_text_fixture(fixture, uploads_dir)
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            if state.get("cancel"):
                state["status"] = "cancelled"
                state["tasks"][fixture_index]["status"] = "cancelled"
                return report
            attempts = []
            started = time.monotonic()
            entries = process_chunk(
                client, model_name, text, fixture.get("chunk_number", 1),
                fixture.get("total_chunks", 1), params,
                previous_entries=fixture.get("previous_entries") or None,
                max_retries=max_retries,
                attempt_observer=attempts.append)
            quality = validate_chunk_quality(text, entries)
            case = {"fixture_id": fixture["id"], "repetition": repetition,
                    "status": "passed" if entries and quality["passed"] else "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "entry_count": len(entries), "attempts": attempts, "quality": quality}
            report["cases"].append(case)
            save_benchmark_report(report_path, report)
            state["logs"].append(
                f"{fixture['id']} repetition {repetition}: {case['status']}")
        state["tasks"][fixture_index]["status"] = "done"
    state["status"] = "complete"
    return report


def _load_review_fixture(fixture, scripts_dir):
    path = os.path.abspath(fixture.get("path") or "")
    if not is_path_inside(path, scripts_dir) or not os.path.isfile(path):
        raise ValueError(f"fixture {fixture.get('id')} must be a file inside scripts")
    with open(path, "rb") as source_file:
        raw = source_file.read()
    if hashlib.sha256(raw).hexdigest() != fixture.get("source_sha256"):
        raise ValueError(f"fixture {fixture['id']} source hash changed")
    try:
        all_entries = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"fixture {fixture['id']} is not valid UTF-8 JSON") from exc
    start = fixture.get("entry_start")
    count = fixture.get("entry_count")
    if not isinstance(start, int) or not isinstance(count, int) or start < 1 or count < 1:
        raise ValueError(f"fixture {fixture['id']} has invalid entry bounds")
    entries = all_entries[start - 1:start - 1 + count]
    if len(entries) != count or _hash_entries(entries) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture['id']} entry hash changed")
    return entries


def run_script_review_benchmark(manifest, environment, report_path, state,
                                config_path, scripts_dir):
    """Run production review batches and persist deterministic quality metrics."""
    if manifest["stage"] != "script_review" or len(manifest["targets"]) != 1:
        raise ValueError("script-review runs require exactly one target")
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, manifest["targets"][0])
    generation = config.get("generation") or {}
    prompts = config.get("prompts") or {}
    params = LLMGenParams(
        prompts.get("review_system_prompt") or REVIEW_SYSTEM_PROMPT,
        prompts.get("review_user_prompt") or REVIEW_USER_PROMPT,
        generation.get("max_tokens", 4096), generation.get("temperature", 0.4),
        generation.get("top_p", 0.8), top_k=generation.get("top_k"),
        min_p=generation.get("min_p"),
        presence_penalty=generation.get("presence_penalty", 0.0),
        banned_tokens=generation.get("banned_tokens", []),
        context_length=status.get("context_length"))
    client = OpenAI(base_url=llm["base_url"], api_key=llm.get("api_key", "local"))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    max_retries = manifest.get("settings", {}).get("max_retries", 0)
    thresholds = manifest.get("quality_thresholds") or {}
    lower = thresholds.get("word_ratio_min", 0.95)
    upper = thresholds.get("word_ratio_max", 1.05)
    for fixture_index, fixture in enumerate(manifest["fixtures"]):
        state["current_task_idx"] = fixture_index
        state["tasks"][fixture_index]["status"] = "running"
        original = _load_review_fixture(fixture, scripts_dir)
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            if state.get("cancel"):
                state["status"] = "cancelled"
                state["tasks"][fixture_index]["status"] = "cancelled"
                return report
            attempts = []
            started = time.monotonic()
            corrected = review_batch(
                client, llm["model_name"], original, 1, 1, params,
                previous_tail=fixture.get("previous_tail") or None,
                max_retries=max_retries, attempt_observer=attempts.append)
            corrected = corrected or []
            text_ok, _, _, ratio = check_text_loss(
                original, corrected, threshold=lower, upper_bound=upper)
            structural_ok = bool(corrected) and all(
                isinstance(entry, dict) and isinstance(entry.get("text"), str)
                and isinstance(entry.get("speaker"), str) for entry in corrected)
            case = {"fixture_id": fixture["id"], "repetition": repetition,
                    "status": "passed" if text_ok and structural_ok else "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "entry_count": len(corrected), "attempts": attempts,
                    "quality": {"passed": text_ok and structural_ok,
                                "word_ratio": round(ratio, 4),
                                "text_loss_passed": text_ok,
                                "structural_passed": structural_ok},
                    "changes": diff_entries(original, corrected)}
            report["cases"].append(case)
            save_benchmark_report(report_path, report)
            state["logs"].append(
                f"{fixture['id']} repetition {repetition}: {case['status']}")
        state["tasks"][fixture_index]["status"] = "done"
    state["status"] = "complete"
    return report
