#!/usr/bin/env python3
"""Generate fixed LoRA probes and write resumable, warning-only evaluations."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

import librosa
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from adapter_artifacts import AdapterValidationError, validate_adapter_artifacts
from adapter_checkpoint_transaction import (ensure_adapter_checkpoint,
    ensure_adapter_generation_snapshot, validate_adapter_checkpoint_generation)
from audio_validation import GeneratedAudioError, remove_stale_audio, validate_finite_audio_values
from config_settings import load_app_config
from lora_evidence import (EVALUATION_EVIDENCE_VERSION, get_evidence_error,
                           get_adapter_generation_sha256, get_adapter_generation_file_identity,
                           get_evaluation_spec_sha256, get_file_sha256, get_tts_model_settings)
from device_utils import normalize_device, resolve_device
from tts import TTSEngine
from utils import atomic_json_write, is_path_inside


EVALUATION_VERSION = EVALUATION_EVIDENCE_VERSION
PROBES = (
    ("narration", "The ancient library stood quietly beneath a sky filled with stars."),
    ("dialogue", "I understand the danger, but we cannot abandon them now."),
)
EVALUATION_SEED = 20260717


def apply_evaluation_seed(seed: int) -> None:
    import torch
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def get_evaluation_directory(root: str, name: str) -> str:
    """Resolve one evaluation directory name inside its configured root."""
    if (not isinstance(name, str) or not name or name in (".", "..") or
            os.path.isabs(name) or "/" in name or "\\" in name):
        raise ValueError("evaluation directory ID must be a directory name")
    path = os.path.realpath(os.path.join(root, name))
    if path == os.path.realpath(root) or not is_path_inside(path, root):
        raise ValueError("evaluation directory is outside its root")
    return path


def get_checkpoint_sha256(checkpoint_dir: str) -> str:
    return get_file_sha256(os.path.join(checkpoint_dir, "adapter_model.safetensors"))


def partition_unique_candidates(adapter_dir: str, candidates_root: str, *, production_snapshot=None):
    if not is_path_inside(candidates_root, adapter_dir):
        raise ValueError("evaluation candidates are outside the adapter")
    if (production_snapshot is not None and len(production_snapshot) == 3
            and production_snapshot[1] == get_adapter_generation_file_identity(adapter_dir)):
        production_generation, _identity, production_hash = production_snapshot
    else:
        production_hash = get_checkpoint_sha256(adapter_dir)
        production_generation = get_adapter_generation_sha256(adapter_dir)
    checkpoint_hashes = {production_generation: "production"}
    unique = []
    duplicates = []
    skipped = []
    if not os.path.isdir(candidates_root):
        return production_hash, unique, duplicates, skipped
    for candidate_id in sorted(os.listdir(candidates_root)):
        candidate_dir = get_evaluation_directory(candidates_root, candidate_id)
        if not os.path.isdir(candidate_dir):
            continue
        try:
            validate_adapter_checkpoint_generation(candidate_dir)
            candidate_hash = get_checkpoint_sha256(candidate_dir)
            candidate_generation = get_adapter_generation_sha256(candidate_dir)
        except (AdapterValidationError, OSError, ValueError, GeneratedAudioError) as error:
            skipped.append({"id": candidate_id, "status": "skipped_incomplete",
                            "reason": str(error)})
            continue
        duplicate_of = checkpoint_hashes.get(candidate_generation)
        if duplicate_of:
            duplicates.append({"id": candidate_id, "duplicate_of": duplicate_of,
                               "sha256": candidate_hash,
                               "adapter_generation_sha256": candidate_generation})
        else:
            checkpoint_hashes[candidate_generation] = candidate_id
            unique.append((candidate_id, candidate_dir, candidate_hash))
    return production_hash, unique, duplicates, skipped


THRESHOLDS = {
    "speaker_similarity_min": 0.55,
    "silence_ratio_max": 0.35,
    "clipping_ratio_max": 0.001,
}


def get_audio_metrics(path: str) -> dict[str, float]:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    validate_finite_audio_values(audio, "decoded evaluation PCM")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if not len(audio):
        raise ValueError("generated audio is empty")
    absolute = np.abs(audio)
    metrics = {
        "duration_seconds": len(audio) / sample_rate,
        "rms": float(np.sqrt(np.mean(np.square(audio)))),
        "silence_ratio": float(np.mean(absolute < 0.005)),
        "clipping_ratio": float(np.mean(absolute >= 0.99)),
    }
    validate_finite_audio_values(list(metrics.values()), "evaluation metrics")
    return metrics


def get_warnings(metrics: dict) -> list[str]:
    validate_finite_audio_values(list(metrics.values()), "evaluation metrics")
    warnings = []
    if metrics["speaker_similarity"] < THRESHOLDS["speaker_similarity_min"]:
        warnings.append("low_speaker_similarity")
    if metrics["silence_ratio"] > THRESHOLDS["silence_ratio_max"]:
        warnings.append("excess_silence")
    if metrics["clipping_ratio"] > THRESHOLDS["clipping_ratio_max"]:
        warnings.append("clipping")
    return warnings


def load_embedding_audio(path: str):
    import torch
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)
    peak = np.max(np.abs(audio)) if len(audio) else 0.0
    if peak:
        audio = audio / peak
    return torch.from_numpy(np.asarray(audio, dtype=np.float32)).unsqueeze(0)


def get_speaker_similarity(model, reference_path: str, probe_path: str,
                           device: str) -> float:
    import torch
    with torch.no_grad():
        reference = model.encode_batch(load_embedding_audio(reference_path).to(device)).flatten()
        probe = model.encode_batch(load_embedding_audio(probe_path).to(device)).flatten()
        return float(torch.nn.functional.cosine_similarity(reference, probe, dim=0).cpu())


def is_complete_evaluation(result: dict, adapter_dir: str, synthesis_settings=None) -> bool:
    if not isinstance(result, dict):
        return False
    probes = result.get("probes")
    if not isinstance(probes, list):
        return False
    for probe in probes:
        if not isinstance(probe, dict):
            return False
        if "metrics" in probe:
            if not isinstance(probe["metrics"], dict):
                return False
            try:
                validate_finite_audio_values(list(probe["metrics"].values()), "cached evaluation metrics")
            except ValueError:
                return False
    if "candidate_recommendation" in result:
        recommendation = result["candidate_recommendation"]
        if not isinstance(recommendation, dict):
            return False
        cleanup = recommendation.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("status") != "complete":
            return False
    try:
        settings = get_tts_model_settings({}) if synthesis_settings is None else synthesis_settings
        recommendation = result.get("candidate_recommendation")
        if recommendation is not None:
            recommended = recommendation.get("recommended")
            if not isinstance(recommended, str) or not recommended:
                return False
            if recommended != "production":
                candidate_dir = get_evaluation_directory(os.path.join(adapter_dir, "candidates"), recommended)
                with open(os.path.join(candidate_dir, "evaluation.json"), encoding="utf-8") as handle:
                    candidate_result = json.load(handle)
                if not is_complete_evaluation(candidate_result, candidate_dir, settings):
                    return False
        return (
            get_evidence_error(
                result, adapter_dir,
                get_evaluation_spec_sha256(PROBES, EVALUATION_SEED, THRESHOLDS, settings),
                expected_synthesis_settings=settings) is None
            and [item.get("id") for item in result.get("probes", [])]
            == [probe_id for probe_id, _text in PROBES]
        )
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def ensure_evaluation_source(result, adapter_dir, synthesis_settings):
    """Admit current serving bytes before publishing captured evaluation evidence."""
    with ensure_adapter_checkpoint(adapter_dir) as current:
        validate_adapter_checkpoint_generation(current)
        error = get_evidence_error(result, str(current), expected_synthesis_settings=synthesis_settings)
        if error:
            raise ValueError(error)
        yield str(current)


def evaluate_adapter(entry: dict, models_dir: str, engine: TTSEngine,
                     embedding_model, device: str, adapter_dir_override: str | None = None,
                     *, synthesis_settings=None, expected_generation=None) -> dict:
    adapter_id = entry["id"]
    production_dir = get_evaluation_directory(models_dir, adapter_id)
    adapter_dir = os.path.realpath(adapter_dir_override) if adapter_dir_override else production_dir
    if not is_path_inside(adapter_dir, production_dir):
        raise ValueError("evaluation override is outside the adapter")
    if not os.path.isfile(os.path.join(adapter_dir, "ref_sample.wav")):
        raise FileNotFoundError(f"missing ref_sample.wav for {adapter_id}")
    settings_reader = getattr(engine, "get_model_settings", None)
    if synthesis_settings is None:
        if not callable(settings_reader):
            raise ValueError("Evaluation requires declared synthesis settings")
        synthesis_settings = settings_reader()
    settings = get_tts_model_settings({"tts": synthesis_settings})
    with ensure_adapter_generation_snapshot(adapter_dir, validate=True) as (snapshot, generation):
        if expected_generation is not None and generation != expected_generation:
            raise ValueError("Adapter serving generation changed since evaluation admission")
        reference_path = os.path.join(snapshot, "ref_sample.wav")
        probe_results = []
        for probe_index, (probe_id, text) in enumerate(PROBES):
            if get_adapter_generation_sha256(snapshot) != generation:
                raise ValueError("Captured evaluation inputs changed during generation")
            if callable(settings_reader) and settings_reader() != settings:
                raise ValueError("Engine synthesis settings changed during evaluation")
            probe_seed = EVALUATION_SEED + probe_index
            apply_evaluation_seed(probe_seed)
            filename = f"evaluation_{probe_id}.wav"
            output_path = os.path.join(adapter_dir, filename)
            remove_stale_audio(output_path)
            generated = engine.generate_voice(
                text=text, instruct_text="", speaker="_evaluation_",
                voice_config={"_evaluation_": {
                    "type": "lora", "adapter_id": adapter_id, "adapter_path": snapshot,
                    "adapter_generation_sha256": generation, "seed": probe_seed,
                }},
                output_path=output_path,
            )
            if generated is False or not os.path.isfile(output_path):
                raise RuntimeError(f"probe generation failed: {probe_id}")
            metrics = get_audio_metrics(output_path)
            metrics["speaker_similarity"] = get_speaker_similarity(
                embedding_model, reference_path, output_path, device)
            warnings = get_warnings(metrics)
            probe_results.append({
                "id": probe_id, "text": text, "audio_file": filename, "seed": probe_seed,
                "audio_sha256": get_file_sha256(output_path),
                "metrics": metrics, "warnings": warnings,
            })
        if (get_adapter_generation_sha256(snapshot) != generation
                or (callable(settings_reader) and settings_reader() != settings)):
            raise ValueError("Captured evaluation inputs changed during generation")
        warnings = sorted({warning for probe in probe_results for warning in probe["warnings"]})
        result = {
            "version": EVALUATION_VERSION,
            "evidence": {
                "checkpoint_sha256": get_checkpoint_sha256(snapshot),
                "reference_audio_sha256": get_file_sha256(reference_path),
                "adapter_generation_sha256": generation,
                "evaluation_spec_sha256": get_evaluation_spec_sha256(
                    PROBES, EVALUATION_SEED, THRESHOLDS, settings),
                "evaluator": "evaluate_lora.py",
            },
            "status": "warning" if warnings else "pass",
            "warning_only": True,
            "warnings": warnings,
            "thresholds": THRESHOLDS,
            "synthesis_settings": settings,
            "evaluated_at": time.time(),
            "probes": probe_results,
        }
        with ensure_evaluation_source(result, adapter_dir, settings):
            pass
        return result


def get_evaluation_rank(result: dict) -> tuple:
    probes = result.get("probes", [])
    similarities = [probe["metrics"]["speaker_similarity"] for probe in probes]
    clipping = [probe["metrics"]["clipping_ratio"] for probe in probes]
    silence = [probe["metrics"]["silence_ratio"] for probe in probes]
    return (
        len(result.get("warnings", [])),
        -(sum(similarities) / len(similarities)) if similarities else 0.0,
        sum(clipping) / len(clipping) if clipping else 1.0,
        sum(silence) / len(silence) if silence else 1.0,
    )


def get_candidate_recommendation(evaluations: dict[str, dict]) -> dict:
    ranked = sorted((get_evaluation_rank(result),
                     0 if candidate_id == "production" else 1, candidate_id)
                    for candidate_id, result in evaluations.items())
    _rank, _production_priority, recommended = ranked[0]
    return {
        "recommended": recommended,
        "production_unchanged": True,
        "reason": "fewest warnings, then highest mean speaker similarity, clipping, and silence",
        "ranking": [candidate_id for _rank, _priority, candidate_id in ranked],
    }


def cleanup_candidates(adapter_dir: str, keep_candidate: str | None,
                       admitted_candidates: list[str]) -> list[str]:
    candidates_root = get_evaluation_directory(adapter_dir, "candidates")
    paths = [(name, get_evaluation_directory(candidates_root, name))
             for name in sorted(admitted_candidates)]
    if any(os.path.islink(os.path.join(candidates_root, name)) for name, _path in paths):
        raise ValueError("evaluation cleanup candidate must not be a symlink")
    removed = []
    if not os.path.isdir(candidates_root):
        return removed
    for name, path in paths:
        if os.path.isdir(path) and name != keep_candidate:
            shutil.rmtree(path)
            removed.append(name)
    return removed


def get_retained_candidate_records(records: list[dict], recommended: str,
                                   skipped_candidates: list[dict] = ()) -> list[dict]:
    retained = {item["id"] for item in skipped_candidates}
    if recommended != "production":
        retained.add(recommended)
    return [record for record in records if record.get("id") in retained]


def main() -> int:
    import torch
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--device", type=normalize_device, default="auto",
                        help="Device: auto, cpu, cuda, cuda:N, mps, rocm, or hip")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    for entry in manifest:
        if entry.get("id") != "":
            get_evaluation_directory(args.models_dir, entry.get("id"))
    device = resolve_device(args.device)
    from speechbrain.inference.speaker import EncoderClassifier
    embedding_model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=os.path.join(args.models_dir, ".evaluation_ecapa"),
        run_opts={"device": device},
    )
    embedding_model.eval()
    config = load_app_config(args.config)
    synthesis_settings = get_tts_model_settings(config)
    engine = TTSEngine(config)

    failures = 0
    manifest_needs_save = True
    for entry in manifest:
        adapter_id = entry.get("id", "")
        if not adapter_id:
            continue
        adapter_dir = get_evaluation_directory(args.models_dir, adapter_id)
        result_path = os.path.join(adapter_dir, "evaluation.json")
        if not os.path.isdir(adapter_dir):
            continue
        existing = {}
        try:
            with open(result_path, encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, json.JSONDecodeError):
            pass
        if not args.overwrite and is_complete_evaluation(existing, adapter_dir, synthesis_settings):
            print(f"SKIP {adapter_id} — evaluation complete", flush=True)
            entry["evaluation"] = {key: existing.get(key) for key in
                                   ("version", "status", "warnings", "evaluated_at")}
            recommendation = existing.get("candidate_recommendation") or {}
            if recommendation.get("recommended"):
                entry["evaluation"]["recommended_candidate"] = recommendation["recommended"]
                entry["evaluation_candidates"] = get_retained_candidate_records(
                    entry.get("evaluation_candidates", []), recommendation["recommended"],
                    recommendation.get("skipped_candidates", []))
            manifest_needs_save = True
            continue
        print(f"EVALUATE {adapter_id}", flush=True)
        try:
            checkpoint_identity = get_adapter_generation_file_identity(adapter_dir)
            result = evaluate_adapter(entry, args.models_dir, engine, embedding_model, device,
                                      synthesis_settings=synthesis_settings)
            evaluations = {"production": result}
            candidates_root = os.path.join(adapter_dir, "candidates")
            production_hash, unique_candidates, duplicate_candidates, skipped_candidates = (
                partition_unique_candidates(adapter_dir, candidates_root,
                    production_snapshot=(result["evidence"]["adapter_generation_sha256"],
                        checkpoint_identity, result["evidence"]["checkpoint_sha256"])))
            result["checkpoint_sha256"] = production_hash
            available_candidates = [candidate_id for candidate_id, _path, _hash
                                    in unique_candidates]
            available_candidates.extend(item["id"] for item in duplicate_candidates)
            admitted_generations = {item["id"]: item["adapter_generation_sha256"]
                                    for item in duplicate_candidates}
            for item in skipped_candidates:
                print(f"  candidate {item['id']} — incomplete, preserved: {item['reason']}",
                      flush=True)
            for item in duplicate_candidates:
                print(f"  candidate {item['id']} — duplicate of {item['duplicate_of']}, skipped",
                      flush=True)
            for candidate_id, candidate_dir, candidate_hash in unique_candidates:
                print(f"  candidate {candidate_id}", flush=True)
                candidate_generation = get_adapter_generation_sha256(candidate_dir)
                admitted_generations[candidate_id] = candidate_generation
                candidate_result = evaluate_adapter(
                    entry, args.models_dir, engine, embedding_model, device,
                    adapter_dir_override=candidate_dir, synthesis_settings=synthesis_settings,
                    expected_generation=candidate_generation)
                if candidate_result["evidence"]["checkpoint_sha256"] != candidate_hash:
                    raise ValueError("Candidate weights changed since evaluation admission")
                candidate_result["checkpoint_sha256"] = candidate_hash
                with ensure_evaluation_source(candidate_result, candidate_dir, synthesis_settings) as current:
                    atomic_json_write(candidate_result, os.path.join(current, "evaluation.json"))
                evaluations[candidate_id] = candidate_result
            recommendation = get_candidate_recommendation(evaluations)
            keep = (recommendation["recommended"]
                    if recommendation["recommended"] != "production" else None)
            available_candidates = sorted(available_candidates)
            recommendation["skipped_candidates"] = skipped_candidates
            recommendation["duplicate_candidates"] = duplicate_candidates
            recommendation["cleanup"] = {
                "status": "pending",
                "planned_removals": [candidate_id for candidate_id in available_candidates
                                     if candidate_id != keep],
                "removed_candidates": [],
            }
            recommendation["candidate_metrics"] = {
                candidate_id: {"rank": get_evaluation_rank(candidate_result),
                               "status": candidate_result["status"],
                               "warnings": candidate_result["warnings"]}
                for candidate_id, candidate_result in evaluations.items()
            }
            recommendation["candidate_metrics"].update({
                item["id"]: {"status": "skipped_duplicate",
                              "duplicate_of": item["duplicate_of"]}
                for item in duplicate_candidates
            })
            result["candidate_recommendation"] = recommendation
            # Persist the comparison and cleanup plan before removing any
            # generated candidate directory. A crash can then be diagnosed and
            # resumed without silently losing the selection evidence.
            with ensure_evaluation_source(result, adapter_dir, synthesis_settings) as current:
                for candidate_id, generation in admitted_generations.items():
                    candidate_dir = get_evaluation_directory(os.path.join(current, "candidates"), candidate_id)
                    validate_adapter_checkpoint_generation(candidate_dir)
                    if get_adapter_generation_sha256(candidate_dir) != generation:
                        raise ValueError("Candidate serving generation changed before evaluation cleanup")
                current_result_path = os.path.join(current, "evaluation.json")
                atomic_json_write(result, current_result_path)
                recommendation["cleanup"]["removed_candidates"] = cleanup_candidates(
                    current, keep, available_candidates)
                recommendation["cleanup"]["status"] = "complete"
                atomic_json_write(result, current_result_path)
            entry["evaluation"] = {key: result[key] for key in
                                   ("version", "status", "warnings", "evaluated_at")}
            entry["evaluation"]["recommended_candidate"] = recommendation["recommended"]
            entry["evaluation_candidates"] = get_retained_candidate_records(
                entry.get("evaluation_candidates", []), recommendation["recommended"],
                recommendation.get("skipped_candidates", []))
            print(f"  {result['status'].upper()} — {', '.join(result['warnings']) or 'clean'}",
                  flush=True)
        except Exception as error:
            failures += 1
            entry["evaluation"] = {"version": EVALUATION_VERSION, "status": "failed",
                                   "warnings": [str(error)], "evaluated_at": time.time()}
            print(f"  FAILED — {error}", flush=True)
        atomic_json_write(manifest, args.manifest)
        manifest_needs_save = False
    if manifest_needs_save:
        atomic_json_write(manifest, args.manifest)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
