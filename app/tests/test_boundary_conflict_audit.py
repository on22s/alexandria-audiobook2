"""Boeffard-style boundary alerts on known cuts: one clean, one with the
first word lost to the previous chunk, one whose last word the ASR misheard,
and one empty span."""
from experiments.boundary_conflict_audit import audit, boundary_status


def test_boundary_status_names_which_end_failed():
    text = "the cat sat on the mat".split()
    assert boundary_status(text, "the cat sat on the mat".split()) == "ok"
    assert boundary_status(text, "cat sat on the mat".split()) == "first"      # first word deleted
    assert boundary_status(text, "the cat sat on the floor".split()) == "last" # last word replaced by another
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


def test_misspelled_boundary_name_is_not_a_conflict():
    """The four Hero of Ages false alarms (2026-09-14): whisper base.en spells
    invented names its own way; the cut was fine."""
    assert boundary_status("fatren squinted up at the sky".split(), "fattron squinted up at the sky".split()) == "ok"
    assert boundary_status("a keeper of terris".split(), "a keeper of terrorists".split()) == "ok"
    assert boundary_status("hemalurgy was a messy art".split(), "himmelergy was a messy art".split()) == "ok"
    assert boundary_status("he took druffel".split(), "he took druffle".split()) == "ok"
    # ...but the real cut errors from the same book still alert
    assert boundary_status("for jordan sanderson who asked".split(), "read for you by michael kramer".split()) == "last"  # "for" lines up, the rest is the narrator credit
    assert boundary_status("druffel said it won't last".split(), "no matter dropple said it won't last".split()) == "first"
    # a one-letter mishearing of a short word (hat/mat, 0.67) is an ASR error, not a cut
    assert boundary_status("the cat sat on the hat".split(), "the cat sat on the mat".split()) == "ok"
