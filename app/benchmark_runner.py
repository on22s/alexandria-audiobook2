"""Production-backed benchmark stage adapters."""

import hashlib
import io
import logging
import re
import os
from core import llm_timeout_seconds
import time
import json
import copy
import dataclasses
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from llm_provider import make_llm_client, resolve_api_key

from benchmark_worker_protocol import get_encoded_worker_payload, get_benchmark_worker_result
from benchmark_core import load_resumable_benchmark_report, save_benchmark_report
from benchmark_fixtures import _hash_entries, get_normalized_source_chunks
from benchmark_validation import (validate_persona_speakers, get_benchmark_file_path,
                                  get_benchmark_directory_path, get_adapter_artifact_path,
                                  get_benchmark_verified_file_path, get_benchmark_training_audio_path)
from chunk_quality import validate_chunk_quality
from config_settings import load_app_config
from generate_script import LLMGenParams, process_chunk
from lmstudio_settings import get_lmstudio_status, get_remote_lmstudio_status
from lora_evidence import get_file_sha256
from utils import is_path_inside
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from review_script import check_text_loss, diff_entries, review_batch
from voicelab_settings import get_voice_lab_script_path
from functools import wraps
from benchmark_execution import (BENCHMARK_STATE, BenchmarkCancelled,
                                 run_benchmark_subprocess, get_cancellable_benchmark_client)
import generate_personas
from find_nicknames import find_nicknames


def get_remote_benchmark_command(ssh_alias, arguments):
    """Pass one shell-quoted command string through SSH's remote shell."""
    return ["ssh", ssh_alias, shlex.join(arguments)]


def get_benchmark_worker_command(arguments, target, ssh_alias):
    """Keep local argv intact or quote one remote argv using the shared rule."""
    return arguments if target == "local" else get_remote_benchmark_command(ssh_alias, arguments)


def run_benchmark_worker(command, marker, failure_message, *, timeout,
                         error_limit=4000, raise_failed=False, input=None):
    """Supervise one worker and interpret its result with the stage's policy."""
    options = {"capture_output": True, "text": True, "timeout": timeout, "check": False}
    if input is not None:
        options["input"] = input
    result = run_benchmark_subprocess(command, **options)
    return get_benchmark_worker_result(result, marker, failure_message,
                                       error_limit=error_limit, raise_failed=raise_failed)


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
        get_benchmark_verified_file_path(
            root_dir, fixture["ref_audio"], fixture["ref_audio_sha256"], "clone reference audio")
    if fixture.get("voice_type") == "lora" and root_dir:
        adapter_path = get_benchmark_directory_path(root_dir, fixture["adapter_path"])
        for filename, expected in fixture["adapter_artifact_sha256"].items():
            artifact_path = get_adapter_artifact_path(adapter_path, filename)
            if get_file_sha256(artifact_path) != expected:
                raise ValueError(f"LoRA adapter artifact hash changed: {filename}")
    return content


def _validate_lora_training_fixture(fixture, root_dir):
    keys = ("dataset_path", "metadata_sha256", "sample_count", "audio_sha256",
            "epochs", "seed", "lr", "lora_r", "lora_alpha", "grad_accum", "language")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    dataset_path = get_benchmark_directory_path(root_dir, fixture["dataset_path"])
    get_benchmark_verified_file_path(
        dataset_path, "metadata.jsonl", fixture["metadata_sha256"], "training metadata")
    for relative_path, expected in fixture["audio_sha256"].items():
        audio_path = get_benchmark_training_audio_path(dataset_path, relative_path)
        if get_file_sha256(audio_path) != expected:
            raise ValueError(f"training audio hash changed: {relative_path}")
    return content


def _validate_preparer_fixture(fixture, root_dir):
    keys = ("audio_path", "audio_sha256", "limit", "language", "model_revision")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    get_benchmark_verified_file_path(
        root_dir, fixture["audio_path"], fixture["audio_sha256"], "preparer audio")
    return content


def _validate_dedup_fixture(fixture, root_dir):
    keys = ("dataset_path", "metadata_sha256", "samples_per_volume",
            "audio_sha256", "model_id", "seed")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    from dedup_benchmark import get_dedup_dataset_path, get_dedup_audio_path
    dataset_path = get_dedup_dataset_path(root_dir, fixture["dataset_path"])
    metadata_path = get_benchmark_file_path(dataset_path, "metadata.jsonl")
    if get_file_sha256(metadata_path) != fixture["metadata_sha256"]:
        raise ValueError("dedup metadata hash changed")
    from dedup_benchmark import get_dedup_selected_entries, validate_dedup_audio_hash_coverage
    with open(metadata_path, encoding="utf-8") as handle:
        selected = get_dedup_selected_entries(
            [json.loads(line) for line in handle if line.strip()], fixture["samples_per_volume"])
    validate_dedup_audio_hash_coverage(selected, fixture["audio_sha256"])
    for relative_path, expected in fixture["audio_sha256"].items():
        audio_path = get_dedup_audio_path(dataset_path, relative_path)
        if get_file_sha256(audio_path) != expected:
            raise ValueError(f"dedup audio hash changed: {relative_path}")
    return content


def _validate_profiling_fixture(fixture, root_dir):
    keys = ("zip_path", "zip_sha256", "model_path", "model_sha256",
            "dataset_id", "seed")
    content = {key: fixture[key] for key in keys}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    for path_key in ("zip_path", "model_path"):
        get_benchmark_file_path(root_dir, fixture[path_key])

    from profiling_benchmark import validate_profiling_files
    validate_profiling_files(fixture, os.path.join(root_dir, fixture["zip_path"]),
                             os.path.join(root_dir, fixture["model_path"]))
    return content


def wrap_benchmark_cancellation(function):
    """Bind one stage's state without changing other concurrent invocations."""
    @wraps(function)
    def run(manifest, environment, report_path, state, *args, **kwargs):
        token = BENCHMARK_STATE.set(state)
        try:
            return function(manifest, environment, report_path, state, *args, **kwargs)
        except BenchmarkCancelled:
            report = load_resumable_benchmark_report(report_path, manifest, environment)
            apply_benchmark_cancellation(state, report, manifest, report_path)
            return report
        finally:
            BENCHMARK_STATE.reset(token)
    return run


def _run_tts_worker(payload, target, settings, root_dir, output_dir, ssh_alias):
    if target == "local":
        worker_payload = payload
    else:
        worker_payload = _stage_remote_tts_assets(payload, root_dir, ssh_alias)
    asset_root = worker_payload.pop("_asset_root", None) if target != "local" else None
    try:
        worker_payload = copy.deepcopy(worker_payload)
        encoded = get_encoded_worker_payload(worker_payload)
        if target == "local":
            command = get_benchmark_worker_command(
                [sys.executable, os.path.join(root_dir, "app", "tts_benchmark.py"),
                 "--payload", encoded, "--output-dir", output_dir], target, ssh_alias)
        else:
            remote_root = settings.get("remote_root")
            remote_python = settings.get("remote_python")
            if not remote_root or not remote_python or not ssh_alias:
                raise ValueError("Thunder TTS requires remote_root, remote_python, and SSH alias")
            arguments = [remote_python, os.path.join(remote_root, "app", "tts_benchmark.py"),
                         "--payload", encoded, "--output-dir", output_dir]
            if asset_root is not None:
                arguments.extend(["--asset-root", asset_root])
            command = get_benchmark_worker_command(arguments, target, ssh_alias)
        return run_benchmark_worker(command, 'TTS_BENCHMARK_RESULT=', 'TTS worker failed', timeout=3600, error_limit=2000, raise_failed=True)

    finally:
        if asset_root is not None:
            apply_remote_tts_asset_cleanup(ssh_alias, asset_root)


def get_valid_remote_tts_asset_root(path):
    if not isinstance(path, str) or not re.fullmatch(r"/tmp/alexandria-tts-benchmark-assets\.[A-Za-z0-9]{10}", path):
        raise ValueError("could not validate newly created remote asset directory")
    return path


def apply_remote_tts_asset_cleanup(ssh_alias, path):
    """Remove only this invocation's validated staging root, even on cancel."""
    path = get_valid_remote_tts_asset_root(path)
    apply_remote_benchmark_asset_cleanup(ssh_alias, path, "TTS")


def apply_remote_benchmark_asset_cleanup(ssh_alias, path, asset_kind):
    """Run bounded cleanup independently of cancellation, retaining primary errors."""
    primary_failure = sys.exc_info()[1]
    try:
        result = subprocess.run(get_remote_benchmark_command(ssh_alias, ["rm", "-rf", "--", path]),
                                capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("remote cleanup command failed")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        message = f"Remote {asset_kind} asset cleanup failed for {path}: {exc}"
        logging.getLogger(__name__).warning(message)
        state = BENCHMARK_STATE.get()
        if state is not None:
            state.setdefault("logs", []).append(message)
        if primary_failure is None:
            raise RuntimeError(message) from exc


def _stage_remote_tts_assets(payload, root_dir, ssh_alias):
    """Copy runtime assets to a newly created private remote temp directory."""
    if not ssh_alias:
        raise ValueError("Thunder SSH alias is required to stage TTS assets")
    staged = copy.deepcopy(payload)
    staged.pop("_asset_root", None)
    fixtures = staged.get("fixtures", [])
    clone_fixtures = [fixture for fixture in fixtures if fixture.get("voice_type") == "clone"]
    lora_fixtures = [fixture for fixture in fixtures if fixture.get("voice_type") == "lora"]
    if not clone_fixtures and not lora_fixtures:
        return staged
    for fixture in clone_fixtures:
        get_benchmark_file_path(root_dir, fixture["ref_audio"])
    for fixture in lora_fixtures:
        local_adapter = get_benchmark_directory_path(root_dir, fixture["adapter_path"])
        for filename in fixture["adapter_artifact_sha256"]:
            get_adapter_artifact_path(local_adapter, filename)
    command = "umask 077; mktemp -d /tmp/alexandria-tts-benchmark-assets.XXXXXXXXXX"
    mkdir = run_benchmark_subprocess(["ssh", ssh_alias, "bash -lc " + shlex.quote(command)],
                           capture_output=True, text=True, timeout=30, check=False)
    if mkdir.returncode:
        raise RuntimeError(mkdir.stderr.strip() or "could not create remote asset directory")
    lines = [line.strip() for line in mkdir.stdout.splitlines() if line.strip()]
    remote_dir = lines[-1] if lines else ""
    try:
        remote_dir = get_valid_remote_tts_asset_root(remote_dir)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    staged["_asset_root"] = remote_dir
    try:
        copied = set()
        for fixture in clone_fixtures:
            digest = fixture["ref_audio_sha256"]
            source = get_benchmark_file_path(root_dir, fixture["ref_audio"])
            remote_path = f"{remote_dir}/{digest}.wav"
            if digest not in copied:
                transfer = run_benchmark_subprocess(
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
                mkdir = run_benchmark_subprocess(get_remote_benchmark_command(ssh_alias, ["mkdir", "-p", "--", remote_adapter]),
                                       capture_output=True, text=True, timeout=30, check=False)
                if mkdir.returncode:
                    raise RuntimeError(mkdir.stderr.strip() or "could not create remote adapter directory")
                local_adapter = os.path.abspath(os.path.join(root_dir, fixture["adapter_path"]))
                for filename in hashes:
                    transfer = run_benchmark_subprocess(
                        ["scp", get_adapter_artifact_path(local_adapter, filename),
                         f"{ssh_alias}:{remote_adapter}/{filename}"], capture_output=True,
                        text=True, timeout=300, check=False)
                    if transfer.returncode:
                        raise RuntimeError(transfer.stderr.strip() or "LoRA adapter transfer failed")
                staged_adapters.add(adapter_digest)
            fixture["adapter_path"] = remote_adapter
        return staged
    except BaseException:
        apply_remote_tts_asset_cleanup(ssh_alias, remote_dir)
        raise


@wrap_benchmark_cancellation
def run_tts_generation_benchmark(manifest, environment, report_path, state,
                                 config_path, root_dir):
    """Run the same production CustomVoice worker locally or through SSH."""
    if manifest["stage"] != "tts_generation" or len(manifest["targets"]) != 1:
        raise ValueError("TTS runs require exactly one target")
    target = manifest["targets"][0]
    settings = manifest.get("settings") or {}
    for fixture in manifest["fixtures"]:
        _validate_tts_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    tts_config = dict(config.get("tts") or {})
    tts_config["max_new_tokens"] = settings.get(
        "max_new_tokens", tts_config.get("max_new_tokens", 2048))
    output_dir = (os.path.join(os.path.dirname(report_path), "audio",
                               os.path.splitext(os.path.basename(report_path))[0])
                  if target == "local" else settings.get("remote_output_dir",
                                                         "/tmp/alexandria-tts-benchmark"))
    thresholds = manifest.get("quality_thresholds") or {}
    def execute_batch(pending):
        payload = {"tts": tts_config, "fixtures": pending,
                   "repetitions": manifest["repetitions"]}
        cases = _run_tts_worker(payload, target, settings, root_dir, output_dir,
                               (config.get("llm_remote_ssh") or "").strip())
        for case in cases:
            metrics = case.get("metrics") or {}
            quality = bool(case["status"] == "passed"
                           and metrics.get("duration_seconds", 0) >= thresholds.get("min_duration_seconds", 0.1)
                           and metrics.get("silence_ratio", 1) <= thresholds.get("max_silence_ratio", 0.98)
                           and metrics.get("clipping_ratio", 1) <= thresholds.get("max_clipping_ratio", 0.01))
            yield {**case, "quality": {"passed": quality},
                   "status": "passed" if quality else "failed"}
    return run_benchmark_batch(manifest, environment, report_path, state, execute_batch)


def _run_lora_training_worker(fixture, target, settings, root_dir, ssh_alias):
    remote_source = None
    try:
        worker_fixture = copy.deepcopy(fixture)
        if target == "local":
            worker_fixture["root_dir"] = root_dir
            python_executable = sys.executable
            train_script = os.path.join(root_dir, "app", "train_lora.py")
            output_root = "/tmp/alexandria-lora-training-local"
            worker_script = os.path.join(root_dir, "app", "lora_training_benchmark.py")
        else:
            remote_root = settings.get("remote_root")
            python_executable = settings.get("remote_python")
            if not remote_root or not python_executable or not ssh_alias:
                raise ValueError("Thunder training requires remote_root, remote_python, and SSH alias")
            mkdir = run_benchmark_subprocess(get_remote_benchmark_command(
                ssh_alias, ["mktemp", "-d", "/tmp/alexandria-lora-training.XXXXXXXXXX"]),
                                   capture_output=True, text=True, timeout=30, check=False)
            if mkdir.returncode:
                raise RuntimeError(mkdir.stderr.strip() or "could not create remote training fixture")
            lines = [line.strip() for line in mkdir.stdout.splitlines() if line.strip()]
            candidate = lines[-1] if lines else ""
            if not re.fullmatch(r"/tmp/alexandria-lora-training\.[A-Za-z0-9]{10}", candidate):
                raise ValueError("could not validate newly created remote training directory")
            remote_source = candidate
            source_dir = os.path.join(root_dir, fixture["dataset_path"])
            files = ["metadata.jsonl", *fixture["audio_sha256"].keys()]
            for relative_path in files:
                remote_path = os.path.join(remote_source, relative_path)
                run_benchmark_subprocess(get_remote_benchmark_command(ssh_alias, ["mkdir", "-p", "--", os.path.dirname(remote_path)]),
                               capture_output=True, text=True, timeout=30, check=True)
                transfer = run_benchmark_subprocess(["scp", os.path.join(source_dir, relative_path),
                                           f"{ssh_alias}:{remote_path}"], capture_output=True,
                                          text=True, timeout=300, check=False)
                if transfer.returncode:
                    raise RuntimeError(transfer.stderr.strip() or "training fixture transfer failed")
            worker_fixture.update({"root_dir": "/tmp", "dataset_path": os.path.basename(remote_source)})
            output_root = "/tmp/alexandria-lora-training-output"
            worker_script = os.path.join(remote_root, "app", "lora_training_benchmark.py")
            train_script = os.path.join(remote_root, "app", "train_lora.py")
        payload = {"fixture": worker_fixture, "python": python_executable,
                   "train_script": train_script, "output_root": output_root}
        encoded = get_encoded_worker_payload(payload)
        worker_command = [python_executable, worker_script, "--payload", encoded]
        command = get_benchmark_worker_command(worker_command, target, ssh_alias)
        return run_benchmark_worker(command, 'LORA_TRAINING_BENCHMARK_RESULT=', 'training worker failed', timeout=7200)
    finally:
        if remote_source is not None:
            apply_remote_benchmark_asset_cleanup(ssh_alias, remote_source, "training")


def apply_benchmark_cancellation(state, report, manifest, report_path):
    """Preserve completed cases and mark unfinished tasks when cancel is queued."""
    if not state.get("cancel"):
        return False
    completed = get_benchmark_completed_cases(report)
    for task, fixture in zip(state.get("tasks", []), manifest["fixtures"]):
        if task.get("status") == "failed":
            continue
        task["status"] = ("done" if all((fixture["id"], repetition) in completed
                           for repetition in range(1, manifest["repetitions"] + 1))
                          else "cancelled")
    state["status"] = "cancelled"
    save_benchmark_report(report_path, report)
    return True


def get_benchmark_completed_cases(report):
    return {(case["fixture_id"], case["repetition"]) for case in report["cases"]}


def is_benchmark_progress_stage(stage):
    return stage in ("script_generation", "script_review")


def save_benchmark_case(report_path, report, case, *, log_state=None):
    """Publish a detached next report; input report and worker case stay unchanged."""
    updated = {**report, "cases": [*report["cases"], copy.deepcopy(case)]}
    save_benchmark_report(report_path, updated)
    if log_state is not None:
        log_state["logs"].append(
            f"{case['fixture_id']} repetition {case['repetition']}: {case['status']}")
    return updated


def apply_benchmark_completion(state, report, manifest, report_path):
    """Keep cancellation authoritative before marking all tasks complete."""
    if apply_benchmark_cancellation(state, report, manifest, report_path):
        return
    expected = {(fixture["id"], repetition) for fixture in manifest["fixtures"]
                for repetition in range(1, manifest["repetitions"] + 1)}
    missing = expected - get_benchmark_completed_cases(report)
    if missing:
        raise RuntimeError(f"missing benchmark cases: {sorted(missing)}")
    for task in state["tasks"]:
        task["status"] = "done"
    state["status"] = "complete"


def run_benchmark_repetitions(manifest, environment, report_path, state, execute_case, *, report=None):
    """Own resume filtering, durable case publication and stage completion."""
    report = (copy.deepcopy(report) if report is not None else
              load_resumable_benchmark_report(report_path, manifest, environment))
    completed = get_benchmark_completed_cases(report)
    track_progress = is_benchmark_progress_stage(manifest["stage"])
    for fixture_index, fixture in enumerate(manifest["fixtures"]):
        if track_progress:
            state["current_task_idx"] = fixture_index
            state["tasks"][fixture_index]["status"] = "running"
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            if apply_benchmark_cancellation(state, report, manifest, report_path):
                return report
            result = execute_case(fixture, repetition)
            case = {"fixture_id": fixture["id"], "repetition": repetition, **result}
            report = save_benchmark_case(report_path, report, case,
                                         log_state=state if track_progress else None)
        if track_progress:
            state["tasks"][fixture_index]["status"] = "done"
    apply_benchmark_completion(state, report, manifest, report_path)
    return report


def run_benchmark_batch(manifest, environment, report_path, state, execute_batch, *, report=None):
    """Preserve warm/remote batching with the same durable lifecycle as local cases."""
    report = (copy.deepcopy(report) if report is not None else
              load_resumable_benchmark_report(report_path, manifest, environment))
    pending = _pending_fixtures_with_repetitions(
        manifest["fixtures"], manifest["repetitions"], get_benchmark_completed_cases(report))
    if apply_benchmark_cancellation(state, report, manifest, report_path):
        return report
    if pending:
        for case in execute_batch(pending):
            report = save_benchmark_case(report_path, report, case,
                log_state=state if is_benchmark_progress_stage(manifest["stage"]) else None)
    apply_benchmark_completion(state, report, manifest, report_path)
    return report


@wrap_benchmark_cancellation
def run_lora_training_benchmark(manifest, environment, report_path, state,
                                config_path, root_dir):
    """Run production LoRA training calibration locally or on Thunder."""
    target = manifest["targets"][0]
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    pending = _pending_fixtures_with_repetitions(
        manifest["fixtures"], manifest["repetitions"], get_benchmark_completed_cases(report))
    for fixture in pending:
        _validate_lora_training_fixture(fixture, root_dir)
    config = load_app_config(config_path) if pending else {}
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_lora_training_worker(fixture, target, manifest.get('settings') or {}, root_dir, (config.get('llm_remote_ssh') or '').strip()),
        report=report)


def _run_preparer_worker(fixture, target, settings, root_dir, ssh_alias):
    worker_fixture = copy.deepcopy(fixture)
    if target == "local":
        python_executable = settings.get("local_python")
        if not python_executable:
            raise ValueError("local preparer benchmark requires local_python")
        worker_fixture["root_dir"] = root_dir
        preparer_script = os.path.join(root_dir, "alexandria_preparer_rocm_compatible.py")
        worker_script = os.path.join(root_dir, "app", "preparer_benchmark.py")
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder preparer requires remote_root, remote_python, and SSH alias")
        remote_audio = f"/tmp/alexandria-preparer-{fixture['audio_sha256']}.wav"
        transfer = run_benchmark_subprocess(
            ["scp", os.path.join(root_dir, fixture["audio_path"]),
             f"{ssh_alias}:{remote_audio}"], capture_output=True, text=True,
            timeout=300, check=False)
        if transfer.returncode:
            raise RuntimeError(transfer.stderr.strip() or "preparer audio transfer failed")
        worker_fixture.update({"root_dir": "/", "audio_path": remote_audio.lstrip("/")})
        preparer_script = os.path.join(remote_root, "alexandria_preparer_rocm_compatible.py")
        worker_script = os.path.join(remote_root, "app", "preparer_benchmark.py")
    payload = {"fixture": worker_fixture, "python": python_executable,
               "preparer_script": preparer_script}
    encoded = get_encoded_worker_payload(payload)
    worker_command = [python_executable, worker_script, "--payload", encoded]
    command = get_benchmark_worker_command(worker_command, target, ssh_alias)
    return run_benchmark_worker(command, 'PREPARER_BENCHMARK_RESULT=', 'preparer worker failed', timeout=3600)


@wrap_benchmark_cancellation
def run_preparer_benchmark(manifest, environment, report_path, state,
                           config_path, root_dir):
    """Run the preparer's production ASR phase locally or on Thunder."""
    target = manifest["targets"][0]
    for fixture in manifest["fixtures"]:
        _validate_preparer_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_preparer_worker(fixture, target, manifest.get('settings') or {}, root_dir, (config.get('llm_remote_ssh') or '').strip()))


def _run_dedup_worker(fixture, target, settings, root_dir, ssh_alias):
    worker_fixture = copy.deepcopy(fixture)
    if target == "local":
        python_executable = settings.get("local_python")
        if not python_executable:
            raise ValueError("local dedup benchmark requires local_python")
        worker_fixture["root_dir"] = root_dir
        analysis_script = get_voice_lab_script_path(root_dir, "voice_analysis.py")
        worker_script = os.path.join(root_dir, "app", "dedup_benchmark.py")
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
            mkdir = run_benchmark_subprocess(get_remote_benchmark_command(ssh_alias, ["mkdir", "-p", "--",
                                    os.path.dirname(remote_path)]), capture_output=True,
                                   text=True, timeout=30, check=False)
            if mkdir.returncode:
                raise RuntimeError(mkdir.stderr.strip() or "could not create remote dedup fixture")
            transfer = run_benchmark_subprocess(["scp", os.path.join(source_dir, relative_path),
                                       f"{ssh_alias}:{remote_path}"], capture_output=True,
                                      text=True, timeout=300, check=False)
            if transfer.returncode:
                raise RuntimeError(transfer.stderr.strip() or "dedup fixture transfer failed")
        worker_fixture.update({"root_dir": "/tmp", "dataset_path": os.path.basename(remote_source)})
        analysis_script = get_voice_lab_script_path(remote_root, "voice_analysis.py")
        worker_script = os.path.join(remote_root, "app", "dedup_benchmark.py")
    payload = {"fixture": worker_fixture, "python": python_executable,
               "analysis_script": analysis_script}
    encoded = get_encoded_worker_payload(payload)
    worker_command = [python_executable, worker_script, "--payload", encoded]
    command = get_benchmark_worker_command(worker_command, target, ssh_alias)
    return run_benchmark_worker(command, 'DEDUP_BENCHMARK_RESULT=', 'dedup worker failed', timeout=3600)


@wrap_benchmark_cancellation
def run_dedup_benchmark(manifest, environment, report_path, state,
                        config_path, root_dir):
    """Run production Voice Lab dedup locally or on Thunder."""
    target = manifest["targets"][0]
    for fixture in manifest["fixtures"]:
        _validate_dedup_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_dedup_worker(fixture, target, manifest.get('settings') or {}, root_dir, (config.get('llm_remote_ssh') or '').strip()))


def _run_profiling_worker(fixture, target, settings, root_dir, ssh_alias):
    zip_path = os.path.join(root_dir, fixture["zip_path"])
    if target == "local":
        python_executable = settings.get("local_python")
        worker_root = root_dir
        worker_zip = zip_path
        model_path = os.path.join(root_dir, fixture["model_path"])
        worker_script = os.path.join(root_dir, "app", "profiling_benchmark.py")
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        model_path = settings.get("remote_model_path")
        if not remote_root or not python_executable or not model_path or not ssh_alias:
            raise ValueError("Thunder profiling requires remote_root, remote_python, remote_model_path, and SSH alias")
        worker_root = remote_root
        worker_zip = f"/tmp/alexandria-profiling-{fixture['zip_sha256']}.zip"
        transfer = run_benchmark_subprocess(["scp", zip_path, f"{ssh_alias}:{worker_zip}"],
                                  capture_output=True, text=True, timeout=300, check=False)
        if transfer.returncode:
            raise RuntimeError(transfer.stderr.strip() or "profiling fixture transfer failed")
        verify = run_benchmark_subprocess(get_remote_benchmark_command(ssh_alias, ["sha256sum", "--", model_path]),
                                capture_output=True, text=True, timeout=300, check=False)
        lines = [line for line in verify.stdout.splitlines() if line.strip()]
        observed = lines[-1].split()[0] if verify.returncode == 0 and lines else ""
        if observed != fixture["model_sha256"]:
            raise ValueError("Thunder profiling model hash does not match the fixture")
        worker_script = os.path.join(remote_root, "app", "profiling_benchmark.py")
    if not python_executable:
        raise ValueError("profiling benchmark requires a Python executable")
    payload = {"fixture": fixture, "root_dir": worker_root,
               "zip_path": worker_zip, "model_path": model_path}
    encoded = get_encoded_worker_payload(payload)
    worker_command = [python_executable, worker_script, "--payload", encoded]
    command = get_benchmark_worker_command(worker_command, target, ssh_alias)
    return run_benchmark_worker(command, 'PROFILING_BENCHMARK_RESULT=', 'profiling worker failed', timeout=3600)


@wrap_benchmark_cancellation
def run_profiling_benchmark(manifest, environment, report_path, state,
                            config_path, root_dir):
    """Run production Voice Lab acoustics and GGUF description generation."""
    target = manifest["targets"][0]
    for fixture in manifest["fixtures"]:
        _validate_profiling_fixture(fixture, root_dir)
    config = load_app_config(config_path)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_profiling_worker(fixture, target, manifest.get('settings') or {}, root_dir, (config.get('llm_remote_ssh') or '').strip()))


def _run_naming_worker(fixture, target, settings, root_dir, ssh_alias):
    if _hash_entries({"entries": fixture["entries"]}) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    if target == "local":
        python_executable = settings.get("local_python") or sys.executable
        script = get_voice_lab_script_path(root_dir, "name_voices.py")
        worker = os.path.join(root_dir, "app", "naming_benchmark.py")
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python") or "python3"
        if not remote_root or not ssh_alias:
            raise ValueError("Thunder naming requires remote_root and SSH alias")
        script = get_voice_lab_script_path(remote_root, "name_voices.py")
        worker = os.path.join(remote_root, "app", "naming_benchmark.py")
    payload = {"fixture": fixture, "python": python_executable, "script": script}
    encoded = get_encoded_worker_payload(payload)
    worker_command = [python_executable, worker, "--payload", encoded]
    command = get_benchmark_worker_command(worker_command, target, ssh_alias)
    return run_benchmark_worker(command, 'NAMING_BENCHMARK_RESULT=', 'naming worker failed', timeout=120)


@wrap_benchmark_cancellation
def run_naming_benchmark(manifest, environment, report_path, state,
                         config_path, root_dir):
    """Run deterministic production Voice Lab naming locally or remotely."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_naming_worker(fixture, target, manifest.get('settings') or {}, root_dir, (config.get('llm_remote_ssh') or '').strip()))


def _validate_persona_fixture(fixture):
    content = {key: fixture[key] for key in ("entries", "speakers", "batch_size")}
    validate_persona_speakers(content["speakers"])
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
                client, model_name, prompt, batch, batch_number, context_length,
                batch_start=batch_start, allowed_speakers=speakers)
            discovery_calls += 1
            generate_personas._write_batch_character_refs(
                ref_dir, characters, speakers, batch_number)

        def capture_preview(_root, _engine, _config, speaker, description, ref_text):
            captures[speaker] = {"description": description, "ref_text": ref_text}
            return True
        for speaker in speakers:
            generate_personas._compile_persona(
                client, model_name, None, voice_config, root, ref_dir, speaker,
                samples, generate_personas.PERSONA_SYSTEM_PROMPT,
                generate_personas.PERSONA_ADVANCED_PROMPT, context_length,
                preview_saver=capture_preview)
            compile_calls += 1
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


@wrap_benchmark_cancellation
def run_persona_generation_benchmark(manifest, environment, report_path, state,
                                     config_path, root_dir):
    """Run advanced persona discovery and compilation against configured LLM.
    See run_script_generation_benchmark's docstring for the local-in-process
    vs thunder-remote-worker split."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, target)
    client = get_cancellable_benchmark_client(
        make_llm_client(llm, timeout=llm_timeout_seconds()))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    for fixture in manifest["fixtures"]:
        _validate_persona_fixture(fixture)

    if target == "thunder":
        def execute_batch(pending):
            payload = {"llm_config": get_remote_llm_worker_profile(llm), "model_name": llm["model_name"], "context_length": status.get("context_length"), "fixtures": pending}
            return _run_llm_worker("persona_generation", payload, manifest.get("settings") or {},
                                   (config.get("llm_remote_ssh") or "").strip())
        return run_benchmark_batch(manifest, environment, report_path, state,
                                    execute_batch, report=report)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_persona_case(fixture, client, llm["model_name"], status.get("context_length")), report=report)


def _validate_nickname_fixture(fixture):
    content = {key: fixture[key] for key in
               ("entries", "expected_aliases", "existing_aliases")}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    return content


def _run_nickname_case(fixture, repetition, client, model_name, context_length, concurrency):
    """Run one nickname-detection fixture/repetition and score it - shared
    by the local in-process loop and llm_benchmark_worker.py's remote loop."""
    started = time.monotonic()
    aliases, evidence = find_nicknames(
        client, model_name, fixture["entries"],
        existing_aliases=fixture["existing_aliases"],
        context_length=context_length, concurrency=concurrency)
    expected = fixture["expected_aliases"]
    correct = sum(aliases.get(key) == value for key, value in expected.items())
    precision = correct / len(aliases) if aliases else (0.0 if expected else 1.0)
    recall = correct / len(expected) if expected else 1.0
    evidence_by_label = {str(key).strip().lower(): value
                         for key, value in evidence.items()}
    evidence_coverage = (sum(bool(evidence_by_label.get(key.lower()))
                             for key in expected) / len(expected) if expected else 1.0)
    quality = {"passed": precision == 1.0 and recall == 1.0
               and evidence_coverage == 1.0,
               "precision": precision, "recall": recall,
               "evidence_coverage": evidence_coverage}
    return {"fixture_id": fixture["id"], "repetition": repetition,
            "status": "passed" if quality["passed"] else "failed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "context_length": context_length, "concurrency": concurrency,
            "aliases": aliases, "evidence": evidence, "quality": quality}


@wrap_benchmark_cancellation
def run_nickname_detection_benchmark(manifest, environment, report_path, state,
                                     config_path, root_dir):
    """Run production context-aware alias discovery against configured LLM.
    See run_script_generation_benchmark's docstring for the local-in-process
    vs thunder-remote-worker split."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, target)
    client = get_cancellable_benchmark_client(
        make_llm_client(llm, timeout=llm_timeout_seconds()))
    context_length = status.get("context_length") or 4096
    concurrency = status.get("parallel") or 1
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    for fixture in manifest["fixtures"]:
        _validate_nickname_fixture(fixture)

    if target == "thunder":
        def execute_batch(pending):
            payload = {"llm_config": get_remote_llm_worker_profile(llm), "model_name": llm["model_name"], "context_length": context_length, "concurrency": concurrency, "fixtures": pending}
            return _run_llm_worker("nickname_detection", payload, manifest.get("settings") or {},
                                   (config.get("llm_remote_ssh") or "").strip())
        return run_benchmark_batch(manifest, environment, report_path, state,
                                    execute_batch, report=report)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_nickname_case(fixture, repetition, client, llm["model_name"], context_length, concurrency), report=report)


def _validate_export_fixture(fixture, root_dir):
    content = {key: fixture[key] for key in
               ("chunks", "audio_sha256", "per_chunk_chapters")}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    for relative_path, expected in fixture["audio_sha256"].items():
        path = get_benchmark_file_path(root_dir, relative_path)
        if get_file_sha256(path) != expected:
            raise ValueError(f"export audio hash changed: {relative_path}")


def _run_export_worker(stage, fixture, target, settings, root_dir, ssh_alias):
    _validate_export_fixture(fixture, root_dir)
    if target == "local":
        python_executable = settings.get("local_python") or sys.executable
        source_root = root_dir
        worker = os.path.join(root_dir, "app", "export_benchmark.py")
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder export requires remote_root, remote_python, and SSH alias")
        source_root = f"/tmp/alexandria-export-{fixture['sha256']}"
        for relative_path in fixture["audio_sha256"]:
            remote_path = os.path.join(source_root, relative_path)
            run_benchmark_subprocess(get_remote_benchmark_command(ssh_alias, ["mkdir", "-p", "--", os.path.dirname(remote_path)]),
                           capture_output=True, text=True, timeout=30, check=True)
            run_benchmark_subprocess(["scp", os.path.join(root_dir, relative_path),
                            f"{ssh_alias}:{remote_path}"], capture_output=True,
                           text=True, timeout=300, check=True)
        worker = os.path.join(remote_root, "app", "export_benchmark.py")
    payload = {"stage": stage, "fixture": fixture, "source_root": source_root}
    encoded = get_encoded_worker_payload(payload)
    worker_command = [python_executable, worker, "--payload", encoded]
    command = get_benchmark_worker_command(worker_command, target, ssh_alias)
    return run_benchmark_worker(command, 'EXPORT_BENCHMARK_RESULT=', 'export worker failed', timeout=900)


@wrap_benchmark_cancellation
def run_export_benchmark(manifest, environment, report_path, state,
                         config_path, root_dir):
    """Run Audacity or M4B production exports locally or on Thunder."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_export_worker(manifest['stage'], fixture, target, manifest.get('settings') or {}, root_dir, (config.get('llm_remote_ssh') or '').strip()))


def _run_dataset_builder_worker(fixture, target, settings, root_dir, ssh_alias,
                                tts_config):
    content = {key: fixture[key] for key in
               ("description", "samples", "global_seed", "seeds")}
    if _hash_entries(content) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture.get('id')} hash changed")
    if target == "local":
        python_executable = settings.get("local_python") or sys.executable
        worker = os.path.join(root_dir, "app", "dataset_builder_benchmark.py")
    else:
        remote_root = settings.get("remote_root")
        python_executable = settings.get("remote_python")
        if not remote_root or not python_executable or not ssh_alias:
            raise ValueError("Thunder Dataset Builder requires remote_root, remote_python, and SSH alias")
        worker = os.path.join(remote_root, "app", "dataset_builder_benchmark.py")
    payload = {"fixture": fixture, "tts": tts_config}
    encoded = get_encoded_worker_payload(payload)
    worker_command = [python_executable, worker, "--payload", encoded]
    command = get_benchmark_worker_command(worker_command, target, ssh_alias)
    return run_benchmark_worker(command, 'DATASET_BUILDER_BENCHMARK_RESULT=', 'Dataset Builder worker failed', timeout=3600)


@wrap_benchmark_cancellation
def run_dataset_builder_benchmark(manifest, environment, report_path, state,
                                  config_path, root_dir):
    """Run the production Dataset Builder batch route locally or on Thunder."""
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_dataset_builder_worker(fixture, target, manifest.get('settings') or {}, root_dir, (config.get('llm_remote_ssh') or '').strip(), config.get('tts') or {}))


class _HashingFixtureReader(io.RawIOBase):
    """Hash the bounded raw reads consumed by the UTF-8 text decoder."""
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.digest = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.source.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
        return count


def get_text_fixture_sources(fixtures, uploads_dir):
    """Read each source once, retaining only the selected fixture texts."""
    sources = {}
    for fixture in fixtures:
        path = os.path.abspath(fixture.get("path") or "")
        if not is_path_inside(path, uploads_dir) or not os.path.isfile(path):
            raise ValueError(f"fixture {fixture.get('id')} must be a file inside uploads")
        sources.setdefault(path, []).append(fixture)
    texts = {}
    for path, source_fixtures in sources.items():
        with open(path, "rb") as source:
            reader = _HashingFixtureReader(source)
            try:
                with io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8",
                                      newline="") as decoded:
                    text = decoded.read()
            except UnicodeDecodeError as exc:
                raise ValueError(f"fixture {source_fixtures[0]['id']} is not UTF-8 text") from exc
            digest = reader.digest.hexdigest()
        for fixture in source_fixtures:
            expected = (fixture.get("source_sha256") if fixture.get("chunk_number") is not None
                        else fixture["sha256"])
            if digest != expected:
                label = "source hash" if fixture.get("chunk_number") is not None else "hash"
                raise ValueError(f"fixture {fixture['id']} {label} changed")
        chunk_groups = {}
        for fixture in source_fixtures:
            if fixture.get("chunk_number") is None:
                if not text.strip():
                    raise ValueError(f"fixture {fixture['id']} is empty")
                texts[fixture["id"]] = text
            else:
                chunk_groups.setdefault(fixture.get("chunk_size", 6000), []).append(fixture)
        for chunk_size, chunk_fixtures in chunk_groups.items():
            chunks = get_normalized_source_chunks(text, chunk_size)
            for fixture in chunk_fixtures:
                chunk_number = fixture["chunk_number"]
                if not isinstance(chunk_number, int) or not 1 <= chunk_number <= len(chunks):
                    raise ValueError(f"fixture {fixture['id']} chunk_number is out of range")
                selected = chunks[chunk_number - 1]
                if hashlib.sha256(selected.encode("utf-8")).hexdigest() != fixture["sha256"]:
                    raise ValueError(f"fixture {fixture['id']} chunk hash changed")
                texts[fixture["id"]] = selected
            del chunks
        del text
    return texts


def _load_text_fixture(fixture, uploads_dir):
    return get_text_fixture_sources([fixture], uploads_dir)[fixture["id"]]


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


def _pending_fixtures_with_repetitions(fixtures, repetitions, completed):
    """Return detached pending fixtures for every warm or remote batch."""
    pending = []
    for fixture in fixtures:
        missing = [repetition for repetition in range(1, repetitions + 1)
                   if (fixture["id"], repetition) not in completed]
        if missing:
            pending_fixture = dict(fixture)
            pending_fixture["repetition_numbers"] = missing
            pending.append(pending_fixture)
    return pending


def _remote_llm_base_url(llm):
    """The URL llm_benchmark_worker.py should use FROM the remote host
    itself - localhost, not the public forwarding URL. Reusing the
    forwarding URL here would route the worker's calls back out through the
    internet a second time, defeating the entire point of running it on
    Thunder in the first place (see docs/guides/THUNDER_COMPUTE.md's network_rtt_seconds
    confound)."""
    port = urlparse(llm.get("base_url") or "").port or 1234
    return f"http://localhost:{port}/v1"


def get_remote_llm_worker_profile(llm):
    """Copy the active profile, resolving its key on the orchestrating host."""
    return {**llm, "base_url": _remote_llm_base_url(llm),
            "api_key": resolve_api_key(llm.get("api_key", "local"))}


def _run_llm_worker(stage, payload, settings, ssh_alias):
    """Run llm_benchmark_worker.py on the remote host for one batch of
    pending fixtures/repetitions, returning the same case-dict shape the
    local in-process path produces. JSON travels on SSH standard input so
    credentials and fixture text do not appear in process arguments."""
    remote_root = settings.get("remote_root")
    remote_python = settings.get("remote_python")
    if not remote_root or not remote_python or not ssh_alias:
        raise ValueError(f"Thunder {stage} requires remote_root, remote_python, and SSH alias")
    command = get_remote_benchmark_command(ssh_alias, [
        remote_python, os.path.join(remote_root, "app", "llm_benchmark_worker.py"),
        "--stage", stage, "--payload-stdin"])
    return run_benchmark_worker(command, 'LLM_BENCHMARK_RESULT=', 'LLM benchmark worker failed', timeout=3600, error_limit=2000, raise_failed=True, input=json.dumps(payload))


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


def _run_script_generation_case(fixture, text, repetition, client, model_name,
                                params, max_retries):
    """Run one script-generation fixture/repetition and score it - shared by
    the local in-process loop and llm_benchmark_worker.py's remote loop, so
    scoring logic exists in exactly one place for both (Rule 15)."""
    attempts = []
    started = time.monotonic()
    entries = process_chunk(
        client, model_name, text, fixture.get("chunk_number", 1),
        fixture.get("total_chunks", 1), params,
        previous_entries=fixture.get("previous_entries") or None,
        max_retries=max_retries,
        attempt_observer=attempts.append)
    quality = validate_chunk_quality(text, entries)
    return {"fixture_id": fixture["id"], "repetition": repetition,
            "status": "passed" if entries and quality["passed"] else "failed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "entry_count": len(entries), "attempts": attempts, "quality": quality}


@wrap_benchmark_cancellation
def run_script_generation_benchmark(manifest, environment, report_path, state,
                                    config_path, uploads_dir):
    """Run script-generation cases and persist after every repetition.

    target=="local" runs in-process against localhost, same as always.
    target=="thunder" dispatches the whole pending batch to
    llm_benchmark_worker.py over SSH instead of calling the remote LM Studio
    endpoint once per case from here - see docs/guides/THUNDER_COMPUTE.md's confounds
    section on why per-call network round trips were baked into every prior
    Thunder measurement.
    """
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
    client = get_cancellable_benchmark_client(
        make_llm_client(llm, timeout=llm_timeout_seconds()))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    max_retries = manifest.get("settings", {}).get("max_retries", 0)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("script-generation max_retries must be a non-negative integer")

    texts = get_text_fixture_sources(manifest["fixtures"], uploads_dir)

    if target == "thunder":
        def execute_batch(pending):
            payload = {"llm_config": get_remote_llm_worker_profile(llm), "model_name": model_name, "max_retries": max_retries, "params": dataclasses.asdict(params), "fixtures": [{**fixture, "text": texts[fixture["id"]]} for fixture in pending]}
            return _run_llm_worker("script_generation", payload, manifest.get("settings") or {},
                                   (config.get("llm_remote_ssh") or "").strip())
        return run_benchmark_batch(manifest, environment, report_path, state,
                                    execute_batch, report=report)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_script_generation_case(fixture, texts[fixture["id"]], repetition, client, model_name, params, max_retries), report=report)


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


def _run_script_review_case(fixture, original, repetition, client, model_name,
                            params, max_retries, lower, upper):
    """Run one script-review fixture/repetition and score it - shared by the
    local in-process loop and llm_benchmark_worker.py's remote loop."""
    attempts = []
    started = time.monotonic()
    corrected = review_batch(
        client, model_name, original, 1, 1, params,
        previous_tail=fixture.get("previous_tail") or None,
        max_retries=max_retries, attempt_observer=attempts.append)
    corrected = corrected or []
    text_ok, _, _, ratio = check_text_loss(
        original, corrected, threshold=lower, upper_bound=upper)
    structural_ok = bool(corrected) and all(
        isinstance(entry, dict) and isinstance(entry.get("text"), str)
        and isinstance(entry.get("speaker"), str) for entry in corrected)
    return {"fixture_id": fixture["id"], "repetition": repetition,
            "status": "passed" if text_ok and structural_ok else "failed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "entry_count": len(corrected), "attempts": attempts,
            "quality": {"passed": text_ok and structural_ok,
                        "word_ratio": round(ratio, 4),
                        "text_loss_passed": text_ok,
                        "structural_passed": structural_ok},
            "changes": diff_entries(original, corrected)}


@wrap_benchmark_cancellation
def run_script_review_benchmark(manifest, environment, report_path, state,
                                config_path, scripts_dir):
    """Run production review batches and persist deterministic quality
    metrics. See run_script_generation_benchmark's docstring for the
    local-in-process vs thunder-remote-worker split."""
    if manifest["stage"] != "script_review" or len(manifest["targets"]) != 1:
        raise ValueError("script-review runs require exactly one target")
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, target)
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
    client = get_cancellable_benchmark_client(
        make_llm_client(llm, timeout=llm_timeout_seconds()))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    report["network_rtt_seconds"] = _measure_llm_network_rtt(client)
    save_benchmark_report(report_path, report)
    max_retries = manifest.get("settings", {}).get("max_retries", 0)
    thresholds = manifest.get("quality_thresholds") or {}
    lower = thresholds.get("word_ratio_min", 0.95)
    upper = thresholds.get("word_ratio_max", 1.05)

    originals = {fixture["id"]: _load_review_fixture(fixture, scripts_dir)
                for fixture in manifest["fixtures"]}

    if target == "thunder":
        def execute_batch(pending):
            payload = {"llm_config": get_remote_llm_worker_profile(llm), "model_name": llm["model_name"], "max_retries": max_retries, "params": dataclasses.asdict(params), "word_ratio_min": lower, "word_ratio_max": upper, "fixtures": [{**fixture, "original": originals[fixture["id"]]} for fixture in pending]}
            return _run_llm_worker("script_review", payload, manifest.get("settings") or {},
                                   (config.get("llm_remote_ssh") or "").strip())
        return run_benchmark_batch(manifest, environment, report_path, state,
                                    execute_batch, report=report)
    return run_benchmark_repetitions(
        manifest, environment, report_path, state,
        lambda fixture, repetition: _run_script_review_case(fixture, originals[fixture["id"]], repetition, client, llm["model_name"], params, max_retries, lower, upper), report=report)
