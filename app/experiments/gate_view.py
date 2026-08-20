"""Look at the clips an identity gate scored, instead of only its median.

WHY IT DID NOT EXIST. `verify_adapter_identity.py` writes one number and a
verdict - `median_ecapa`, `passed` - and records no rows and no clip paths. So
goal 6.5 lists "the 21 identity gates, and the promoted adapters now shipping"
as unlooked-at, and there was no way to look: the artifact cannot say which
audio it scored.

The audio survives anyway. The gate renders into `<adapter>/identity_check/`
and leaves it there - 95 such directories exist - and it takes its held-out
lines from the dataset's val split IN ORDER, so `check_<i>.wav` is the model's
reading of val line `<i>`. That is enough to reconstruct every pair without
regenerating anything, which matters because regenerating would be a different
sample and could not explain the number already recorded.

Emits the same view voice_compare_view produces, so the eye sees the generated
clip beside the human reading the same line.
"""
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

from experiments.provenance import provenance  # noqa: E402


def val_clips(dataset, limit):
    """-> [(human_wav, text)] in the order the gate consumed them."""
    meta = os.path.join(dataset, "val", "metadata.jsonl")
    if not os.path.exists(meta):
        raise SystemExit("no val split at %s; the gate had nothing held out "
                         "and this view cannot be built" % meta)
    clips = []
    with open(meta, encoding="utf-8") as handle:
        for line in list(handle)[:limit]:
            if not line.strip():
                continue
            entry = json.loads(line)
            path = os.path.join(dataset, entry["audio_filepath"])
            if os.path.exists(path):
                clips.append((path, entry.get("text") or ""))
    return clips


def pair(adapter, dataset, limit):
    """-> rows in voice_compare_view's shape, or a refusal saying what is
    missing. Never silently renders a partial set: a view built from three of
    six clips would be a highlight reel of whatever survived."""
    work = os.path.join(adapter, "identity_check")
    if not os.path.isdir(work):
        raise SystemExit("no identity_check/ under %s - this adapter's gate "
                         "clips were not kept, so there is nothing to look at "
                         "without re-running the gate" % adapter)
    clips = val_clips(dataset, limit)
    rows, missing = [], []
    for index, (human_wav, text) in enumerate(clips):
        generated = os.path.join(work, "check_%d.wav" % index)
        if not os.path.exists(generated):
            missing.append(os.path.basename(generated))
            continue
        rows.append({"id": "val-%d" % index, "text": text,
                     "human_wav": os.path.relpath(human_wav, REPO),
                     "lora_wav": os.path.relpath(generated, REPO),
                     "book": os.path.basename(adapter.rstrip("/"))})
    if missing:
        print("  note: %d gate clip(s) absent (%s) - the gate recorded "
              "generation failures for these" % (len(missing), ", ".join(missing[:3])))
    if not rows:
        raise SystemExit("no clip pairs could be formed")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--lines", type=int, default=6)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pick", default="spread",
                        choices=["spread", "best", "worst", "first"])
    args = parser.parse_args()

    rows = pair(os.path.abspath(args.adapter), os.path.abspath(args.dataset),
                args.lines)
    bridge = os.path.splitext(args.out)[0] + "_pairs.json"
    with open(bridge, "w", encoding="utf-8") as handle:
        # Stamped like any other artifact: this file says which held-out clips
        # were paired with which gate renders, and a view built from it is only
        # as trustworthy as that pairing.
        json.dump({"rows": rows, "provenance": provenance(__file__, args)},
                  handle, indent=1, ensure_ascii=False)
    print("paired %d gate clips with their human originals" % len(rows))

    result = subprocess.run(
        [sys.executable, os.path.join(APP, "experiments", "voice_compare_view.py"),
         "--generated", bridge, "--lines", str(len(rows)),
         "--pick", args.pick, "--out", args.out],
        cwd=REPO)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
