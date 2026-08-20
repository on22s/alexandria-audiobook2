"""Per-reader Japanese CER in reading space, from stored hypotheses.

WHY IT IS NEEDED. Goal 5.4 says of Japanese: "the pooled result fails both and
**every reader fails on CER**", over a table of 24.6-34.9%. Every one of those
figures is `wer_mean` - agreement on the WRITTEN form. This project decided
long ago that the written form is the wrong thing to score Japanese on, and
`asr_backends.to_reading` exists for exactly that reason; its own docstring
records the pooled effect, 28.7% as written against 9.9% on readings, and says
plainly that "the 20% target failed on one and passes on the other, on
identical audio and identical model output".

The pooled reading score was taken (asr_ja_readings.json, n=50, 0.0989). The
PER-READER reading score never was, and "every reader fails" is a per-reader
claim. A pool can pass while a member fails.

WHY NO ASR RE-RUN. `--keep-hypotheses` stored every clip's reference and
hypothesis, and the clip id carries the reader
(`botchan-by-soseki-natsume-2-00001`). So this is arithmetic on committed text:
no audio, no model, no GPU. Re-running ASR to recover a number already implied
by stored output would also risk answering with a different decode.

SAME FUNCTIONS AS THE GOAL. `to_reading` and `word_error_rate` are imported
from asr_backends rather than reimplemented; a second scorer that disagreed
with the first is the drift [[Rule 15]] is about, and a reading-space CER
computed two ways would be indistinguishable from a bug.

WHAT WOULD FALSIFY THE CORRECTION. Any reader above 20% in reading space. Then
the pool passes while a member does not, and the document must name which.
"""
import argparse
import json
import os
import re
import statistics
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from asr_backends import to_reading, word_error_rate  # noqa: E402
from experiments.provenance import provenance  # noqa: E402
from experiments.manifest import read_inputs  # noqa: E402

TARGET_CER = 0.20
# Clip ids end in -NNNNN; everything before that is the reader.
READER_RE = re.compile(r"^(?P<reader>.+?)-(?P<index>\d+)$")


def reader_of(clip_id):
    m = READER_RE.match(str(clip_id or ""))
    return m.group("reader") if m else None


def collect_hypotheses(payload):
    """-> [(clip_id, reference, hypothesis)] from any backend in the artifact."""
    out = []
    results = payload.get("results") or {}
    for backend, entry in results.items():
        for row in (entry or {}).get("hypotheses") or []:
            out.append((row.get("id"), row.get("reference"), row.get("hypothesis"),
                        backend, row.get("wer")))
    return out


def rescore(paths):
    rows, unreadable = [], []
    read = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        read.append(path)
        got = collect_hypotheses(payload)
        if not got:
            unreadable.append(os.path.basename(path))
            continue
        for clip_id, ref, hyp, backend, written_wer in got:
            ref_kana, hyp_kana = to_reading(ref or ""), to_reading(hyp or "")
            if ref_kana is None or hyp_kana is None:
                raise SystemExit(
                    "pykakasi is unavailable, so nothing here can be scored on "
                    "readings. Refusing rather than reporting a character "
                    "score under a reading label.")
            # An empty reading means the reference held no kana at all after
            # conversion - punctuation or latin only. Scoring it would divide
            # by zero or reward silence; it is counted and excluded.
            if not ref_kana:
                unreadable.append(clip_id)
                continue
            rows.append({
                "id": clip_id, "reader": reader_of(clip_id), "backend": backend,
                "artifact": os.path.basename(path),
                "cer_written": written_wer,
                "cer_reading": round(word_error_rate(ref_kana, hyp_kana,
                                                     char_level=True), 4),
            })
    return rows, unreadable, read


def summarise(rows):
    by_reader = {}
    for row in rows:
        by_reader.setdefault(row["reader"], []).append(row)
    out = {}
    for reader, group in sorted(by_reader.items()):
        reading = [r["cer_reading"] for r in group]
        written = [r["cer_written"] for r in group if r["cer_written"] is not None]
        out[reader] = {
            "n": len(group),
            "cer_written_mean": round(statistics.mean(written), 4) if written else None,
            "cer_reading_mean": round(statistics.mean(reading), 4),
            "cer_reading_median": round(statistics.median(reading), 4),
            "passes_target": bool(statistics.mean(reading) <= TARGET_CER),
        }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("artifacts", nargs="+",
                    help="ASR artifacts written with --keep-hypotheses")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "asr_ja_reading_per_reader.json"))
    args = ap.parse_args()

    rows, unreadable, read = rescore(args.artifacts)
    if not rows:
        raise SystemExit("no hypotheses found - these artifacts were written "
                         "without --keep-hypotheses, so there is nothing to "
                         "rescore")
    per_reader = summarise(rows)
    pooled = [r["cer_reading"] for r in rows]
    written = [r["cer_written"] for r in rows if r["cer_written"] is not None]
    failing = sorted(k for k, v in per_reader.items() if not v["passes_target"])
    payload = {
        "scope": "per-reader Japanese CER scored on kana readings, recomputed "
                 "from stored hypotheses; no audio and no model involved",
        "target_cer": TARGET_CER,
        "clips": len(rows), "excluded_no_kana_in_reference": len(unreadable),
        "pooled": {
            "cer_written_mean": round(statistics.mean(written), 4) if written else None,
            "cer_reading_mean": round(statistics.mean(pooled), 4),
            "cer_reading_median": round(statistics.median(pooled), 4),
        },
        "per_reader": per_reader,
        "readers_failing_target": failing,
        "rows": rows,
        "read_inputs": read_inputs(read, REPO),
        "status": "complete",
    }
    payload["provenance"] = provenance(__file__, args)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)

    print(f"{'reader':40s} {'n':>3s} {'written':>9s} {'reading':>9s}  verdict")
    for reader, v in sorted(per_reader.items()):
        verdict = "passes" if v["passes_target"] else "FAILS"
        w = f"{v['cer_written_mean']:.4f}" if v["cer_written_mean"] is not None else "-"
        print(f"  {reader[:38]:38s} {v['n']:3d} {w:>9s} "
              f"{v['cer_reading_mean']:9.4f}  {verdict}")
    p = payload["pooled"]
    pooled_written = ("-" if p["cer_written_mean"] is None
                      else f"{p['cer_written_mean']:.4f}")
    print(f"\n  {'POOLED':38s} {len(rows):3d} {pooled_written:>9s} "
          f"{p['cer_reading_mean']:9.4f}")
    print(f"\n  target CER <= {TARGET_CER}; readers failing it: "
          f"{failing if failing else 'none'}")
    if unreadable:
        print(f"  excluded (reference had no kana): {len(unreadable)}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
