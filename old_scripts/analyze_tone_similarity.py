#!/usr/bin/env python3
"""
Tone & voice similarity analysis across Alexandria dataset groups.

Compares:
  - TTS-generated samples (Kizu volumes 1-10)
  - Audiobook narrator samples (Luci Christian, Cherami Leigh, etc.)
  - Source audiobook chapter samples (original narrators)

Outputs: similarity heatmap, prosody divergence table, UMAP projection, summary.
"""

import os, sys, re, json, tempfile, zipfile, pickle, warnings, itertools, hashlib
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
import torch
import librosa
import soundfile as sf
from scipy.spatial.distance import pdist, squareform, cdist
from scipy.stats import wasserstein_distance, ks_2samp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# ─── Device ─────────────────────────────────────────────────────────
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {device}  |  ROCm HIP: {torch.version.hip if hasattr(torch.version, 'hip') else 'N/A'}")

# ─── Config ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

# Dynamically built from zips2/_deduped/ — one entry per deduplicated narrator voice.
# Run dedupe_zips2.py first to populate _deduped/ if it is empty.
# Subfolders starting with "_" (pipeline outputs) are skipped automatically.
_DEDUPED_ROOT = Path("/home/fakemitch/Desktop/zips2/_deduped")
ZIP_GROUPS = {}
if _DEDUPED_ROOT.is_dir():
    for _zp in sorted(_DEDUPED_ROOT.glob("*.zip")):
        _key = _zp.stem.replace("-converted", "").strip()
        # Sanitise for dict key: lowercase, spaces/hyphens to underscores
        _key = re.sub(r"[^a-z0-9]+", "_", _key.lower()).strip("_")
        ZIP_GROUPS[_key] = [str(_zp)]
else:
    print(f"WARNING: {_DEDUPED_ROOT} not found — run dedupe_zips2.py first.")

# How many samples per group to use (None = all)
MAX_SAMPLES_PER_GROUP = 200  # e.g. 200

# Output dir
OUTPUT_DIR = PROJECT_ROOT / "tone_analysis_output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Cache
CACHE_FILE = OUTPUT_DIR / "embeddings_cache.pkl"

# ─── Speaker embedding model ────────────────────────────────────────
print("Loading SpeechBrain ECAPA-TDNN speaker embedding model...")
from speechbrain.inference.speaker import EncoderClassifier

speaker_encoder = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir=str(OUTPUT_DIR / "models" / "ecapa"),
    run_opts={"device": device},
)
speaker_encoder.eval()

def extract_speaker_embedding(wav: np.ndarray, sr: int) -> np.ndarray:
    """Extract 192-dim speaker embedding from waveform."""
    with torch.no_grad():
        # SpeechBrain expects float32, shape (1, T) or (T,)
        if wav.dtype != np.float32:
            wav = wav.astype(np.float32)
        # Resample to 16kHz if needed (ECAPA expects 16k)
        if sr != 16000:
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
            sr = 16000
        # Normalize
        wav = wav / (np.abs(wav).max() + 1e-12)
        tensor = torch.from_numpy(wav).unsqueeze(0).to(device)
        emb = speaker_encoder.encode_batch(tensor).squeeze().cpu().numpy()
    return emb

# ─── Prosody features ───────────────────────────────────────────────
def extract_prosody(wav: np.ndarray, sr: int) -> dict:
    """Extract prosodic features: pitch stats, energy stats, speaking rate proxy."""
    if sr != 16000:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
        sr = 16000

    dur = len(wav) / sr

    # Pitch (f0) via librosa pyin
    f0, voiced_flag, voiced_probs = librosa.pyin(
        wav, fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C7"), sr=sr
    )
    f0_voiced = f0[voiced_flag]
    if len(f0_voiced) == 0:
        f0_voiced = np.array([0.0])

    # Energy (RMS)
    rms = librosa.feature.rms(y=wav, frame_length=2048, hop_length=512)[0]
    rms_db = librosa.amplitude_to_db(rms, ref=np.max)

    # Spectral centroid (brightness)
    spec_cent = librosa.feature.spectral_centroid(y=wav, sr=sr, hop_length=512)[0]

    # MFCC baseline (mean/std)
    mfcc = librosa.feature.mfcc(y=wav, sr=sr, n_mfcc=13, hop_length=512)

    return {
        "f0_mean": float(np.nanmean(f0_voiced)),
        "f0_std": float(np.nanstd(f0_voiced)),
        "f0_range": float(np.ptp(f0_voiced)),
        "rms_mean_db": float(np.mean(rms_db)),
        "rms_std_db": float(np.std(rms_db)),
        "spec_cent_mean": float(np.mean(spec_cent)),
        "spec_cent_std": float(np.std(spec_cent)),
        "mfcc_mean": mfcc.mean(axis=1),
        "mfcc_std": mfcc.std(axis=1),
        "duration": dur,
    }

# ─── WAV loading from zip ──────────────────────────────────────────
def load_wav_from_zip(zip_path: str, wav_name: str, sr_target: int = 16000) -> tuple:
    """Load a WAV from a zip file, return (waveform, sr)."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(wav_name) as f:
            wav, sr = sf.read(f)
    if len(wav.shape) > 1:
        wav = wav.mean(axis=1)  # mono
    if sr != sr_target:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=sr_target)
        sr = sr_target
    return wav, sr

def iter_wavs_in_zip(zip_path: str):
    """Yield (wav_name) for each WAV in a zip. Skips metadata files."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith(".wav"):
                # For kizu zips, prefer train samples
                yield name

# ─── Main pipeline ─────────────────────────────────────────────────
def main():
    all_embeddings = {}   # group -> list of embedding vectors
    all_prosody   = {}    # group -> list of prosody dicts
    all_wav_names = {}    # group -> list of (zip, wav_name)
    group_to_color = {}   # group -> color for plots

    # First try loading cache
    if CACHE_FILE.exists():
        print(f"Loading cached embeddings from {CACHE_FILE}")
        with open(CACHE_FILE, "rb") as f:
            cache_data = pickle.load(f)
        all_embeddings = cache_data.get("embeddings", {})
        all_prosody = cache_data.get("prosody", {})
        all_wav_names = cache_data.get("wav_names", {})
        # Check which groups we still need
        missing_groups = [g for g in ZIP_GROUPS if g not in all_embeddings]
        if missing_groups:
            print(f"Missing groups in cache: {missing_groups}")
        else:
            print("All groups cached, skipping extraction.")
    else:
        cache_data = {}
        missing_groups = list(ZIP_GROUPS.keys())

    # Extract for missing groups
    if any(g for g in ZIP_GROUPS if g not in all_embeddings):
        for group_name, zip_paths in ZIP_GROUPS.items():
            if group_name in all_embeddings:
                continue
            print(f"\n─── Processing group: {group_name} ───")
            group_embs = []
            group_pros = []
            group_wavs = []

            for zp in zip_paths:
                if not os.path.exists(zp):
                    print(f"  Zip not found: {zp}, skipping")
                    continue
                wav_names = list(iter_wavs_in_zip(zp))
                if MAX_SAMPLES_PER_GROUP and len(wav_names) > MAX_SAMPLES_PER_GROUP:
                    # Stratified: if train/val split, sample proportionally
                    train_names = [n for n in wav_names if n.startswith("train/")]
                    val_names   = [n for n in wav_names if n.startswith("val/")]
                    if train_names and val_names:
                        half = MAX_SAMPLES_PER_GROUP // 2
                        wav_names = (np.random.choice(train_names, min(half, len(train_names)), replace=False).tolist()
                                   + np.random.choice(val_names, min(half, len(val_names)), replace=False).tolist())
                    else:
                        wav_names = np.random.choice(wav_names, MAX_SAMPLES_PER_GROUP, replace=False).tolist()

                print(f"  Extracting {len(wav_names)} samples from {os.path.basename(zp)}...")
                for wname in tqdm(wav_names, desc=f"  {group_name}"):
                    try:
                        wav, sr = load_wav_from_zip(zp, wname)

                        # Speaker embedding
                        emb = extract_speaker_embedding(wav, sr)
                        group_embs.append(emb)

                        # Prosody
                        pros = extract_prosody(wav, sr)
                        group_pros.append(pros)

                        group_wavs.append((zp, wname))
                    except Exception as e:
                        # Silently skip corrupt samples
                        pass

            if group_embs:
                all_embeddings[group_name] = np.array(group_embs)
                all_prosody[group_name] = group_pros
                all_wav_names[group_name] = group_wavs
                print(f"  → {len(group_embs)} embeddings extracted")

    # Save cache
    cache_data = {
        "embeddings": all_embeddings,
        "prosody": all_prosody,
        "wav_names": all_wav_names,
    }
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(cache_data, f)
    print(f"\nCache saved to {CACHE_FILE}")

    # ────────────────────────────────────────────────────────────────
    #  ANALYSIS
    # ────────────────────────────────────────────────────────────────

    group_names = sorted(all_embeddings.keys())
    print(f"\n{'='*60}")
    print(f"Analyzing {len(group_names)} groups")
    print(f"{'='*60}")

    # ── 1. Speaker Similarity Matrix ──
    print("\n─── Computing speaker embedding similarity matrix ───")
    n_groups = len(group_names)
    sim_matrix = np.zeros((n_groups, n_groups))
    sim_std    = np.zeros((n_groups, n_groups))

    for i, g1 in enumerate(group_names):
        for j, g2 in enumerate(group_names):
            embs1 = all_embeddings[g1]
            embs2 = all_embeddings[g2]

            if i == j:
                # Intra-group: mean pairwise cosine sim
                if len(embs1) > 1:
                    cos_sim = 1 - squareform(pdist(embs1, metric="cosine"))
                    sim_matrix[i, j] = np.mean(cos_sim)
                    sim_std[i, j] = np.std(cos_sim)
                else:
                    sim_matrix[i, j] = 1.0
                    sim_std[i, j] = 0.0
            else:
                # Inter-group: mean cross cosine sim
                cos_sim = 1 - cdist(embs1, embs2, metric="cosine")
                sim_matrix[i, j] = np.mean(cos_sim)
                sim_std[i, j] = np.std(cos_sim)

    # Plot similarity matrix
    fig, ax = plt.subplots(figsize=(14, 12))
    # Short labels
    short_names = [n.replace("_", " ") for n in group_names]
    sns.heatmap(sim_matrix, annot=True, fmt=".3f", xticklabels=short_names,
                yticklabels=short_names, cmap="RdYlGn", vmin=0, vmax=1,
                ax=ax, linewidths=0.5)
    ax.set_title("Mean Cosine Similarity of Speaker Embeddings", fontsize=14)
    plt.tight_layout()
    plt.savefig(str(OUTPUT_DIR / "speaker_similarity_matrix.png"), dpi=150)
    plt.close()
    print(f"  Saved heatmap to {OUTPUT_DIR / 'speaker_similarity_matrix.png'}")

    # ── 2. Group-level similarity table ──
    sim_df = pd.DataFrame(sim_matrix, index=short_names, columns=short_names)
    sim_df.to_csv(str(OUTPUT_DIR / "speaker_similarity_table.csv"))
    print(f"  Saved CSV table to {OUTPUT_DIR / 'speaker_similarity_table.csv'}")

    # ── 3. Find closest/farthest pairs ──
    print("\n─── Top-5 most similar group pairs (inter-group) ───")
    pairs = []
    for i in range(n_groups):
        for j in range(i+1, n_groups):
            pairs.append((short_names[i], short_names[j], sim_matrix[i, j]))
    pairs.sort(key=lambda x: -x[2])
    for name1, name2, sim in pairs[:5]:
        print(f"  {name1:25s} ↔ {name2:25s}  sim={sim:.4f}")

    print("\n─── Top-5 least similar group pairs (inter-group) ───")
    for name1, name2, sim in pairs[-5:]:
        print(f"  {name1:25s} ↔ {name2:25s}  sim={sim:.4f}")

    # Intra-group cohesion
    print("\n─── Intra-group cohesion (higher = more consistent voice) ───")
    intra = [(short_names[i], sim_matrix[i, i]) for i in range(n_groups)]
    intra.sort(key=lambda x: -x[1])
    for name, sim in intra:
        print(f"  {name:25s}  cohesion={sim:.4f}")

    # ── 4. Prosody divergence (EMD) ──
    print("\n─── Prosody distribution comparison (Earth Mover's Distance) ───")
    prosody_metrics = ["f0_mean", "f0_std", "f0_range", "rms_mean_db",
                       "rms_std_db", "spec_cent_mean", "spec_cent_std", "duration"]

    emd_table = np.zeros((n_groups, n_groups))
    prosody_results = []

    for metric in prosody_metrics:
        emd_mat = np.zeros((n_groups, n_groups))
        for i, g1 in enumerate(group_names):
            vals1 = np.array([p[metric] for p in all_prosody[g1]])
            for j, g2 in enumerate(group_names):
                vals2 = np.array([p[metric] for p in all_prosody[g2]])
                emd = wasserstein_distance(vals1, vals2)
                emd_mat[i, j] = emd
        prosody_results.append((metric, emd_mat))

        # Find most divergent pair for this metric
        max_idx = np.unravel_index(np.argmax(emd_mat), emd_mat.shape)
        print(f"  {metric:20s} max EMD={emd_mat[max_idx]:.2f}  "
              f"({short_names[max_idx[0]]} vs {short_names[max_idx[1]]})")

    # EMD heatmap (mean across all prosody metrics)
    mean_emd = np.mean([r[1] for r in prosody_results], axis=0)
    fig, ax = plt.subplots(figsize=(14, 12))
    sns.heatmap(mean_emd, annot=True, fmt=".2f", xticklabels=short_names,
                yticklabels=short_names, cmap="YlOrRd", ax=ax, linewidths=0.5)
    ax.set_title("Mean Prosody Divergence (Earth Mover's Distance)", fontsize=14)
    plt.tight_layout()
    plt.savefig(str(OUTPUT_DIR / "prosody_divergence_matrix.png"), dpi=150)
    plt.close()
    print(f"\n  Saved prosody divergence heatmap to {OUTPUT_DIR / 'prosody_divergence_matrix.png'}")

    # ── 5. Prosody box plots ──
    print("\n─── Generating prosody distribution plots ───")
    for metric in prosody_metrics[:6]:  # skip mfcc, duration
        fig, ax = plt.subplots(figsize=(12, 5))
        data = []
        labels = []
        for g in group_names:
            vals = [p[metric] for p in all_prosody[g]]
            if vals:
                data.append(vals)
                labels.append(short_names[group_names.index(g)])
        sns.boxplot(data=data, ax=ax)
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.set_title(f"Prosody: {metric}")
        plt.tight_layout()
        plt.savefig(str(OUTPUT_DIR / f"prosody_{metric}.png"), dpi=100)
        plt.close()
    print(f"  Saved {len(prosody_metrics[:6])} box plots to {OUTPUT_DIR}")

    # ── 6. UMAP projection ──
    print("\n─── Computing UMAP projection ───")
    import umap

    # Build combined embedding matrix
    all_embs_list = []
    all_group_idx = []
    for i, g in enumerate(group_names):
        embs = all_embeddings[g]
        all_embs_list.append(embs)
        all_group_idx.extend([i] * len(embs))

    combined_embs = np.vstack(all_embs_list)
    group_idx_arr = np.array(all_group_idx)

    print(f"  Total samples: {combined_embs.shape[0]}, embedding dim: {combined_embs.shape[1]}")

    # Sample if too many for UMAP
    if combined_embs.shape[0] > 5000:
        idxs = np.random.choice(combined_embs.shape[0], 5000, replace=False)
        combined_embs = combined_embs[idxs]
        group_idx_arr = group_idx_arr[idxs]

    reducer = umap.UMAP(n_neighbors=15, min_dist=0.1, random_state=42)
    umap_coords = reducer.fit_transform(combined_embs)

    # Plot UMAP
    palette = sns.color_palette("husl", n_groups)
    fig, ax = plt.subplots(figsize=(12, 10))
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
    plt.savefig(str(OUTPUT_DIR / "umap_embedding_projection.png"), dpi=150)
    plt.close()
    print(f"  Saved UMAP to {OUTPUT_DIR / 'umap_embedding_projection.png'}")

    # ── 7. Summary ──
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"\nGroups analyzed: {n_groups}")
    for g in group_names:
        print(f"  {g:25s}  {len(all_embeddings[g]):5d} samples")

    print(f"\nOutput directory: {OUTPUT_DIR}")
    print(f"Files generated:")
    for f in sorted(OUTPUT_DIR.glob("*")):
        if f.suffix in (".png", ".csv", ".pkl"):
            print(f"  {f.name}")

    print("\nDone!")


if __name__ == "__main__":
    main()