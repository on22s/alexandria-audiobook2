#!/usr/bin/env python3
"""
Alexandria voice analysis pipeline.

Phase 1 – dedup: For each narrator subfolder in zips2/, computes pairwise
speaker similarity between volumes and identifies duplicate voices. Produces
per-folder heatmaps and dedup-cluster reports.

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
import pickle
import random
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

warnings.filterwarnings("ignore")

DEFAULT_ZIPS2 = Path("/home/fakemitch/Desktop/zips2")
PROJECT_ROOT  = Path(__file__).resolve().parent

DEDUP_SAMPLES   = 150
ANALYZE_SAMPLES = 200
DEDUP_THRESHOLD = 0.45

PROSODY_METRICS = [
    "f0_mean", "f0_std", "f0_range",
    "rms_mean_db", "rms_std_db",
    "spec_cent_mean", "spec_cent_std",
    "duration",
]

EXCLUDE_ZIPS = {
    "split_test.zip", "tag_test.zip",
    "vol_test_vol01.zip", "vol_test_vol02.zip",
    "vol_test_Kaname_Angry_vol01.zip", "vol_test_Kaname_Angry_vol02.zip",
}


# ─── Model ──────────────────────────────────────────────────────────────────

def load_model(savedir, device):
    print("Loading SpeechBrain ECAPA-TDNN speaker embedding model...")
    from speechbrain.inference.speaker import EncoderClassifier
    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(savedir),
        run_opts={"device": device},
    )
    model.eval()
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
    mfcc    = librosa.feature.mfcc(y=wav, sr=sr, n_mfcc=13, hop_length=512)
    return {
        "f0_mean":        float(np.nanmean(f0v)),
        "f0_std":         float(np.nanstd(f0v)),
        "f0_range":       float(np.ptp(f0v)),
        "rms_mean_db":    float(np.mean(rms_db)),
        "rms_std_db":     float(np.std(rms_db)),
        "spec_cent_mean": float(np.mean(sc)),
        "spec_cent_std":  float(np.std(sc)),
        "mfcc_mean":      mfcc.mean(axis=1),
        "mfcc_std":       mfcc.std(axis=1),
        "duration":       dur,
    }


# ─── ZIP helpers ─────────────────────────────────────────────────────────────

def load_wav_from_zip(zip_path, wav_name, sr_target=16000):
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(wav_name) as f:
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

def run_dedup(model, device, zips2_root, output_dir):
    """
    For each narrator subfolder of zips2_root, compute pairwise speaker
    similarity across its ZIP files, then report which are the same voice.
    """
    output_dir.mkdir(exist_ok=True)
    cache_file = output_dir / "embeddings_cache.pkl"
    cache = pickle.load(open(cache_file, "rb")) if cache_file.exists() else {}

    narrator_dirs = sorted(
        d for d in zips2_root.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )
    if not narrator_dirs:
        print(f"No narrator folders found under {zips2_root}")
        return

    results = {}

    for ndir in narrator_dirs:
        folder_name = ndir.name
        print(f"\n{'='*60}")
        print(f"FOLDER: {folder_name}")
        print(f"{'='*60}")

        zips = [z for z in sorted(ndir.iterdir())
                if z.is_file() and z.name not in EXCLUDE_ZIPS and zipfile.is_zipfile(z)]
        if not zips:
            print("  No zips found.")
            continue
        print(f"  Found {len(zips)} zips")

        zip_embeddings = {}
        zip_labels     = []

        for zp in zips:
            label     = zp.stem
            cache_key = f"{folder_name}/{label}"

            if cache_key in cache:
                zip_embeddings[label] = cache[cache_key]
                print(f"  {label:35s} (cached, {len(cache[cache_key][0])} samples)")
                zip_labels.append(label)
                continue

            all_wavs = list_wavs_in_zip(str(zp))
            if not all_wavs:
                print(f"  {label:35s} (no WAVs, skipping)")
                continue

            selected      = random.sample(all_wavs, min(DEDUP_SAMPLES, len(all_wavs)))
            embs, used    = [], []
            for wn in tqdm(selected, desc=f"  {label}", leave=False):
                try:
                    wav, sr = load_wav_from_zip(str(zp), wn)
                    embs.append(extract_embedding(wav, sr, model, device))
                    used.append(wn)
                except Exception:
                    pass

            if embs:
                zip_embeddings[label] = (np.array(embs), used)
                cache[cache_key]      = zip_embeddings[label]
                print(f"  {label:35s} {len(embs):4d} samples")
                zip_labels.append(label)
            else:
                print(f"  {label:35s} (extraction failed)")

        pickle.dump(cache, open(cache_file, "wb"))

        if len(zip_labels) < 2:
            print("  Need at least 2 zips to compare.")
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

        # Cluster identical voices
        visited, clusters = set(), []
        for i in range(n):
            if i in visited:
                continue
            cluster = [i]
            visited.add(i)
            for j in range(i + 1, n):
                if sim_matrix[i, j] > DEDUP_THRESHOLD:
                    cluster.append(j)
                    visited.add(j)
            clusters.append(cluster)

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
        }

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

def run_analyze(model, device, deduped_root, output_dir):
    """
    Cross-group speaker similarity, prosody divergence (EMD), and UMAP
    projection across all ZIPs in deduped_root.
    """
    output_dir.mkdir(exist_ok=True)
    cache_file = output_dir / "embeddings_cache.pkl"

    if not deduped_root.is_dir():
        print(f"WARNING: {deduped_root} not found — run dedupe_zips2.py first.")
        return

    zip_groups = {}
    for zp in sorted(deduped_root.glob("*.zip")):
        key = re.sub(r"[^a-z0-9]+", "_",
                     zp.stem.replace("-converted", "").strip().lower()).strip("_")
        zip_groups[key] = [str(zp)]

    if not zip_groups:
        print(f"No ZIPs found in {deduped_root}")
        return

    # Load cache
    if cache_file.exists():
        print(f"Loading cached embeddings from {cache_file}")
        cache_data    = pickle.load(open(cache_file, "rb"))
        all_embs      = cache_data.get("embeddings", {})
        all_prosody   = cache_data.get("prosody", {})
        all_wav_names = cache_data.get("wav_names", {})
    else:
        all_embs = all_prosody = all_wav_names = {}

    # Extract missing groups
    for group_name, zip_paths in zip_groups.items():
        if group_name in all_embs:
            continue
        print(f"\n─── Processing group: {group_name} ───")
        g_embs, g_pros, g_wavs = [], [], []

        for zp in zip_paths:
            if not os.path.exists(zp):
                print(f"  Zip not found: {zp}, skipping")
                continue
            wav_names = list_wavs_in_zip(zp)
            if ANALYZE_SAMPLES and len(wav_names) > ANALYZE_SAMPLES:
                train = [n for n in wav_names if n.startswith("train/")]
                val   = [n for n in wav_names if n.startswith("val/")]
                if train and val:
                    half      = ANALYZE_SAMPLES // 2
                    wav_names = (
                        np.random.choice(train, min(half, len(train)), replace=False).tolist()
                        + np.random.choice(val,   min(half, len(val)),   replace=False).tolist()
                    )
                else:
                    wav_names = np.random.choice(wav_names, ANALYZE_SAMPLES, replace=False).tolist()

            print(f"  Extracting {len(wav_names)} samples from {os.path.basename(zp)}...")
            for wname in tqdm(wav_names, desc=f"  {group_name}"):
                try:
                    wav, sr = load_wav_from_zip(zp, wname)
                    g_embs.append(extract_embedding(wav, sr, model, device))
                    g_pros.append(extract_prosody(wav, sr))
                    g_wavs.append((zp, wname))
                except Exception:
                    pass

        if g_embs:
            all_embs[group_name]      = np.array(g_embs)
            all_prosody[group_name]   = g_pros
            all_wav_names[group_name] = g_wavs
            print(f"  → {len(g_embs)} embeddings extracted")

    pickle.dump(
        {"embeddings": all_embs, "prosody": all_prosody, "wav_names": all_wav_names},
        open(cache_file, "wb"),
    )
    print(f"\nCache saved to {cache_file}")

    group_names = sorted(all_embs.keys())
    n_groups    = len(group_names)
    short_names = [n.replace("_", " ") for n in group_names]
    print(f"\n{'='*60}")
    print(f"Analyzing {n_groups} groups")
    print(f"{'='*60}")

    # ── Speaker similarity matrix ──
    print("\n─── Computing speaker embedding similarity matrix ───")
    sim_matrix = np.zeros((n_groups, n_groups))
    for i, g1 in enumerate(group_names):
        for j, g2 in enumerate(group_names):
            e1, e2 = all_embs[g1], all_embs[g2]
            if i == j:
                sim_matrix[i, j] = (
                    np.mean(1 - squareform(pdist(e1, "cosine"))) if len(e1) > 1 else 1.0
                )
            else:
                sim_matrix[i, j] = np.mean(1 - cdist(e1, e2, "cosine"))

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
        for i, g1 in enumerate(group_names):
            v1 = np.array([p[metric] for p in all_prosody[g1]])
            for j, g2 in enumerate(group_names):
                v2 = np.array([p[metric] for p in all_prosody[g2]])
                emd_mat[i, j] = wasserstein_distance(v1, v2)
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
    import umap as umap_lib

    all_embs_list = [all_embs[g] for g in group_names]
    group_idx_arr = np.concatenate([np.full(len(e), i) for i, e in enumerate(all_embs_list)])
    combined      = np.vstack(all_embs_list)
    print(f"  Total samples: {combined.shape[0]}, embedding dim: {combined.shape[1]}")

    if combined.shape[0] > 5000:
        idxs          = np.random.choice(combined.shape[0], 5000, replace=False)
        combined      = combined[idxs]
        group_idx_arr = group_idx_arr[idxs]

    umap_coords = umap_lib.UMAP(n_neighbors=15, min_dist=0.1, random_state=42).fit_transform(combined)
    palette     = sns.color_palette("husl", n_groups)
    fig, ax     = plt.subplots(figsize=(12, 10))
    for i in range(n_groups):
        mask = group_idx_arr == i
        if mask.sum() == 0:
            continue
        ax.scatter(umap_coords[mask, 0], umap_coords[mask, 1],
                   c=[palette[i]], label=short_names[i], alpha=0.6, s=5)
    ax.legend(markerscale=5, fontsize=8, loc="best")
    ax.set_title("UMAP Projection of Speaker Embeddings", fontsize=14)
    ax.set_xlabel("UMAP-1")
    ax.set_ylabel("UMAP-2")
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
    print("\nDone!")


# ─── Pipeline summary ────────────────────────────────────────────────────────

def write_pipeline_summary(zips2_root, dedup_dir, analyze_dir):
    """
    Write a snapshot of pipeline state to dedup_dir/pipeline_summary.log.

    Categories:
      DONE           – dedup PNG exists AND group key is in analyze cache
      PENDING ANALYZE – dedup PNG exists but not yet in analyze cache
      PENDING DEDUP  – narrator folder exists in zips2 but no dedup PNG yet
      NO ZIPS        – narrator folder has no valid zip files (failed/empty)
    """
    deduped_narrators = {
        p.stem[len("dedup_"):]
        for p in dedup_dir.glob("dedup_*.png")
    }

    analyze_cache_file = analyze_dir / "embeddings_cache.pkl"
    analyzed_groups = set()
    if analyze_cache_file.exists():
        analyzed_groups = set(pickle.load(open(analyze_cache_file, "rb")).keys())

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

        has_dedup = name in deduped_narrators
        norm = re.sub(r"[^a-z0-9]+", "_",
                      name.replace("-converted", "").strip().lower()).strip("_")
        is_analyzed = norm in analyzed_groups

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
        "--device", choices=["cuda", "cpu"], default=None,
        help="Force a specific device (default: auto-detect)",
    )
    parser.add_argument(
        "--then-analyze", action="store_true", dest="then_analyze",
        help="After --phase dedup completes, automatically chain into analyze phase",
    )
    args = parser.parse_args()

    if args.device:
        device = args.device
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}  |  ROCm HIP: {getattr(torch.version, 'hip', 'N/A')}")

    model_savedir = args.dedup_out / "models" / "ecapa"
    model         = load_model(model_savedir, device)
    deduped_root  = args.zips2 / "_deduped"

    if args.phase in ("dedup", "both"):
        print(f"\n{'#'*60}")
        print("## PHASE 1: WITHIN-FOLDER DEDUP")
        print(f"{'#'*60}")
        run_dedup(model, device, args.zips2, args.dedup_out)

    run_analyze_phase = args.phase in ("analyze", "both") or (
        args.phase == "dedup" and args.then_analyze
    )
    if run_analyze_phase:
        print(f"\n{'#'*60}")
        print("## PHASE 2: CROSS-GROUP ANALYSIS")
        print(f"{'#'*60}")
        run_analyze(model, device, deduped_root, args.analyze_out)
        write_pipeline_summary(args.zips2, args.dedup_out, args.analyze_out)


if __name__ == "__main__":
    main()
