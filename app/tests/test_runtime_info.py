from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import runtime_info


class RuntimeInfoTests(unittest.TestCase):
    def tearDown(self):
        runtime_info.get_runtime_info.cache_clear()

    def test_runtime_info_reads_git_metadata_and_versions_without_importing_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            git_dir = Path(tmp, ".git")
            ref = "refs/heads/feature"
            revision = "1234567890abcdef1234567890abcdef12345678"
            Path(git_dir, "refs", "heads").mkdir(parents=True)
            Path(git_dir, "HEAD").write_text(f"ref: {ref}\n")
            Path(git_dir, ref).write_text(f"{revision}\n")

            with patch.object(runtime_info.metadata, "version", return_value="1.2.3"):
                result = runtime_info.get_runtime_info(tmp)

        self.assertEqual(revision, result["revision"])
        self.assertEqual("12345678", result["short_revision"])
        self.assertEqual("feature", result["branch"])
        self.assertEqual("git", result["revision_source"])
        self.assertEqual(set(runtime_info.RUNTIME_PACKAGES), set(result["packages"]))

    def test_environment_revision_overrides_git_and_missing_packages_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("os.environ", {"ALEXANDRIA_BUILD_COMMIT": "release-2026-07"}), \
             patch.object(runtime_info.metadata, "version",
                          side_effect=runtime_info.metadata.PackageNotFoundError):
            result = runtime_info.get_runtime_info(tmp)

        self.assertEqual("release-2026-07", result["revision"])
        self.assertEqual("release-", result["short_revision"])
        self.assertEqual("environment", result["revision_source"])
        self.assertIsNone(result["branch"])
        self.assertTrue(all(value is None for value in result["packages"].values()))

    def test_worktree_gitdir_marker_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp, "checkout")
            common_dir = Path(tmp, "metadata")
            git_dir = common_dir / "worktrees" / "checkout"
            checkout.mkdir()
            git_dir.mkdir(parents=True)
            Path(checkout, ".git").write_text("gitdir: ../metadata/worktrees/checkout\n")
            Path(git_dir, "commondir").write_text("../..\n")
            Path(git_dir, "HEAD").write_text("ref: refs/heads/feature\n")
            Path(common_dir, "refs", "heads").mkdir(parents=True)
            Path(common_dir, "refs", "heads", "feature").write_text(
                "abcdef0123456789\n")

            result = runtime_info.get_runtime_info(str(checkout))

        self.assertEqual("abcdef0123456789", result["revision"])
        self.assertEqual("feature", result["branch"])


if __name__ == "__main__":
    unittest.main()
