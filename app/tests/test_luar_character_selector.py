from experiments.luar_character_selector import get_profiles, is_generic


def test_generic_labels_are_not_character_profiles():
    assert is_generic("UNKNOWN") and is_generic("THE NARRATOR")


def test_profiles_use_only_explicit_baseline_predictions():
    entries = [{"id": "1", "quote_type": "Explicit", "line": "hello"},
               {"id": "2", "quote_type": "Implicit", "line": "ignored"}]
    baseline = {"1": {"predicted": "ALICE"}, "2": {"predicted": "BOB"}}
    assert get_profiles(entries, baseline) == {"ALICE": ["hello"]}
