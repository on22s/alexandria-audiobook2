"""#2101: batch output collision must be case-aware on Windows.

The guard that keeps one batch task from overwriting another's multi-GB ZIP
used raw string membership while the rest of the file compares names through
os.path.normcase. These tests patch normcase so the Windows behaviour is
proved on a case-sensitive Linux CI runner, and they keep a POSIX-mode case
where distinct spellings must stay distinct.
"""
import asyncio
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks

import routers.preparer as preparer


def run_batch(tasks, outputs, uploads, normcase):
    """Run the real preparer batch worker and return the --output names used.

    The worker is captured from the claimed-task registration instead of being
    reimplemented, so the collision guard under test is the shipped one.
    """
    captured = {}
    written = []

    def register(background_tasks, task_name, claim_id, callback, *args, **kwargs):
        captured["run"] = args[-1]
        return claim_id

    def stream(cmd, cwd, state, **kwargs):
        written.append(cmd[cmd.index("--output") + 1])
        return 0, []

    request = preparer.BatchPreparerRequest(tasks=[
        {"audio_filename": audio, "output_filename": output} for audio, output in tasks
    ])
    with patch.object(preparer, "PREPARER_OUTPUT_DIR", str(outputs)), \
            patch.object(preparer, "UPLOADS_DIR", str(uploads)), \
            patch.object(preparer, "_resolve_preparer_interpreter", return_value="python"), \
            patch.object(preparer, "_revalidate_voicelab_paths", return_value=None), \
            patch.object(preparer, "check_global_gpu_lock"), \
            patch.object(preparer, "check_disk_space", return_value=(True, 10)), \
            patch.object(preparer, "reserve_background_task", return_value="fixture-claim"), \
            patch.object(preparer, "register_claimed_background_task", side_effect=register), \
            patch.object(preparer, "_stream_subprocess_to_logs", side_effect=stream), \
            patch.object(preparer.os.path, "normcase", side_effect=normcase):
        asyncio.run(preparer._start_preparer_batch(request, BackgroundTasks()))
        state = preparer.process_state["batch_preparer"]
        state["cancel"] = False
        captured["run"]()
    return [Path(path).name for path in written]


class PreparerOutputCollisionTests(unittest.TestCase):
    def setUp(self):
        self.state = preparer.process_state["batch_preparer"]
        self.saved_state = dict(self.state)

    def tearDown(self):
        self.state.clear()
        self.state.update(self.saved_state)

    def test_existing_file_differing_only_in_case_is_not_overwritten_on_windows(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root, "outputs")
            outputs.mkdir()
            uploads = Path(root, "uploads")
            uploads.mkdir()
            (uploads / "one.wav").write_bytes(b"audio")
            (outputs / "Book.zip").write_bytes(b"older dataset")
            self.assertEqual(
                ["book_1.zip"],
                run_batch([("one.wav", "book.zip")], outputs, uploads, str.lower),
            )
            self.assertEqual(b"older dataset", (outputs / "Book.zip").read_bytes())

    def test_two_batch_outputs_differing_only_in_case_get_distinct_names_on_windows(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root, "outputs")
            outputs.mkdir()
            uploads = Path(root, "uploads")
            uploads.mkdir()
            for name in ("one.wav", "two.wav"):
                (uploads / name).write_bytes(b"audio")
            self.assertEqual(
                ["A.zip", "a_1.zip"],
                run_batch([("one.wav", "A.zip"), ("two.wav", "a.zip")],
                          outputs, uploads, str.lower),
            )

    def test_case_sensitive_filesystem_keeps_distinct_spellings_distinct(self):
        """The fix must not overreach: POSIX normcase is the identity."""
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root, "outputs")
            outputs.mkdir()
            uploads = Path(root, "uploads")
            uploads.mkdir()
            (uploads / "one.wav").write_bytes(b"audio")
            (outputs / "Book.zip").write_bytes(b"older dataset")
            self.assertEqual(
                ["book.zip"],
                run_batch([("one.wav", "book.zip")], outputs, uploads, lambda value: value),
            )

    def test_all_comparisons_share_one_normcase_call_site(self):
        source = Path(preparer.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, len(re.findall(r"\bos\.path\.normcase\(", source)))
        self.assertEqual(1, source.count("_get_preparer_name_key(source_path)"))
        self.assertEqual(1, source.count("_get_preparer_name_key(audio_path)"))
        self.assertIn("_get_preparer_name_key(filename)", source)
        self.assertIn("_get_preparer_name_key(e.name)", source)
        self.assertIn("_get_preparer_name_key(candidate)", source)


if __name__ == "__main__":
    unittest.main()
