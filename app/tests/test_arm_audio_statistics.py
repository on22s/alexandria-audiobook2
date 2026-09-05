import os
import tempfile
import unittest
from unittest import mock

from experiments import arm_audio_statistics as stats


class ArmAudioStatisticsScopeTests(unittest.TestCase):
    def test_empty_scope_is_rejected(self):
        """A normal-looking zero-book artifact is not a measurement."""
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(SystemExit, "nothing was measured"):
                stats.collect_books(work, ["control", "tight"], 40,
                                    mock.sentinel.rng)

    def test_one_complete_pair_is_accepted(self):
        """The guard distinguishes real paired input from an empty scope."""
        with tempfile.TemporaryDirectory() as work:
            for arm in ("control", "tight"):
                os.makedirs(os.path.join(work, "book", arm))
            with mock.patch.object(
                    stats, "arm_mean",
                    side_effect=[({"snr_db": 1.0}, 2),
                                 ({"snr_db": 2.0}, 2)]):
                books = stats.collect_books(
                    work, ["control", "tight"], 40, mock.sentinel.rng)
        self.assertEqual(1, len(books))
        self.assertEqual({"control": 2, "tight": 2}, books[0]["clips"])
        self.assertEqual(2.0, books[0]["arms"]["tight"]["snr_db"])


if __name__ == "__main__":
    unittest.main()
