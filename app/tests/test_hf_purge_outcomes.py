"""Post-write purge uncertainty must not be reported as a mutation-free refusal."""
import hashlib
import unittest
from unittest.mock import patch

from tests import test_hf_purge_guards as guards

ARCHIVE, tool = guards.ARCHIVE, guards.tool


class PurgeOutcomeTests(unittest.TestCase):
    setUp = guards.PurgeGuardTests.setUp
    tearDown = guards.PurgeGuardTests.tearDown

    def run_error(self, message):
        with self.assertRaisesRegex(Exception, message) as caught:
            tool.run_batch(self.spec, self.hub, log=lambda *_: None)
        self.assertEqual("PurgeVerificationError", type(caught.exception).__name__)
        self.assertNotIsInstance(caught.exception, tool.PurgeRefused)
        self.assertEqual("unverified", caught.exception.report["accounting_status"])
        self.assertEqual(1000, caught.exception.report["expected_bytes"])
        self.assertNotIn("old/adapter_model.safetensors", self.hub.repos[ARCHIVE])
        return caught.exception.report

    def test_acknowledged_purge_with_objects_still_listed_records_completed_steps(self):
        with patch.object(self.hub, "permanently_delete_lfs_files", return_value=None) as purge:
            report = self.run_error("still listed")
        purge.assert_called_once()
        self.assertEqual(["lessons_committed", "paths_deleted", "permanent_delete_returned"],
                         report["completed_steps"])
        self.assertEqual(0, report["observed_freed_bytes"])

    def test_storage_read_error_after_actual_deletion_reports_accounting_uncertainty(self):
        original_purge = self.hub.permanently_delete_lfs_files
        original_list = self.hub.list_lfs_files
        purged = []

        def purge(repo, objects):
            original_purge(repo, objects)
            purged.append(True)

        def listing(repo):
            if purged:
                raise ConnectionError("fixture accounting unavailable")
            return original_list(repo)

        with patch.object(self.hub, "permanently_delete_lfs_files", side_effect=purge) as dispatch, \
             patch.object(self.hub, "list_lfs_files", side_effect=listing):
            report = self.run_error("accounting unavailable")
        dispatch.assert_called_once()
        self.assertNotIn(hashlib.sha256(b"W" * 1000).hexdigest(), self.hub.lfs[ARCHIVE])
        self.assertIn("permanent_delete_returned", report["completed_steps"])
        self.assertIsNone(report["observed_freed_bytes"])

    def test_wrong_post_purge_accounting_keeps_the_completed_deletion_visible(self):
        original = self.hub.permanently_delete_lfs_files

        def purge(repo, objects):
            original(repo, objects)
            self.hub.lfs[repo]["concurrent-storage-fixture"] = 100_000_000

        with patch.object(self.hub, "permanently_delete_lfs_files", side_effect=purge) as dispatch:
            report = self.run_error("freed .* expected")
        dispatch.assert_called_once()
        self.assertEqual(-99_999_000, report["observed_freed_bytes"])
        self.assertIn("permanent_delete_returned", report["completed_steps"])
        self.assertNotIn(hashlib.sha256(b"W" * 1000).hexdigest(), self.hub.lfs[ARCHIVE])

    def test_post_delete_shared_object_guard_stops_before_permanent_purge(self):
        original = self.hub.create_commit

        def commit(repo, operations, commit_message):
            original(repo, operations, commit_message)
            if commit_message.startswith("Remove"):
                self.hub._put(repo, "new-reference/weights", b"W" * 1000)

        with patch.object(self.hub, "create_commit", side_effect=commit), \
             patch.object(self.hub, "permanently_delete_lfs_files") as purge:
            report = self.run_error("another path")
        purge.assert_not_called()
        self.assertEqual(["lessons_committed", "paths_deleted"], report["completed_steps"])
        self.assertIn(hashlib.sha256(b"W" * 1000).hexdigest(), self.hub.lfs[ARCHIVE])

    def test_delete_api_exception_reports_attempt_uncertainty_and_never_retries(self):
        original = self.hub.permanently_delete_lfs_files

        def purge(repo, objects):
            original(repo, objects)
            raise TimeoutError("fixture acknowledgment lost")

        with patch.object(self.hub, "permanently_delete_lfs_files", side_effect=purge) as dispatch:
            report = self.run_error("acknowledgment lost")
        dispatch.assert_called_once()
        self.assertEqual("permanently_delete_lfs_files", report["pending_operation"])
        self.assertNotIn("permanent_delete_returned", report["completed_steps"])
        self.assertIsNone(report["observed_freed_bytes"])
        self.assertNotIn(hashlib.sha256(b"W" * 1000).hexdigest(), self.hub.lfs[ARCHIVE])


if __name__ == "__main__":
    unittest.main()
