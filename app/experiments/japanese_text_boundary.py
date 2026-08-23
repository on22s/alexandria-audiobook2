"""Group Silero windows into known ordered Japanese transcript lines."""
import argparse
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from asr_backends import (build_alignment_probe, run_silero_whisper_cpp,
                          score_alignment, to_reading, word_error_rate)


def get_text_cost(reference, hypothesis):
    ref = to_reading(reference)
    hyp = to_reading(hypothesis)
    if ref is None or hyp is None:
        raise RuntimeError("pykakasi is required for Japanese reading alignment")
    return word_error_rate(" ".join(ref), " ".join(hyp), char_level=True)


def group_windows(references, windows):
    """Return one monotonic consecutive window group per reference."""
    n, m = len(references), len(windows)
    if not n or m < n:
        return []
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        for end in range(i, m - (n - i) + 1):
            for start in range(i - 1, end):
                if dp[i - 1][start] == inf:
                    continue
                spoken = "".join(w[2] for w in windows[start:end])
                cost = get_text_cost(references[i - 1], spoken)
                candidate = dp[i - 1][start] + cost
                if candidate < dp[i][end]:
                    dp[i][end], back[i][end] = candidate, start
    if back[n][m] is None:
        return []
    groups, end = [], m
    for i in range(n, 0, -1):
        start = back[i][end]
        chunk = windows[start:end]
        groups.append((chunk[0][0], chunk[-1][1], "".join(w[2] for w in chunk)))
        end = start
    return list(reversed(groups))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default=os.path.join(REPO, "whisper.cpp/models/ggml-large-v3.bin"))
    parser.add_argument("--binary", default=os.path.join(REPO, "whisper.cpp/build/bin/whisper-cli"))
    args = parser.parse_args()
    build = json.load(open(args.build, encoding="utf-8"))
    probe = os.path.join(REPO, "ab_test_runtime/asr_bench/ja_text_boundary.wav")
    wav, truth = build_alignment_probe(build["test"], probe)
    _text, windows = run_silero_whisper_cpp(wav, args.model, args.binary, language="ja")
    grouped = group_windows([t["text"] for t in truth], windows)
    result = score_alignment(truth, grouped)
    document = {"build": os.path.relpath(args.build, REPO),
                "method": "monotonic minimum reading-CER grouping of consecutive Silero windows",
                "raw_windows": len(windows), "grouped_windows": len(grouped),
                "alignment": result}
    from utils import atomic_json_write
    atomic_json_write(document, args.out)
    print(json.dumps(document, indent=2, ensure_ascii=False))
    if not result.get("scored"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
