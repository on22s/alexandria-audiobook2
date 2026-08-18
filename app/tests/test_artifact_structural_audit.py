import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import audit_experiment_artifacts as audit


class StructuralAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_classifies_all_identity_contracts_without_scientific_claims(self):
        self.write("supported.json", {
            "status": "complete", "rows": [],
            "provenance": {"seed": 7, "args": {"seed": 7},
                           "git": {"commit": "abc", "dirty": True,
                                   "harness_sha256": "0" * 64}}})
        self.write("provisional.json", {
            "rows": [{"arm": "x"}],
            "meta": {"git": {"commit": "def", "dirty": False}}})
        self.write("unidentified.json", {"rows": []})
        self.write("list.json", [])
        result = audit.build_audit(str(self.root))

        by_name = {row["artifact"]: row for row in result["artifacts"]}
        self.assertEqual("supported_structure",
                         by_name["supported.json"]["classification"])
        self.assertEqual("provisional",
                         by_name["provisional.json"]["classification"])
        self.assertEqual("exploratory",
                         by_name["unidentified.json"]["classification"])
        self.assertEqual("exploratory",
                         by_name["list.json"]["classification"])
        self.assertIn("do not validate scientific conclusions", result["scope"])

    def test_unreadable_json_is_visible_not_skipped(self):
        (self.root / "broken.json").write_text("{", encoding="utf-8")
        result = audit.build_audit(str(self.root))
        self.assertEqual(1, len(result["artifacts"]))
        self.assertIn("unreadable JSON", result["artifacts"][0]["reason"])


class IndexableArtifactTests(unittest.TestCase):
    """A checked-in index may only contain what everyone else can see.

    PR #340 failed CI on exactly this: six artifacts existed on this machine
    and not in the repository, the local index counted them, and CI - which
    checks out the committed files and regenerates - reported the index stale.
    Every run of the overnight queue would have reproduced it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.tmp.name, check=True,
                       capture_output=True)

    def _write(self, name):
        (self.root / name).write_text(json.dumps({"rows": []}), encoding="utf-8")

    def test_only_tracked_artifacts_are_indexed_and_the_rest_are_named(self):
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        self._write("committed.json")
        self._git("add", "committed.json")
        self._git("commit", "-qm", "add")
        self._write("local_only.json")

        with patch.object(audit, "REPO", self.tmp.name):
            keep, skipped = audit.indexable_artifacts(self.tmp.name)

        self.assertEqual(["committed.json"], [os.path.basename(p) for p in keep])
        self.assertEqual(["local_only.json"], skipped,
                         "an unindexed artifact must still be reported")

    def test_a_directory_without_git_indexes_everything(self):
        """The fallback, asserted rather than assumed.

        Returning an empty index outside a repo would be a plausible-looking
        answer to a question that was never asked - a source export would
        silently report that no evidence exists.
        """
        self._write("a.json")
        self._write("b.json")
        with patch.object(audit, "REPO", self.tmp.name):
            keep, skipped = audit.indexable_artifacts(self.tmp.name)
        self.assertEqual(2, len(keep))
        self.assertEqual([], skipped)


if __name__ == "__main__":
    unittest.main()
