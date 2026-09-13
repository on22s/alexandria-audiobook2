"""library_time_split holds out the LAST clips by time with a real gap, and
refuses to shrink the split silently."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from experiments import library_time_split as lts


def _rows(n, step=10.0, dur=8.0):
    """n clips, each `dur` s long, starting every `step` s."""
    return [{"_name": f"sample_{i}.wav", "start": i * step,
             "end": i * step + dur, "duration": dur, "text": "x"}
            for i in range(n)]


class TimeSplitTest(unittest.TestCase):
    def test_val_is_the_latest_block_and_train_ends_before_the_gap(self):
        rows = _rows(100)                       # 0 .. 998 s
        train, val, dropped = lts.time_split(rows, val_n=10, train_n=50, gap_s=120)
        self.assertEqual([f"sample_{i}.wav" for i in range(90, 100)],
                         [r["_name"] for r in val])
        self.assertEqual(50, len(train))
        self.assertTrue(all(r["end"] <= val[0]["start"] - 120 for r in train))
        # 12 clips (indices 78..89) end inside the gap and are on neither side.
        self.assertEqual(12, len(dropped))
        self.assertFalse({r["_name"] for r in train} & {r["_name"] for r in val})

    def test_a_gap_that_leaves_too_few_training_clips_is_refused(self):
        rows = _rows(100)
        with self.assertRaises(SystemExit):
            lts.time_split(rows, val_n=10, train_n=85, gap_s=120)

    def test_the_random_split_shape_it_replaces_would_have_neighbours(self):
        """The rejecting case for the ORIGINAL design: a clip-level random
        split puts adjacent clips on both sides, which this split never does."""
        rows = _rows(100)
        train, val, _ = lts.time_split(rows, val_n=10, train_n=50, gap_s=120)
        starts = sorted(r["start"] for r in val)
        last_train_end = max(r["end"] for r in train)
        self.assertGreaterEqual(starts[0] - last_train_end, 120)


if __name__ == "__main__":
    unittest.main()
