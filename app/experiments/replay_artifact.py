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
import json
import os
import shlex
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("artifacts", nargs="+")
    ap.add_argument("--python", default=os.path.join(REPO, "app", "env", "bin", "python"))
    ap.add_argument("--print-only", action="store_true",
                    help="emit the commands without running them (default)")
    args = ap.parse_args()

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
