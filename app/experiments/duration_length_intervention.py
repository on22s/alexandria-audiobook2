"""Test whether grouping short Japanese lines repairs clone duration spread.

The same text, reference voice, and seed are compared two ways: two existing
short-line renders separately versus one newline-joined render. Human duration
is the sum of the same two source clips. This changes prompt grouping only.
"""
import argparse
import hashlib
import json
import math
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

from experiments.duration_probe import seconds, resolve  # noqa: E402
from experiments.generation import GenerationFailed, render  # noqa: E402
from experiments.provenance import input_sha256, provenance  # noqa: E402
from utils import atomic_json_write  # noqa: E402


def get_character_count(text):
    return sum(not character.isspace() for character in text)


def build_short_pairs(rows, pair_count):
    eligible = [row for row in rows if row.get("clone_wav")]
    ordered = sorted(eligible, key=lambda row: (
        get_character_count(row.get("text", "")), row.get("id", "")))
    selected = ordered[:pair_count * 2]
    if len(selected) != pair_count * 2:
        raise ValueError("not enough complete rows for requested pairs")
    return list(zip(selected[::2], selected[1::2]))


def summarize(rows):
    baseline = [row["separate_ratio"] for row in rows]
    grouped = [row["grouped_ratio"] for row in rows]
    changes = [abs(before - 1) - abs(after - 1)
               for before, after in zip(baseline, grouped)]
    ties = sum(math.isclose(change, 0.0, abs_tol=1e-12)
               for change in changes)
    gains = sum(change > 0 and not math.isclose(change, 0.0, abs_tol=1e-12)
                for change in changes)
    return {
        "n": len(rows),
        "separate_median": round(statistics.median(baseline), 4),
        "grouped_median": round(statistics.median(grouped), 4),
        "pairs_closer_to_one": gains,
        "pairs_farther_from_one": len(rows) - gains - ties,
        "pairs_tied": ties,
    }


def build_cache_identity(args, build):
    config_path = os.path.join(APP, "config.json")
    ref = {"type": "clone", "ref_audio": build["ref_sample"],
           "ref_text": build["ref_text"], "seed": str(args.seed)}
    return {
        "seed": args.seed,
        "inputs": input_sha256([
            args.input, args.build, config_path, build["ref_sample"],
            __file__, os.path.join(APP, "tts.py"),
            os.path.join(APP, "experiments", "generation.py")]),
        "reference": ref,
    }


def get_duration_measurement_identity(args, build, pairs):
    identity = build_cache_identity(args, build)
    identity["pair_audio_inputs"] = input_sha256([
        resolve(row[key]) for pair in pairs for row in pair
        for key in ("human_wav", "clone_wav")])
    return identity


def get_grouped_duration_path(args, index, identity):
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return os.path.join(args.out_dir, f"pair_{index:02d}_{key}.wav")


def get_duration_pair_result(index, left, right, wav):
    durations = [seconds(path) for path in (left["human_wav"], right["human_wav"],
                 left["clone_wav"], right["clone_wav"], wav)]
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in durations):
        raise ValueError(f"pair {index} has missing or invalid audio duration")
    human = sum(durations[:2])
    text = left["text"].rstrip() + "\n" + right["text"].lstrip()
    return {"pair": index, "ids": [left["id"], right["id"]],
            "characters": get_character_count(text),
            "separate_ratio": sum(durations[2:4]) / human,
            "grouped_ratio": durations[4] / human,
            "grouped_wav": os.path.relpath(wav, REPO)}


def get_completed_duration_result(path, args):
    """Read a full matched-pair result still describing its actual inputs/WAVs."""
    with open(args.input, encoding="utf-8") as handle:
        source = json.load(handle)
    with open(args.build, encoding="utf-8") as handle:
        build = json.load(handle)
    pairs = build_short_pairs(source["rows"], args.pairs)
    identity = get_duration_measurement_identity(args, build, pairs)
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if (not isinstance(document, dict) or type(document.get("seed")) is not int
            or document["seed"] != args.seed or document.get("cache_identity") != identity
            or document.get("design") != "same text/reference/seed; separate vs newline-grouped"):
        raise ValueError("duration result has changed inputs or experiment settings")
    rows = document.get("rows")
    if not isinstance(rows, list) or len(rows) != args.pairs:
        raise ValueError("duration result does not cover every requested pair")
    expected = [get_duration_pair_result(index, left, right,
                get_grouped_duration_path(args, index, identity))
                for index, (left, right) in enumerate(pairs)]
    for row, measured in zip(rows, expected):
        if (not isinstance(row, dict) or type(row.get("pair")) is not int
                or type(row.get("characters")) is not int
                or any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key])
                       for key in ("separate_ratio", "grouped_ratio")) or row != measured):
            raise ValueError("duration result disagrees with its matched-pair audio")
    summary = document.get("summary")
    if (not isinstance(summary, dict) or summary != summarize(expected)
            or any(type(summary.get(key)) is not int for key in
                   ("n", "pairs_closer_to_one", "pairs_farther_from_one", "pairs_tied"))
            or any(type(summary.get(key)) not in (int, float) or not math.isfinite(summary[key])
                   for key in ("separate_median", "grouped_median"))):
        raise ValueError("duration summary disagrees with complete paired measurements")
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--input", default=os.path.join(
        REPO, "ab_test_runtime", "experiments",
        "kokoro_same_speaker_generate.json"))
    parser.add_argument("--build", default=os.path.join(
        REPO, "ab_test_runtime", "kokoro_same_speaker_eval", "build.json"))
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--out-dir", default=os.path.join(
        REPO, "ab_test_runtime", "duration_length_intervention"))
    parser.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments",
        "duration_length_intervention.json"))
    parser.add_argument("--check-artifact", help="validate a completed result without inference")
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("pairs must be positive")
    if args.check_artifact:
        try:
            get_completed_duration_result(args.check_artifact, args)
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.exit(1, f"REFUSING incomplete or stale duration result: {error}\n")
        return

    with open(args.input, encoding="utf-8") as handle:
        source = json.load(handle)
    with open(args.build, encoding="utf-8") as handle:
        build = json.load(handle)
    pairs = build_short_pairs(source["rows"], args.pairs)
    cache_identity = get_duration_measurement_identity(args, build, pairs)
    ref = cache_identity["reference"]
    os.makedirs(args.out_dir, exist_ok=True)
    from tts import TTSEngine
    config_path = os.path.join(APP, "config.json")
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    engine = TTSEngine(config)

    results = []
    for index, (left, right) in enumerate(pairs):
        wav = get_grouped_duration_path(args, index, cache_identity)
        text = left["text"].rstrip() + "\n" + right["text"].lstrip()
        if not seconds(wav):
            try:
                render(engine, text, "", "SPEAKER", {"SPEAKER": ref}, ref, wav)
            except GenerationFailed as exc:
                raise RuntimeError(f"pair {index} generation failed") from exc
        results.append(get_duration_pair_result(index, left, right, wav))
        atomic_json_write({"rows": results}, args.out + ".checkpoint")

    if cache_identity != get_duration_measurement_identity(args, build, pairs):
        raise RuntimeError("duration inputs changed during measurement; refusing publication")
    document = {"design": "same text/reference/seed; separate vs newline-grouped",
                "seed": args.seed, "summary": summarize(results), "rows": results,
                "cache_identity": cache_identity,
                "provenance": provenance(__file__, args, inputs=input_sha256(
                    [args.input, args.build]))}
    atomic_json_write(document, args.out)
    print(json.dumps(document["summary"], indent=2))


if __name__ == "__main__":
    main()
