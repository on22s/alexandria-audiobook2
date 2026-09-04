#!/usr/bin/env python3
"""Pair every generated clip with the text it was asked to say.

WHY THIS IS NEEDED. The fidelity runs record `gen_wav` and every acoustic
ratio, but NOT the source text - so 2,834 generated clips sit on disk with no
way to ask the only question nobody has ever asked of this pipeline: does the
audio say the words? `tts_output_validation.py` has existed unrun for want of
a manifest. This builds it.

HOW THE PAIRING IS SOUND. `extract_val` reads `val/metadata.jsonl` in file
order and takes the first N entries, and the fidelity run wrote `gen_i.wav`
for the i-th of those. So the pairing is by index into that same function's
output - imported and called, not reimplemented (Rule 15), because a
reimplementation that skipped a missing-audio entry differently would silently
shift every text by one and score fluent audio as gibberish.

JAPANESE IS EXCLUDED, NOT FAILED. The whisper model on this machine is
`small.en`. A Japanese line scored by an English-only model would produce a
low score that means "wrong model", not "bad audio" - the exact shape of
fallback that turns a configuration error into thousands of believable
non-results. Rows whose source text contains kana or kanji are dropped and
counted.
"""
import argparse
import json
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


def load_fidelity_module():
    import importlib.util
    path = os.path.join(REPO, "app", "experiments",
                        "library_voice_fidelity_resume_20260831.py")
    spec = importlib.util.spec_from_file_location("lvf", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(work, models, zips, lines):
    lvf = load_fidelity_module()
    rows, skipped_cjk, missing_wav, no_zip = [], 0, 0, 0
    for name, dataset, _meta in lvf.adapter_sources(models):
        gen_dir = os.path.join(work, name)
        if not os.path.isdir(gen_dir):
            continue
        zpath = lvf.find_zip(dataset, zips)
        if not zpath:
            no_zip += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            clips = lvf.extract_val(zpath, tmp, lines)
        for i, (_human, text) in enumerate(clips):
            wav = os.path.join(gen_dir, "gen_%d.wav" % i)
            if not os.path.exists(wav):
                missing_wav += 1
                continue
            if not text.strip():
                continue
            if CJK.search(text):
                skipped_cjk += 1
                continue
            rows.append({"text": text, "wav": wav, "adapter": name})
    return rows, {"skipped_japanese": skipped_cjk,
                  "generated_clip_absent": missing_wav,
                  "adapters_without_a_zip": no_zip}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", required=True, help="a fidelity run's work dir")
    ap.add_argument("--models", default=os.path.join(REPO, "lora_models"))
    ap.add_argument("--zips", default=None)
    ap.add_argument("--lines", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.zips is None:
        args.zips = load_fidelity_module().DEFAULT_ZIPS

    rows, counts = build(args.work, args.models, args.zips, args.lines)
    json.dump(rows, open(args.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("manifest rows      : %d" % len(rows))
    for k, v in counts.items():
        print("  %-24s %d" % (k, v))
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
