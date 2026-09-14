import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments"))
from chapter_cuts import HEADINGS, control_indices, heading_indices  # noqa: E402
from lora_serving_eval import make_windows  # noqa: E402


class MakeWindows(unittest.TestCase):
    def test_no_cuts_is_fixed_stride(self):
        self.assertEqual(make_windows(7, 3), [[0, 1, 2], [3, 4, 5], [6]])

    def test_cut_restarts_the_stride_and_shortens_the_window_before_it(self):
        self.assertEqual(make_windows(8, 3, [4]), [[0, 1, 2], [3], [4, 5, 6], [7]])

    def test_cuts_outside_range_or_duplicated_are_ignored(self):
        self.assertEqual(make_windows(6, 3, [0, 6, 9, 3, 3]), [[0, 1, 2], [3, 4, 5]])


class Headings(unittest.TestCase):
    def test_mushoku_separators_match_only_whole_line_marks(self):
        seg = [{"text": "■"}, {"text": "-----"}, {"text": "■ not a heading"},
               {"text": "-----\n\nThe diary stops there."}, {"text": "He said ■"}]
        # index 0 is never a cut (a window already starts there)
        self.assertEqual(heading_indices(seg, HEADINGS["mushoku16"]), [1, 3])

    def test_grimgar_needs_the_volume_line_then_a_numbered_title(self):
        seg = [{"text": "x"}, {"text": "Grimgar of Fantasy and Ash: Volume 3\n\n6. The Vote"},
               {"text": "Grimgar of Fantasy and Ash: Volume 3"}, {"text": "6. The Vote"}]
        self.assertEqual(heading_indices(seg, HEADINGS["grimgar03"]), [1])

    def test_control_has_same_count_and_avoids_headings(self):
        ctl = control_indices(100, 5, {10, 20, 30}, seed=0)
        self.assertEqual(len(ctl), 5)
        self.assertFalse(set(ctl) & {0, 10, 20, 30})
        self.assertEqual(ctl, control_indices(100, 5, {10, 20, 30}, seed=0))


if __name__ == "__main__":
    unittest.main()
