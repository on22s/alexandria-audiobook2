from experiments.contextual_tone_model import (fit_means, get_contexts,
                                                get_unit_intervals, score)


def test_contexts_include_sentence_boundaries():
    assert get_contexts([1, 3, 4]) == [(0, 1, 3), (1, 3, 4), (3, 4, 0)]


def test_context_model_falls_back_to_tone_mean():
    rows = [{"tone": 2, "context": "1-2-4", "contour": [-1, 0, 1]}]
    fallback = fit_means(rows, "tone")
    assert score(rows, {}, "context", fallback) == [1.0]


def test_unit_intervals_cover_audio_without_gaps():
    span = lambda start, end: type("Span", (), {"start": start, "end": end})()
    assert get_unit_intervals([span(1, 2), span(4, 5)], 6.0, 6) == [
        (0.0, 3.0), (3.0, 6.0)]
