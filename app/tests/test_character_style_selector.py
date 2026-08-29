from experiments.character_style_selector import select


def test_explicit_nongeneric_quotes_seed_a_generic_override():
    entries = [
        {"id": "e1", "line": "apples orchards apples", "quote_type": "Explicit", "expected_speaker": "ALICE"},
        {"id": "e2", "line": "engines pistons engines", "quote_type": "Explicit", "expected_speaker": "BOB"},
        {"id": "q", "line": "apples orchards apples", "quote_type": "Implicit", "expected_speaker": "ALICE"},
    ]
    rows = [
        {"arm": "baseline", "id": "book:e1", "predicted": "ALICE", "correct": True, "candidates": []},
        {"arm": "baseline", "id": "book:e2", "predicted": "BOB", "correct": True, "candidates": []},
        {"arm": "baseline", "id": "book:q", "predicted": "THE WOMAN", "correct": False, "candidates": []},
    ]
    result = select(entries, rows)
    assert result[-1]["predicted"] == "ALICE"
    assert result[-1]["reason"] == "style_profile_override"
