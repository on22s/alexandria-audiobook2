import pytest

from experiments.ctc_text_boundary import get_segments


def test_get_segments_preserves_text_and_boundaries():
    assert get_segments(["一", "二"], [(0.1, 0.8, -0.2), (0.9, 1.5, -0.3)]) == [
        (0.1, 0.8, "一"), (0.9, 1.5, "二")]


def test_get_segments_rejects_partial_alignment():
    with pytest.raises(ValueError, match="different number"):
        get_segments(["一", "二"], [(0.1, 0.8, -0.2)])
