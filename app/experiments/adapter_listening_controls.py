#!/usr/bin/env python3
"""Generate the positive-control clips the adapter listening package refuses
to build without: a DIFFERENT narrator reading the SAME held-out lines.

WHY. `adapter_listening.py` pairs each reference line against the shipped and
rebuilt adapters, and its three controls pair a reference against a foreign
narrator. The first package (2026-09-06) took the foreign clips from that
narrator's OWN identity check - a different holdout draw - so the control
said different words as well as being a different voice, and the rater
rejected all three by content. That 3-of-3 certified that a mismatch is
audible, not that speaker identity is. The package builder now refuses to
build a control unless the foreign narrator has read THIS line, and that
needs a TTS pass, which is this script.

WHAT IT WRITES. For the first pair's first `--lines` held-out lines (the
lines the controls use), the foreign adapter reads the line's text, seeded,
into

    <holdout>/<adapter>/foreign_<FOREIGN>/check_<i>.wav

plus a manifest with the texts, the adapter sha and provenance.
`adapter_listening._foreign_clip` looks there and nowhere else.

    python experiments/adapter_listening_controls.py --lines 3
"""
import argparse
import hashlib
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

from experiments import adapter_listening as al  # noqa: E402


def foreign_dir(holdout, adapter, foreign=al.FOREIGN):
    return os.path.join(holdout, adapter, f"foreign_{foreign}")


def holdout_lines(holdout, adapter, n):
    """-> [(index, text)] for the first n held-out lines, in val order, which
    is the order identity_check's check_<i>.wav follow."""
    meta = os.path.join(holdout, adapter, "val", "metadata.jsonl")
    rows = []
    with open(meta, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    if len(rows) < n:
        sys.exit(f"{meta} has {len(rows)} lines; asked for {n}")
    return [(i, rows[i]["text"]) for i in range(n)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adapter", default=al.PAIRS[0][0],
                    help="the pair whose held-out lines the controls use")
    ap.add_argument("--foreign", default=al.FOREIGN)
    ap.add_argument("--lines", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260906)
    ap.add_argument("--config", default=os.path.join(APP, "config.json"))
    ap.add_argument("--data-root", default=REPO)
    args = ap.parse_args()

    holdout, _, shipped = al._roots(args.data_root)
    foreign_adapter = os.path.join(shipped, args.foreign)
    if not os.path.isdir(foreign_adapter):
        sys.exit(f"no adapter at {foreign_adapter}")
    out_dir = foreign_dir(holdout, args.adapter, args.foreign)
    os.makedirs(out_dir, exist_ok=True)
    lines = holdout_lines(holdout, args.adapter, args.lines)

    from tts import TTSEngine
    engine = TTSEngine(json.load(open(args.config, encoding="utf-8")))
    voice = {"type": "lora", "adapter_path": foreign_adapter, "seed": str(args.seed)}
    written = []
    t0 = time.time()
    for index, text in lines:
        path = os.path.join(out_dir, f"check_{index}.wav")
        ok = engine.generate_lora_voice(text, "", voice, path)
        if not ok or not al._wav_ok(path):
            sys.exit(f"generation failed for line {index}: {text[:60]}")
        written.append({"index": index, "text": text,
                        "wav": os.path.relpath(path, REPO)})
        print(f"  {index}: {text[:70]}")

    with open(os.path.join(foreign_adapter, "adapter_model.safetensors"), "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    from experiments.provenance import provenance
    doc = {"experiment": "adapter_listening_controls",
           "adapter": args.adapter, "foreign": args.foreign,
           "foreign_adapter_sha256": sha, "seed": args.seed,
           "lines": written, "elapsed_s": round(time.time() - t0, 1),
           "provenance": provenance(__file__, args)}
    with open(os.path.join(out_dir, "controls.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"\n{len(written)} control clips -> {out_dir}")


if __name__ == "__main__":
    main()
