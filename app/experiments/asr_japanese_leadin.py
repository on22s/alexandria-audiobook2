"""Goal 5.4, Japanese alignment: how much of the 272 ms is the clips' own lead-in silence?

The instrument (asr_backends.build_alignment_probe) scores each predicted segment start
against the FILE start of each clip. A method that finds where speech begins is late by
however much quiet the clip carries before its first word. This measures that lead-in on
the same 50 clips (kokoro_ja_asr_eval, row offset 0) two independent ways, and rescores
Silero VAD's segment starts against both truths:

  - silero      first speech segment start from asr_backends.run_silero_vad on the clip
  - energy      first 10 ms frame where RMS stays 20 dB above the clip's own 10th-percentile
                floor for 50 ms - a definition that shares nothing with any detector scored

Scoring Silero against a truth built from Silero would be circular, so only the energy
onset is used as the alternative truth. The two lead-in estimates are both stored because
their disagreement bounds how precisely "speech start" is defined at all.
"""
import argparse, json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, HERE)
from experiments.provenance import provenance  # noqa: E402
import asr_backends as ab  # noqa: E402


def energy_onset(path, frame=0.01, rise_db=20.0, hold=0.05):
    import numpy as np
    import soundfile as sf
    a, sr = sf.read(path, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    n = int(frame * sr)
    f = np.array([np.sqrt(np.mean(a[i:i + n] ** 2) + 1e-12) for i in range(0, len(a) - n, n)])
    thr = np.percentile(f, 10) * 10 ** (rise_db / 20)
    k = int(hold / frame)
    for i in range(len(f) - k):
        if (f[i:i + k] > thr).all():
            return i * frame
    return 0.0


def dist(xs):
    xs = sorted(xs)
    return {"median_s": round(statistics.median(xs), 3), "p10_s": round(xs[len(xs) // 10], 3),
            "p90_s": round(xs[len(xs) * 9 // 10 - 1], 3), "max_s": round(xs[-1], 3), "n": len(xs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="ab_test_runtime/kokoro_ja_asr_eval/build.json")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--row-offset", type=int, default=0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = json.load(open(os.path.join(REPO, a.build), encoding="utf-8"))["test"][a.row_offset:a.row_offset + a.limit]
    lead_silero, lead_energy = {}, {}
    for r in rows:
        wav = os.path.join(REPO, r["human_wav"])
        _, segs = ab.run_silero_vad(wav)
        lead_silero[r["id"]] = min(s[0] for s in segs) if segs else 0.0
        lead_energy[r["id"]] = energy_onset(wav)
    probe, truth = ab.build_alignment_probe(rows, os.path.join(REPO, "ab_test_runtime", "asr_bench", "leadin_probe.wav"))
    _, segs = ab.run_silero_vad(probe)
    starts = sorted(s[0] for s in segs)
    speech_truth = [dict(t, start=t["start"] + lead_energy[t["id"]]) for t in truth]
    out = {
        "instrument": {"build": a.build, "row_offset": a.row_offset, "clips": len(truth), "gap_s": 0.5},
        "leadin_silero": dist(lead_silero.values()),
        "leadin_energy": dist(lead_energy.values()),
        "silero_vs_clip_start": ab.score_alignment(truth, [(s, s) for s in starts]),
        "silero_vs_energy_speech_onset": ab.score_alignment(speech_truth, [(s, s) for s in starts]),
        "per_clip_leadin": {i: {"silero": round(lead_silero[i], 3), "energy": round(lead_energy[i], 3)} for i in lead_silero},
        "provenance": provenance(__file__, a),
    }
    json.dump(out, open(a.out, "w"), indent=1, ensure_ascii=False)
    print("lead-in silero", out["leadin_silero"]["median_s"], "energy", out["leadin_energy"]["median_s"])
    print("silero vs clip start", out["silero_vs_clip_start"]["median_error_s"],
          "| vs energy speech onset", out["silero_vs_energy_speech_onset"]["median_error_s"],
          out["silero_vs_energy_speech_onset"]["within_tolerance_pct"], "% within 0.3 s")


if __name__ == "__main__":
    main()
