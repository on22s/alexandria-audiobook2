from unittest.mock import patch

from experiments import build_separator_earcheck as earcheck


def test_resolve_clip_uses_allrows_fallback_for_explicit_terms():
    with patch.object(earcheck.os.path, "exists", side_effect=[False, True]):
        path = earcheck.resolve_clip("none", "kansai")
    assert path.endswith("respelling_none_allrows/kansai_respelled.wav")


def test_resolve_clip_keeps_primary_when_present():
    with patch.object(earcheck.os.path, "exists", return_value=True):
        path = earcheck.resolve_clip("plain", "kansai")
    assert path.endswith("respelling_measure/kansai_plain.wav")
