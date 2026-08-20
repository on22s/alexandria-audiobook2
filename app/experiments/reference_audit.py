"""Is the reference clip short, and is it unrepresentative of the speaker?

WHERE THIS COMES FROM. Goals 2.1, 2.5 and 2.6 each read OPEN on cells of the
CLONE arm, and the clone arm has exactly one input besides the text: the
reference clip. Two independent 2026 sources say that input is undersized here.
Qwen's own cloning guide puts the useful range at 3-15 seconds, "roughly
linear from 3 to 15, then plateaus"; the BSC Wildspoof submission measured
speaker similarity degrading as prompts shorten. Every eval set this project
scores on sits in the bottom quarter of that range - aishell3 at 3.45s,
kokoro at 5.17s, ljspeech at 6.15s - and the shortest of the three is Chinese,
which is the one failing cell of 2.6.

That is a coincidence worth converting into a measurement rather than a story.

WHAT THIS RECORDS, per eval set:
  * reference duration, against the 10-15s band the guide recommends;
  * the reference's own f0 median and vocal tract length;
  * the same two taken over the speaker's whole corpus;
  * the ratio between them.

The ratio is the point. A short reference is a plausible cause of a weak clone;
a short reference that is ALSO pitched below the speaker's own median is a
plausible cause of goal 2.5's specific failure, which is English cloning
landing at 0.81x on f0 median - a global scalar, not a contour error. If the
reference is representative, that mechanism is dead and the shortness has to
act some other way.

IT PROVES NOTHING ON ITS OWN. This measures inputs, not outputs. It says
whether the hypothesis is worth a GPU arm, and which language should carry it.
"""
import argparse
import contextlib
import glob
import json
import os
import statistics
import sys
import wave

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from voice_compare_view import pitch_stats, vocal_tract_length  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

RECOMMENDED = (10.0, 15.0)


def _main_checkout():
    import subprocess
    try:
        out = subprocess.run(["git", "-C", REPO, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            return line.split(" ", 1)[1].strip()
    return None


ROOTS = [REPO]


def resolve(path):
    """The audio is untracked and lives in the main checkout ([[Rule 24]])."""
    if not path:
        return None
    if os.path.isabs(path):
        return path
    for root in ROOTS:
        candidate = os.path.join(root, path)
        if os.path.exists(candidate):
            return candidate
    return None


def duration(path):
    try:
        with contextlib.closing(wave.open(path)) as handle:
            return round(handle.getnframes() / float(handle.getframerate()), 3)
    except Exception:
        pass
    try:
        import soundfile
        info = soundfile.info(path)
        return round(info.frames / float(info.samplerate), 3)
    except Exception:
        return None


def measure(path):
    out = {}
    try:
        out.update(pitch_stats(path) or {})
    except Exception:
        pass
    try:
        vtl = vocal_tract_length(path)
        if vtl is not None:
            out["vtl_cm"] = round(float(vtl), 4)
    except Exception:
        pass
    return out


def corpus_clips(build, build_path, limit):
    """The speaker's own clips, whatever this build calls them.

    Builds differ: the corpus sets carry a `train_dir`, the library sets carry
    a `test` list of rows with `human_wav`. Both are the same speaker, which is
    all this needs. Returning [] is reported, never silently averaged over.
    """
    found = []
    train_dir = resolve(build.get("train_dir") or "")
    if train_dir and os.path.isdir(train_dir):
        found = sorted(glob.glob(os.path.join(train_dir, "*.wav")))
    if not found:
        for row in (build.get("test") or []):
            p = resolve(row.get("human_wav") or row.get("wav") or "")
            if p:
                found.append(p)
    if not found:
        near = os.path.join(os.path.dirname(build_path), "human")
        if os.path.isdir(near):
            found = sorted(glob.glob(os.path.join(near, "*.wav")))
    return found[:limit] if limit else found


def audit(build_path, limit):
    with open(build_path, encoding="utf-8") as handle:
        build = json.load(handle)
    ref = resolve(build.get("ref_sample"))
    row = {
        "build": os.path.relpath(build_path, ROOTS[-1]),
        "corpus": build.get("corpus"),
        "ref_sample": build.get("ref_sample"),
        "ref_source_id": build.get("ref_source_id"),
    }
    if not ref:
        row["error"] = "reference clip not found in any audio root"
        return row
    row["ref_seconds"] = duration(ref)
    lo, hi = RECOMMENDED
    if row["ref_seconds"] is not None:
        row["ref_in_recommended_band"] = bool(lo <= row["ref_seconds"] <= hi)
        row["seconds_short_of_band"] = (
            round(lo - row["ref_seconds"], 2) if row["ref_seconds"] < lo else 0.0)
    row["reference"] = measure(ref)

    clips = corpus_clips(build, build_path, limit)
    row["corpus_clips_measured"] = len(clips)
    if not clips:
        row["error_corpus"] = "no corpus clips found; ratios not computed"
        return row
    per = [measure(c) for c in clips]
    row["corpus"] = {}
    row["ratio_reference_over_corpus"] = {}
    for key in ("f0_median", "f0_spread", "vtl_cm"):
        vals = [m[key] for m in per if m.get(key) is not None]
        if not vals:
            continue
        med = statistics.median(vals)
        row["corpus"][key] = round(med, 4)
        rv = row["reference"].get(key)
        row["ratio_reference_over_corpus"][key] = (
            round(rv / med, 4) if rv is not None and med else None)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", action="append", default=[],
                    help="build json to audit. Repeatable. Default: every "
                         "build.json under ab_test_runtime/*_eval/ plus the "
                         "library builds.")
    ap.add_argument("--clips", type=int, default=60,
                    help="corpus clips to measure per set (0 = all)")
    ap.add_argument("--audio-root", action="append", default=[])
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "reference_audit.json"))
    args = ap.parse_args()

    ROOTS[:] = [r for r in (args.audio_root or []) if r] or [REPO]
    main_checkout = _main_checkout()
    if main_checkout and main_checkout not in ROOTS:
        ROOTS.append(main_checkout)
    print(f"audio roots: {ROOTS}")

    builds = list(args.build)
    if not builds:
        for root in ROOTS:
            builds += sorted(glob.glob(os.path.join(root, "ab_test_runtime", "*_eval", "build.json")))
            builds += sorted(glob.glob(os.path.join(root, "ab_test_runtime", "second_english_eval", "*_build.json")))
            if builds:
                break
    if not builds:
        raise SystemExit("no build files found - nothing to audit")

    rows = [audit(b, args.clips) for b in builds]
    payload = {
        "scope": "reference-clip duration and representativeness per eval set; "
                 "inputs to the clone arm, not outputs",
        "recommended_band_seconds": list(RECOMMENDED),
        "clips_per_set": args.clips,
        "rows": rows, "status": "complete",
        "provenance": provenance(__file__, args),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)

    print(f"\n{'eval set':30s} {'ref_s':>7s} {'band':>5s} "
          f"{'f0 ref/corpus':>14s} {'vtl ref/corpus':>15s}  n")
    for r in rows:
        if r.get("error"):
            print(f"  {str(r.get('corpus'))[:28]:28s} {r['error']}")
            continue
        ratio = r.get("ratio_reference_over_corpus", {})
        f0 = ratio.get("f0_median")
        vtl = ratio.get("vtl_cm")
        band = "yes" if r.get("ref_in_recommended_band") else "NO"
        name = str(r.get("corpus") if isinstance(r.get("corpus"), str)
                   else os.path.basename(os.path.dirname(r["build"])))[:28]
        print(f"  {name:28s} {r.get('ref_seconds', 0) or 0:7.2f} {band:>5s} "
              f"{(f'{f0:.4f}' if f0 else '-'):>14s} "
              f"{(f'{vtl:.4f}' if vtl else '-'):>15s}  "
              f"{r.get('corpus_clips_measured', 0)}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
