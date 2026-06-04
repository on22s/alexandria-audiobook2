#!/usr/bin/env python3
"""
Within-folder voice deduplication for /home/fakemitch/Desktop/zips2/.

Each subfolder = one narrator. Analyzes all zips within each folder,
groups them by voice similarity, renames uniquely and copies to output.
"""

import os, sys, json, warnings, zipfile, pickle, random, shutil
from pathlib import Path
from collections import defaultdict

import numpy as np
import torch
import librosa
import soundfile as sf
from scipy.spatial.distance import cdist
from scipy.cluster.hierarchy import linkage, fcluster
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm

warnings.filterwarnings("ignore")

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {device}")

ROOT = Path("/home/fakemitch/Desktop/zips2")
OUTPUT = ROOT / "_deduped"
OUTPUT.mkdir(exist_ok=True)
CACHE_DIR = OUTPUT / "_cache"
CACHE_DIR.mkdir(exist_ok=True)

SAMPLES_PER_ZIP = 150
THRESHOLD = 0.45  # same-voice threshold

# ─── Model ─────────────────────────────────────────────────────────
print("Loading ECAPA-TDNN speaker embedding model...")
from speechbrain.inference.speaker import EncoderClassifier
speaker_encoder = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir=str(OUTPUT / "_models" / "ecapa"),
    run_opts={"device": device},
)
speaker_encoder.eval()

def extract_embedding(wav, sr):
    with torch.no_grad():
        if wav.dtype != np.float32:
            wav = wav.astype(np.float32)
        if sr != 16000:
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
        wav = wav / (np.abs(wav).max() + 1e-12)
        t = torch.from_numpy(wav).unsqueeze(0).to(device)
        return speaker_encoder.encode_batch(t).squeeze().cpu().numpy()

def load_wav(zip_path, wav_name, sr_target=16000):
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(wav_name) as f:
            wav, sr = sf.read(f)
    if len(wav.shape) > 1:
        wav = wav.mean(axis=1)
    if sr != sr_target:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=sr_target)
    return wav, sr_target

# ─── Process each folder ───────────────────────────────────────────
folders = sorted([d for d in ROOT.iterdir() if d.is_dir() and not d.name.startswith("_")])

for folder in folders:
    zips = sorted([f for f in folder.iterdir()
                   if f.is_file() and (f.suffix == ".zip" or "_vol" in f.name)])
    if len(zips) < 2:
        if zips:
            # Single zip — copy as-is
            short = folder.name.replace("-converted", "").strip()
            dst = OUTPUT / f"{short}.zip"
            shutil.copy2(str(zips[0]), str(dst))
            print(f"\n  {folder.name} — 1 zip, copied as {dst.name}")
        else:
            print(f"\n  {folder.name} — empty, skipped")
        continue

    print(f"\n{'='*60}")
    print(f"  {folder.name}  ({len(zips)} zips)")
    print(f"{'='*60}")

    # Extract embeddings
    embs_dict = {}
    short_labels = []
    label_to_path = {}  # label → actual file path (handles extensionless files)

    for zp in zips:
        label = zp.stem
        cache_key = f"{folder.name}/{label}"
        cache_path = CACHE_DIR / f"{cache_key.replace('/', '_')}.pkl"

        label_to_path[label] = zp
        if cache_path.exists():
            with open(cache_path, "rb") as f:
                embs_dict[label] = pickle.load(f)
            print(f"    {label:50s} (cached, {len(embs_dict[label])} samples)")
            short_labels.append(label)
            continue

        all_wavs = [n for n in zipfile.ZipFile(zp).namelist() if n.endswith(".wav")]
        # Prefer train/ samples if they exist
        train_wavs = [n for n in all_wavs if n.startswith("train/")]
        if train_wavs:
            all_wavs = train_wavs

        if not all_wavs:
            print(f"    {label:50s} (no WAVs)")
            continue

        n_samples = min(SAMPLES_PER_ZIP, len(all_wavs))
        selected = random.sample(all_wavs, n_samples)

        embs = []
        for wn in tqdm(selected, desc=f"    {label[:40]}", leave=False):
            try:
                wav, sr = load_wav(str(zp), wn)
                embs.append(extract_embedding(wav, sr))
            except:
                pass

        if embs:
            arr = np.array(embs)
            embs_dict[label] = arr
            short_labels.append(label)
            with open(cache_path, "wb") as f:
                pickle.dump(arr, f)
            print(f"    {label:50s} {len(embs):4d} samples")
        else:
            print(f"    {label:50s} (extraction failed)")

    if len(short_labels) < 2:
        if len(short_labels) == 1:
            dst = OUTPUT / f"{folder.name.replace('-converted','').strip()}.zip"
            shutil.copy2(str(zips[0]), str(dst))
            print(f"    → Copied as {dst.name}")
        continue

    # ── Cluster ──
    n = len(short_labels)
    sim = np.ones((n, n))
    for i in range(n):
        for j in range(i, n):
            c = 1 - cdist(embs_dict[short_labels[i]], embs_dict[short_labels[j]], metric="cosine")
            sim[i, j] = sim[j, i] = float(np.mean(c))

    dist = 1 - sim
    np.fill_diagonal(dist, 0)
    Z = linkage(sim, method="average")
    clusters = fcluster(Z, THRESHOLD, criterion="distance")
    unique_cids = sorted(set(clusters))

    # Map clusters to characters
    char_map = {}
    for cid in unique_cids:
        vols = [short_labels[i] for i in range(n) if clusters[i] == cid]
        char_map[cid] = vols

    # Print clusters
    short_name = folder.name.replace("-converted", "").strip()
    print(f"\n  ── Dedup Results ──")
    for ci, (cid, vols) in enumerate(char_map.items()):
        if len(vols) == 1:
            print(f"    Character_{ci+1}: {vols[0]} (unique)")
        else:
            intra = np.mean([sim[short_labels.index(a), short_labels.index(b)]
                            for a in vols for b in vols if a != b])
            print(f"    Character_{ci+1}: {vols[0]} ... {vols[-1]} ({len(vols)} zips, intra-sim={intra:.3f})")

    # Copy representative zips
    narrator_prefix = short_name.replace(" ", "_").replace(",", "").replace("-", "_").lower()
    vol_counter = {}
    for ci, (cid, vols) in enumerate(char_map.items()):
        vol_counter[ci] = 0
        # Pick the most central zip in the cluster
        if len(vols) > 1:
            # Which vol has highest avg sim to rest of cluster?
            best_vol = None
            best_sim = -1
            for v in vols:
                others = [x for x in vols if x != v]
                avg = np.mean([sim[short_labels.index(v), short_labels.index(x)] for x in others])
                if avg > best_sim:
                    best_sim = avg
                    best_vol = v
            chosen = [best_vol]
        else:
            chosen = vols

        for v in chosen:
            vol_counter[ci] += 1
            src = label_to_path[v]
            new_name = f"narrator_{narrator_prefix}_char{ci+1}_vol{vol_counter[ci]:02d}.zip"
            dst = OUTPUT / new_name
            shutil.copy2(str(src), str(dst))
            print(f"    → {new_name}")

    # ── Plot ──
    try:
        short = [s.replace(folder.name + "_", "")[:15] for s in short_labels]
        fig, ax = plt.subplots(figsize=(max(8, n*0.9), max(6, n*0.7)))
        sns.heatmap(sim, annot=True, fmt=".2f",
                    xticklabels=short, yticklabels=short,
                    cmap="RdYlGn", vmin=0, vmax=1, ax=ax, linewidths=0.5)
        ax.set_title(f"Speaker Similarity — {folder.name}", fontsize=11)
        plt.tight_layout()
        plt.savefig(str(OUTPUT / f"_plot_{folder.name[:40]}.png"), dpi=120)
        plt.close()
    except:
        pass

# Summary
print(f"\n{'='*60}")
print("DONE — deduped dataset in:")
print(f"  {OUTPUT}")
print(f"\nContents:")
for f in sorted(OUTPUT.iterdir()):
    if f.suffix == ".zip":
        size = f.stat().st_size / 1024 / 1024
        print(f"  {f.name:60s} {size:.1f} MB")
print(f"\nPlots saved in {OUTPUT}/")
for f in sorted(OUTPUT.glob("_plot_*.png")):
    print(f"  {f.name}")