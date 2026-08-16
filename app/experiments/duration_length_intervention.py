"""Test whether grouping short Japanese lines repairs clone duration spread.

The same text, reference voice, and seed are compared two ways: two existing
short-line renders separately versus one newline-joined render. Human duration
is the sum of the same two source clips. This changes prompt grouping only.
"""
import argparse
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

from experiments.duration_probe import seconds  # noqa: E402
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
    gains = sum(abs(after - 1) < abs(before - 1)
                for before, after in zip(baseline, grouped))
    return {
        "n": len(rows),
        "separate_median": round(statistics.median(baseline), 4),
        "grouped_median": round(statistics.median(grouped), 4),
        "pairs_closer_to_one": gains,
        "pairs_farther_from_one": len(rows) - gains,
    }


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
    args = parser.parse_args()

    source = json.load(open(args.input, encoding="utf-8"))
    build = json.load(open(args.build, encoding="utf-8"))
    pairs = build_short_pairs(source["rows"], args.pairs)
    ref = {"type": "clone", "ref_audio": build["ref_sample"],
           "ref_text": build["ref_text"], "seed": str(args.seed)}
    os.makedirs(args.out_dir, exist_ok=True)
    from tts import TTSEngine
    engine = TTSEngine(json.load(open(os.path.join(APP, "config.json"),
                                      encoding="utf-8")))

    results = []
    for index, (left, right) in enumerate(pairs):
        wav = os.path.join(args.out_dir, f"pair_{index:02d}.wav")
        text = left["text"].rstrip() + "\n" + right["text"].lstrip()
        if not seconds(wav):
            try:
                render(engine, text, "", "SPEAKER", {"SPEAKER": ref}, ref, wav)
            except GenerationFailed as exc:
                raise RuntimeError(f"pair {index} generation failed") from exc
        human = seconds(left["human_wav"]) + seconds(right["human_wav"])
        separate = seconds(left["clone_wav"]) + seconds(right["clone_wav"])
        results.append({"pair": index, "ids": [left["id"], right["id"]],
                        "characters": get_character_count(text),
                        "separate_ratio": separate / human,
                        "grouped_ratio": seconds(wav) / human,
                        "grouped_wav": os.path.relpath(wav, REPO)})
        atomic_json_write({"rows": results}, args.out + ".checkpoint")

    document = {"design": "same text/reference/seed; separate vs newline-grouped",
                "seed": args.seed, "summary": summarize(results), "rows": results,
                "provenance": provenance(__file__, args, inputs=input_sha256(
                    [args.input, args.build]))}
    atomic_json_write(document, args.out)
    print(json.dumps(document["summary"], indent=2))


if __name__ == "__main__":
    main()
