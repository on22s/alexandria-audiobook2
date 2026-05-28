#!/usr/bin/env python3
"""
Narrator voice analysis:
  1. Assigns descriptive labels (pitch, age, energy, gender presentation)
  2. Detects emotional sub-clusters within each narrator
  3. For large narrators (>1 distinct emotion), renames with emotion tags
"""

import os, sys, warnings, zipfile, pickle, random, json
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

ROOT = Path("/home/fakemitch/Desktop/zips2/_deduped")
OUTPUT = ROOT.parent / "_analyzed"
OUTPUT.mkdir(exist_ok=True)
OUTPUT_NEW = ROOT.parent / "_deduped_labeled"
OUTPUT_NEW.mkdir(exist_ok=True)

CACHE_DIR = OUTPUT / "_cache"
CACHE_DIR.mkdir(exist_ok=True)

SAMPLES_PER_ZIP = 200  # more for reliable emotion detection

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

# ─── Prosody extraction for emotion detection ──────────────────────
def extract_prosody(wav, sr):
    """Returns pitch, energy, spectral features."""

    f0, voiced_flag, _ = librosa.pyin(
        wav, fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C7"), sr=sr
    )
    f0_voiced = f0[voiced_flag]
    if len(f0_voiced) == 0:
        f0_voiced = np.array([0.0])

    rms = librosa.feature.rms(y=wav, frame_length=2048, hop_length=512)[0]
    rms_db = librosa.amplitude_to_db(rms, ref=np.max)

    spec_cent = librosa.feature.spectral_centroid(y=wav, sr=sr, hop_length=512)[0]
    spec_bw = librosa.feature.spectral_bandwidth(y=wav, sr=sr, hop_length=512)[0]
    zcr = librosa.feature.zero_crossing_rate(wav, hop_length=512)[0]
    mfcc = librosa.feature.mfcc(y=wav, sr=sr, n_mfcc=13, hop_length=512)

    return {
        "f0_mean": float(np.nanmean(f0_voiced)),
        "f0_std": float(np.nanstd(f0_voiced)),
        "f0_range": float(np.ptp(f0_voiced)),
        "f0_min": float(np.nanmin(f0_voiced)),
        "f0_max": float(np.nanmax(f0_voiced)),
        "rms_mean_db": float(np.mean(rms_db)),
        "rms_std_db": float(np.std(rms_db)),
        "spec_cent_mean": float(np.mean(spec_cent)),
        "spec_cent_std": float(np.std(spec_cent)),
        "spec_bw_mean": float(np.mean(spec_bw)),
        "zcr_mean": float(np.mean(zcr)),
        "mfcc_mean": mfcc.mean(axis=1).tolist(),
    }


# ─── Voice description based on prosody ────────────────────────────
def describe_voice(all_prosody):
    """Produce descriptive labels for a narrator based on prosody stats."""
    f0_mean = np.mean([p["f0_mean"] for p in all_prosody if p["f0_mean"] > 0])
    f0_range = np.mean([p["f0_range"] for p in all_prosody])
    f0_std = np.mean([p["f0_std"] for p in all_prosody])
    rms_mean = np.mean([p["rms_mean_db"] for p in all_prosody])
    rms_std = np.mean([p["rms_std_db"] for p in all_prosody])
    spec_cent = np.mean([p["spec_cent_mean"] for p in all_prosody])
    zcr_mean = np.mean([p["zcr_mean"] for p in all_prosody])

    tags = []

    # Pitch-based age/gender presentation
    if f0_mean < 140:
        tags.append("deep_voice")
        if f0_std < 25:
            tags.append("monotone")
        else:
            tags.append("expressive_low")
    elif f0_mean < 200:
        tags.append("mid_voice")
        if f0_std < 30:
            tags.append("even_keeled")
        else:
            tags.append("varied_mid")
    elif f0_mean < 260:
        tags.append("bright_voice")
        if f0_std > 40:
            tags.append("animated")
        else:
            tags.append("clear_bright")
    else:
        tags.append("high_voice")
        if f0_std > 50:
            tags.append("very_animated")
        else:
            tags.append("light_voice")

    # Energy descriptors
    if rms_mean < -20:
        tags.append("quiet")
    elif rms_mean > -10:
        tags.append("loud")
    else:
        tags.append("moderate_volume")

    if rms_std > 8:
        tags.append("dynamic_range")
    elif rms_std < 4:
        tags.append("steady")

    # Brightness (spectral centroid)
    if spec_cent < 800:
        tags.append("warm_tone")
    elif spec_cent > 1400:
        tags.append("bright_tone")
    else:
        tags.append("neutral_tone")

    # ZCR — breathiness/roughness
    if zcr_mean > 0.08:
        tags.append("breathy")
    elif zcr_mean < 0.03:
        tags.append("smooth")

    # Pitch range — expressiveness
    if f0_range > 200:
        tags.append("wide_range_dramatic")
    elif f0_range < 60:
        tags.append("narrow_range_subdued")
    else:
        tags.append("moderate_range")

    return ", ".join(tags)


# ─── Voice-type keys and classifier ──────────────────────────────
PROSODY_KEYS = ["f0_mean", "f0_std", "f0_range", "rms_mean_db", "rms_std_db", "spec_cent_mean", "zcr_mean"]

VOICE_TYPE_LABELS = {
    0: "authoritative_deep",
    1: "youthful_energetic",
    2: "intimate_dramatic",
    3: "bright_expressive",
}

_kmeans_model = None
_kmeans_scaler = None

from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

def fit_voice_clusters(all_avg_prosody):
    """Fit KMeans on all narrators' avg prosody. Returns cluster labels."""
    global _kmeans_model, _kmeans_scaler
    features = np.array([[p[k] for k in PROSODY_KEYS] for p in all_avg_prosody])
    _kmeans_scaler = StandardScaler()
    z = _kmeans_scaler.fit_transform(features)
    _kmeans_model = KMeans(n_clusters=4, random_state=42, n_init=10)
    return _kmeans_model.fit_predict(z)

def classify_voice_type(avg_prosody):
    feat = np.array([[avg_prosody[k] for k in PROSODY_KEYS]])
    z = _kmeans_scaler.transform(feat)
    cid = _kmeans_model.predict(z)[0]
    return VOICE_TYPE_LABELS.get(cid, f"type_{cid}")

def energy_flavor(avg_prosody, all_rms_std):
    rms_std = avg_prosody["rms_std_db"]
    mean, std = np.mean(all_rms_std), np.std(all_rms_std)
    z = (rms_std - mean) / std if std > 0 else 0
    if z < -0.5:
        return "calm"
    elif z < 0.5:
        return "dynamic"
    else:
        return "intense"


# ─── Main ──────────────────────────────────────────────────────────
def main():
    zips = sorted([f for f in ROOT.iterdir() if f.suffix == ".zip"])
    print(f"Found {len(zips)} deduped zips to analyze\n")

    narrator_data = defaultdict(lambda: {"zips": [], "prosody": [], "embs": []})

    # Phase 1: Extract prosody + embeddings per zip
    for zp in zips:
        label = zp.stem
        cache_key = f"analysis_{label}"
        cache_path = CACHE_DIR / f"{cache_key}.pkl"

        if cache_path.exists():
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
            narrator_data[label]["prosody"].extend(data["prosody"])
            # For clustering, also store per-zip average
            narrator_data[label]["zips"].append({
                "path": zp,
                "avg_prosody": data["avg_prosody"],
                "avg_emb": data["avg_emb"],
            })
            narrator_data[label]["embs"].append(data["avg_emb"])
            print(f"  {label:60s} (cached)")
            continue

        all_wavs = [n for n in zipfile.ZipFile(zp).namelist() if n.endswith(".wav")]
        train_wavs = [n for n in all_wavs if n.startswith("train/")]
        if train_wavs:
            all_wavs = train_wavs

        n_samples = min(SAMPLES_PER_ZIP, len(all_wavs))
        selected = random.sample(all_wavs, n_samples)

        prosody_list = []
        emb_list = []
        for wn in tqdm(selected, desc=f"  {label[:50]}", leave=False):
            try:
                wav, sr = load_wav(str(zp), wn)
                emb = extract_embedding(wav, sr)
                pros = extract_prosody(wav, sr)
                emb_list.append(emb)
                prosody_list.append(pros)
            except:
                pass

        if emb_list:
            avg_emb = np.mean(emb_list, axis=0)
            avg_pros = {k: np.mean([p[k] for p in prosody_list]) for k in PROSODY_KEYS}

            data = {"prosody": prosody_list, "avg_prosody": avg_pros, "avg_emb": avg_emb}
            with open(cache_path, "wb") as f:
                pickle.dump(data, f)

            narrator_data[label]["prosody"].extend(prosody_list)
            narrator_data[label]["zips"].append({
                "path": zp,
                "avg_prosody": avg_pros,
                "avg_emb": avg_emb,
            })
            narrator_data[label]["embs"].append(avg_emb)
            print(f"  {label:60s} {len(prosody_list)} samples")

    # Phase 2: Describe each narrator, detect sub-clusters
    print(f"\n{'='*70}")
    print("NARRATOR PROFILES")
    print(f"{'='*70}")

    all_descriptions = []

    # Gather all avg prosody for global KMeans fitting
    all_avg_prosody = []
    for label, data in sorted(narrator_data.items()):
        if not data["zips"]:
            continue
        for z in data["zips"]:
            all_avg_prosody.append(z["avg_prosody"])

    cluster_labels = fit_voice_clusters(all_avg_prosody)
    all_rms_std = [p["rms_std_db"] for p in all_avg_prosody]
    narrator_index = 0

    for label, data in sorted(narrator_data.items()):
        if not data["zips"]:
            continue

        all_pros = data["prosody"]

        # Voice description
        desc = describe_voice(all_pros)
        all_descriptions.append((label, desc))

        print(f"\n  {label}")
        print(f"  {'─'*60}")
        print(f"  Description: {desc}")

        # Classify each zip in this narrator
        for z in data["zips"]:
            z["voice_type"] = classify_voice_type(z["avg_prosody"])
            z["energy"] = energy_flavor(z["avg_prosody"], all_rms_std)
            narrator_index += 1

        # Show
        for z in data["zips"]:
            print(f"  → {z['voice_type']:25s}  energy={z['energy']}")

    # Phase 3: Copy with descriptive names
    print(f"\n{'='*70}")
    print("RENAMED OUTPUT")
    print(f"{'='*70}")

    for label, data in sorted(narrator_data.items()):
        for z in data["zips"]:
            src = z["path"]
            # Extract narrator name from label
            parts = label.replace("narrator_", "").split("_char")
            narrator_name = parts[0] if len(parts) > 1 else label
            char_part = f"_char{parts[1]}" if len(parts) > 1 else ""
            voice_type = z.get("voice_type", "unknown")

            # Get description tags for filename
            desc_map = {d[0]: d[1] for d in all_descriptions}
            voice_summary = desc_map.get(label, "").replace(", ", "_")[:40]

            new_name = f"narrator_{narrator_name}{char_part}_{voice_type}_{voice_summary}.zip"
            dst = OUTPUT_NEW / new_name
            import shutil
            shutil.copy2(str(src), str(dst))
            print(f"  {src.name:55s} → {new_name}")

    # Save profile report
    report = {"narrators": []}
    for label, data in sorted(narrator_data.items()):
        desc = dict(all_descriptions).get(label, "")
        report["narrators"].append({
            "name": label,
            "description": desc,
            "voice_types": list(set(z.get("voice_type", "unknown") for z in data["zips"])),
        })

    with open(OUTPUT / "narrator_profiles.json", "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nProfiles saved to {OUTPUT / 'narrator_profiles.json'}")
    print(f"Labeled zips in: {OUTPUT_NEW}")
    print("\nDone!")


if __name__ == "__main__":
    main()