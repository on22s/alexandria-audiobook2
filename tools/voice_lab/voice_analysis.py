#!/usr/bin/env python3
"""
Alexandria voice analysis pipeline.

Phase 1 – dedup: For each narrator subfolder in zips2/, computes pairwise
speaker similarity between volumes and identifies duplicate voices. Produces
per-folder heatmaps and dedup-cluster reports, and merges confirmed same-speaker
ZIPs into zips2/_deduped/ for the train stage without discarding unique clips.

Phase 2 – analyze: Across all deduplicated narrators in zips2/_deduped/,
computes cross-group speaker similarity, prosody divergence (EMD), UMAP
projection, and a full summary report.

Usage:
    python voice_analysis.py                      # run both phases in sequence
    python voice_analysis.py --phase dedup        # within-folder dedup only
    python voice_analysis.py --phase analyze      # cross-group analysis only
    python voice_analysis.py --zips2 /path/to/zips2
"""

import argparse
import datetime
import os
import re
import random
import shutil
import sys
import warnings
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import librosa
import soundfile as sf
from scipy.spatial.distance import cdist, pdist, squareform
from scipy.stats import wasserstein_distance
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from device_utils import normalize_device, resolve_device
from voicelab_settings import get_deduped_zip_name
from voice_dataset_merge import merge_voice_datasets
from voice_clustering import cluster_voices, load_cluster_overrides
from utils import atomic_json_write
from lora_evidence import get_file_sha256
from voice_analysis_cache import (get_voice_analysis_model_state_sha256,
                                 get_voice_analysis_model_files,
                                 get_voice_analysis_dependency_versions,
                                 get_voice_analysis_stage_identity)

warnings.filterwarnings("ignore")

DEFAULT_ZIPS2 = Path(os.environ.get("ALEXANDRIA_ZIPS_DIR", PROJECT_ROOT / "zips2"))

DEDUP_SAMPLES   = 150
ANALYZE_SAMPLES = 200
DEDUP_THRESHOLD = 0.45

PROSODY_METRICS = [
    "f0_mean", "f0_std", "f0_range",
    "rms_mean_db", "rms_std_db",
    "spec_cent_mean", "spec_cent_std",
    "duration",
]


def _load_pickle_cache(path, default):
    from voice_analysis_cache import load_voice_analysis_pickle
    return load_voice_analysis_pickle(path, default)


def _atomic_pickle_dump(value, path):
    from voice_analysis_cache import save_voice_analysis_pickle
    save_voice_analysis_pickle(value, path)


EXCLUDE_ZIPS = {
    "split_test.zip", "tag_test.zip",
    "vol_test_vol01.zip", "vol_test_vol02.zip",
    "vol_test_Kaname_Angry_vol01.zip", "vol_test_Kaname_Angry_vol02.zip",
}


# ─── Model ──────────────────────────────────────────────────────────────────

_EMBEDDING_MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"

def load_model(savedir, device):
    print("Loading SpeechBrain ECAPA-TDNN speaker embedding model...")
    from speechbrain.inference.speaker import EncoderClassifier
    model = EncoderClassifier.from_hparams(
        source=_EMBEDDING_MODEL_ID,
        savedir=str(savedir),
        run_opts={"device": device},
    )
    model.eval()
    model._alexandria_state_sha256 = get_voice_analysis_model_state_sha256(model)
    model._alexandria_model_id = _EMBEDDING_MODEL_ID
    model._alexandria_model_files = get_voice_analysis_model_files(model, savedir)
    model._alexandria_dependency_versions = get_voice_analysis_dependency_versions()
    return model


# ─── Feature extraction ─────────────────────────────────────────────────────

def extract_embedding(wav, sr, model, device):
    with torch.no_grad():
        if wav.dtype != np.float32:
            wav = wav.astype(np.float32)
        if sr != 16000:
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
        wav = wav / (np.abs(wav).max() + 1e-12)
        tensor = torch.from_numpy(wav).unsqueeze(0).to(device)
        return model.encode_batch(tensor).squeeze().cpu().numpy()


def extract_prosody(wav, sr):
    if sr != 16000:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
        sr = 16000
    dur = len(wav) / sr
    f0, voiced_flag, _ = librosa.pyin(
        wav, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"), sr=sr
    )
    f0v = f0[voiced_flag]
    if len(f0v) == 0:
        f0v = np.array([0.0])
    rms     = librosa.feature.rms(y=wav, frame_length=2048, hop_length=512)[0]
    rms_db  = librosa.amplitude_to_db(rms, ref=np.max)
    sc      = librosa.feature.spectral_centroid(y=wav, sr=sr, hop_length=512)[0]
    return {
        "f0_mean":        float(np.nanmean(f0v)),
        "f0_std":         float(np.nanstd(f0v)),
        "f0_range":       float(np.ptp(f0v)),
        "rms_mean_db":    float(np.mean(rms_db)),
        "rms_std_db":     float(np.std(rms_db)),
        "spec_cent_mean": float(np.mean(sc)),
        "spec_cent_std":  float(np.std(sc)),
        "duration":       dur,
    }


# ─── ZIP helpers ─────────────────────────────────────────────────────────────

def load_wav_from_zip(zip_path, wav_name, sr_target=16000, archive=None):
    """Decode one member, borrowing an extraction loop's open archive when supplied."""
    if archive is None:
        with zipfile.ZipFile(zip_path) as opened:
            return load_wav_from_zip(zip_path, wav_name, sr_target, archive=opened)
    with archive.open(wav_name) as f:
        wav, sr = sf.read(f)
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != sr_target:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=sr_target)
    return wav, sr_target


def list_wavs_in_zip(zip_path):
    """Return WAV names from a zip. Prefers train/ subfolder if present."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    train = [n for n in names if n.endswith(".wav") and n.startswith("train/")]
    return train if train else [n for n in names if n.endswith(".wav")]


# ─── Phase 1: Dedup ─────────────────────────────────────────────────────────

def get_analysis_file_hashes(paths):
    from lora_evidence import get_file_sha256
    return {Path(path).name: get_file_sha256(path) for path in paths}


def get_dedup_narrator_evidence(source_zips, outputs, expected_sources):
    sources = get_analysis_file_hashes(source_zips)
    if sources != expected_sources:
        raise RuntimeError("Dedup sources changed before completion; rerun the phase")
    return {"sources": sources, "outputs": get_analysis_file_hashes(outputs)}


def get_completed_analysis_phase(path):
    import json
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        if path.exists():
            print(f"Warning: unreadable phase evidence {path}: {exc}")
        return {}
    return document if isinstance(document, dict) and document.get("status") == "complete" else {}


def get_narrator_pipeline_completion(name, zips, deduped_root, dedup_state, analyze_state):
    narrators = dedup_state.get("narrators")
    if not isinstance(narrators, dict):
        return False, False
    record = narrators.get(name)
    if not isinstance(record, dict) or not isinstance(record.get("outputs"), dict) or not record["outputs"]:
        return False, False
    if any(not isinstance(filename, str) or Path(filename).name != filename
           for filename in record["outputs"]):
        return False, False
    outputs = [deduped_root / filename for filename in record["outputs"]]
    try:
        if (record.get("sources") != get_analysis_file_hashes(zips) or
                record["outputs"] != get_analysis_file_hashes(outputs)):
            return False, False
        groups = analyze_state.get("groups")
        if not isinstance(groups, dict):
            return True, False
        for output in outputs:
            group = groups.get(normalize_group_key(output.stem))
            current_inputs = get_analysis_file_hashes([
                path for path in deduped_root.glob("*.zip")
                if normalize_group_key(path.stem) == normalize_group_key(output.stem)])
            if (not isinstance(group, dict) or group.get("inputs") != current_inputs
                    or type(group.get("samples")) is not int
                    or group["samples"] < 1 or
                    (group.get("inputs") if isinstance(group.get("inputs"), dict) else {}).get(output.name) != record["outputs"][output.name]):
                return True, False
    except (OSError, ValueError):
        return False, False
    return True, bool(analyze_state)


def run_dedup(model, device, zips2_root, output_dir, seed=42):
    """
    For each narrator subfolder of zips2_root, compute pairwise speaker
    similarity across its ZIP files, identify which are the same voice, and
    merge each confirmed same-speaker cluster into zips2_root/_deduped/ so the
    train stage has exactly one provenance-preserving ZIP per distinct voice.
    """
    output_dir.mkdir(exist_ok=True)
    from voice_analysis_cache import save_voice_analysis_checkpoint, compact_voice_analysis_checkpoints
    cache_file = output_dir / "embeddings_cache.pkl"
    cache = _load_pickle_cache(cache_file, {})
    state_file = output_dir / "phase_state.json"
    atomic_json_write({"status": "running", "narrators": {}}, str(state_file))
    narrator_records = {}
    incomplete = False

    deduped_dir = zips2_root / "_deduped"
    deduped_dir.mkdir(exist_ok=True)
    # Keep prior outputs until their replacements are complete. Expected files
    # are tracked and stale outputs are removed only after every narrator has
    # finished, so an interrupted merge cannot destroy the last good dataset.
    expected_outputs = set()

    narrator_dirs = sorted(
        d for d in zips2_root.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )
    if not narrator_dirs:
        print(f"No narrator folders found under {zips2_root}")
        return

    results = {}
    override_path = zips2_root / "cluster_overrides.json"

    for ndir in narrator_dirs:
        folder_name = ndir.name
        print(f"\n{'='*60}")
        print(f"FOLDER: {folder_name}")
        print(f"{'='*60}")

        zips = [z for z in sorted(ndir.iterdir())
                if z.is_file() and z.name not in EXCLUDE_ZIPS and zipfile.is_zipfile(z)]
        if not zips:
            raise RuntimeError(f"Dedup incomplete: no readable ZIPs in {ndir}; preserving prior outputs")
        print(f"  Found {len(zips)} zips")

        expected_sources = get_analysis_file_hashes(zips)
        folder_updates = {}
        folder_outputs = []
        zip_embeddings = {}
        zip_labels     = []

        for zp in zips:
            label     = zp.stem
            source_sha256 = get_file_sha256(zp)
            identity = get_voice_analysis_stage_identity(
                model, device, [zp], Path(__file__), DEDUP_SAMPLES, seed)
            cache_key = f"{folder_name}/{label}::{identity['sha256']}"

            all_wavs = list_wavs_in_zip(str(zp))
            if not all_wavs:
                raise RuntimeError(f"Dedup incomplete: no WAVs in {zp}; preserving prior outputs")
            selected = random.Random(seed).sample(all_wavs, min(DEDUP_SAMPLES, len(all_wavs)))
            if (cache_key in cache and cache[cache_key][1] == selected
                    and len(cache[cache_key][0]) == len(selected)):
                zip_embeddings[label] = cache[cache_key]
                print(f"  {label:35s} (cached, {len(cache[cache_key][0])} samples)")
                zip_labels.append(label)
                continue

            embs, used    = [], []
            with zipfile.ZipFile(zp) as archive:
                for wn in tqdm(selected, desc=f"  {label}", leave=False):
                    try:
                        wav, sr = load_wav_from_zip(str(zp), wn, archive=archive)
                        embs.append(extract_embedding(wav, sr, model, device))
                        used.append(wn)
                    except Exception as e:
                        incomplete = True
                        tqdm.write(f"  Warning: extraction failed for {wn}: {e}")

            if embs:
                if get_file_sha256(zp) != source_sha256:
                    raise RuntimeError(f"ZIP changed during extraction: {zp}")
                if identity != get_voice_analysis_stage_identity(
                        model, device, [zp], Path(__file__), DEDUP_SAMPLES, seed):
                    raise RuntimeError(f"Analysis identity changed during extraction: {zp}")
                zip_embeddings[label] = (np.array(embs), used)
                cache[cache_key]      = zip_embeddings[label]
                folder_updates[cache_key] = zip_embeddings[label]
                print(f"  {label:35s} {len(embs):4d} samples")
                zip_labels.append(label)
            else:
                raise RuntimeError(f"Dedup incomplete: every sampled WAV failed in {zp}; "
                                   "preserving prior outputs")

        if folder_updates:
            save_voice_analysis_checkpoint(folder_updates, cache_file)

        if len(zip_labels) < 2:
            print("  Need at least 2 zips to compare.")
            # Still nothing to dedup against, but a single (or zero) valid
            # zip is trivially "unique" - copy it through so a narrator with
            # just one volume isn't silently dropped from _deduped/.
            if len(zip_labels) == 1:
                rep_path = next(zp for zp in zips if zp.stem == zip_labels[0])
                dest_path = deduped_dir / get_deduped_zip_name(folder_name, rep_path.name)
                expected_outputs.add(dest_path)
                folder_outputs.append(dest_path)
                temporary = dest_path.with_suffix(dest_path.suffix + ".tmp")
                shutil.copy2(rep_path, temporary)
                os.replace(temporary, dest_path)
                print(f"  [UNIQUE] kept {rep_path.name}")
                narrator_records[folder_name] = get_dedup_narrator_evidence(zips, folder_outputs, expected_sources)
            continue

        # Pairwise similarity matrix
        n          = len(zip_labels)
        sim_matrix = np.zeros((n, n))
        for i in range(n):
            for j in range(i, n):
                ei = zip_embeddings[zip_labels[i]][0]
                ej = zip_embeddings[zip_labels[j]][0]
                v  = float(np.mean(1 - cdist(ei, ej, metric="cosine")))
                sim_matrix[i, j] = sim_matrix[j, i] = v

        short_labels = [
            l.replace("dataset_", "").replace("-converted", "").replace("_", " ")
            for l in zip_labels
        ]

        # Print similarity table
        print(f"\n  ── Pairwise Speaker Similarity ──")
        print(f"  {'':30s}", end="")
        for sl in short_labels:
            print(f"{sl[:12]:>12s}", end="")
        print()
        for i in range(n):
            print(f"  {short_labels[i]:30s}", end="")
            for j in range(n):
                if i == j:
                    print(f"  {'—':>10s}", end="")
                else:
                    val    = sim_matrix[i, j]
                    marker = " ●" if val > DEDUP_THRESHOLD else "  "
                    print(f"{marker}{val:>8.3f}", end="")
            print()

        overrides = load_cluster_overrides(override_path, folder_name)
        clusters, cluster_decisions = cluster_voices(
            zip_labels, sim_matrix, DEDUP_THRESHOLD, overrides)

        print(f"\n  ── Dedup Clusters (threshold={DEDUP_THRESHOLD}) ──")
        for ci, cluster in enumerate(clusters):
            if len(cluster) == 1:
                print(f"  [UNIQUE] {short_labels[cluster[0]]}")
            else:
                print(f"  [GROUP {ci+1}] Same voice ({len(cluster)} zips):")
                for idx in cluster:
                    print(f"             • {short_labels[idx]}"
                          f"  (sim~{sim_matrix[cluster[0]][idx]:.3f})")

        print(f"\n  ── Rename Suggestions ──")
        for ci, cluster in enumerate(clusters):
            if len(cluster) > 1:
                print(f"  Same voice → character_{ci+1}_volXX:")
                for idx in cluster:
                    print(f"    {zip_labels[idx]}  →  character_{ci+1}_{zip_labels[idx]}")
            else:
                print(f"  {short_labels[cluster[0]]} → UNIQUE narrator")

        # Unique clusters pass through unchanged. Confirmed same-speaker
        # clusters are merged with exact decoded-PCM deduplication and source
        # provenance instead of silently discarding every ZIP but the largest.
        label_to_path = {zp.stem: zp for zp in zips}
        print(f"\n  ── Copying representatives to {deduped_dir} ──")
        for ci, cluster in enumerate(clusters):
            # The largest member supplies the stable, readable destination
            # name; it no longer determines which training data survives.
            rep_idx = max(cluster, key=lambda idx: label_to_path[zip_labels[idx]].stat().st_size)
            rep_path = label_to_path[zip_labels[rep_idx]]
            dest_path = deduped_dir / get_deduped_zip_name(folder_name, rep_path.name)
            expected_outputs.add(dest_path)
            folder_outputs.append(dest_path)
            if len(cluster) > 1:
                cluster_paths = [label_to_path[zip_labels[idx]] for idx in cluster]
                merge_result = merge_voice_datasets(cluster_paths, dest_path)
                print(f"  [GROUP {ci+1}] {merge_result['status']} {len(cluster_paths)} ZIPs"
                      f" → {dest_path.name}")
            else:
                temporary = dest_path.with_suffix(dest_path.suffix + ".tmp")
                shutil.copy2(rep_path, temporary)
                os.replace(temporary, dest_path)
                print(f"  [UNIQUE] kept {rep_path.name}")

        narrator_records[folder_name] = get_dedup_narrator_evidence(zips, folder_outputs, expected_sources)

        # Heatmap
        fig, ax = plt.subplots(figsize=(max(8, n * 1.2), max(6, n * 0.9)))
        sns.heatmap(sim_matrix, annot=True, fmt=".3f",
                    xticklabels=short_labels, yticklabels=short_labels,
                    cmap="RdYlGn", vmin=0, vmax=1, ax=ax, linewidths=0.5)
        ax.set_title(f"Speaker Similarity — {folder_name}", fontsize=13)
        plt.tight_layout()
        plot_path = output_dir / f"dedup_{folder_name}.png"
        plt.savefig(str(plot_path), dpi=150)
        plt.close()
        print(f"\n  Plot saved: {plot_path}")

        results[folder_name] = {
            "labels": zip_labels, "short_labels": short_labels,
            "matrix": sim_matrix, "clusters": clusters,
            "overrides": overrides, "decisions": cluster_decisions,
        }

    if not incomplete:
        for previous in deduped_dir.iterdir():
            if (previous.is_file() and previous.suffix.lower() == ".zip"
                    and previous not in expected_outputs):
                previous.unlink()
    else:
        print("Dedup incomplete: preserving prior outputs until sampled extraction succeeds")

    cluster_report = {
        "version": 1,
        "algorithm": "deterministic_complete_link",
        "threshold": DEDUP_THRESHOLD,
        "override_path": str(override_path),
        "narrators": {
            narrator: {
                "labels": result["labels"],
                "similarity_matrix": result["matrix"].tolist(),
                "clusters": [[result["labels"][index] for index in cluster]
                             for cluster in result["clusters"]],
                "overrides": result["overrides"],
                "decisions": result["decisions"],
            }
            for narrator, result in results.items()
        },
    }
    compact_voice_analysis_checkpoints(cache_file)
    atomic_json_write(cluster_report, str(output_dir / "dedup_clusters.json"))
    atomic_json_write({"status": "partial" if incomplete else "complete", "narrators": narrator_records}, str(state_file))

    print(f"\n{'='*60}")
    print("DEDUP SUMMARY")
    print(f"{'='*60}")
    for fname, res in results.items():
        print(f"\n{fname}:")
        for cluster in res["clusters"]:
            names = [res["short_labels"][idx] for idx in cluster]
            if len(cluster) > 1:
                print(f"  REDUNDANT: {' ↔ '.join(names)}")
            else:
                print(f"  UNIQUE:    {names[0]}")
    print(f"\nOutput: {output_dir}")


# ─── Phase 2: Analyze ────────────────────────────────────────────────────────

def normalize_group_key(name):
    """Derive an analyze-phase group key from a narrator/zip-stem name.

    Used by both run_analyze (building zip_groups) and write_pipeline_summary
    (recomputing the same key to check membership in analyzed_groups) - they
    must always agree, so this is the one place that decision lives.
    """
    return re.sub(r"[^a-z0-9]+", "_",
                  name.replace("-converted", "").strip().lower()).strip("_")

def get_analysis_selected_wavs(zip_paths, seed):
    sampling = np.random.RandomState(seed % (2 ** 32))
    selected = []
    for zp in zip_paths:
        wav_names = list_wavs_in_zip(zp)
        if ANALYZE_SAMPLES and len(wav_names) > ANALYZE_SAMPLES:
            train = [name for name in wav_names if name.startswith("train/")]
            val = [name for name in wav_names if name.startswith("val/")]
            if train and val:
                half = ANALYZE_SAMPLES // 2
                wav_names = (sampling.choice(train, min(half, len(train)), replace=False).tolist()
                             + sampling.choice(val, min(half, len(val)), replace=False).tolist())
            else:
                wav_names = sampling.choice(wav_names, ANALYZE_SAMPLES, replace=False).tolist()
        selected.extend((zp, name) for name in wav_names)
    return selected


def get_embedding_projection(embeddings):
    """Return finite plotting coordinates without requiring a tiny UMAP graph."""
    count = len(embeddings)
    if count == 1:
        return np.zeros((1, 2)), "Single sample"
    if count == 2:
        centered = embeddings - embeddings.mean(axis=0)
        left, singular, _ = np.linalg.svd(centered, full_matrices=False)
        coordinates = np.zeros((2, 2))
        # Two centered points have one component; pad the second axis with zero.
        coordinates[:, 0] = left[:, 0] * singular[0]
        return coordinates, "PCA"
    import umap as umap_lib
    options = {"n_neighbors": min(15, count - 1), "min_dist": 0.1, "random_state": 42}
    if count == 3:
        options["init"] = "random"
    return umap_lib.UMAP(**options).fit_transform(embeddings), "UMAP"


def run_analyze(model, device, deduped_root, output_dir, seed=42):
    """
    Cross-group speaker similarity, prosody divergence (EMD), and UMAP
    projection across all ZIPs in deduped_root.
    """
    from utils import atomic_json_write
    output_dir.mkdir(exist_ok=True)
    state_file = output_dir / "phase_state.json"
    atomic_json_write({"status": "running", "groups": {}}, str(state_file))
    incomplete = False
    cache_file = output_dir / "embeddings_cache.pkl"

    if not deduped_root.is_dir():
        error = f"{deduped_root} not found — run the dedup phase first (--phase dedup, or --phase both)."
        atomic_json_write({"status": "failed", "groups": {}, "error": error}, str(state_file))
        print(f"WARNING: {error}")
        return

    zip_groups = {}
    for zp in sorted(deduped_root.glob("*.zip")):
        key = normalize_group_key(zp.stem)
        # Append, don't assign: two zips that normalize to the same key both
        # belong to the group instead of the later one silently replacing the
        # earlier (which would drop a zip from the analyze phase).
        zip_groups.setdefault(key, []).append(str(zp))

    if not zip_groups:
        error = f"No ZIPs found in {deduped_root}"
        atomic_json_write({"status": "failed", "groups": {}, "error": error}, str(state_file))
        print(error)
        return

    from voice_analysis_cache import save_voice_analysis_checkpoint, compact_voice_analysis_checkpoints
    # Load both completed caches and interrupted extraction checkpoints.
    if cache_file.exists() or cache_file.with_suffix(cache_file.suffix + ".parts").exists():
        print(f"Loading cached embeddings from {cache_file}")
        cache_data    = _load_pickle_cache(cache_file, {})
        all_embs      = cache_data.get("embeddings", {})
        all_prosody   = cache_data.get("prosody", {})
        all_wav_names = cache_data.get("wav_names", {})
        if all_embs is all_prosody is all_wav_names:
            print("WARNING: analyze cache has the known shared-dictionary corruption; rebuilding")
            all_embs = {}
            all_prosody = {}
            all_wav_names = {}
    else:
        cache_data = {}
        all_embs = {}
        all_prosody = {}
        all_wav_names = {}

    cached_embs, cached_prosody, cached_wavs = all_embs, all_prosody, all_wav_names
    identities = cache_data.get("identities", {})
    all_embs, all_prosody = {}, {}

    # Extract missing groups
    for group_name, zip_paths in zip_groups.items():
        identity = get_voice_analysis_stage_identity(
            model, device, zip_paths, Path(__file__), ANALYZE_SAMPLES, seed)
        selected_wavs = get_analysis_selected_wavs(zip_paths, seed)
        if (identities.get(group_name) == identity and group_name in cached_embs
                and group_name in cached_prosody and group_name in cached_wavs
                and cached_wavs[group_name] == selected_wavs
                and len(cached_embs[group_name]) == len(cached_prosody[group_name]) == len(selected_wavs)
                and selected_wavs):
            all_embs[group_name] = cached_embs[group_name]
            all_prosody[group_name] = cached_prosody[group_name]
            continue
        print(f"\n─── Processing group: {group_name} ───")
        g_embs, g_pros, g_wavs = [], [], []
        if not selected_wavs:
            incomplete = True
        print(f"  Extracting {len(selected_wavs)} samples...")
        from itertools import groupby
        for zp, members in groupby(selected_wavs, key=lambda member: member[0]):
            with zipfile.ZipFile(zp) as archive:
                for _, wname in tqdm(members, desc=f"  {group_name}"):
                    try:
                        wav, sr = load_wav_from_zip(zp, wname, archive=archive)
                        embedding = extract_embedding(wav, sr, model, device)
                        prosody = extract_prosody(wav, sr)
                        g_embs.append(embedding)
                        g_pros.append(prosody)
                        g_wavs.append((zp, wname))
                    except Exception as e:
                        incomplete = True
                        tqdm.write(f"  Warning: extraction failed for {wname}: {e}")

        if g_embs:
            if identity != get_voice_analysis_stage_identity(
                    model, device, zip_paths, Path(__file__), ANALYZE_SAMPLES, seed):
                raise RuntimeError(f"Analysis identity changed during extraction: {group_name}")
            all_embs[group_name]      = np.array(g_embs)
            all_prosody[group_name]   = g_pros
            cached_embs[group_name] = all_embs[group_name]
            cached_prosody[group_name] = g_pros
            cached_wavs[group_name] = g_wavs
            identities[group_name] = identity
            print(f"  → {len(g_embs)} embeddings extracted")
            save_voice_analysis_checkpoint(
                {"embeddings": {group_name: cached_embs[group_name]},
                 "prosody": {group_name: cached_prosody[group_name]},
                 "wav_names": {group_name: cached_wavs[group_name]},
                 "identities": {group_name: identity}}, cache_file, nested=True)
    compact_voice_analysis_checkpoints(cache_file)
    print(f"\nCache saved to {cache_file}")

    group_names = sorted(all_embs.keys())
    n_groups    = len(group_names)
    if n_groups == 0:
        raise ValueError("Analysis incomplete: empty extracted feature distribution "
                         "(check that the _deduped ZIPs contain readable WAVs)")
    short_names = [n.replace("_", " ") for n in group_names]
    print(f"\n{'='*60}")
    print(f"Analyzing {n_groups} groups")
    print(f"{'='*60}")

    # ── Speaker similarity matrix ──
    print("\n─── Computing speaker embedding similarity matrix ───")
    sim_matrix = np.zeros((n_groups, n_groups))
    for i, g1 in enumerate(group_names):
        for j in range(i, n_groups):
            g2 = group_names[j]
            e1, e2 = all_embs[g1], all_embs[g2]
            if i == j:
                sim_matrix[i, j] = (
                    np.mean(1 - squareform(pdist(e1, "cosine"))) if len(e1) > 1 else 1.0
                )
            else:
                sim_matrix[i, j] = sim_matrix[j, i] = np.mean(1 - cdist(e1, e2, "cosine"))

    fig, ax = plt.subplots(figsize=(14, 12))
    sns.heatmap(sim_matrix, annot=True, fmt=".3f", xticklabels=short_names,
                yticklabels=short_names, cmap="RdYlGn", vmin=0, vmax=1,
                ax=ax, linewidths=0.5)
    ax.set_title("Mean Cosine Similarity of Speaker Embeddings", fontsize=14)
    plt.tight_layout()
    plt.savefig(str(output_dir / "speaker_similarity_matrix.png"), dpi=150)
    plt.close()
    print(f"  Saved heatmap to {output_dir / 'speaker_similarity_matrix.png'}")

    pd.DataFrame(sim_matrix, index=short_names, columns=short_names).to_csv(
        str(output_dir / "speaker_similarity_table.csv")
    )
    print(f"  Saved CSV to {output_dir / 'speaker_similarity_table.csv'}")

    # Top pairs
    pairs = [
        (short_names[i], short_names[j], sim_matrix[i, j])
        for i in range(n_groups) for j in range(i + 1, n_groups)
    ]
    pairs.sort(key=lambda x: -x[2])
    print("\n─── Top-5 most similar group pairs ───")
    for a, b, s in pairs[:5]:
        print(f"  {a:25s} ↔ {b:25s}  sim={s:.4f}")
    print("\n─── Top-5 least similar group pairs ───")
    for a, b, s in pairs[-5:]:
        print(f"  {a:25s} ↔ {b:25s}  sim={s:.4f}")

    print("\n─── Intra-group cohesion ───")
    for name, sim in sorted(
        [(short_names[i], sim_matrix[i, i]) for i in range(n_groups)],
        key=lambda x: -x[1]
    ):
        print(f"  {name:25s}  cohesion={sim:.4f}")

    # ── Prosody divergence (EMD) ──
    print("\n─── Prosody distribution comparison (Earth Mover's Distance) ───")
    prosody_results = []
    for metric in PROSODY_METRICS:
        emd_mat = np.zeros((n_groups, n_groups))
        vectors = [np.array([p[metric] for p in all_prosody[group]])
                   for group in group_names]
        for i in range(n_groups):
            # Preserve SciPy's empty/nonfinite behavior on the diagonal.
            if not vectors[i].size or not np.isfinite(vectors[i]).all():
                emd_mat[i, i] = wasserstein_distance(vectors[i], vectors[i])
            for j in range(i + 1, n_groups):
                emd_mat[i, j] = emd_mat[j, i] = wasserstein_distance(vectors[i], vectors[j])
        prosody_results.append((metric, emd_mat))
        max_idx = np.unravel_index(np.argmax(emd_mat), emd_mat.shape)
        print(f"  {metric:20s} max EMD={emd_mat[max_idx]:.2f}  "
              f"({short_names[max_idx[0]]} vs {short_names[max_idx[1]]})")

    mean_emd = np.mean([r[1] for r in prosody_results], axis=0)
    fig, ax = plt.subplots(figsize=(14, 12))
    sns.heatmap(mean_emd, annot=True, fmt=".2f", xticklabels=short_names,
                yticklabels=short_names, cmap="YlOrRd", ax=ax, linewidths=0.5)
    ax.set_title("Mean Prosody Divergence (Earth Mover's Distance)", fontsize=14)
    plt.tight_layout()
    plt.savefig(str(output_dir / "prosody_divergence_matrix.png"), dpi=150)
    plt.close()
    print(f"\n  Saved prosody heatmap to {output_dir / 'prosody_divergence_matrix.png'}")

    # ── Prosody box plots ──
    print("\n─── Generating prosody distribution plots ───")
    for metric in PROSODY_METRICS[:6]:
        fig, ax = plt.subplots(figsize=(12, 5))
        data = [[p[metric] for p in all_prosody[g]] for g in group_names]
        sns.boxplot(data=data, ax=ax)
        ax.set_xticks(range(len(short_names)))
        ax.set_xticklabels(short_names, rotation=45, ha="right")
        ax.set_title(f"Prosody: {metric}")
        plt.tight_layout()
        plt.savefig(str(output_dir / f"prosody_{metric}.png"), dpi=100)
        plt.close()
    print(f"  Saved {len(PROSODY_METRICS[:6])} box plots to {output_dir}")

    # ── UMAP projection ──
    print("\n─── Computing UMAP projection ───")
    all_embs_list = [all_embs[g] for g in group_names]
    group_idx_arr = np.concatenate([np.full(len(e), i) for i, e in enumerate(all_embs_list)])
    combined      = np.vstack(all_embs_list)
    print(f"  Total samples: {combined.shape[0]}, embedding dim: {combined.shape[1]}")

    if combined.shape[0] > 5000:
        idxs          = np.random.choice(combined.shape[0], 5000, replace=False)
        combined      = combined[idxs]
        group_idx_arr = group_idx_arr[idxs]

    umap_coords, projection_name = get_embedding_projection(combined)
    palette     = sns.color_palette("husl", n_groups)
    fig, ax     = plt.subplots(figsize=(12, 10))
    for i in range(n_groups):
        mask = group_idx_arr == i
        if mask.sum() == 0:
            continue
        ax.scatter(umap_coords[mask, 0], umap_coords[mask, 1],
                   c=[palette[i]], label=short_names[i], alpha=0.6, s=5)
    ax.legend(markerscale=5, fontsize=8, loc="best")
    ax.set_title(f"{projection_name} Projection of Speaker Embeddings", fontsize=14)
    ax.set_xlabel("UMAP-1" if projection_name == "UMAP" else "Component-1")
    ax.set_ylabel("UMAP-2" if projection_name == "UMAP" else "Component-2")
    plt.tight_layout()
    plt.savefig(str(output_dir / "umap_embedding_projection.png"), dpi=150)
    plt.close()
    print(f"  Saved UMAP to {output_dir / 'umap_embedding_projection.png'}")

    # ── Summary ──
    print(f"\n{'='*60}")
    print("ANALYZE SUMMARY")
    print(f"{'='*60}")
    print(f"\nGroups analyzed: {n_groups}")
    for g in group_names:
        print(f"  {g:25s}  {len(all_embs[g]):5d} samples")
    print(f"\nOutput directory: {output_dir}")
    for f in sorted(output_dir.glob("*")):
        if f.suffix in (".png", ".csv", ".pkl"):
            print(f"  {f.name}")
    groups = {}
    for group_name in group_names:
        if identities.get(group_name) != get_voice_analysis_stage_identity(
                model, device, zip_groups[group_name], Path(__file__), ANALYZE_SAMPLES, seed):
            raise RuntimeError(f"Analysis identity changed before completion: {group_name}")
        groups[group_name] = {"inputs": get_analysis_file_hashes(zip_groups[group_name]),
                              "samples": len(all_embs[group_name])}
    complete = not incomplete and set(group_names) == set(zip_groups)
    atomic_json_write({"status": "complete" if complete else "partial", "groups": groups}, str(state_file))
    print("\nDone!" if complete else "\nAnalysis incomplete: failed groups or samples remain.")


# ─── Pipeline summary ────────────────────────────────────────────────────────

def write_pipeline_summary(zips2_root, dedup_dir, analyze_dir):
    """
    Write a snapshot of pipeline state to dedup_dir/pipeline_summary.log.

    Completion requires explicit phase evidence matching current source and
    output ZIP bytes. Plots and extraction checkpoints alone are not completion.
    """
    dedup_state = get_completed_analysis_phase(dedup_dir / "phase_state.json")
    analyze_state = get_completed_analysis_phase(analyze_dir / "phase_state.json")

    narrator_dirs = sorted(
        d for d in zips2_root.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )

    done, pending_analyze, pending_dedup, no_zips = [], [], [], []

    for ndir in narrator_dirs:
        name = ndir.name
        zips = [z for z in sorted(ndir.iterdir())
                if z.is_file() and z.name not in EXCLUDE_ZIPS and zipfile.is_zipfile(z)]

        if not zips:
            no_zips.append(name)
            continue

        has_dedup, is_analyzed = get_narrator_pipeline_completion(
            name, zips, zips2_root / "_deduped", dedup_state, analyze_state)

        if has_dedup and is_analyzed:
            done.append((name, len(zips)))
        elif has_dedup:
            pending_analyze.append((name, len(zips)))
        else:
            pending_dedup.append((name, len(zips)))

    lines = [
        f"# Alexandria Pipeline Summary — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# zips2: {zips2_root}",
        f"",
        f"=== DONE: deduped + analyzed ({len(done)}) ===",
    ]
    for name, n in done:
        lines.append(f"  [DONE]    {name}  ({n} vols)")

    lines += [
        f"",
        f"=== PENDING ANALYZE — deduped but not yet analyzed ({len(pending_analyze)}) ===",
    ]
    for name, n in pending_analyze:
        lines.append(f"  [ANALYZE] {name}  ({n} vols)")

    lines += [
        f"",
        f"=== PENDING DEDUP — not yet deduped ({len(pending_dedup)}) ===",
    ]
    for name, n in pending_dedup:
        lines.append(f"  [DEDUP]   {name}  ({n} vols)")

    lines += [
        f"",
        f"=== NO ZIPS FOUND — failed or still building ({len(no_zips)}) ===",
    ]
    for name in no_zips:
        lines.append(f"  [EMPTY]   {name}")

    total = len(done) + len(pending_analyze) + len(pending_dedup) + len(no_zips)
    lines += [
        f"",
        f"# {total} narrator folders total: "
        f"{len(done)} done | {len(pending_analyze)} pending analyze | "
        f"{len(pending_dedup)} pending dedup | {len(no_zips)} empty/failed",
        f"#",
        f"# To process all pending in one pass:",
        f"#   python voice_analysis.py --device cpu --phase dedup --then-analyze --zips2 {zips2_root}",
    ]

    log_path = dedup_dir / "pipeline_summary.log"
    log_path.write_text("\n".join(lines) + "\n")
    print(f"\nPipeline summary written → {log_path}")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--phase", choices=["dedup", "analyze", "both"], default="both",
        help="Which phase to run (default: both)",
    )
    parser.add_argument(
        "--zips2", type=Path, default=DEFAULT_ZIPS2,
        help=f"Root folder containing narrator ZIP subfolders (default: {DEFAULT_ZIPS2})",
    )
    parser.add_argument(
        "--dedup-out", type=Path, default=PROJECT_ROOT / "dedup_analysis",
        help="Output folder for the dedup phase",
    )
    parser.add_argument(
        "--analyze-out", type=Path, default=PROJECT_ROOT / "tone_analysis_output",
        help="Output folder for the analyze phase",
    )
    parser.add_argument(
        "--device", type=normalize_device, default="auto",
        help="Force a specific device (default: auto-detect)",
    )
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducible sample selection")
    parser.add_argument(
        "--then-analyze", action="store_true", dest="then_analyze",
        help="After --phase dedup completes, automatically chain into analyze phase",
    )
    args = parser.parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed % (2 ** 32))

    device = resolve_device(args.device)
    print(f"Device: {device}  |  ROCm HIP: {getattr(torch.version, 'hip', 'N/A')}")

    model_savedir = args.dedup_out / "models" / "ecapa"
    model         = load_model(model_savedir, device)
    deduped_root  = args.zips2 / "_deduped"

    if args.phase in ("dedup", "both"):
        print(f"\n{'#'*60}")
        print("## PHASE 1: WITHIN-FOLDER DEDUP")
        print(f"{'#'*60}")
        run_dedup(model, device, args.zips2, args.dedup_out, seed=args.seed)

    run_analyze_phase = args.phase in ("analyze", "both") or (
        args.phase == "dedup" and args.then_analyze
    )
    if run_analyze_phase:
        print(f"\n{'#'*60}")
        print("## PHASE 2: CROSS-GROUP ANALYSIS")
        print(f"{'#'*60}")
        run_analyze(model, device, deduped_root, args.analyze_out, seed=args.seed)
        write_pipeline_summary(args.zips2, args.dedup_out, args.analyze_out)

    phase_states = []
    if args.phase in ("dedup", "both"):
        phase_states.append(args.dedup_out / "phase_state.json")
    if run_analyze_phase:
        phase_states.append(args.analyze_out / "phase_state.json")
    incomplete = [path for path in phase_states if not get_completed_analysis_phase(path)]
    if incomplete:
        print("Voice analysis did not complete every requested phase: " + ", ".join(map(str, incomplete)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
