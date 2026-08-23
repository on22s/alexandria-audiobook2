import pytest
import torch

from experiments.ctc_text_boundary import get_log_probs, get_segments


def test_get_segments_preserves_text_and_boundaries():
    assert get_segments(["一", "二"], [(0.1, 0.8, -0.2), (0.9, 1.5, -0.3)]) == [
        (0.1, 0.8, "一"), (0.9, 1.5, "二")]


def test_get_segments_rejects_partial_alignment():
    with pytest.raises(ValueError, match="different number"):
        get_segments(["一", "二"], [(0.1, 0.8, -0.2)])


def test_get_log_probs_reads_transformers_output_logits():
    output = type("Output", (), {"logits": torch.tensor([[[1.0, 2.0]]])})()
    assert torch.allclose(get_log_probs(output).exp().sum(-1), torch.ones(1, 1))
