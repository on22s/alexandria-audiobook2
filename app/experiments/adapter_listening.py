#!/usr/bin/env python3
"""Does the ECAPA gap between two adapters correspond to anything audible?

WHY THIS EXISTS. Every adapter conclusion in goal 2.7 rests on ECAPA cosine,
and nobody has ever checked that it tracks what a person hears. On 2026-09-06
the guarded re-gate said `warm_baritone_40s_m_1` scores 0.670 shipped against
0.355 rebuilt - a gap of 0.315, the largest in the set - while
`breathy_alto_50s_f_fantasy` reads 0.367 against 0.384, a gap of 0.017 that is
nothing. If the metric measures something audible, those two adapters should
sound very different from each other in how obvious the better arm is. If a
listener cannot tell them apart, every ECAPA number in this document is a
number about embeddings and not about voices.

THE PREDICTION IS WRITTEN BEFORE THE RATING, into the public manifest:
  - warm_baritone sets: the shipped arm is picked as closer to the reference,
    clearly and consistently.
  - breathy_alto sets: near chance, because the metric says there is nothing
    to hear.
A result where BOTH are near chance means ECAPA is not measuring audible
speaker identity at this scale. A result where BOTH are obvious means the
metric is under-reporting a real difference in the small-gap case.

CONTROLS ARE NOT OPTIONAL. Three sets pair the reference against a clip from a
DIFFERENT NARRATOR ENTIRELY. A rater who cannot reject those is not hearing
speaker identity at all, and nothing else in the package would be readable -
the same role the `preferred_very_slow` controls played in
`blinded_listening_ratings.json`, which passed 3 of 3.

THE CLIPS ARE ALREADY ON DISK. Each gate generated speech for the same twelve
held-out lines - `check_0.wav` .. `check_11.wav` under each adapter's
`identity_check/` - and all three arms read the SAME `guarded_holdout/<name>`
directory with `--lines 12`, so `check_i` is the same sentence in every arm.
No GPU work is needed to build this package.

THE KEY IS SEALED SEPARATELY and its sha256 is recorded in the public file, so
a rating can be shown to have been made before the key was opened. Same
contract as `blinded_listening.py`; this is a narrower question than that
script's instruction/casting comparison, which is why it is its own probe
rather than a fifth mode bolted onto it.
"""
import argparse
import json
import os
import random
import shutil
import sys
import wave

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
from experiments.provenance import file_sha256, provenance  # noqa: E402

# THE CLIPS LIVE IN THE LIVE CHECKOUT, NOT IN A WORKTREE. Development happens
# in a worktree (Rule 24) where ab_test_runtime/ and lora_models/ hold nothing:
# their contents are generated, not tracked. Resolving these against the
# script's own location made the first run refuse every clip - correctly, but
# for the wrong reason. --data-root names where the audio actually is.
DATA_ROOT = REPO


def _roots(data_root):
    return (os.path.join(data_root, "ab_test_runtime", "guarded_holdout"),
            os.path.join(data_root, "ab_test_runtime", "tight_rebuild"),
            os.path.join(data_root, "lora_models"))


HOLDOUT, REBUILD, SHIPPED = _roots(REPO)

# (adapter, shipped ECAPA, rebuilt ECAPA) from guarded__<name>__{shipped,rebuilt}
PAIRS = [
    ("warm_baritone_40s_m_1", 0.6704, 0.3549, "large"),
    ("breathy_alto_50s_f_fantasy", 0.3672, 0.3841, "none"),
]
FOREIGN = "velvety_mezzo_30s_f_gothic"   # a different narrator, for controls


class PackageError(RuntimeError):
    """The clips on disk cannot support a blinded package."""


def _wav_ok(path):
    """A clip must exist and carry audio. A zero-length file rates as silence."""
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        return False
    try:
        with wave.open(path) as handle:
            return handle.getnframes() > 0
    except (OSError, wave.Error):
        return False


def _arm_clip(adapter, arm, index):
    if arm == "shipped":
        base = os.path.join(SHIPPED, adapter, "identity_check")
    else:
        base = os.path.join(REBUILD, adapter, "tight", "adapter", "identity_check")
    return os.path.join(base, f"check_{index}.wav")


def _reference(adapter, index):
    return os.path.join(HOLDOUT, adapter, "val", f"unseen_{index:03d}.wav")


def _text(adapter, index):
    meta = os.path.join(HOLDOUT, adapter, "val", "metadata.jsonl")
    with open(meta, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return rows[index].get("text") or ""


def build(package_dir, public_path, key_path, seed, lines, data_root=None):
    global HOLDOUT, REBUILD, SHIPPED
    if data_root:
        HOLDOUT, REBUILD, SHIPPED = _roots(data_root)
    for path in (package_dir, public_path, key_path):
        if os.path.exists(path):
            raise PackageError(f"refusing to overwrite existing {path}")
    rng = random.Random(seed)
    temporary = package_dir + ".building"
    if os.path.exists(temporary):
        raise PackageError(f"stale temporary package: {temporary}")
    os.makedirs(temporary)
    public_sets, key_sets = [], []
    try:
        planned = []
        for adapter, ship, reb, gap in PAIRS:
            for i in range(lines):
                planned.append((adapter, i, "shipped", "rebuilt", gap,
                                round(ship - reb, 4)))
        for i in range(3):                      # positive controls
            planned.append((PAIRS[0][0], i, "shipped", "foreign", "control", None))

        for index, (adapter, line, arm_a, arm_b, gap, delta) in enumerate(planned):
            ref = _reference(adapter, line)
            a = _arm_clip(adapter, arm_a, line)
            b = (_arm_clip(FOREIGN, "shipped", line) if arm_b == "foreign"
                 else _arm_clip(adapter, arm_b, line))
            for path in (ref, a, b):
                if not _wav_ok(path):
                    raise PackageError(f"unusable clip {path}")
            options = [(arm_a, a), (arm_b, b)]
            rng.shuffle(options)
            set_id = f"set_{index:02d}"
            folder = os.path.join(temporary, set_id)
            os.makedirs(folder)
            shutil.copyfile(ref, os.path.join(folder, "reference.wav"))
            labelled = {}
            for slot, (arm, path) in zip("AB", options):
                shutil.copyfile(path, os.path.join(folder, f"{slot}.wav"))
                labelled[slot] = arm
            public_sets.append({
                "set": set_id,
                "text": _text(adapter, line),
                "question": "Which of A or B sounds more like the same speaker "
                            "as reference.wav? Answer A, B or same.",
                "clips": ["reference.wav", "A.wav", "B.wav"],
            })
            key_sets.append({
                "set": set_id, "adapter": adapter, "line": line,
                "slots": labelled, "gap_class": gap, "ecapa_gap": delta,
                "is_control": gap == "control",
            })
        key = {
            "note": "Concealed key. Do not open before the ratings are written.",
            "sets": key_sets,
            "provenance": provenance(__file__, {"seed": seed, "lines": lines}),
        }
        with open(key_path, "w", encoding="utf-8") as handle:
            json.dump(key, handle, indent=1, ensure_ascii=False)
        os.rename(temporary, package_dir)
        public = {
            "note": "Blinded adapter-fidelity listening package. The key is a "
                    "separate file; its sha256 below proves a rating was made "
                    "before the key was opened.",
            "asks": "For each set, play reference.wav, then A.wav and B.wav. "
                    "Say which sounds more like the SAME PERSON as the "
                    "reference. 'same' is a permitted answer.",
            "prediction_registered_before_rating": {
                "large_gap_sets": "the shipped arm is chosen clearly and "
                                  "consistently (ECAPA gap +0.315)",
                "no_gap_sets": "near chance (ECAPA gap +0.017 - the metric "
                               "says there is nothing to hear)",
                "controls": "the foreign narrator is rejected in all 3; if not, "
                            "nothing else in this package is readable",
                "falsifies_ecapa_if": "both classes come out near chance, which "
                                      "would mean the metric does not track "
                                      "audible speaker identity at this scale",
            },
            "sets": public_sets,
            "package_dir": os.path.relpath(package_dir, REPO),
            "concealed_key_sha256": file_sha256(key_path),
            "limitations": [
                "Two adapters, one narrator each, twelve lines drawn from one "
                "held-out set - this asks whether the metric tracks anything "
                "audible, not how well.",
                "The rater is expected to be the project owner, who is not a "
                "naive listener; goal 7.1 records that limitation already.",
                "'same' answers are counted separately and are not evidence "
                "for either arm.",
            ],
            "provenance": provenance(__file__, {"seed": seed, "lines": lines}),
        }
        with open(public_path, "w", encoding="utf-8") as handle:
            json.dump(public, handle, indent=1, ensure_ascii=False)
    except Exception:
        for path in (public_path, key_path):
            if os.path.isfile(path):
                os.unlink(path)
        for path in (temporary, package_dir):
            if os.path.isdir(path):
                shutil.rmtree(path)
        raise
    return public, key


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--package-dir", default=os.path.join(
        REPO, "ab_test_runtime", "adapter_listening_package"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "adapter_listening.json"))
    ap.add_argument("--key", default=os.path.join(
        REPO, "ab_test_runtime", "adapter_listening_concealed_key.json"))
    ap.add_argument("--data-root", default=REPO,
                    help="checkout holding the generated audio; a worktree has "
                         "none, since ab_test_runtime/ and lora_models/ are "
                         "not tracked")
    ap.add_argument("--seed", type=int, default=20260906)
    ap.add_argument("--lines", type=int, default=4,
                    help="lines per adapter pair")
    args = ap.parse_args()
    public, _ = build(args.package_dir, args.out, args.key, args.seed,
                      args.lines, args.data_root)
    print(f"sets: {len(public['sets'])}  "
          f"({2 * args.lines} comparisons + 3 controls)")
    print(f"package: {args.package_dir}")
    print(f"public:  {args.out}")
    print(f"key:     {args.key}  (sha {public['concealed_key_sha256'][:16]})")
    print("\nPlay each set's reference.wav, then A.wav and B.wav, and write "
          "A / B / same per set. Do not open the key first.")


if __name__ == "__main__":
    main()
