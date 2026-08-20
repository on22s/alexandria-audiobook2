"""Build a reference clip that is long enough AND typical of its speaker.

WHY. `reference_audit.py` measured the clone arm's only input besides text and
found it wrong on both counts, in the same direction as the arm's failures:

  aishell3 (zh)  3.45s   ref vtl 1.0360x the speaker's corpus median   arm misses vtl by 1.0607x
  ljspeech (en)  6.15s   ref f0  0.9439x the speaker's corpus median   arm misses f0 low, 0.81x

Two published sources say the length alone is a problem - Qwen's cloning guide
puts the useful band at 10-15s and calls quality "roughly linear from 3 to 15",
and the BSC Wildspoof 2026 submission measured speaker similarity degrading as
prompts shorten. Nothing here has ever been run above 6.15s.

WHAT IT DOES. Concatenates consecutive utterances from the speaker's own
training material until the clip reaches the target length, choosing the
starting point whose resulting clip is CLOSEST TO THE SPEAKER'S CORPUS MEDIAN
on f0 and vocal tract length. Length and typicality are separate defects and
this fixes both, which also means a result cannot attribute the change to
either one alone - stated here because the arm that uses this must say so.

WHY CONSECUTIVE, NOT BEST-OF-N SHUFFLED. Splicing unrelated utterances
together makes a clip with discontinuities at every join, which is a different
kind of reference rather than a longer one. Consecutive utterances from one
session share room, mic distance and register, so the join is the natural gap
between sentences.

WHAT IT DOES NOT DO. It does not touch the test split, the adapter, or any
scoring code. The output is a new build json beside the old one; the old one
stays exactly where it is so the two can be run as arms of the same comparison.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from voice_compare_view import pitch_stats, vocal_tract_length  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

TARGET_SECONDS = 13.0
BAND = (10.0, 15.0)
GAP_SECONDS = 0.25
ROOTS = [REPO]


def _main_checkout():
    try:
        out = subprocess.run(["git", "-C", REPO, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            return line.split(" ", 1)[1].strip()
    return None


def resolve(path):
    if not path:
        return None
    if os.path.isabs(path):
        return path
    for root in ROOTS:
        candidate = os.path.join(root, path)
        if os.path.exists(candidate):
            return candidate
    return None


def read_wav(path):
    import soundfile
    data, rate = soundfile.read(path, dtype="float32", always_2d=False)
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    return data, rate


def measure(path):
    out = {}
    try:
        out.update(pitch_stats(path) or {})
    except Exception:
        pass
    try:
        vtl = vocal_tract_length(path)
        if vtl is not None:
            out["vtl_cm"] = float(vtl)
    except Exception:
        pass
    return out


def corpus_medians(clip_paths, limit):
    """The speaker's own centre, which the new reference is chosen to sit at."""
    per = [measure(p) for p in clip_paths[:limit]]
    out = {}
    for key in ("f0_median", "vtl_cm"):
        vals = [m[key] for m in per if m.get(key) is not None]
        if vals:
            out[key] = statistics.median(vals)
    return out


def candidates(entries, train_dir, target):
    """Every consecutive run of utterances that reaches `target` seconds.

    Returns (start_index, [paths], [texts], seconds). Runs are minimal: each
    stops as soon as it passes the target, so a run never carries a whole extra
    sentence of length it did not need.
    """
    import soundfile
    durations = []
    for e in entries:
        p = os.path.join(train_dir, e["audio_filepath"])
        try:
            info = soundfile.info(p)
            durations.append(info.frames / float(info.samplerate))
        except Exception:
            durations.append(None)
    # INSIDE THE BAND, NOT MERELY PAST THE TARGET. A first version stopped at
    # the first run reaching `target` and accepted anything up to BAND[1]+3.
    # With ~9s LJSpeech utterances that produced a 16.8s reference - past the
    # 15s ceiling where Qwen's guide says quality "plateaus and eventually
    # degrades", and where it warns long prompts can make generation hang. A
    # reference built to fix a length problem must not overshoot into the other
    # end of the same curve. Every prefix that lands INSIDE the band is a
    # candidate; a run that jumps the band entirely contributes none.
    lo, hi = BAND
    out = []
    for i in range(len(entries)):
        total, paths, texts = 0.0, [], []
        for j in range(i, len(entries)):
            if durations[j] is None:
                break
            total += durations[j]
            if paths:
                total += GAP_SECONDS      # the join is part of the clip
            paths.append(os.path.join(train_dir, entries[j]["audio_filepath"]))
            texts.append(entries[j].get("text", ""))
            if lo <= total <= hi:
                out.append((i, list(paths), list(texts), round(total, 3)))
            if total > hi:
                break
    return out


def write_concat(paths, out_path, gap_seconds=GAP_SECONDS):
    """One clip, with a natural pause at each sentence join."""
    import numpy as np
    import soundfile
    chunks, rate = [], None
    for p in paths:
        data, r = read_wav(p)
        rate = rate or r
        if r != rate:
            raise SystemExit(f"sample-rate mismatch in {p}: {r} vs {rate}")
        chunks.append(data)
    gap = np.zeros(int(gap_seconds * rate), dtype="float32")
    joined = chunks[0]
    for c in chunks[1:]:
        joined = np.concatenate([joined, gap, c])
    soundfile.write(out_path, joined, rate)
    return len(joined) / float(rate)


def distance(m, centre):
    """How far a candidate sits from the speaker's centre, both measures at
    once. Ratios, so the two scales are comparable; absolute, so being high is
    penalised exactly as much as being low."""
    terms = []
    for key in ("f0_median", "vtl_cm"):
        if m.get(key) is not None and centre.get(key):
            terms.append(abs(m[key] / centre[key] - 1.0))
    return sum(terms) / len(terms) if terms else None


def rebuild(build_path, target, out_build, out_wav, corpus_limit, keep):
    with open(build_path, encoding="utf-8") as handle:
        build = json.load(handle)
    meta = resolve(build.get("metadata"))
    train_dir = resolve(build.get("train_dir"))
    if not meta or not train_dir:
        raise SystemExit(f"{build_path}: needs metadata + train_dir to rebuild "
                         f"a reference from the speaker's own material")
    entries = [json.loads(line) for line in open(meta, encoding="utf-8") if line.strip()]

    clip_paths = [os.path.join(train_dir, e["audio_filepath"]) for e in entries]
    centre = corpus_medians(clip_paths, corpus_limit)
    if not centre:
        raise SystemExit(f"{build_path}: could not measure the speaker's corpus")

    runs = candidates(entries, train_dir, target)
    if not runs:
        raise SystemExit(f"{build_path}: no consecutive run of utterances "
                         f"lands inside {BAND[0]}-{BAND[1]}s")

    scored = []
    for start, paths, texts, seconds in runs[:keep]:
        tmp = out_wav + f".cand{start}.wav"
        try:
            write_concat(paths, tmp)
            m = measure(tmp)
            d = distance(m, centre)
            scored.append({"start": start, "paths": paths, "texts": texts,
                           "seconds": seconds, "measures": m, "distance": d,
                           "tmp": tmp})
        except Exception as exc:                       # noqa: BLE001
            print(f"  candidate {start} unusable: {exc}")
    scored = [s for s in scored if s["distance"] is not None]
    if not scored:
        raise SystemExit(f"{build_path}: no candidate could be measured")
    scored.sort(key=lambda s: s["distance"])
    best = scored[0]

    os.replace(best["tmp"], out_wav)
    for s in scored:
        if s is not best and os.path.exists(s["tmp"]):
            os.remove(s["tmp"])

    new = dict(build)
    new["ref_sample"] = os.path.relpath(out_wav, ROOTS[-1])
    new["ref_text"] = " ".join(t.strip() for t in best["texts"] if t.strip())
    new["ref_source_id"] = f"concat@{best['start']}x{len(best['paths'])}"
    new["ref_seconds"] = round(best["seconds"], 3)
    new["reference_rebuild"] = {
        "from_build": os.path.relpath(build_path, ROOTS[-1]),
        "previous_ref_sample": build.get("ref_sample"),
        "previous_ref_seconds": build.get("ref_seconds"),
        "target_seconds": target,
        "candidates_considered": len(scored),
        "corpus_centre": {k: round(v, 4) for k, v in centre.items()},
        "chosen_measures": {k: round(v, 4) for k, v in best["measures"].items()
                            if isinstance(v, (int, float))},
        "chosen_distance": round(best["distance"], 5),
        "worst_distance": round(scored[-1]["distance"], 5),
        "note": "length and typicality were both changed; a result from this "
                "build cannot attribute a difference to either alone",
    }
    new["provenance"] = provenance(__file__)
    with open(out_build, "w", encoding="utf-8") as handle:
        json.dump(new, handle, indent=1, ensure_ascii=False)
    return new


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", required=True)
    ap.add_argument("--target-seconds", type=float, default=TARGET_SECONDS)
    ap.add_argument("--corpus-clips", type=int, default=60)
    ap.add_argument("--candidates", type=int, default=40,
                    help="consecutive runs to measure before choosing")
    ap.add_argument("--out-build", required=True)
    ap.add_argument("--out-wav", required=True)
    ap.add_argument("--audio-root", action="append", default=[])
    args = ap.parse_args()

    ROOTS[:] = [r for r in (args.audio_root or []) if r] or [REPO]
    main_checkout = _main_checkout()
    if main_checkout and main_checkout not in ROOTS:
        ROOTS.append(main_checkout)

    build = resolve(args.build) or args.build
    os.makedirs(os.path.dirname(os.path.abspath(args.out_wav)), exist_ok=True)
    new = rebuild(build, args.target_seconds, args.out_build, args.out_wav,
                  args.corpus_clips, args.candidates)
    r = new["reference_rebuild"]
    print(f"{os.path.basename(build)}")
    print(f"  was  {r['previous_ref_seconds']}s  {r['previous_ref_sample']}")
    print(f"  now  {new['ref_seconds']}s  {new['ref_sample']}")
    print(f"  speaker centre     {r['corpus_centre']}")
    print(f"  chosen reference   {r['chosen_measures']}")
    print(f"  distance from centre: {r['chosen_distance']} "
          f"(worst candidate {r['worst_distance']}, "
          f"{r['candidates_considered']} considered)")
    print(f"  wrote {args.out_build}")


if __name__ == "__main__":
    main()
