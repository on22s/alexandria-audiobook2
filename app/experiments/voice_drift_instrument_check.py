"""Hand-checkable instrument test for the voice-drift gate (Rule 21).

Two halves of ONE voice's reference clip must score above the drift threshold
and above that voice against a DIFFERENT voice. Needs the sibling interpreter
that has speechbrain and two lora_models/*/ref_sample.wav clips, so it is a
runnable probe, not a unit test - the release verifier forbids skipped unit
tests, and a unit test that cannot run here would have to skip.

    app/env/bin/python app/experiments/voice_drift_instrument_check.py [--repo DIR]

`--repo` points at the checkout whose lora_models/ holds the clips (default:
this one).

Exit 0 = the instrument discriminates as the threshold assumes; 1 = it does
not; 2 = NOT MEASURED (interpreter or clips missing). Measured 2026-09-11 on
the RX 9070 XT box: same voice 0.694, different voices -0.005 / 0.100 / 0.151.
"""
import argparse
import glob
import os
import sys
import tempfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import core  # noqa: E402
import voice_drift  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=os.path.dirname(APP))
    args = ap.parse_args()
    python_bin = voice_drift.get_speaker_model_python(core._load_voicelab_config())
    clips = sorted(glob.glob(os.path.join(args.repo, "lora_models", "*", "ref_sample.wav")))
    if not python_bin or len(clips) < 3:
        print("NOT MEASURED: needs the sibling speechbrain interpreter and three lora_models ref clips")
        return 2
    import soundfile as sf
    a, sr = sf.read(clips[0])
    half = len(a) // 2
    with tempfile.TemporaryDirectory() as tmp:
        a1, a2 = os.path.join(tmp, "a1.wav"), os.path.join(tmp, "a2.wav")
        sf.write(a1, a[:half], sr)
        sf.write(a2, a[half:], sr)
        pairs = [[a1, a2], [a1, clips[1]], [a1, clips[2]]]
        scores, err = voice_drift.ecapa_pairs(pairs, python_bin)
    if err:
        print(f"NOT MEASURED: {err}")
        return 2
    same, other1, other2 = scores
    print(f"same voice (two halves): {same:.3f}")
    print(f"different voice:         {other1:.3f}  {other2:.3f}")
    print(f"threshold:               {voice_drift.DRIFT_MIN_SIMILARITY}")
    ok = same > voice_drift.DRIFT_MIN_SIMILARITY > max(other1, other2)
    print("PASS" if ok else "FAIL - the threshold does not separate same from different")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
