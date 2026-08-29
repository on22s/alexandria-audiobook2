"""Reference-free pitch agreement for Japanese accent and Mandarin tone.

This is a baseline, not forced alignment: expected linguistic units are laid
over the voiced contour at equal time. Coverage and the limitation are written
into the artifact so the number cannot be mistaken for mora timestamps.
"""
import argparse
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from experiments.provenance import provenance  # noqa: E402


def get_japanese_template(phrases):
    values = []
    for phrase in phrases:
        count, accent = phrase["moras"], phrase["accent"]
        if count <= 0:
            continue
        if accent == 1:
            values.extend([1.0] + [0.0] * (count - 1))
        elif accent > 1:
            values.extend([0.0] + [1.0] * (accent - 1) +
                          [0.0] * (count - accent))
        else:
            values.extend([0.0] + [1.0] * (count - 1))
    return values


def get_chinese_template(sequence):
    shapes = {"1": [1, 1, 1, 1, 1], "2": [0, .2, .45, .7, 1],
              "3": [.6, .25, 0, .3, .7], "4": [1, .75, .5, .25, 0]}
    return [point for tone in sequence for point in shapes.get(tone, [])]


def get_agreement(wav, expected):
    import numpy as np
    from voice_compare_view import load_audio, f0_contour
    audio, rate = load_audio(wav, 22050)
    _times, f0 = f0_contour(audio, rate)
    if f0 is None or len(expected) < 2:
        return None
    voiced = np.asarray(f0, dtype="float64")
    voiced = voiced[np.isfinite(voiced) & (voiced > 0)]
    if len(voiced) < 10:
        return None
    measured = np.interp(np.linspace(0, 1, len(expected)),
                         np.linspace(0, 1, len(voiced)), 12 * np.log2(voiced))
    measured -= measured.mean()
    target = np.asarray(expected, dtype="float64")
    target -= target.mean()
    corr = None if target.std() < 1e-9 or measured.std() < 1e-9 else float(
        np.corrcoef(target, measured)[0, 1])
    expected_moves, measured_moves = np.sign(np.diff(target)), np.sign(np.diff(measured))
    keep = expected_moves != 0
    direction = float(np.mean(expected_moves[keep] == measured_moves[keep])) if keep.any() else None
    return {"correlation": corr, "direction_accuracy": direction,
            "voiced_frames": int(len(voiced)), "expected_points": len(expected)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--arm", default="clone")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    generated = json.load(open(args.generated, encoding="utf-8"))
    expected_doc = json.load(open(args.expected, encoding="utf-8"))
    expected_by_id = {row["id"]: row for row in expected_doc["results"]}
    language, rows = expected_doc["language"], []
    for row in generated.get("rows", []):
        expectation = expected_by_id.get(row.get("id"))
        wav = row.get(f"{args.arm}_wav")
        if not expectation or not wav:
            continue
        template = (get_japanese_template(expectation["accent_phrases"])
                    if language == "ja" else
                    get_chinese_template(expectation["tone_sequence"]))
        result = get_agreement(os.path.join(REPO, wav), template)
        if result:
            rows.append({"id": row["id"], **result})
    def mean(key):
        values = [r[key] for r in rows if r.get(key) is not None]
        return statistics.mean(values) if values else None
    document = {"language": language, "arm": args.arm, "scored": len(rows),
                "available": len(generated.get("rows", [])),
                "correlation_mean": mean("correlation"),
                "direction_accuracy_mean": mean("direction_accuracy"),
                "alignment": "equal-time voiced-contour baseline; not forced alignment",
                "rows": rows, "provenance": provenance(__file__, args)}
    from utils import atomic_json_write
    atomic_json_write(document, args.out)
    print(json.dumps({k: v for k, v in document.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
