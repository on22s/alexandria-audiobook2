"""Does a freshly trained adapter actually sound like its narrator?

THE FAILURE THIS GATES. Five adapters in the shipped library resembled nobody -
0.027, 0.061, 0.099, 0.397, 0.404 speaker similarity against the narrators they
were trained on, where a working voice reaches ~0.73. All five recovered on a
retrain, two of them to ~0.67. The voices were recoverable the whole time and
nothing said otherwise.

WHY LOSS CANNOT DO THIS JOB. Final loss on the 0.027 adapter was 4.07. Across
the entire 75-adapter library loss sits at ~4.1 regardless of whether the voice
works. It is blind to this failure, so training "succeeding" means nothing.

The sibling gate `verify_adapter_stops` catches a different defect - adapters
that never stop generating - and the same shape works here: generate a handful
of lines, measure, refuse. Neither is visible without generating audio.

WHY HELD-OUT CLIPS. Scoring against training clips measures memorisation. The
dataset builder writes a 180/20 split and `train_lora.py` now honours it, so
the val clips are genuinely unseen and this is a real test rather than a
recital.

THRESHOLD. Default 0.45. Measured reference points from the library:

    working adapters        0.65 - 0.74
    the five failures       0.027 - 0.404
    human vs human ceiling  ~0.83

0.45 sits in the empty band between the failures and the working ones. It is
deliberately generous: the purpose is catching a voice that resembles NOBODY,
not ranking good voices against each other.
"""
import argparse
import json
import math
from pathlib import Path
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))


def get_directory_identity_clips(dataset, lines):
    """Read the same held-out rows used by generation and cache validation."""
    clips = []
    with open(os.path.join(dataset, "val", "metadata.jsonl"), encoding="utf-8") as handle:
        for line in list(handle)[:lines]:
            if not line.strip():
                continue
            entry = json.loads(line)
            path = os.path.join(dataset, entry["audio_filepath"])
            if os.path.isfile(path):
                clips.append((path, entry.get("text") or ""))
    return clips


def get_identity_gate_inputs(adapter, dataset, lines):
    """Hash current adapter bytes and the held-out inputs, without inference."""
    from experiments.provenance import input_sha256

    if type(lines) is not int or lines < 1:
        raise ValueError("requested lines must be a positive integer")
    paths = [os.path.join(adapter, "adapter_model.safetensors"),
             os.path.join(adapter, "adapter_config.json"),
             os.path.join(adapter, "training_meta.json"),
             os.path.join(adapter, "ref_sample.wav"), __file__,
             os.path.join(APP, "tts.py"),
             os.path.join(APP, "experiments", "generation.py"),
             os.path.join(APP, "experiments", "library_voice_fidelity.py")]
    for name in ("ref_sample.txt",):
        path = os.path.join(adapter, name)
        if os.path.exists(path):
            paths.append(path)
    config = os.path.join(APP, "config.json")
    if os.path.exists(config):
        paths.append(config)
    if str(dataset).endswith(".zip"):
        paths.append(dataset)
    else:
        paths.append(os.path.join(dataset, "val", "metadata.jsonl"))
        paths.extend(path for path, _ in get_directory_identity_clips(dataset, lines))
    return input_sha256(paths)


def get_completed_identity_gate(path, adapter, dataset, lines=6, seed=1234,
                                threshold=0.45):
    """Read a fully measured gate for these exact inputs and settings."""
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("identity gate must be an object")
    expected = {"schema_version": 1, "requested_lines": lines, "seed": seed,
                "inputs": get_identity_gate_inputs(adapter, dataset, lines)}
    evidence = document.get("measurement")
    if (not isinstance(evidence, dict) or evidence != expected
            or type(evidence.get("schema_version")) is not int
            or type(evidence.get("requested_lines")) is not int
            or type(evidence.get("seed")) is not int):
        raise ValueError("identity gate inputs or measurement settings changed")
    scores = document.get("ecapa_scores")
    if (not isinstance(scores, list) or len(scores) != lines
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or not -1 <= value <= 1 for value in scores)):
        raise ValueError("identity gate must measure every requested line")
    if (type(document.get("lines")) is not int or document["lines"] != lines
            or type(document.get("generation_failures")) is not int
            or document["generation_failures"] != 0):
        raise ValueError("identity gate contains incomplete or failed generation")
    median = statistics.median(scores)
    if (type(document.get("threshold")) not in (int, float)
            or not math.isfinite(document["threshold"])
            or document["threshold"] != threshold
            or type(document.get("median_ecapa")) not in (int, float)
            or document["median_ecapa"] != round(median, 4)
            or type(document.get("passed")) is not bool
            or document["passed"] != (median >= threshold)
            or document.get("adapter") != os.path.relpath(adapter, REPO)):
        raise ValueError("identity gate verdict does not match measured scores")
    return document


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adapter", required=True,
                    help="trained adapter directory")
    ap.add_argument("--dataset", required=True,
                    help="dataset directory or zip holding val/")
    ap.add_argument("--lines", type=int, default=6)
    ap.add_argument("--min-ecapa", type=float, default=0.45,
                    help="below this the adapter resembles nobody; the library "
                         "band between failures (<=0.404) and working (>=0.65)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default=None)
    ap.add_argument("--check-artifact", help="validate a cached gate without inference")
    args = ap.parse_args()
    if args.lines < 1 or not math.isfinite(args.min_ecapa) or not -1 <= args.min_ecapa <= 1:
        ap.error("lines must be positive and min-ecapa must be a finite cosine threshold")
    if args.check_artifact:
        try:
            document = get_completed_identity_gate(
                args.check_artifact, args.adapter, args.dataset,
                args.lines, args.seed, args.min_ecapa)
        except (OSError, ValueError, KeyError, TypeError) as error:
            ap.exit(1, f"REFUSING incomplete or stale identity gate: {error}\n")
        sys.exit(0 if document["passed"] else 3)
    inputs = get_identity_gate_inputs(args.adapter, args.dataset, args.lines)

    from library_voice_fidelity import extract_val, ecapa_pairs
    work = os.path.join(args.adapter, "identity_check")
    os.makedirs(work, exist_ok=True)

    if args.dataset.endswith(".zip"):
        clips = extract_val(args.dataset, work, args.lines)
    else:
        clips = get_directory_identity_clips(args.dataset, args.lines)
    if len(clips) != args.lines:
        sys.exit(f"need {args.lines} held-out clips; only {len(clips)} available")

    from tts import TTSEngine
    from experiments.generation import render, GenerationFailed
    engine = TTSEngine(json.load(open(os.path.join(APP, "config.json"),
                                      encoding="utf-8")))
    entry = {"type": "lora",
             "adapter_path": os.path.relpath(args.adapter, REPO),
             "seed": str(args.seed)}
    pairs, failed = [], 0
    print(f"{len(clips)} held-out lines through "
          f"{os.path.basename(args.adapter.rstrip('/'))}\n")
    for i, (human_wav, text) in enumerate(clips):
        gen = os.path.join(work, f"check_{i}.wav")
        try:
            render(engine, text, "", "SPEAKER", {"SPEAKER": entry}, entry, gen)
        except GenerationFailed as exc:
            failed += 1
            print(f"  line {i}: GENERATION FAILED {str(exc)[:60]}")
            continue
        pairs.append([human_wav, gen])

    sib = os.environ.get(
        "ALEXANDRIA_SIBLING_PYTHON",
        os.path.join(os.path.dirname(REPO), "alexandria-audiobook.git",
                     "app", "env", "bin", "python"))
    cos, err = ecapa_pairs(pairs, sib)
    vals = [c for c in (cos or []) if c is not None]
    if not vals:
        # Not measured is not the same as failed. Refusing here would block a
        # good adapter over a missing interpreter; passing would defeat the
        # gate. Exit 2 marks it as unrun.
        print(f"\n  NOT MEASURED: {err}")
        print("  Speaker similarity was not computed, so this adapter is "
              "neither passed nor refused.")
        sys.exit(2)

    if (failed or len(cos or []) != args.lines or len(vals) != args.lines
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or not -1 <= value <= 1 for value in vals)):
        print("NOT MEASURED: incomplete or invalid held-out scores")
        sys.exit(2)
    if inputs != get_identity_gate_inputs(args.adapter, args.dataset, args.lines):
        sys.exit("identity inputs changed during measurement; refusing publication")
    median = statistics.median(vals)
    ok = median >= args.min_ecapa
    # DISPLAY PRECISION MUST EXCEED COMPARISON PRECISION. At 3 decimal places
    # husky_baritone_40s_m_military read "FAIL - 0.450 is below 0.45", which
    # is false on its face and sends a reader hunting a comparison bug that is
    # not there: the median is 0.4499. The artifact already stores 4 places;
    # the message now matches it, and a near-miss says how near.
    margin = args.min_ecapa - median
    for i, c in enumerate(vals):
        print(f"  line {i}: {c:.3f}")
    print(f"\n  median {median:.3f}, threshold {args.min_ecapa:.2f}")
    near = (" - a near miss, short by %.4f" % margin) if 0 < margin <= 0.005 else ""
    verdict = (f"PASS - the adapter sounds like its narrator ({median:.4f})"
               if ok else
               f"FAIL - {median:.4f} is below {args.min_ecapa:.2f}{near}. Working "
               f"adapters reach 0.65-0.74; the five known-broken ones scored "
               f"0.027-0.404. Retrain before shipping: on 2026-08-07 all five "
               f"failures recovered on a rerun, two of them to ~0.67.")
    print(f"  {verdict}")

    doc = {"adapter": os.path.relpath(args.adapter, REPO),
           "median_ecapa": round(median, 4), "lines": len(vals),
           "generation_failures": failed, "threshold": args.min_ecapa,
           "passed": ok, "verdict": verdict, "ecapa_scores": vals,
           "measurement": {"schema_version": 1, "requested_lines": args.lines,
                           "seed": args.seed, "inputs": inputs}}
    # PROVENANCE, which this gate has never recorded. 87 gate artifacts exist
    # with no commit, no host and no dirty flag - and goal 2.7 rests on them:
    # "9 were promoted", and breathy_alto_50s_f_fantasy's 0.404 -> 0.503 rescue
    # are read straight out of these files. A number that cannot name the code
    # that produced it is an anecdote, and promote_adapters.py ships voices on
    # the strength of it. Same idiom as every other experiment here.
    try:
        from experiments.provenance import provenance
        doc["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        doc["provenance"] = {"error": str(exc)[:120]}
    out = args.out or os.path.join(args.adapter, "identity_check.json")
    from utils import atomic_json_write
    atomic_json_write(doc, out)
    print(f"\nwrote {out}")
    # Non-zero so a training chain refuses to promote a voice that resembles
    # nobody, the same contract verify_adapter_stops uses.
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
