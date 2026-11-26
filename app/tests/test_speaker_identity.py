import copy
import json
import unittest

from speaker_identity import (build_speaker_consistency_report,
                              get_speaker_label_index, get_validated_alias_map,
                              resolve_speaker_label,
                              stabilize_speaker_identities, _identity_key)


def _entry(speaker):
    return {"speaker": speaker, "text": "Spoken text.", "instruct": "Natural."}


class SpeakerIdentityTests(unittest.TestCase):
    def test_prose_split_repair_respects_established_roster_spelling(self):
        entries = [{"speaker": "R UDEUS", "text": "Rudeus returned."}]
        original = copy.deepcopy(entries)
        result = stabilize_speaker_identities(entries, ["R UDEUS", "UNUSED ROSTER"])
        self.assertEqual("R UDEUS", result["entries"][0]["speaker"])
        self.assertEqual(["R UDEUS", "UNUSED ROSTER"], result["speakers"])
        self.assertEqual(original, entries)

    def test_split_repair_requires_a_complete_name_token_in_prose(self):
        for text in ("Rudeusson returned.", "Arudeus returned.", "Rudeus_1 returned."):
            with self.subTest(text=text):
                result = stabilize_speaker_identities([{"speaker": "R UDEUS", "text": text}])
                self.assertEqual("R UDEUS", result["entries"][0]["speaker"])

    def test_split_repair_returns_the_actual_repaired_speaker_and_keeps_roster(self):
        result = stabilize_speaker_identities(
            [{"speaker": "R UDEUS", "text": "(Rudeus) returned."},
             {"speaker": "ALICE", "text": "Hello."}], ["UNUSED ROSTER"])
        self.assertEqual(["RUDEUS", "ALICE"], [e["speaker"] for e in result["entries"]])
        self.assertEqual({"RUDEUS", "ALICE", "UNUSED ROSTER"}, set(result["speakers"]))
        self.assertNotIn("R UDEUS", result["speakers"])


    def test_canonically_equivalent_unicode_names_share_existing_identity(self):
        composed = "JOSÉ"
        decomposed = "JOSE\u0301"
        self.assertEqual(composed, resolve_speaker_label(decomposed, [composed]))
        entries = [_entry(decomposed)]
        original = copy.deepcopy(entries)
        result = stabilize_speaker_identities(entries, [composed])
        self.assertEqual(composed, result["entries"][0]["speaker"])
        self.assertEqual(original, entries)
        self.assertIsNone(resolve_speaker_label("JOSÈ", [composed]))
        self.assertIsNone(resolve_speaker_label("ＪＯＳＥ", ["JOSE"]))


    def test_consistency_report_counts_usage_and_deduplicates_suggestions(self):
        entries = [{"speaker": "ROSWAAL"}, {"speaker": "ROSWAL"},
                   {"speaker": "ROSWAAL"}]
        review = [{"entry_number": 2, "speaker": "ROSWAL",
                   "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]},
                  {"entry_number": 3, "speaker": "ROSWAL",
                   "candidates": [{"speaker": "ROSWAAL", "similarity": 0.9231}]}]

        report = build_speaker_consistency_report(entries, review)

        self.assertEqual(2, report["speaker_count"])
        self.assertEqual(2, report["speakers"][0]["entry_count"])
        self.assertEqual(1, len(report["review_suggestions"]))
    def test_safe_variants_reuse_first_established_spelling_without_mutation(self):
        entries = [_entry(" roswaal "), _entry("ROSWAAL"), _entry("Voice O.S.")]
        original = copy.deepcopy(entries)

        result = stabilize_speaker_identities(entries, ["ROSWAAL", "VOICE (O.S.)"])

        self.assertEqual(["ROSWAAL", "ROSWAAL", "VOICE (O.S.)"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual(original, entries)

    def test_uncertain_typo_and_extended_name_are_reported_not_merged(self):
        entries = [_entry("ROSWAL"), _entry("OTTO SUWEN")]

        result = stabilize_speaker_identities(entries, ["ROSWAAL", "OTTO"])

        self.assertEqual(["ROSWAL", "OTTO SUWEN"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual(["ROSWAAL"],
                         [item["speaker"] for item in result["review"][0]["candidates"]])
        self.assertEqual(["OTTO"],
                         [item["speaker"] for item in result["review"][1]["candidates"]])

    def test_distinct_names_are_not_reported(self):
        result = stabilize_speaker_identities(
            [_entry("EMILIA"), _entry("VILLAGER 2"), _entry("SUBARU'S MOTHER")],
            ["SUBARU", "VILLAGER 1"])
        self.assertEqual([], result["review"])

    def test_resolve_speaker_label_matches_punctuation_and_spacing_variants(self):
        labels = ["MR. SMITH", "NARRATOR"]
        self.assertEqual("MR. SMITH", resolve_speaker_label("MR SMITH", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("Mr.Smith", labels))

    def test_resolve_speaker_label_returns_none_when_no_match(self):
        self.assertIsNone(resolve_speaker_label("NOBODY", ["MR. SMITH", "NARRATOR"]))
        self.assertIsNone(resolve_speaker_label("", ["MR. SMITH"]))

    def test_resolve_speaker_label_is_deterministic_on_duplicate_keys(self):
        # Both labels normalize to the same identity key; sorted order picks
        # the same winner every time regardless of input iteration order.
        labels = ["Mr Smith", "MR. SMITH"]
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", labels))
        self.assertEqual("MR. SMITH", resolve_speaker_label("mr smith", list(reversed(labels))))




class CombiningMarkIdentityTests(unittest.TestCase):
    """A combining mark is kept only where it is part of the letter.

    THE BUG. `_identity_key` removed everything that is not `\\w`, and `\\w`
    does not match combining marks (Mn/Mc). Devanagari vowel signs are Mc, so
    `राम` and `रमा` both folded to `रम`, and `सीता`/`सता` to `सत`; the
    stabilizer then rewrote the second character's label to the first and two
    characters shared one voice.

    THE OTHER HALF. Keeping every Mn/Mc split names that are one character:
    casefold turns `İ` into `i` plus a dot, and Arabic harakat, Hebrew niqqud
    and Latin/Greek accents are optional diacritics, not different names. Only
    the vowel signs, viramas and nuktas of the Brahmic and Southeast-Asian
    abugidas are identity-bearing, so only those marks are kept.
    """

    def test_devanagari_vowel_signs_are_kept_in_the_key(self):
        self.assertEqual("राम", _identity_key("राम"))
        self.assertNotEqual(_identity_key("राम"), _identity_key("रमा"))
        self.assertNotEqual(_identity_key("सीता"), _identity_key("सता"))

    def test_other_abugida_marks_keep_names_distinct(self):
        pairs = [
            ("Bengali", "রাম", "রমা"),           # Mc vowel sign
            ("Tamil", "ராமா", "ரமா"),             # Mc vowel sign
            ("Thai", "กิ", "ก"),                  # Mn vowel sign
            ("Khmer", "កិ", "ក"),                  # Mn vowel sign
            ("Tibetan", "ཀི", "ཀ"),                # Mn vowel sign
        ]
        for script, first, second in pairs:
            with self.subTest(script=script):
                self.assertNotEqual(_identity_key(first), _identity_key(second))

    def test_precomposed_indic_letter_keeps_its_nukta(self):
        # NFC decomposes U+0958 to क + nukta; the two spellings are one letter
        # and both differ from plain क.
        self.assertEqual(_identity_key("\u0958"), _identity_key("क\u093c"))
        self.assertNotEqual(_identity_key("\u0958"), _identity_key("क"))

    def test_optional_diacritics_fold_exactly_as_before(self):
        """The pairs main already merged must stay merged.

        Latin/Greek/Cyrillic accents (including the ones casefold introduces),
        Arabic harakat and Hebrew niqqud are optional: they do not make a new
        name, so they fold away.
        """
        pairs = [
            ("İbrahim", "Ibrahim"),              # casefold makes U+0307
            ("İSMAİL", "ISMAIL"),
            ("مُحَمَّد", "محمد"),                   # Arabic harakat
            ("رَام", "رام"),
            ("שָׁלוֹם", "שלום"),                    # Hebrew niqqud
            ("ǰ", "j"), ("ẖ", "h"), ("ẗ", "t"), ("ẘ", "w"), ("ẙ", "y"),
            ("ΐ", "ι"), ("ΰ", "υ"), ("ῦ", "υ"), ("ᾶ", "α"),
        ]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.assertEqual(_identity_key(first), _identity_key(second))

    def test_precomposed_greek_varia_is_left_as_main_had_it(self):
        # `ὺ` (U+1F7A) is one letter, not a bare mark: NFC keeps it composed
        # and the old key kept it too. Only the forms casefold actually
        # decomposes (ΐ, ΰ, ᾶ, ῦ) fold to their base letter.
        self.assertEqual("ὺ", _identity_key("ὺ"))
        self.assertEqual("ὺ", _identity_key("υ\u0300"))
        self.assertNotEqual(_identity_key("ὺ"), _identity_key("υ"))

    def test_controls_and_latin_keys_are_unchanged(self):
        """Representative Latin keys stay as they were."""
        expected = {
            "Ram": "ram", "Rama": "rama",
            "MR SMITH": "mrsmith", "MRS SMITH": "mrssmith",
            "김민수": "김민수", "김민소": "김민소",
            "สมชาย": "สมชาย", "สมชย": "สมชย",
            "R UDEUS": "rudeus", "RuDeUs": "rudeus",
            "SUBARU'S": "subarus", "MR. SMITH": "mrsmith",
            "Má": "má", "Ma": "ma",
        }
        for name, key in expected.items():
            with self.subTest(name=name):
                self.assertEqual(key, _identity_key(name))

    def test_latin_accents_match_across_composed_and_decomposed_forms(self):
        decomposed = "JOSE\u0301"
        self.assertEqual(_identity_key("JOSÉ"), _identity_key(decomposed))
        self.assertEqual(_identity_key("Nguyễn"), _identity_key("Nguye\u0302\u0303n"))
        self.assertNotEqual(_identity_key("JOSÉ"), _identity_key("JOSE"))
        # Casefold decomposes ǰ into j plus a combining caron; that mark is
        # not part of an abugida, so it folds away and matches plain j. NFC
        # runs once, before casefold - it is not run again to recompose.
        self.assertEqual(_identity_key("ǰ"), _identity_key("J\u030C"))
        self.assertEqual(_identity_key("ǰ"), _identity_key("j"))

    def test_casefold_and_optional_diacritics_merge_end_to_end(self):
        result = stabilize_speaker_identities(
            [_entry("İBRAHİM"), _entry("Ibrahim")], ["İBRAHİM"])
        self.assertEqual(["İBRAHİM", "İBRAHİM"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual("İBRAHİM",
                         resolve_speaker_label("Ibrahim", ["İBRAHİM"]))
        result = stabilize_speaker_identities(
            [_entry("مُحَمَّد"), _entry("محمد")], ["مُحَمَّد"])
        self.assertEqual(["مُحَمَّد", "مُحَمَّد"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual("مُحَمَّد",
                         resolve_speaker_label("محمد", ["مُحَمَّد"]))

    def test_two_devanagari_names_are_not_relabelled_by_stabilization(self):
        entries = [_entry("राम"), _entry("रमा")]
        result = stabilize_speaker_identities(entries, ["राम", "रमा"])
        self.assertEqual(["राम", "रमा"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual([], result["changes"])
        self.assertEqual({"राम", "रमा"}, set(result["speakers"]))

    def test_the_order_of_arrival_does_not_change_the_outcome(self):
        result = stabilize_speaker_identities(
            [_entry("सता"), _entry("सीता")], ["सीता", "सता"])
        self.assertEqual(["सता", "सीता"],
                         [entry["speaker"] for entry in result["entries"]])
        self.assertEqual([], result["changes"])

    def test_a_saved_script_reloaded_from_disk_keeps_both_speakers(self):
        """Saved books store the labels, not the key, so reload re-derives it."""
        entries = json.loads(json.dumps([_entry("राम"), _entry("रमा")]))
        result = stabilize_speaker_identities(entries, ["राम", "रमा"])
        self.assertEqual(["राम", "रमा"],
                         [entry["speaker"] for entry in result["entries"]])

    def test_punctuation_and_spacing_still_fold_one_devanagari_name(self):
        result = stabilize_speaker_identities([_entry(" राम "), _entry("राम।")],
                                              ["राम"])
        self.assertEqual(["राम", "राम"],
                         [entry["speaker"] for entry in result["entries"]])

    def test_label_lookup_keeps_combining_mark_names_apart(self):
        labels = ["सीता", "सता"]
        self.assertEqual("सीता", resolve_speaker_label("सीता ", labels))
        self.assertIsNone(resolve_speaker_label("सता", ["सीता"]))
        self.assertEqual({"सीता", "सता"}, set(get_speaker_label_index(labels)))

    def test_alias_registry_does_not_report_a_false_target_conflict(self):
        """Two distinct names may each have their own canonical.

        Under the old key both variants folded to `रम`, so the second target
        looked like a conflicting normalization and the whole registry was
        refused. They are different characters, so both edges are valid.
        """
        registry = get_validated_alias_map({"राम": "रामजी", "रमा": "रमेश"})
        self.assertEqual({"राम": "रामजी", "रमा": "रमेश"}, registry)


class SplitNameSpellingTest(unittest.TestCase):
    """The best-formed spelling wins, not the first-arrived one.

    THE BUG. Generating mushoku18, the model emitted `R UDEUS` 86 times and
    `RUDEUS` 16 - the name split after its first letter, which happened to
    exactly one character in three books (every other capitalised name came
    through whole). Both spellings normalise to the same identity, so they were
    already being merged; the canonical was simply whichever arrived first, and
    the split one arrived first. All 102 lines in that book and 137 in
    mushoku23 ended up attributed to a person no cast list contains, so they
    match no voice.
    """

    def test_the_intact_spelling_wins_even_when_the_split_one_comes_first(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "first"},
            {"speaker": "RUDEUS", "text": "second"},
        ])
        self.assertEqual(["RUDEUS", "RUDEUS"],
                         [e["speaker"] for e in out["entries"]])
        self.assertIn("RUDEUS", out["speakers"])
        self.assertNotIn("R UDEUS", out["speakers"])

    def test_a_genuinely_multi_word_name_is_left_alone(self):
        """The repair must not glue real names together: these have no
        alternative spelling, so there is nothing to prefer."""
        names = ["NORTH KING WII TAA", "OLD MAN", "KNUCKLE GUARD"]
        out = stabilize_speaker_identities([{"speaker": n, "text": "x"} for n in names])
        self.assertEqual(names, [e["speaker"] for e in out["entries"]])

    def test_the_roster_still_wins_over_both_spellings(self):
        # An explicit cast list is the caller's own answer and outranks
        # anything inferred from how the model happened to write it.
        out = stabilize_speaker_identities(
            [{"speaker": "R UDEUS", "text": "a"}, {"speaker": "RUDEUS", "text": "b"}],
            established_speakers=["Rudeus"])
        self.assertEqual(["Rudeus", "Rudeus"], [e["speaker"] for e in out["entries"]])

    def test_the_change_is_recorded_so_it_can_be_audited(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "first"},
            {"speaker": "RUDEUS", "text": "second"},
        ])
        kinds = {c["type"] for c in out["changes"]}
        self.assertIn("speaker_spelling", kinds)


class FinishedScriptRepairTest(unittest.TestCase):
    """A script already saved with the broken name must still be repairable.

    Preferring the better spelling only helps while both exist. In mushoku18's
    saved script they do not: generation had already collapsed `RUDEUS` onto
    `R UDEUS` line by line, leaving 67 of the broken form and none of the good
    one. The book's own prose is the evidence - it says "Rudeus" 123 times and
    "R udeus" never.
    """

    def test_a_split_name_is_rejoined_when_the_prose_confirms_it(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "『Ariel-sama.』"},
            {"speaker": "NARRATOR", "text": "Rudeus turned to look at her."},
        ])
        self.assertEqual("RUDEUS", out["entries"][0]["speaker"])

    def test_nothing_is_joined_without_that_evidence(self):
        """`J SMITH` is a person, not a typo. Guessing invents a character."""
        out = stabilize_speaker_identities([
            {"speaker": "J SMITH", "text": "He said nothing at all."},
        ])
        self.assertEqual("J SMITH", out["entries"][0]["speaker"])

    def test_a_two_word_name_is_not_glued_together(self):
        out = stabilize_speaker_identities([
            {"speaker": "OLD MAN", "text": "The old man walked away."},
        ])
        self.assertEqual("OLD MAN", out["entries"][0]["speaker"])

    def test_the_repair_is_recorded(self):
        out = stabilize_speaker_identities([
            {"speaker": "R UDEUS", "text": "Rudeus said so himself."},
        ])
        self.assertIn("speaker_split_repair", {c["type"] for c in out["changes"]})


if __name__ == "__main__":
    unittest.main()
