"""An unavailable artifact is an unknown audit row, not an aborted audit."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.audit import audit_experiment_artifacts as audit


class ArtifactUnreadableMetadataTests(unittest.TestCase):
    def assert_unknown(self, row, name, error):
        self.assertEqual(row["artifact"], name)
        self.assertEqual(row["completeness"], "unknown")
        self.assertEqual(row["classification"], "exploratory")
        self.assertFalse(row["has_rows"])
        self.assertIn(error, row["reason"])

    def test_missing_and_dangling_files_return_unknown_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            dangling = Path(directory) / "dangling.json"
            dangling.symlink_to(missing)
            for path in (missing, dangling):
                with self.subTest(path=path.name):
                    row = audit.classify_artifact(str(path))
                    self.assert_unknown(row, path.name, "FileNotFoundError")
                    self.assertIsNone(row["bytes"])
                    self.assertIsNone(row["sha256"])

    def test_deleted_after_stat_and_denied_hash_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.json"
            path.write_bytes(b'{"rows": []}')
            original_size = audit.os.path.getsize
            def delete_after_stat(name):
                size = original_size(name)
                path.unlink()
                return size
            with mock.patch.object(audit.os.path, "getsize", side_effect=delete_after_stat):
                row = audit.classify_artifact(str(path))
            self.assert_unknown(row, path.name, "FileNotFoundError")
            self.assertEqual(row["bytes"], 12)
            self.assertIsNone(row["sha256"])
            path.write_bytes(b'{"rows": []}')
            with mock.patch.object(audit, "file_sha256", side_effect=PermissionError("denied")):
                row = audit.classify_artifact(str(path))
            self.assert_unknown(row, path.name, "PermissionError")
            self.assertEqual(path.read_bytes(), b'{"rows": []}')

    def test_full_audit_retains_unavailable_row_and_classifies_other_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "gone.json")
            readable = Path(directory) / "good.json"
            content = b'{"rows": [{"value": 1}]}'
            readable.write_bytes(content)
            with mock.patch.object(audit, "indexable_artifacts", return_value=([missing, str(readable)], [])):
                report = audit.build_audit(directory)
            rows = {row["artifact"]: row for row in report["artifacts"]}
            self.assert_unknown(rows["gone.json"], "gone.json", "FileNotFoundError")
            self.assertEqual(rows["good.json"]["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(rows["good.json"]["bytes"], len(content))
            self.assertTrue(rows["good.json"]["has_rows"])
            self.assertEqual(readable.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
