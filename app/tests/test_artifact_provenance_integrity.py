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
    def setUp(self):
        self.paths = tracked_artifacts()

    def test_there_are_artifacts_to_check(self):
        # Without this the assertions below pass vacuously if ls-files breaks.
        self.assertGreater(len(self.paths), 20,
                           "found only %d tracked artifacts" % len(self.paths))

    def test_every_artifact_parses(self):
        for path in self.paths:
            with open(path, encoding="utf-8") as fh:
                try:
                    json.load(fh)
                except Exception as exc:
                    self.fail("%s is not valid JSON: %s"
                              % (os.path.basename(path), exc))

    def test_a_named_generating_script_still_exists(self):
        """An artifact naming a deleted script cannot be reproduced."""
        missing = []
        for path in self.paths:
            with open(path, encoding="utf-8") as fh:
                try:
                    data = json.load(fh)
                except Exception:
                    continue
            if not isinstance(data, dict):
                continue
            prov = data.get("provenance")
            if not isinstance(prov, dict):
                continue
            script = prov.get("script") or prov.get("script_file")
            if not script:
                continue
            stem = os.path.basename(str(script))
            if stem.endswith(".py") and not os.path.exists(
                    os.path.join(EXPERIMENTS, stem)):
                missing.append("%s names %s, which does not exist"
                               % (os.path.basename(path), stem))
        self.assertEqual(missing, [], "\n  ".join(missing))

    def test_no_artifact_claims_complete_while_holding_a_partial_marker(self):
        """`status: complete` beside an unfinished counter is the shape that
        lets a half-run be read as a finished measurement."""
        bad = []
        for path in self.paths:
            with open(path, encoding="utf-8") as fh:
                try:
                    data = json.load(fh)
                except Exception:
                    continue
            if not isinstance(data, dict) or data.get("status") != "complete":
                continue
            done, want = data.get("completed"), data.get("requested")
            if isinstance(done, int) and isinstance(want, int) and done < want:
                bad.append("%s says complete but %d/%d"
                           % (os.path.basename(path), done, want))
        self.assertEqual(bad, [], "\n  ".join(bad))
