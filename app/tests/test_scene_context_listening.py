"""The scene-context arms must be the comparison the readings assume."""
import copy
import json
import os
import tempfile
import unittest

from experiments import scene_context_listening as scl


def read_fixture():
    with open(scl.FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


class FixtureTests(unittest.TestCase):
    def test_every_line_is_the_pdnc_text_it_names(self):
        fixture = read_fixture()
        scenes = scl.load_scenes()
        self.assertEqual(6, len(scenes))
        for raw, scene in zip(fixture["scenes"], scenes):
            path = os.path.join(scl.REPO, fixture["source_fixture"].format(book=raw["book"]))
            with open(path, encoding="utf-8") as handle:
                rows = {row["id"]: row for row in json.load(handle)["entries"]}
            for turn in scene["turns"]:
                row = rows[turn["id"]]
                self.assertEqual(row["expected_speaker"], turn["speaker"])
                self.assertEqual(scl.get_spoken_text(row["line"]), turn["text"])
                self.assertNotIn("_", turn["text"])
            self.assertEqual(2, len({turn["speaker"] for turn in scene["turns"]}))

    def test_italics_markup_and_wrapping_are_not_spoken(self):
        self.assertEqual("he _may_ fall".replace("_", ""),
                         scl.get_spoken_text("he _may_\nfall"))

    def test_broken_fixtures_are_refused(self):
        def turn0(fixture):
            return fixture["scenes"][0]["turns"][0]
        cases = [
            (lambda f: turn0(f).update(id="PrideAndPrejudice-99999"), "no PDNC row"),
            (lambda f: turn0(f).update(instruct="  "), "no instruct"),
            (lambda f: f["scenes"][0].update(scene=""), "no scene description"),
            (lambda f: f["scenes"][0].update(voices={"MRS. BENNET": "female",
                                                    "MR. BENNET": "female"}),
             "one female and one male"),
            (lambda f: f["scenes"][0]["voices"].pop("MR. BENNET"), "one female and one male"),
            (lambda f: f["scenes"][1].update(id=f["scenes"][0]["id"]), "duplicate scene id"),
            (lambda f: f.update(scenes=f["scenes"][:1]), "at least two scenes"),
            (lambda f: f["scenes"][0].update(turns=f["scenes"][0]["turns"][:1]),
             "two-speaker exchange"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fixture.json")
            for mutate, message in cases:
                with self.subTest(message=message):
                    fixture = copy.deepcopy(read_fixture())
                    mutate(fixture)
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(fixture, handle)
                    with self.assertRaisesRegex(scl.SceneFixtureError, message):
                        scl.load_scenes(path)

    def test_an_uncast_speaker_is_refused(self):
        fixture = copy.deepcopy(read_fixture())
        fixture["scenes"][0]["voices"] = {"MRS. BENNET": "female", "MR. COLLINS": "male"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fixture.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(fixture, handle)
            with self.assertRaisesRegex(scl.SceneFixtureError, "is not cast"):
                scl.load_scenes(path)


class WrongSceneTests(unittest.TestCase):
    def test_no_scene_ever_borrows_its_own_description(self):
        for count in range(2, 10):
            ids = [f"s{n}" for n in range(count)]
            mapping = scl.get_wrong_scene_map(ids)
            with self.subTest(count=count):
                self.assertTrue(all(mapping[i] != i for i in ids))
                self.assertEqual(sorted(ids), sorted(mapping.values()))
                self.assertEqual(mapping, scl.get_wrong_scene_map(list(ids)))

    def test_the_fixture_borrows_from_a_different_book(self):
        # The docstring promises a fluent but wrong description. A scene from
        # the same novel could share characters and mood, and weaken the control.
        scenes = scl.load_scenes()
        books = {scene["id"]: scene["book"] for scene in scenes}
        mapping = scl.get_wrong_scene_map([scene["id"] for scene in scenes])
        for own, borrowed in mapping.items():
            self.assertNotEqual(books[own], books[borrowed], own)


class ArmInstructionTests(unittest.TestCase):
    def test_each_arm_sends_exactly_its_stated_instruction(self):
        scenes = scl.load_scenes()
        arms = scl.get_arm_instructions(scenes)
        described = {scene["id"]: scene["scene"] for scene in scenes}
        mapping = scl.get_wrong_scene_map([scene["id"] for scene in scenes])
        for scene in scenes:
            own = arms[scene["id"]]
            self.assertEqual(set(scl.ARMS), set(own))
            for index, turn in enumerate(scene["turns"]):
                self.assertEqual(turn["instruct"], own["line"][index])
                self.assertEqual(f"{scene['scene']} {turn['instruct']}",
                                 own["scene"][index])
                self.assertEqual(
                    f"{described[mapping[scene['id']]]} {turn['instruct']}",
                    own["wrong_scene"][index])
                self.assertEqual(3, len({own[arm][index] for arm in scl.ARMS}))


class ContentGateTests(unittest.TestCase):
    def test_only_arms_worse_than_line_by_the_margin_are_flagged(self):
        self.assertEqual([], scl.get_content_regressions(
            {"line": 0.02, "scene": 0.07, "wrong_scene": 0.0}))
        self.assertEqual(["scene"], scl.get_content_regressions(
            {"line": 0.02, "scene": 0.0701, "wrong_scene": 0.0}))

    def test_an_unscored_arm_is_never_a_pass(self):
        self.assertEqual(["wrong_scene"], scl.get_content_regressions(
            {"line": 0.0, "scene": 0.0, "wrong_scene": None}))
        self.assertEqual(["line", "scene", "wrong_scene"], scl.get_content_regressions(
            {"line": None, "scene": 0.0, "wrong_scene": 0.0}))


class VoiceTests(unittest.TestCase):
    CONFIG = {
        "F": {"type": "custom", "voice": "Serena", "character_style": "Warm.",
              "default_style": "Calm."},
        "M": {"type": "lora", "adapter_path": "x", "character_style": "Dry."},
        "C": {"type": "clone", "ref_audio": "a.wav"},
    }

    def test_styles_are_cleared_where_both_render_paths_look(self):
        entries, config = scl.get_arm_voices(
            self.CONFIG, {"female": "F", "male": "M"}, 99)
        for role, name in (("female", "F"), ("male", "M")):
            for entry in (entries[role], config[name]):
                self.assertNotIn("character_style", entry)
                self.assertNotIn("default_style", entry)
                self.assertEqual("99", entry["seed"])
        self.assertIn("character_style", self.CONFIG["F"])  # input untouched

    def test_clone_and_missing_voices_are_refused(self):
        with self.assertRaisesRegex(scl.SceneFixtureError, "ignore instruct"):
            scl.get_arm_voices(self.CONFIG, {"female": "C", "male": "M"}, 1)
        with self.assertRaisesRegex(scl.SceneFixtureError, "not in the voice config"):
            scl.get_arm_voices(self.CONFIG, {"female": "F", "male": "X"}, 1)


if __name__ == "__main__":
    unittest.main()
