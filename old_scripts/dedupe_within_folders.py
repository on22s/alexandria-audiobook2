#!/usr/bin/env python3
"""
Within-folder voice deduplication for Alexandria dataset zips.

For each folder of zip files, computes pairwise speaker similarity between every pair of zips.
Outputs a similarity matrix and a dedup report: which zip pairs are the same voice (redundant),
and which are distinct (unique characters/narrators).

Use this to decide how to rename/group zips before training.
"""

import os, sys, json, warnings, zipfile, pickle, random
from pathlib import Path
from collections import defaultdict

import numpy as np
import torch
import librosa
import soundfile as sf
from scipy.spatial.distance import cdist
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm

warnings.filterwarnings("ignore")

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {device}")

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "dedup_analysis"
OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_FILE = OUTPUT_DIR / "embeddings_cache.pkl"

SAMPLES_PER_ZIP = 150  # enough to judge voice similarity

# ─── Folders and their zips ────────────────────────────────────────
# Dynamically built from zips2/ narrator subfolders.
# Each subfolder (e.g. "Brittney Karbowski Reincarnated Slime-converted/")
# contains volume ZIPs for one narrator. Subfolders starting with "_" are
# pipeline outputs (e.g. _deduped, _analyzed) and are skipped.
ZIPS2_ROOT = Path("/home/fakemitch/Desktop/zips2")
FOLDERS = {
    d.name: {
        "path": d,
        "pattern": "*.zip",
        "label_fn": lambda p: p.stem,
    }
    for d in sorted(ZIPS2_ROOT.iterdir())
    if d.is_dir() and not d.name.startswith("_")
}

# ─── Model ─────────────────────────────────────────────────────────
print("Loading ECAPA-TDNN speaker embedding model...")
from speechbrain.inference.speaker import EncoderClassifier

speaker_encoder = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir=str(OUTPUT_DIR / "models" / "ecapa"),
    run_opts={"device": device},
)
speaker_encoder.eval()

def extract_embedding(wav: np.ndarray, sr: int) -> np.ndarray:
    with torch.no_grad():
        if wav.dtype != np.float32:
            wav = wav.astype(np.float32)
        if sr != 16000:
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
            sr = 16000
        wav = wav / (np.abs(wav).max() + 1e-12)
        tensor = torch.from_numpy(wav).unsqueeze(0).to(device)
        emb = speaker_encoder.encode_batch(tensor).squeeze().cpu().numpy()
    return emb

def load_wav(zip_path, wav_name, sr_target=16000):
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(wav_name) as f:
            wav, sr = sf.read(f)
    if len(wav.shape) > 1:
        wav = wav.mean(axis=1)
    if sr != sr_target:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=sr_target)
        sr = sr_target
    return wav, sr

# ─── Main ──────────────────────────────────────────────────────────
def main():
    # Load or build cache
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "rb") as f:
            cache = pickle.load(f)
        print(f"Loaded cache ({len(cache)} zip entries)")
    else:
        cache = {}

    results = {}

    for folder_name, folder_cfg in FOLDERS.items():
        print(f"\n{'='*60}")
        print(f"FOLDER: {folder_name}")
        print(f"{'='*60}")

        zip_dir = folder_cfg["path"]
        zips = sorted(zip_dir.glob(folder_cfg["pattern"]))

        # Exclude non-dataset zips
        exclude = {"split_test.zip", "tag_test.zip", "vol_test_vol01.zip", "vol_test_vol02.zip",
                    "vol_test_Kaname_Angry_vol01.zip", "vol_test_Kaname_Angry_vol02.zip"}
        zips = [z for z in zips if z.name not in exclude]

        if not zips:
            print("  No zips found.")
            continue

        print(f"  Found {len(zips)} zips")

        # Extract embeddings per zip
        zip_embeddings = {}   # label -> (np.array of embeddings, [wav_names])
        zip_labels = []

        for zp in zips:
            label = folder_cfg["label_fn"](zp)
            cache_key = f"{folder_name}/{label}"

            if cache_key in cache:
                zip_embeddings[label] = cache[cache_key]
                print(f"  {label:35s} (cached, {len(cache[cache_key][0])} samples)")
                zip_labels.append(label)
                continue

            # Get all WAVs
            all_wavs = []
            with zipfile.ZipFile(zp) as zf:
                for name in zf.namelist():
                    if name.endswith(".wav") and not name.startswith("train/"):
                        all_wavs.append(name)
                train_wavs = [n for n in zf.namelist() if n.endswith(".wav") and n.startswith("train/")]
                if train_wavs:
                    all_wavs = train_wavs

            if not all_wavs:
                print(f"  {label:35s} (no WAVs found, skipping)")
                continue

            # Sample
            n_samples = min(SAMPLES_PER_ZIP, len(all_wavs))
            selected = random.sample(all_wavs, n_samples)

            embs = []
            wav_names_used = []
            for wn in tqdm(selected, desc=f"  {label}", leave=False):
                try:
                    wav, sr = load_wav(str(zp), wn)
                    emb = extract_embedding(wav, sr)
                    embs.append(emb)
                    wav_names_used.append(wn)
                except Exception as e:
                    pass

            if embs:
                zip_embeddings[label] = (np.array(embs), wav_names_used)
                cache[cache_key] = zip_embeddings[label]
                print(f"  {label:35s} {len(embs):4d} samples")
                zip_labels.append(label)
            else:
                print(f"  {label:35s} (extraction failed)")

        # Save cache
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(cache, f)

        if len(zip_labels) < 2:
            print("  Need at least 2 zips to compare.")
            continue

        # ── Pairwise similarity ──
        n = len(zip_labels)
        sim_matrix = np.zeros((n, n))

        for i in range(n):
            for j in range(i, n):
                embs_i = zip_embeddings[zip_labels[i]][0]
                embs_j = zip_embeddings[zip_labels[j]][0]
                cos_sim = 1 - cdist(embs_i, embs_j, metric="cosine")
                sim_matrix[i, j] = float(np.mean(cos_sim))
                sim_matrix[j, i] = sim_matrix[i, j]

        # ── Report ──
        short_labels = [l.replace("dataset_", "").replace("-converted", "").replace("_", " ") for l in zip_labels]

        print(f"\n  ── Pairwise Speaker Similarity ──")

        # Auto-dedup threshold: look for natural clusters
        # Typically same-speaker cosine > 0.5-0.6, different speaker < 0.3
        THRESHOLD_SAME = 0.45  # above this = same voice

        # Print upper triangle
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
                    val = sim_matrix[i, j]
                    marker = " ●" if val > THRESHOLD_SAME else "  "
                    print(f"{marker}{val:>8.3f}", end="")
            print()

        # Dedup clusters
        print(f"\n  ── Dedup Clusters (threshold = {THRESHOLD_SAME}) ──")
        visited = set()
        clusters = []
        for i in range(n):
            if i in visited:
                continue
            cluster = [i]
            visited.add(i)
            for j in range(i+1, n):
                if sim_matrix[i, j] > THRESHOLD_SAME:
                    cluster.append(j)
                    visited.add(j)
            clusters.append(cluster)

        for ci, cluster in enumerate(clusters):
            if len(cluster) == 1:
                print(f"  [UNIQUE] {short_labels[cluster[0]]}")
            else:
                print(f"  [GROUP {ci+1}] Same voice ({len(cluster)} zips):")
                for idx in cluster:
                    print(f"             • {short_labels[idx]}  (sim~{sim_matrix[cluster[0]][idx]:.3f})")

        # ── Suggestions ──
        print(f"\n  ── Rename Suggestions ──")
        for ci, cluster in enumerate(clusters):
            if len(cluster) > 1:
                print(f"  These are the SAME voice → rename to character_{ci+1}_volXX:")
                for idx in cluster:
                    print(f"    {zip_labels[idx]}  →  character_{ci+1}_{zip_labels[idx]}")
            else:
                print(f"  {short_labels[cluster[0]]} → UNIQUE narrator")

        # ── Save plot ──
        fig, ax = plt.subplots(figsize=(max(8, n*1.2), max(6, n*0.9)))
        sns.heatmap(sim_matrix, annot=True, fmt=".3f",
                    xticklabels=short_labels, yticklabels=short_labels,
                    cmap="RdYlGn", vmin=0, vmax=1, ax=ax, linewidths=0.5)
        ax.set_title(f"Speaker Similarity — {folder_name}", fontsize=13)
        plt.tight_layout()
        plot_path = OUTPUT_DIR / f"dedup_{folder_name}.png"
        plt.savefig(plot_path, dpi=150)
        plt.close()
        print(f"\n  Plot saved: {plot_path}")

        # Store
        results[folder_name] = {
            "labels": zip_labels,
            "short_labels": short_labels,
            "matrix": sim_matrix,
            "clusters": clusters,
        }

    # ── Summary ──
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for folder_name, res in results.items():
        print(f"\n{folder_name}:")
        for ci, cluster in enumerate(res["clusters"]):
            names = [res["short_labels"][idx] for idx in cluster]
            if len(cluster) > 1:
                print(f"  REDUNDANT: {' ↔ '.join(names)}")
            else:
                print(f"  UNIQUE:    {names[0]}")
    print(f"\nOutput: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()