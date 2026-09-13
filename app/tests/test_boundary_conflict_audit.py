"""Boeffard-style boundary alerts on known cuts: one clean, one with the
first word lost to the previous chunk, one whose last word the ASR misheard,
and one empty span."""
from experiments.boundary_conflict_audit import audit, boundary_status


def test_boundary_status_names_which_end_failed():
    text = "the cat sat on the mat".split()
    assert boundary_status(text, "the cat sat on the mat".split()) == "ok"
    assert boundary_status(text, "cat sat on the mat".split()) == "first"      # first word deleted
    assert boundary_status(text, "the cat sat on the hat".split()) == "last"   # last word substituted
    assert boundary_status(text, "uh the cat sat on the".split()) == "last"    # insertion before is fine, last missing
    assert boundary_status(text, "nothing alike here at all".split()) == "both"
    assert boundary_status(text, []) == "empty"


def test_audit_windows_words_by_time_with_slack():
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
             for i, w in enumerate("the cat sat on the mat and the dog ran".split())]
    chunks = [{"audio_filepath": "a", "start": 0.0, "end": 2.9, "text": "The cat sat on the mat"},
              {"audio_filepath": "b", "start": 3.0, "end": 5.0, "text": "and the dog ran"},
              # cut 0.3 s late: 'and' (3.0-3.4) falls in the previous chunk's span
              {"audio_filepath": "c", "start": 3.45, "end": 5.0, "text": "and the dog ran"}]
    rows = audit(chunks, words, slack=0.15)
    assert [r["status"] for r in rows] == ["ok", "ok", "first"]
