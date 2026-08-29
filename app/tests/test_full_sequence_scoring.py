import math

from experiments.full_sequence_scoring import recover_logprob


def test_recover_logprob_inverts_single_token_bias():
    original = 0.017
    bias = 7.0
    biased = original * math.exp(bias) / (1 + original * (math.exp(bias) - 1))
    assert math.isclose(recover_logprob(math.log(biased), bias),
                        math.log(original), abs_tol=1e-10)


def test_recover_logprob_preserves_ranking():
    def apply(probability, bias):
        return probability * math.exp(bias) / (
            1 + probability * (math.exp(bias) - 1))
    recovered = [recover_logprob(math.log(apply(p, 10.0)), 10.0)
                 for p in (0.2, 0.01)]
    assert recovered[0] > recovered[1]
