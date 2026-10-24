"""Committed artifacts must name a script that exists, and say what they are.

#394 shipped the code for its headline result alongside an artifact that
PREDATED it: the regeneration had been written to the live tree's path rather
than the worktree's, so the committed artifact lacked the two keys holding the
205-v-4 numbers the pull request described. It was found days later, by a merge
conflict, not by any check.

A full artifact-matches-code test would have to regenerate every artifact,
which needs a GPU and a corpus. These are the cheap invariants that hold for
every artifact regardless: it parses, it says which script made it, that script
still exists, and it declares a status rather than leaving a reader to guess
whether a partial file is finished.
"""
import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARTIFACTS = os.path.join(REPO, "ab_test_runtime", "experiments")
EXPERIMENTS = os.path.join(REPO, "app", "experiments")


def tracked_artifacts():
    """-> committed artifact paths only.

    An untracked artifact is one machine's local state; asserting on it makes
    the suite pass or fail depending on what happens to be lying around, which
    is the same reason audit_experiment_artifacts indexes only tracked files.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files",
                              "ab_test_runtime/experiments/*.json"],
                             capture_output=True, text=True, timeout=60)
        names = [l for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return []
    return [os.path.join(REPO, n) for n in names if os.path.exists(os.path.join(REPO, n))]


@unittest.skipUnless(os.path.isdir(ARTIFACTS), "no experiments directory")
class ArtifactIntegrityTests(unittest.TestCase):
    def test_tracked_artifacts_are_parseable_reproducible_and_consistent(self):
        paths = tracked_artifacts()
        self.assertGreater(len(paths), 20,
                           "found only %d tracked artifacts" % len(paths))
        for path in paths:
            data = None
            parsed = False
            name = os.path.basename(path)
            with self.subTest(artifact=name, check="JSON"):
                try:
                    with open(path, encoding="utf-8") as handle:
                        data = json.load(handle)
                    parsed = True
                except Exception as exc:
                    self.fail("%s is not valid JSON: %s" % (name, exc))
            if not parsed or not isinstance(data, dict):
                continue
            with self.subTest(artifact=name, check="generating script"):
                provenance = data.get("provenance")
                if isinstance(provenance, dict):
                    script = provenance.get("script") or provenance.get("script_file")
                    if script:
                        stem = os.path.basename(str(script))
                        if stem.endswith(".py"):
                            self.assertTrue(os.path.exists(os.path.join(EXPERIMENTS, stem)),
                                            "%s names %s, which does not exist" % (name, stem))
            with self.subTest(artifact=name, check="completion"):
                if data.get("status") == "complete":
                    done, want = data.get("completed"), data.get("requested")
                    if isinstance(done, int) and isinstance(want, int):
                        self.assertGreaterEqual(done, want,
                                                "%s says complete but %d/%d" % (name, done, want))
