import math

import pytest

from experiments.ctc_text_boundary import get_log_probs, get_segments


class Logits:
    def __init__(self, values):
        self.values = values

    def log_softmax(self, dim):
        assert dim == -1
        denominator = sum(math.exp(value) for value in self.values)
        return [value - math.log(denominator) for value in self.values]


def test_get_segments_preserves_text_and_boundaries():
    assert get_segments(["一", "二"], [(0.1, 0.8, -0.2), (0.9, 1.5, -0.3)]) == [
        (0.1, 0.8, "一"), (0.9, 1.5, "二")]


def test_get_segments_rejects_partial_alignment():
    with pytest.raises(ValueError, match="different number"):
        get_segments(["一", "二"], [(0.1, 0.8, -0.2)])


def test_get_log_probs_reads_transformers_output_logits():
    output = type("Output", (), {"logits": Logits([1.0, 2.0])})()
    probabilities = [math.exp(value) for value in get_log_probs(output)]
    assert sum(probabilities) == pytest.approx(1.0)
