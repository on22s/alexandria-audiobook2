from unittest.mock import patch

from experiments.japanese_text_boundary import group_windows


def test_grouping_is_monotonic_and_can_merge_internal_pause_windows():
    windows = [(0, 1, "a"), (1, 2, "b"), (3, 4, "c")]
    with patch("experiments.japanese_text_boundary.get_text_cost",
               side_effect=lambda ref, hyp: 0 if (ref, hyp) in {("ab", "ab"), ("c", "c")} else 10):
        assert group_windows(["ab", "c"], windows) == [(0, 2, "ab"), (3, 4, "c")]


def test_grouping_fails_loud_when_vad_has_fewer_windows_than_lines():
    assert group_windows(["a", "b"], [(0, 1, "ab")]) == []
