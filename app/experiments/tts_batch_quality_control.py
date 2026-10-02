"""Control for the TTS batching test (tts_batch_quality.py): is W=1-vs-W=N drift bigger than W=1-vs-W=1 sampling noise?

Compares the original W=1 wavs (seed 42) with W=1 reruns at seed 43 and seed 42, and with the W=4 / W=16 clips,
using the same ECAPA and duration statistics. The reruns came from a copy of tts_batch_quality.py in which both
`batch_seed=42` became `batch_seed=int(os.environ.get("CTL_SEED","42"))` and the wav directory took the seed as a
suffix; line selection stays at seed 42, so every run renders the same 32 lines. Paths are the tnr-0 (RTX A6000)
layout the result was produced on (set AA2_APP and TTS_CTL_HOME to relocate). Result: ab_test_runtime/experiments/tts_batch_quality_control__tnr0-a6000-20260929.json
The committed artifact was written by the version of this script before it took its paths from the environment and
stamped provenance; the comparison itself is unchanged.
"""
import json, os, statistics as st, sys
H = os.environ.get("TTS_CTL_HOME", os.getcwd())     # read before the chdir below
APP = os.environ.get("AA2_APP", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, APP); os.chdir(APP)
import soundfile as sf
from voice_reference import _speaker_similarities
from experiments.provenance import provenance
orig = f"{H}/tts_batch_quality_wavs/w1"
out = {}
for tag, d in (("seed43", f"{H}/tts_batch_quality_wavs_ctl_43/w1"), ("seed42_rerun", f"{H}/tts_batch_quality_wavs_ctl_42/w1"),
               ("w4_seed42", f"{H}/tts_batch_quality_wavs/w4"), ("w16_seed42", f"{H}/tts_batch_quality_wavs/w16")):
    idx = [i for i in range(32) if os.path.exists(f"{orig}/temp_batch_{i}.wav") and os.path.exists(f"{d}/temp_batch_{i}.wav")]
    sims = _speaker_similarities([(f"{orig}/temp_batch_{i}.wav", f"{d}/temp_batch_{i}.wav") for i in idx])
    ss = [s for s in sims if s is not None]
    rr = [sf.info(f"{d}/temp_batch_{i}.wav").duration / sf.info(f"{orig}/temp_batch_{i}.wav").duration for i in idx]
    out[tag] = {"n": len(idx), "ecapa_median": round(st.median(ss), 3), "ecapa_min": round(min(ss), 3),
                "dur_ratio_median": round(st.median(rr), 3), "dur_ratio_worst": round(max(rr, key=lambda x: abs(x - 1)), 3),
                "clips_dur_off_by_over_25pct": sum(1 for r in rr if abs(r - 1) > .25),
                "byte_identical_clips": sum(1 for i in idx if open(f"{orig}/temp_batch_{i}.wav", "rb").read() == open(f"{d}/temp_batch_{i}.wav", "rb").read())}
    print(tag, out[tag], flush=True)
out["provenance"] = provenance(__file__)
json.dump(out, open(f"{H}/tts_ctl_compare_20260929.json", "w"), indent=1)
