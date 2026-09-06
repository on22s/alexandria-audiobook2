#!/usr/bin/env python3
"""A listening package a naive rater can do, with a control that works.

WHY A SECOND PACKAGE. Goal 7.1 records what it needs: "a second rater who is
not the project owner, because every limitation below turns on that". The
2026-09-06 adapter package was rated by the owner AND its controls turned out
void, so it cannot serve.

THE CONTROL FLAW, AND WHY IT NEEDED NO TTS TO FIX. That package's controls
paired a reference against the SAME line from the trained adapter and a
DIFFERENT line from a foreign narrator - so the foreign clip stood out by its
words, and the rater identified all three unprompted. The fix is not to
generate matching speech: it is to make BOTH candidates differ from the
reference in content, so content carries no signal about which is the same
person. Two real human clips, one from the reference's narrator and one from
another, does exactly that and needs no model at all.

    old control:  ref=line7   A=line7 (same voice)   B=line3 (other voice)
                  -> B is identifiable by its words

    new control:  ref=line7   A=line2 (same voice)   B=line3 (other voice)
                  -> neither candidate shares the reference's words

WHAT THE CONTROL PROVES. That the rater can hear speaker identity across
different sentences, which is the ability the test sets assume. A rater who
cannot pass it has not produced a readable result, and that is the whole point
of having one.

THE TEST SETS ARE UNCHANGED IN KIND: reference and both candidates are the same
sentence, only the adapter differs, so those cannot be answered from content
either.

The key is sealed separately and its sha256 recorded in the public manifest.
"""
import argparse
import json
import os
import random
import shutil
import sys
import wave

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import file_sha256, provenance  # noqa: E402

# (adapter, shipped ECAPA, rebuilt ECAPA) from guarded__<name>__{shipped,rebuilt}
PAIRS = [("warm_baritone_40s_m_1", 0.6704, 0.3549, "large"),
         ("breathy_alto_50s_f_fantasy", 0.3672, 0.3841, "none")]
# a narrator from a different book, for the content-neutral control
OTHER = "crisp_mezzo_30s_f"


class PackageError(RuntimeError):
    """The clips on disk cannot support a package."""


def _roots(data_root):
    return (os.path.join(data_root, "ab_test_runtime", "guarded_holdout"),
            os.path.join(data_root, "ab_test_runtime", "tight_rebuild"),
            os.path.join(data_root, "lora_models"))


def _ok(path):
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        return False
    try:
        with wave.open(path) as handle:
            return handle.getnframes() > 0
    except (OSError, wave.Error):
        return False


def build(package_dir, public_path, key_path, seed, lines, data_root):
    for path in (package_dir, public_path, key_path):
        if os.path.exists(path):
            raise PackageError(f"refusing to overwrite existing {path}")
    holdout, rebuild, shipped = _roots(data_root)
    rng = random.Random(seed)
    tmp = package_dir + ".building"
    if os.path.exists(tmp):
        raise PackageError(f"stale temporary package: {tmp}")
    os.makedirs(tmp)
    public_sets, key_sets = [], []

    def human(adapter, index):
        return os.path.join(holdout, adapter, "val", f"unseen_{index:03d}.wav")

    def arm(adapter, which, index):
        base = (os.path.join(shipped, adapter, "identity_check") if which == "shipped"
                else os.path.join(rebuild, adapter, "tight", "adapter", "identity_check"))
        return os.path.join(base, f"check_{index}.wav")

    def text(adapter, index):
        meta = os.path.join(holdout, adapter, "val", "metadata.jsonl")
        with open(meta, encoding="utf-8") as handle:
            rows = [json.loads(l) for l in handle if l.strip()]
        return rows[index].get("text") or ""

    try:
        plan = []
        for adapter, ship, reb, gap in PAIRS:
            for i in range(lines):
                plan.append(("test", adapter, i, gap, round(ship - reb, 4)))
        for i in range(3):
            plan.append(("control", PAIRS[0][0], i, "control", None))

        for index, (kind, adapter, line, gap, delta) in enumerate(plan):
            ref = human(adapter, line)
            if kind == "test":
                options = [("shipped", arm(adapter, "shipped", line)),
                           ("rebuilt", arm(adapter, "rebuilt", line))]
                shown_text = text(adapter, line)
            else:
                # BOTH candidates are a DIFFERENT sentence from the reference,
                # so content cannot say which is the same person.
                same_line = (line + 7) % 20
                options = [("same_person", human(adapter, same_line)),
                           ("other_person", human(OTHER, (line + 3) % 20))]
                shown_text = ""
            for _, path in [("ref", ref)] + options:
                if not _ok(path):
                    raise PackageError(f"unusable clip {path}")
            rng.shuffle(options)
            set_id = f"set_{index:02d}"
            folder = os.path.join(tmp, set_id)
            os.makedirs(folder)
            shutil.copyfile(ref, os.path.join(folder, "reference.wav"))
            slots = {}
            for slot, (label, path) in zip("AB", options):
                shutil.copyfile(path, os.path.join(folder, f"{slot}.wav"))
                slots[slot] = label
            public_sets.append({
                "set": set_id, "text": shown_text,
                "clips": ["reference.wav", "A.wav", "B.wav"]})
            key_sets.append({"set": set_id, "kind": kind, "adapter": adapter,
                             "line": line, "slots": slots, "gap_class": gap,
                             "ecapa_gap": delta})

        key = {"note": "Concealed key. Do not open before the ratings are written.",
               "sets": key_sets,
               "provenance": provenance(__file__, {"seed": seed, "lines": lines})}
        with open(key_path, "w", encoding="utf-8") as handle:
            json.dump(key, handle, indent=1, ensure_ascii=False)
        os.rename(tmp, package_dir)
        public = {
            "note": "Blinded listening package for a rater who is not the "
                    "project owner. The key is a separate file; its sha256 "
                    "below proves a rating was made before the key was opened.",
            "asks": "For each set, play reference.wav, then A.wav and B.wav, "
                    "and say which sounds more like the SAME PERSON as the "
                    "reference. 'Can't tell' is a permitted answer.",
            "control_design":
                "Three sets pair the reference against one more recording of "
                "the same narrator and one of a different narrator. BOTH are "
                "different sentences from the reference, so content carries no "
                "signal about which is the same person - the flaw that voided "
                "the 2026-09-06 controls, where only the foreign clip differed "
                "in content. A rater who cannot pass these has not produced a "
                "readable result.",
            "sets": public_sets,
            "package_dir": os.path.relpath(package_dir, REPO),
            "concealed_key_sha256": file_sha256(key_path),
            "limitations": [
                "Two adapters. This asks whether the metric tracks anything "
                "audible, not how well.",
                "The test sets use synthesised speech on both sides; the "
                "controls use human recordings on both sides.",
            ],
            "provenance": provenance(__file__, {"seed": seed, "lines": lines}),
        }
        with open(public_path, "w", encoding="utf-8") as handle:
            json.dump(public, handle, indent=1, ensure_ascii=False)
    except Exception:
        for path in (public_path, key_path):
            if os.path.isfile(path):
                os.unlink(path)
        for path in (tmp, package_dir):
            if os.path.isdir(path):
                shutil.rmtree(path)
        raise
    return public, key


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--package-dir", default=os.path.join(
        REPO, "ab_test_runtime", "second_rater_package"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "second_rater_listening.json"))
    ap.add_argument("--key", default=os.path.join(
        REPO, "ab_test_runtime", "second_rater_concealed_key.json"))
    ap.add_argument("--data-root", default=REPO,
                    help="checkout holding the generated audio; a worktree has "
                         "none, since those directories are not tracked")
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--lines", type=int, default=4)
    args = ap.parse_args()
    public, _ = build(args.package_dir, args.out, args.key, args.seed,
                      args.lines, args.data_root)
    print(f"sets: {len(public['sets'])}  ({2 * args.lines} comparisons + 3 controls)")
    print(f"package: {args.package_dir}")
    print(f"key sha: {public['concealed_key_sha256'][:16]}")


if __name__ == "__main__":
    main()
