"""The instrument behind issue #653's Phase 0 numbers.

Each case is a real PDNC label or a real model answer from the eval
artifacts. The first version of this scorer got three of them wrong in ways
that moved the headline: it read a missing gold file as "zero quotes"
(1,084 rows in a band that cannot exist), counted WOODEN-LEGGED MAN as an
unnamed extra although the gold aliases him to JONATHAN SMALL (2,628 rows),
and marked "THE PRIEST" wrong for gold "A PRIEST".
"""
import unittest

from experiments import background_speakers as bs


SUN_ALIASES = [{"A PRIEST", "PRIEST"}, {"A WAITER", "WAITER"},
               {"AN OLD MAN", "OLD MAN ON THE BUS"},
               {"BRETT", "BRETT ASHLEY", "LADY BRETT"}]
SUN_LABELS = {"A PRIEST", "A WAITER", "THE WAITER", "AN OLD MAN",
              "THE WIFE", "THE HUSBAND", "BRETT ASHLEY", "JAKE BARNES",
              "THE BASQUE"}


class SameSpeakerTest(unittest.TestCase):
    def check(self, predicted, gold, expected):
        self.assertIs(bs.is_same_speaker(predicted, gold, SUN_ALIASES,
                                         SUN_LABELS), expected,
                      f"{predicted!r} vs gold {gold!r}")

    def test_article_and_also_suffix_carry_no_identity(self):
        self.check("THE PRIEST", "A PRIEST", True)
        self.check("A PRIEST (ALSO: PRIEST)", "A PRIEST", True)
        self.check("OLD MAN", "AN OLD MAN", True)
        self.check("THE OLD MAN ON THE BUS", "AN OLD MAN", True)

    def test_loose_form_shared_by_two_gold_people_is_not_credited(self):
        # A WAITER and THE WAITER are different people in this gold.
        self.check("WAITER", "THE WAITER", False)
        self.check("A WAITER", "THE WAITER", False)

    def test_wrong_people_stay_wrong(self):
        self.check("THE HUSBAND", "THE WIFE", False)
        self.check("THE BASQUE", "AN OLD MAN", False)
        self.check("JAKE BARNES", "BRETT ASHLEY", False)
        self.check("UNKNOWN", "A PRIEST", False)

    def test_alias_still_credited(self):
        self.check("BRETT", "BRETT ASHLEY", True)


class DescriptiveLabelTest(unittest.TestCase):
    def test_descriptive_forms(self):
        for label in ("THE MARINER", "A HIGH PIPING VOICE", "AN OLD MAN",
                      "ONE OF THE MEN", "GIRL", "GUARD 2", "CROWD",
                      "THE FIRST POLICEMAN"):
            self.assertTrue(bs.is_descriptive_label(label), label)

    def test_names_and_titled_names_are_not_descriptive(self):
        for label in ("LADY BERTRAM", "DOCTOR MANDELET", "JONATHAN SMALL",
                      "MR. THOMAS MARVEL", "UNKNOWN", "NARRATOR", ""):
            self.assertFalse(bs.is_descriptive_label(label), label)


class GoldClassTest(unittest.TestCase):
    INDEX = {"counts": {}, "aliases": {}, "labels": {}}

    def test_descriptor_of_a_named_person_is_its_own_class(self):
        self.assertEqual(bs.get_gold_class(
            "pdnc_thesignofthefour", "WOODEN-LEGGED MAN", self.INDEX, {}),
            "descriptor_of_named")

    def test_bare_role_alias_does_not_make_an_extra_named(self):
        # Gold aliases THE FOOTMAN to FOOTMAN; that is still an extra.
        index = {"counts": {}, "labels": {},
                 "aliases": {"pdnc_ahandfulofdust": [{"THE FOOTMAN", "FOOTMAN"}]}}
        self.assertEqual(bs.get_gold_class(
            "pdnc_ahandfulofdust", "THE FOOTMAN", index, {}), "descriptive")

    def test_title_used_as_name(self):
        titles = bs.get_title_names()
        self.assertEqual(bs.get_gold_class(
            "pdnc_theinvisibleman", "THE INVISIBLE MAN", self.INDEX, titles),
            "title_name")

    def test_missing_gold_count_raises_instead_of_banding_as_zero(self):
        rows = [{"id": "pdnc_x:1", "expected": "THE MARINER",
                 "predicted": "UNKNOWN"}]
        with self.assertRaises(KeyError):
            bs.score_rows(rows, self.INDEX, {})


class OutcomeTest(unittest.TestCase):
    def test_outcomes_are_exhaustive(self):
        self.assertEqual(bs.get_outcome("X", True), "correct")
        self.assertEqual(bs.get_outcome("", False), "abstain")
        self.assertEqual(bs.get_outcome("UNKNOWN", False), "abstain")
        self.assertEqual(bs.get_outcome("THE BARMAN", False), "other_descriptive")
        self.assertEqual(bs.get_outcome("HENFREY", False), "named")


class VolumeTest(unittest.TestCase):
    def test_repeat_generations_group_into_one_volume(self):
        self.assertEqual(bs.get_volume("x/Arc 3 - Volume 4_3.json"),
                         "Arc 3 - Volume 4")
        self.assertEqual(bs.get_volume("Arc 3 - Volume 4.json"),
                         "Arc 3 - Volume 4")



class AbScoringTest(unittest.TestCase):
    """run_ab on a two-line book: one named, one unnamed speaker."""

    def setUp(self):
        import json
        import os
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.book = "pdnc_tiny"
        gold = {"entries": [
            {"id": "1", "line": "Where is she?", "expected_speaker": "BRENDA"},
            {"id": "2", "line": "Gone, sir.", "expected_speaker": "THE PORTER"}],
            "aliases": []}
        with open(os.path.join(self.dir, f"attribution_gold_{self.book}.json"), "w") as f:
            json.dump(gold, f)
        with open(os.path.join(self.dir, f"{self.book}.cast.json"), "w") as f:
            json.dump({"cast": [{"name": "BRENDA LAST", "aliases": ["BRENDA"]},
                                {"name": "THE PORTER", "aliases": []}]}, f)
        self.write_run("cast", ["BRENDA LAST", "THE PORTER"])
        self.write_run("base", ["BRENDA", "UNKNOWN"])

    def write_run(self, arm, speakers):
        import json
        import os
        lines = ["Where is she?", "Gone, sir."]
        doc = {"segmented": [{"text": t} for t in lines],
               "named": [{"text": t, "speaker": s} for t, s in zip(lines, speakers)]}
        with open(os.path.join(self.dir, f"{self.book}__{arm}.json.threepass_checkpoint.json"), "w") as f:
            json.dump(doc, f)

    def score(self):
        from types import SimpleNamespace
        return bs.run_ab(SimpleNamespace(fixtures=self.dir, runs=self.dir, casts=self.dir,
                                         books=[self.book], arms=["base", "cast"]))

    def test_cast_alias_credits_full_name_strict_does_not(self):
        rows = {r["arm"]: r for r in self.score()["rows"]}
        self.assertEqual(rows["cast"]["named"]["strict"], 0)
        self.assertEqual(rows["cast"]["named"]["cast_alias"], 1)
        self.assertEqual(rows["cast"]["descriptive"]["strict"], 1)
        self.assertEqual(rows["base"]["descriptive"]["cast_alias"], 0)
        self.assertEqual(rows["base"]["unknown_lines"], 1)

    def test_incomplete_pass_two_is_refused(self):
        import json
        import os
        path = os.path.join(self.dir, f"{self.book}__base.json.threepass_checkpoint.json")
        doc = json.load(open(path))
        doc["named"] = doc["named"][:1]
        json.dump(doc, open(path, "w"))
        with self.assertRaises(ValueError):
            self.score()


if __name__ == "__main__":
    unittest.main()
