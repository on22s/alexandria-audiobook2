"""Do the prosodic/voice-quality features two audiobook-TTS papers prune on
predict adapter fidelity, beyond the ECAPA tone spread already measured?

Chalamandaris et al. (LREC 2014) prune phrases by Mahalanobis distance of
(F0 mean, F0 std) from the corpus centroid; Piits et al. (LREC 2022) found
character speech has lower HNR, steeper spectral slope and bigger loudness
swings than narration. This computes those per clip on a seeded sample of each
shipped dataset and joins to dataset_tone_spread.json's ECAPA fidelity.

Result (2026-09-13, 74 adapters x 40 clips): three features correlate with
fidelity on their own (across-clip F0 spread r=-0.37, between/within F0
r=-0.39, alpha-ratio spread r=-0.43) and the five REBUILD datasets have 2.6x
the across-clip pitch spread of working ones (44.6 vs 17.1 Hz, p=0.02) - but
partialled on dataset_tone_spread's ECAPA tightness every feature falls to
|r|<=0.25. A second instrument agrees with the tone-mixture diagnosis and does
not improve on it. CPU only, librosa pyin, seeded clip sample.
"""
import argparse, io, json, os, random, sys, time, zipfile
import numpy as np, librosa

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--manifest", default=os.path.join(REPO, "lora_models", "manifest.json"))
ap.add_argument("--spread", default=os.path.join(REPO, "ab_test_runtime", "experiments", "dataset_tone_spread.json"))
ap.add_argument("--clips", type=int, default=40)
ap.add_argument("--seed", type=int, default=20260913)
ap.add_argument("--out", default=os.path.join(REPO, "ab_test_runtime", "experiments", "dataset_prosodic_spread.json"))
args = ap.parse_args()
MANIFEST, SPREAD, OUT, N_CLIPS, SR = args.manifest, args.spread, args.out, args.clips, 16000


def clip_features(y):
    f0, voiced, _ = librosa.pyin(y, fmin=60, fmax=500, sr=SR, frame_length=1024)
    f0v = f0[voiced & np.isfinite(f0)]
    rms = librosa.feature.rms(y=y)[0]
    db = 20 * np.log10(rms + 1e-8)
    db = db[db > db.max() - 40]  # ignore silence
    S = np.abs(librosa.stft(y, n_fft=1024)) ** 2
    freqs = librosa.fft_frequencies(sr=SR, n_fft=1024)
    lo = (freqs >= 50) & (freqs <= 1000); hi = (freqs > 1000) & (freqs <= 5000)
    alpha = 10 * np.log10(S[lo].sum() / (S[hi].sum() + 1e-12) + 1e-12)
    band = (freqs >= 0) & (freqs <= 500)
    logp = 10 * np.log10(S[band].mean(axis=1) + 1e-12)
    slope = np.polyfit(freqs[band], logp, 1)[0] * 1000  # dB per kHz, 0-500 Hz
    h, p = librosa.effects.hpss(y)
    hnr_proxy = 10 * np.log10((h ** 2).sum() / ((p ** 2).sum() + 1e-12) + 1e-12)
    return {
        "f0_mean": float(np.mean(f0v)) if len(f0v) else float("nan"),
        "f0_std": float(np.std(f0v)) if len(f0v) else float("nan"),
        "voiced_frac": float(voiced.mean()),
        "loud_std": float(db.std()), "alpha_ratio": float(alpha),
        "slope_0_500": float(slope), "hnr_proxy": float(hnr_proxy),
        "dur": float(len(y) / SR),
    }


def dataset_summary(feats):
    keys = ["f0_mean", "f0_std", "voiced_frac", "loud_std", "alpha_ratio", "slope_0_500", "hnr_proxy", "dur"]
    M = np.array([[f[k] for k in keys] for f in feats], dtype=float)
    M = M[np.isfinite(M).all(axis=1)]
    out = {"clips_used": int(len(M))}
    for i, k in enumerate(keys):
        out[k + "_mean"] = float(M[:, i].mean()); out[k + "_std"] = float(M[:, i].std())
    # Chalamandaris: Mahalanobis of (f0_mean, f0_std) to the dataset centroid
    X = M[:, :2]
    cov = np.cov(X.T) + np.eye(2) * 1e-6
    d = np.sqrt(np.einsum("ij,jk,ik->i", X - X.mean(0), np.linalg.inv(cov), X - X.mean(0)))
    out["f0_mahal_mean"] = float(d.mean()); out["f0_mahal_p90"] = float(np.percentile(d, 90))
    out["f0_outlier_frac_2sd"] = float((d > 2.0).mean())
    # scale-free mixture proxies: spread of clip-level f0 mean relative to typical within-clip std
    out["f0_between_over_within"] = float(M[:, 0].std() / (M[:, 1].mean() + 1e-6))
    return out


manifest = json.load(open(MANIFEST))
spread = {r["adapter"]: r for r in json.load(open(SPREAD))["results"]}
rng = random.Random(args.seed)
results, t0 = [], time.time()
for i, ad in enumerate(manifest):
    z = zipfile.ZipFile(ad["zip_source"])
    wavs = [n for n in z.namelist() if n.startswith("train/") and n.endswith(".wav")]
    rng.shuffle(wavs)
    feats = []
    for n in wavs[:N_CLIPS]:
        y, _ = librosa.load(io.BytesIO(z.read(n)), sr=SR, mono=True)
        if len(y) < SR // 2:
            continue
        feats.append(clip_features(y))
    s = dataset_summary(feats)
    s.update({"adapter": ad["id"], "dataset": ad["dataset_id"],
              "ecapa": spread.get(ad["id"], {}).get("ecapa"),
              "tightness": spread.get(ad["id"], {}).get("tightness")})
    results.append(s)
    json.dump({"n_clips_per_dataset": N_CLIPS, "seed": args.seed, "results": results,
               "provenance": provenance(__file__, args)}, open(OUT, "w"), indent=1)
    print(f"{i+1}/{len(manifest)} {ad['id']} clips={s['clips_used']} f0={s['f0_mean_mean']:.0f}±{s['f0_mean_std']:.0f} "
          f"mahal={s['f0_mahal_mean']:.2f} hnr={s['hnr_proxy_mean']:.1f} ecapa={s['ecapa']} [{time.time()-t0:.0f}s]", flush=True)
print("DONE")
