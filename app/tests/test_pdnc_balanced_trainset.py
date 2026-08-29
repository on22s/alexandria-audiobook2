import random

from experiments.pdnc_balanced_trainset import balanced_sample


def make(speaker, category, number):
    return {"teacher": speaker, "speaker_category": category, "line": str(number),
            "quote_structure": "split" if number % 2 else "continuous"}


def test_balanced_sample_caps_dominant_speakers_and_keeps_categories():
    rows = ([make("MAJOR", "major", i) for i in range(100)] +
            [make("MIDDLE", "intermediate", i) for i in range(8)] +
            [make("MINOR", "minor", i) for i in range(8)])
    chosen = balanced_sample(rows, 30, 8, random.Random(1))
    counts = {speaker: sum(r["teacher"] == speaker for r in chosen)
              for speaker in ("MAJOR", "MIDDLE", "MINOR")}
    assert counts == {"MAJOR": 8, "MIDDLE": 8, "MINOR": 8}


def test_balanced_sample_is_seed_reproducible():
    rows = [make("A", "major", i) for i in range(20)]
    first = balanced_sample([dict(r) for r in rows], 5, 5, random.Random(7))
    second = balanced_sample([dict(r) for r in rows], 5, 5, random.Random(7))
    assert [r["line"] for r in first] == [r["line"] for r in second]
