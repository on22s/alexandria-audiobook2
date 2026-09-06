"""The leak check must fail on a leak, and its first version could not.

It compared basenames. Every split renumbers from zero, so a clip present in
both `train/` and `val/` carries two different names and the comparison could
never fire - it reported CLEAN on a file compared against itself. It also lived
inside a bash heredoc where no test could reach it.

These fixtures are the three cases it was hand-checked on when the bug was
found, kept so it cannot quietly stop discriminating.
"""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

from experiments.holdout_leak import clip_keys, leaked  # noqa: E402


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for i, (vol, text) in enumerate(rows):
            handle.write(json.dumps({"audio_filepath": f"train/train_{i:04d}.wav",
                                     "text": text, "source_volume": vol}) + "\n")
    return path


class HoldoutLeak(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = _write(os.path.join(self.tmp, "train.jsonl"),
                        [("book_vol01", "the first line"),
                         ("book_vol01", "the second line"),
                         ("book_vol02", "a third line")])
        self.b = _write(os.path.join(self.tmp, "val.jsonl"),
                        [("book_vol03", "an unseen line"),
                         ("book_vol03", "another unseen line")])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disjoint_splits_are_clean(self):
        self.assertEqual((0, 2), leaked(self.a, self.b))

    def test_a_file_against_itself_is_a_total_leak(self):
        """The case the old rule passed. It must be the loudest failure."""
        self.assertEqual((3, 3), leaked(self.a, self.a))

    def test_one_shared_clip_is_caught_despite_different_filenames(self):
        """Both files number from zero, so names cannot be the signal."""
        c = _write(os.path.join(self.tmp, "val2.jsonl"),
                   [("book_vol03", "an unseen line"),
                    ("book_vol01", "the second line")])
        self.assertEqual((1, 2), leaked(self.a, c))

    def test_same_text_in_a_different_volume_is_not_a_leak(self):
        """A stock phrase repeated across books is not the same recording."""
        c = _write(os.path.join(self.tmp, "val3.jsonl"),
                   [("book_vol09", "the second line")])
        self.assertEqual((0, 1), leaked(self.a, c))

    def test_an_empty_side_refuses_rather_than_reporting_clean(self):
        empty = _write(os.path.join(self.tmp, "empty.jsonl"), [])
        with self.assertRaises(ValueError):
            leaked(self.a, empty)

    def test_filenames_are_not_part_of_the_identity(self):
        self.assertTrue(all(len(k) == 2 for k in clip_keys(self.a)),
                        "a clip's key is (source_volume, text)")


if __name__ == "__main__":
    unittest.main()
