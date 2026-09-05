"""Is tightness selecting for a CLEAN RECORDING rather than a consistent voice?

WHY THIS MATTERS MORE THAN IT LOOKS. Choosing the clips nearest a dataset's
speaker-embedding centroid improves the adapter trained on them - 43 of 54
books on the second half of the tightening alone, p=0.00002. The stated
mechanism is that a tighter dataset is more nearly one voice. But a speaker
embedding is computed from audio, and audio that is quiet, clipped, noisy or
half silence will embed oddly for reasons that have nothing to do with WHOSE
voice it is. If the tight arms are simply the cleaner recordings, the effective
lever is audio quality, and the field's answer to that is a perceptual filter -
Emilia-Pipe keeps 29.4% of raw audio at DNSMOS >= 3.0 - not a centroid.

THESE ARE SIGNAL STATISTICS, NOT PERCEPTUAL QUALITY, and the distinction is the
whole reason this is honest. No MOS predictor is installed here; UTMOSv2 and
NISQA are what `robotic_proxy.py` already names as the right tools and neither
is available. So this does not score quality. It asks a narrower question that
does not need a validated quality metric: DO THE TWO ARMS DIFFER SYSTEMATICALLY
ON MEASURABLE PROPERTIES OF THE SIGNAL? A null is informative - it says the
tight arm is not merely the cleaner audio. A difference does not prove the
mechanism, it identifies a confound worth measuring properly.

WHAT IS MEASURED, and why each could masquerade as tightness:

    snr_db          crude level separation: loud frames against quiet ones.
                    Noisy clips embed toward each other, not toward a speaker.
    silence_frac    fraction of frames below a floor. A clip that is mostly
                    room tone carries little speaker evidence either way.
    spectral_flat   flat spectra mean noise-like content.
    clip_rate       samples at full scale. Clipping distorts formants, which is
                    exactly what a speaker embedding reads.
    rms_db          overall level, and
    rms_var         how much the level moves within a clip.

Paired by book, so a book with quiet source material cannot dominate: every
comparison is that book's tight arm against that book's own control arm.
"""
import argparse, glob, json, os, statistics, sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.provenance import provenance  # noqa: E402


def clip_stats(path):
    """-> dict of signal statistics for one wav, or None if unreadable."""
    import soundfile as sf
    try:
        y, sr = sf.read(path, dtype="float64", always_2d=False)
    except Exception:
        return None
    if y.ndim > 1:
        y = y.mean(axis=1)
    if len(y) < sr // 10:
        return None
    n = 1024
    frames = len(y) // n
    if frames < 4:
        return None
    f = y[:frames * n].reshape(frames, n)
    rms = np.sqrt((f ** 2).mean(axis=1) + 1e-12)
    hi = float(np.percentile(rms, 90))
    lo = float(np.percentile(rms, 10))
    # Crude, and labelled as such: the loud/quiet percentile ratio, not a
    # speech/noise separation. It is comparable BETWEEN arms of one book, which
    # is all this needs to be.
    snr = 20.0 * np.log10(max(hi, 1e-9) / max(lo, 1e-9))
    floor = max(hi * 0.05, 1e-6)
    spec = np.abs(np.fft.rfft(f * np.hanning(n), axis=1)) + 1e-12
    gm = np.exp(np.log(spec).mean(axis=1))
    flat = float((gm / spec.mean(axis=1)).mean())
    return {
        "snr_db": float(snr),
        "silence_frac": float((rms < floor).mean()),
        "spectral_flat": flat,
        "clip_rate": float((np.abs(y) > 0.999).mean()),
        "rms_db": float(20.0 * np.log10(max(float(rms.mean()), 1e-9))),
        "rms_var": float(rms.std()),
    }


FEATURES = ["snr_db", "silence_frac", "spectral_flat", "clip_rate",
            "rms_db", "rms_var"]
# DNSMOS is the published no-reference model Emilia-Pipe filters on at >= 3.0.
# It is here to VALIDATE the six statistics above, which are hand-rolled and
# therefore exactly the kind of unchecked instrument this project has been
# burned by. If DNSMOS agrees with them on the same books, they were measuring
# something real; if it disagrees, they were not, and the conclusion drawn from
# them has to be withdrawn rather than defended.
DNSMOS_FEATURES = ["ovrl_mos", "sig_mos", "bak_mos"]


def dnsmos_stats(path):
    """-> DNSMOS scores for one clip, or None. Requires 16 kHz."""
    import soundfile as sf
    from speechmos import dnsmos
    try:
        y, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception:
        return None
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != 16000:
        import librosa
        y = librosa.resample(y, orig_sr=sr, target_sr=16000)
        sr = 16000
    if len(y) < sr // 2:
        return None
    try:
        r = dnsmos.run(y, sr)
    except Exception:
        return None
    return {k: float(r[k]) for k in DNSMOS_FEATURES if k in r}


def arm_mean(arm_dir, sample, rng, with_dnsmos=False):
    wavs = sorted(glob.glob(os.path.join(arm_dir, "train", "*.wav")))
    if not wavs:
        return None, 0
    if sample and len(wavs) > sample:
        wavs = [wavs[i] for i in rng.choice(len(wavs), sample, replace=False)]
    rows = []
    for w in wavs:
        st = clip_stats(w)
        if not st:
            continue
        if with_dnsmos:
            d = dnsmos_stats(w)
            if d:
                st.update(d)
        rows.append(st)
    if not rows:
        return None, 0
    keys = [k for k in FEATURES + (DNSMOS_FEATURES if with_dnsmos else [])
            if all(k in r for r in rows)]
    return {k: statistics.mean(r[k] for r in rows) for k in keys}, len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", default=os.path.join(
        REPO, "ab_test_runtime", "tight_selection"))
    ap.add_argument("--arms", nargs="+", default=["control", "tight"])
    ap.add_argument("--sample", type=int, default=40,
                    help="clips per arm; 0 for all")
    ap.add_argument("--dnsmos", action="store_true",
                    help="also score every sampled clip with DNSMOS, the model "
                         "Emilia-Pipe filters on; ~0.8s per clip")
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "arm_audio_statistics.json"))
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    books = []
    for tag in sorted(os.listdir(args.work)):
        dirs = {a: os.path.join(args.work, tag, a) for a in args.arms}
        if not all(os.path.isdir(d) for d in dirs.values()):
            continue
        means, counts = {}, {}
        for a, d in dirs.items():
            m, n = arm_mean(d, args.sample, rng, args.dnsmos)
            if m is None:
                means = None
                break
            means[a], counts[a] = m, n
        if means:
            books.append({"tag": tag, "clips": counts, "arms": means})
        print(f"  {len(books):3} {tag[:52]}", flush=True)

    doc = {
        "note": "Signal statistics per arm, paired by book. NOT perceptual "
                "quality - no MOS predictor is installed. Asks only whether "
                "the arms differ systematically on measurable properties of "
                "the signal, which is what would make audio quality a confound "
                "for the tightness result.",
        "arms": args.arms, "books": len(books),
        "clips_per_arm_sampled": args.sample,
        "per_book": books,
    }
    a, b = args.arms[0], args.arms[1]
    from scipy import stats
    comp = {}
    for k in FEATURES + (DNSMOS_FEATURES if args.dnsmos else []):
        if not all(k in bk["arms"][a] and k in bk["arms"][b] for bk in books):
            continue
        xs = [bk["arms"][a][k] for bk in books]
        ys = [bk["arms"][b][k] for bk in books]
        d = [y - x for x, y in zip(xs, ys)]
        if len(d) >= 3 and any(d):
            w = stats.wilcoxon(d)
            comp[k] = {"mean_%s" % a: statistics.mean(xs),
                       "mean_%s" % b: statistics.mean(ys),
                       "mean_delta": statistics.mean(d),
                       "wins": sum(1 for x in d if x > 0),
                       "n": len(d), "p": float(w.pvalue)}
    doc["comparison"] = comp
    doc["provenance"] = provenance(__file__, vars(args))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(doc, open(args.out, "w", encoding="utf-8"), indent=1)

    print(f"\n{len(books)} books, {a} vs {b}, {args.sample} clips per arm\n")
    print(f"{'feature':16}{a:>12}{b:>12}{'delta':>11}{'wins':>8}{'p':>10}")
    for k, c in comp.items():
        print(f"{k:16}{c['mean_%s' % a]:12.4f}{c['mean_%s' % b]:12.4f}"
              f"{c['mean_delta']:+11.4f}{c['wins']:5}/{c['n']:<3}{c['p']:10.4f}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
