"""Record that a view exists, because the views themselves are not committed.

Goal 6.5 asks for "a rendered view for every audio arm whose numbers appear in
this document". Every one of the six views it credits is UNTRACKED - they are
9-18 MB HTML files with the audio embedded, and `reports/` and the other view
directories are gitignored on purpose. So the goal's own evidence does not
survive a fresh clone and cannot be checked by anyone but the machine that made
it, which is the condition 6.3 exists to prevent.

Committing the HTML is not the answer: seventy gates at ten megabytes each is
not a repository. What is durable, and what the goal is actually about, is the
record that a specific arm was rendered and looked at - the arm, the file, its
size and hash, and the numbers it was rendered to explain.

This writes that record. It does not claim anyone looked; `looked_at` stays
false until a human says otherwise.
"""
import argparse
import glob
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402


def sha256(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def gate_score(adapter_name, root=REPO):
    """-> the median_ecapa this view was rendered to explain, if recorded."""
    for pattern in ("gate_promote__%s.json", "gate_recheck__%s.json"):
        path = os.path.join(root, "ab_test_runtime", "experiments",
                            pattern % adapter_name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
            return {"artifact": os.path.basename(path),
                    "median_ecapa": doc.get("median_ecapa"),
                    "passed": doc.get("passed")}
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # The views are written wherever the chains run, which is not necessarily
    # the checkout this script lives in - development happens in a worktree
    # (Rule 24) while the queue renders into the live tree. Naming the root
    # explicitly beats inferring it from __file__ and finding nothing.
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--views", nargs="+", default=[
        os.path.join(REPO, "ab_test_runtime", "reports", "gate_view__*.html"),
        os.path.join(REPO, "ab_test_runtime", "voice_compare", "*.html"),
        os.path.join(REPO, "ab_test_runtime", "asr_clip_view", "*.html")])
    parser.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "audio_views.json"))
    args = parser.parse_args()

    root = os.path.abspath(args.repo)
    if args.views == parser.get_default("views"):
        args.views = [os.path.join(root, "ab_test_runtime", "reports", "gate_view__*.html"),
                      os.path.join(root, "ab_test_runtime", "voice_compare", "*.html"),
                      os.path.join(root, "ab_test_runtime", "asr_clip_view", "*.html")]
    records = []
    for pattern in args.views:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            arm = name[len("gate_view__"):-5] if name.startswith("gate_view__") \
                else name[:-5]
            records.append({
                "arm": arm,
                "view": os.path.relpath(path, root),
                "tracked_in_git": False,   # every view directory is gitignored
                "bytes": os.path.getsize(path),
                "sha256": sha256(path),
                "explains": gate_score(arm, root),
                "looked_at": False,
                "observation": None,
            })
    payload = {
        "note": ("A record that these views exist and what each was rendered "
                 "to explain. The HTML is deliberately not committed - 70 "
                 "gates at ~10 MB each is not a repository - so this is the "
                 "part that survives a clone. `looked_at` is false until a "
                 "human records what they saw in `observation`."),
        "views": records,
        "provenance": provenance(__file__, args),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    scored = [r for r in records if r["explains"]]
    print("recorded %d views (%d tied to a gate score), none marked looked-at"
          % (len(records), len(scored)))
    for r in scored:
        e = r["explains"]
        print("  %-40s median %.3f %s" % (r["arm"], e["median_ecapa"],
                                          "PASS" if e["passed"] else "FAIL"))
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
