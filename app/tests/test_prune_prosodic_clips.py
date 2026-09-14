"""Known cases for the two pruning rules before they touch a real dataset.

The rules are Chalamandaris et al. (LREC 2014): drop the phrases farthest
from the corpus centroid in (F0 mean, F0 std), and drop phrases whose text and
audio disagree. Each fixture is one the rule must KEEP and one it must DROP,
so a scorer that keeps everything - or drops everything - fails here.
"""
from experiments.prune_prosodic_clips import mahalanobis_flags, segmental_flags


def test_mahalanobis_flags_the_outlying_phrases_and_only_those():
    # 18 narration clips around 120 Hz +/- small spread, 2 "character voice"
    # clips an octave up with wide spread.
    feats = [{"id": f"n{i}", "f0_mean": 120 + (i % 5), "f0_std": 12 + (i % 3)} for i in range(18)]
    feats += [{"id": "c1", "f0_mean": 240, "f0_std": 45}, {"id": "c2", "f0_mean": 230, "f0_std": 50}]
    dropped = mahalanobis_flags(feats, discard_fraction=0.10)
    assert dropped == {"c1", "c2"}


def test_mahalanobis_keeps_everything_when_asked_to_drop_nothing():
    feats = [{"id": f"n{i}", "f0_mean": 120 + i, "f0_std": 10} for i in range(10)]
    assert mahalanobis_flags(feats, discard_fraction=0.0) == set()


def test_mahalanobis_ignores_clips_without_pitch():
    feats = [{"id": f"n{i}", "f0_mean": 120 + i, "f0_std": 10} for i in range(10)]
    feats.append({"id": "silent", "f0_mean": float("nan"), "f0_std": float("nan")})
    assert "silent" not in mahalanobis_flags(feats, discard_fraction=0.1)


def test_segmental_flags_only_a_real_text_audio_disagreement():
    rows = [
        {"id": "ok", "text": "the quick brown fox jumps over the lazy dog", "hypothesis": "the quick brown fox jumps over the lazy dog"},
        {"id": "typo", "text": "the quick brown fox jumps over the lazy dog", "hypothesis": "the quick brown fox jumped over the lazy dog"},
        {"id": "wrong", "text": "the quick brown fox jumps over the lazy dog", "hypothesis": "it was the best of times it was the worst"},
        {"id": "empty", "text": "the quick brown fox", "hypothesis": ""},
    ]
    assert segmental_flags(rows, max_wer=0.35) == {"wrong", "empty"}
