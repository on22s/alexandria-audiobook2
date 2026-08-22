"""Rebuild the command that produced an artifact, from the artifact itself.

WHY. The evidence base is mostly not reproducible: of 480 artifacts, 198 carry
no provenance at all, 202 cannot say whether the tree was dirty, and of the 108
best-classified, 81 were written from a dirty tree. Goal 5.4 is the file's
best-documented goal - six artifacts, every one `supported_structure` - and all
six are dirty, so the goal that cites its evidence properly cannot reproduce
any of it.

Re-running them fixes that, now that the tree is clean and gpu_job.sh refuses a
dirty one. The question is what command to re-run, and the honest answer is not
"whatever I remember": provenance already records `script` and the full parsed
`args`, so the command can be reconstructed from the artifact rather than from
recollection. That is the same principle as reading the log instead of guessing
at the failure.

WHAT IT REFUSES TO DO. Invent. An artifact with no provenance cannot be
replayed and is reported as such rather than approximated - a reconstructed
command that is subtly not the original produces a NEW result wearing an old
name, which is worse than an unreproducible one because it looks fixed.

ARGUMENT RECONSTRUCTION IS MECHANICAL, not clever: argparse dest names map back
to `--flag` form, booleans become presence or absence, lists expand. Anything
that does not round-trip is reported, not guessed at.
"""
import argparse
import glob
import json
import os
import shlex
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def to_flags(args):
    """-> argv fragment for a parsed-args dict, or (None, reason)."""
    argv = []
    for key, value in sorted(args.items()):
        flag = "--" + key.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            argv.append(flag)
        elif isinstance(value, (list, tuple)):
            if not value:
                continue
            argv.append(flag)
            argv += [str(v) for v in value]
        elif isinstance(value, dict):
            return None, f"{key} is a dict; cannot round-trip to a flag"
        else:
            argv += [flag, str(value)]
    return argv, None


def replay_command(path, python):
    """-> (argv, reason). argv is None when the artifact cannot be replayed."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {exc}"
    if not isinstance(document, dict):
        return None, "top-level list; no provenance possible"
    prov = document.get("provenance") or {}
    if not isinstance(prov, dict) or not prov.get("script"):
        return None, "no provenance; cannot reconstruct the command"
    args = prov.get("args")
    if not isinstance(args, dict):
        return None, "provenance records no parsed args"
    flags, reason = to_flags(args)
    if flags is None:
        return None, reason
    script = os.path.join(REPO, "app", "experiments", prov["script"])
    if not os.path.exists(script):
        return None, f"script no longer exists: {prov['script']}"
    return [python, "-u", script] + flags, None


def added_commit(path):
    """-> (sha, date) of the commit that introduced `path`, or (None, None).

    The commit that ADDED an artifact is not necessarily the commit that
    produced it - 48% of recorded runs came from a dirty tree, so the file may
    have sat unstaged for days. It bounds the code version rather than pinning
    it, and every caller here says so.
    """
    # --full-history AND AN EXPLICIT SORT, because the obvious form is not
    # deterministic. `git log --diff-filter=A -1 -- path` applies history
    # SIMPLIFICATION: it follows one parent through each merge and reports
    # whichever add that path reaches. A file that came to main through a
    # squash-merge has two adding commits - the branch's original and main's
    # squashed copy - and which one git reports depends on the topology it is
    # asked from. Measured 2026-08-22 on pitch_quality_longref.json: the PR
    # branch said d24a5928 and GitHub's merge ref said 6da598d1, for the same
    # file with the same content. The index is regenerated and compared in CI,
    # so a value that depends on the vantage point makes it unreproducible, and
    # #404 failed twice on exactly this with nothing wrong in the PR.
    #
    # --full-history disables the simplification so every add is listed, and
    # sorting by (author date, sha) picks the same one from any vantage point.
    # The sha tiebreak matters: same-day squashes are the common case here.
    try:
        out = subprocess.run(
            ["git", "log", "--full-history", "--diff-filter=A",
             "--format=%as %H", "--", path],
            cwd=REPO, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    if not out:
        return None, None
    date, _, sha = sorted(out.splitlines())[0].partition(" ")
    return sha.strip() or None, date.strip() or None


def script_from_name(path):
    """-> the experiment script an artifact is named after, or None.

    Artifacts here are conventionally named for their producer -
    alignment_diagnosis_trimmed.json from alignment_diagnosis.py. Matching
    longest-first so `lexicon_corpus_scan` is not claimed by `lexicon`.
    """
    stem = os.path.basename(path)
    if not stem.endswith(".json"):
        return None
    stem = stem[:-len(".json")]
    names = sorted((os.path.basename(p)[:-3]
                    for p in glob.glob(os.path.join(REPO, "app", "experiments", "*.py"))),
                   key=len, reverse=True)
    for name in names:
        if stem == name or stem.startswith(name + "__") or stem.startswith(name + "_"):
            return name + ".py"
    return None


def resolve_producer(path, python):
    """-> how confidently this artifact's origin can be established.

    THREE TIERS, AND THE MIDDLE ONE IS THE POINT. An earlier version of this
    file reported only "replayable" or not, which wrote off 373 artifacts as
    unattributable. They are not: git records when every one of them was added
    - 100% of a 60-artifact sample - and 251 of the 373 name their producing
    script by convention, so the code that made them can be read even though
    the arguments cannot.

      provenance  script and args recorded -> an exact command
      git+naming  script known and its version bounded -> investigable, NOT
                  replayable. Deliberately emits no command: a guessed argv
                  produces a new result wearing an old name.
      none        neither
    """
    argv, reason = replay_command(path, python)
    if argv is not None:
        return {"tier": "provenance", "argv": argv, "script": None,
                "commit": None, "added": None, "note": None}
    script = script_from_name(path)
    sha, date = added_commit(os.path.relpath(path, REPO))
    if script:
        return {"tier": "git+naming", "argv": None, "script": script,
                "commit": sha, "added": date,
                "note": f"read the producer as it was: "
                        f"git show {(sha or 'HEAD')[:8]}:app/experiments/{script}"}
    return {"tier": "none", "argv": None, "script": None, "commit": sha,
            "added": date, "note": reason}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("artifacts", nargs="+")
    ap.add_argument("--python", default=os.path.join(REPO, "app", "env", "bin", "python"))
    ap.add_argument("--print-only", action="store_true",
                    help="emit the commands without running them (default)")
    ap.add_argument("--report", action="store_true",
                    help="classify each artifact by how confidently its origin "
                         "can be established, instead of emitting commands")
    args = ap.parse_args()

    if args.report:
        import collections
        tiers = collections.Counter()
        for name in args.artifacts:
            path = name if os.path.isabs(name) else os.path.join(
                REPO, "ab_test_runtime", "experiments", name)
            r = resolve_producer(path, args.python)
            tiers[r["tier"]] += 1
            extra = r["script"] or (r["note"] or "")
            print(f"  {r['tier']:12} {os.path.basename(path):52} {extra}")
        print(f"\n  {dict(tiers)}", file=sys.stderr)
        return 0

    ok = skipped = 0
    for name in args.artifacts:
        path = name if os.path.isabs(name) else os.path.join(
            REPO, "ab_test_runtime", "experiments", name)
        argv, reason = replay_command(path, args.python)
        base = os.path.basename(path)
        if argv is None:
            print(f"# SKIP {base}: {reason}", file=sys.stderr)
            skipped += 1
            continue
        print(" ".join(shlex.quote(a) for a in argv))
        ok += 1
    print(f"# {ok} replayable, {skipped} not", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
