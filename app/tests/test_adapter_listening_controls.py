"""The adapter listening package's foreign-narrator control must be the
foreign voice reading THE SAME line, or nothing at all."""
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from experiments import adapter_listening as al
from experiments import adapter_listening_controls as alc


def _wav(path, seconds=0.5, rate=24000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(rate)
        fh.writeframes(np.zeros(int(seconds * rate), dtype="<i2").tobytes())


class ForeignClipTest(unittest.TestCase):
    def test_no_generated_control_means_none_so_the_builder_still_refuses(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(al, "HOLDOUT", tmp):
            self.assertIsNone(al._foreign_clip("warm_baritone_40s_m_1", 0))

    def test_a_generated_control_is_found_only_at_the_foreign_path(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(al, "HOLDOUT", tmp):
            good = os.path.join(tmp, "warm_baritone_40s_m_1",
                                f"foreign_{al.FOREIGN}", "check_1.wav")
            _wav(good)
            # An empty file is not a control (a silent clip would rate as
            # "different voice" and pass the control for the wrong reason).
            empty = os.path.join(tmp, "warm_baritone_40s_m_1",
                                 f"foreign_{al.FOREIGN}", "check_2.wav")
            os.makedirs(os.path.dirname(empty), exist_ok=True)
            Path(empty).write_bytes(b"")
            self.assertEqual(good, al._foreign_clip("warm_baritone_40s_m_1", 1))
            self.assertIsNone(al._foreign_clip("warm_baritone_40s_m_1", 2))
            self.assertIsNone(al._foreign_clip("warm_baritone_40s_m_1", 0))


class HoldoutLinesTest(unittest.TestCase):
    def test_lines_come_in_val_order_which_is_check_index_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = os.path.join(tmp, "a", "val", "metadata.jsonl")
            os.makedirs(os.path.dirname(meta))
            Path(meta).write_text("\n".join(json.dumps(
                {"audio_filepath": f"val/unseen_{i:03d}.wav", "text": f"line {i}"})
                for i in range(4)) + "\n", encoding="utf-8")
            self.assertEqual([(0, "line 0"), (1, "line 1"), (2, "line 2")],
                             alc.holdout_lines(tmp, "a", 3))
            with self.assertRaises(SystemExit):
                alc.holdout_lines(tmp, "a", 5)


if __name__ == "__main__":
    unittest.main()
