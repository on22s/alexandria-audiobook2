"""A character's voice follows its settled age/gender states (#653 PR 2): the
state timeline, script line -> chunk, the per-chunk version overlay, the
ranked voice sources for a state (premade library voices before generating,
unused before used), and the routes that suggest, apply and clear it."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import tts
from routers import voices as voices_module
from speaker_traits import (PERSIST_LINES, get_chunk_index_for_entry, get_speaker_trait_summary,
                            get_state_timeline)


def line(speaker, text, gender="male", age="toddler"):
    return {"speaker": speaker, "text": text, "speaker_gender": gender,
            "speaker_age_group": age, "speaker_ageless": False}


def grow_up_script():
    """RUDY: 12 toddler lines, a stray 3-line 'adult' stretch, then 12 child
    lines; ROXY one steady state throughout."""
    script = [{"speaker": "NARRATOR", "text": "Chapter 1: Birth"}]
    script += [line("RUDY", f"baby line {i}") for i in range(12)]
    script += [line("RUDY", f"stray line {i}", age="adult") for i in range(3)]
    script += [line("ROXY", f"roxy line {i}", "female", "young_adult") for i in range(12)]
    script += [{"speaker": "NARRATOR", "text": "Years passed. Chapter 5: The Child"}]
    script += [line("RUDY", f"child line {i}", age="child") for i in range(12)]
    return script


class StateTimelineTests(unittest.TestCase):
    def test_states_match_the_summary_and_start_at_the_settling_line(self):
        script = grow_up_script()
        timeline = get_state_timeline(script)
        self.assertNotIn("ROXY", timeline)   # one state -> nothing to switch
        rudy = timeline["RUDY"]
        self.assertEqual([("male", "toddler"), ("male", "child")],
                         [(s["gender"], s["age_group"]) for s in rudy])
        summary = get_speaker_trait_summary([e for e in script if e.get("speaker") == "RUDY"])
        self.assertEqual([{"gender": s["gender"], "age_group": s["age_group"]} for s in rudy],
                         summary["states"])
        self.assertEqual("baby line 0", script[rudy[0]["from_entry"]]["text"])
        self.assertEqual("child line 0", script[rudy[1]["from_entry"]]["text"])

    def test_a_short_stray_stretch_is_not_a_state(self):
        script = grow_up_script()
        self.assertLess(3, PERSIST_LINES)
        ages = [s["age_group"] for s in get_state_timeline(script)["RUDY"]]
        self.assertNotIn("adult", ages)


class LineToChunkTests(unittest.TestCase):
    chunks = [{"speaker": "NARRATOR", "text": "Chapter 1"},
              {"speaker": "Rudy", "text": "Hello there.  How are\nyou? I'm fine."},
              {"speaker": "ROXY", "text": "I'm fine."},
              {"speaker": "RUDY", "text": "I'm fine. Edited later."}]

    def test_merged_and_reflowed_chunks_still_map_by_content(self):
        self.assertEqual(1, get_chunk_index_for_entry(self.chunks, "RUDY", "How are you?"))

    def test_the_search_starts_at_start_and_respects_the_speaker(self):
        self.assertEqual(1, get_chunk_index_for_entry(self.chunks, "RUDY", "I'm fine."))
        self.assertEqual(3, get_chunk_index_for_entry(self.chunks, "RUDY", "I'm fine.", start=2))
        self.assertEqual(2, get_chunk_index_for_entry(self.chunks, "ROXY", "I'm fine."))

    def test_an_unmatched_line_is_none_not_a_guess(self):
        self.assertIsNone(get_chunk_index_for_entry(self.chunks, "RUDY", "never said"))
        self.assertIsNone(get_chunk_index_for_entry(self.chunks, "RUDY", ""))


class VersionOverlayTests(unittest.TestCase):
    entry = {"type": "custom", "voice": "Ryan", "character_style": "toddler voice", "seed": "7",
             "versions": {"child": {"type": "lora", "adapter_id": "kid_a",
                                    "adapter_path": "lora_models/kid_a", "age_group": "child"}},
             "version_timeline": [{"from_index": 40, "version_id": "child"}]}

    def test_the_version_applies_from_its_chunk_on(self):
        config = {"RUDY": self.entry}
        before = tts.voice_config_for_chunk(config, "RUDY", 39)["RUDY"]
        after = tts.voice_config_for_chunk(config, "RUDY", 40)["RUDY"]
        self.assertEqual(("custom", None), (before["type"], before.get("adapter_id")))
        self.assertEqual(("lora", "kid_a", "lora_models/kid_a"),
                         (after["type"], after["adapter_id"], after["adapter_path"]))
        self.assertEqual("7", after["seed"])                       # untouched fields carry over
        self.assertNotEqual("child", after.get("age_group"))       # the version's label is not
        self.assertEqual("custom", config["RUDY"]["type"])         # the file is untouched

    def test_no_timeline_returns_the_same_object(self):
        config = {"RUDY": {k: v for k, v in self.entry.items() if k != "version_timeline"}}
        self.assertIs(config, tts.voice_config_for_chunk(config, "RUDY", 500))

    def test_a_missing_version_falls_back_to_the_main_voice(self):
        entry = {**self.entry, "version_timeline": [{"from_index": 0, "version_id": "gone"}]}
        self.assertEqual("custom", tts.voice_config_for_chunk({"RUDY": entry}, "RUDY", 5)["RUDY"]["type"])

    def test_a_null_point_returns_to_the_main_voice(self):
        entry = {**self.entry, "version_timeline": [{"from_index": 40, "version_id": "child"},
                                                    {"from_index": 90, "version_id": None}]}
        types = [tts.voice_config_for_chunk({"RUDY": entry}, "RUDY", i)["RUDY"]["type"] for i in (39, 40, 89, 90)]
        self.assertEqual(["custom", "lora", "lora", "custom"], types)

    def test_style_timeline_still_applies_on_top(self):
        entry = {**self.entry, "style_timeline": [{"from_index": 60, "character_style": "seven, bright"}]}
        at_70 = tts.voice_config_for_chunk({"RUDY": entry}, "RUDY", 70)["RUDY"]
        self.assertEqual(("lora", "seven, bright"), (at_70["type"], at_70["character_style"]))


def candidate(adapter_id, gender, age, kind="lora"):
    return {"adapter_id": adapter_id, "name": adapter_id.title(), "type": kind,
            "gender": gender, "age_group": age, "description": ""}


class VoiceSourceTests(unittest.TestCase):
    candidates = [candidate("girl_a", "female", "child"), candidate("boy_used", "male", "child"),
                  candidate("boy_free", "male", "child", "builtin_lora"),
                  candidate("man_free", "male", "adult"), candidate("boy_unknown_age", "male", "unknown"),
                  candidate("teen_free", "male", "teen")]
    users = {"boy_used": ["PAUL"], "mine": ["RUDY"]}

    def sources(self, state, entry=None, users=None):
        return voices_module.get_state_voice_sources(state, "RUDY", entry or {}, self.candidates,
                                                     self.users if users is None else users)

    def test_unused_library_voices_come_before_used_ones_and_gender_is_respected(self):
        got = self.sources({"gender": "male", "age_group": "child"})
        self.assertEqual("boy_free", got["library_unused"][0]["adapter_id"])
        self.assertEqual(["boy_used"], [c["adapter_id"] for c in got["library_used"]])
        self.assertEqual(["PAUL"], got["library_used"][0]["used_by"])
        ids = [c["adapter_id"] for c in got["library_unused"] + got["library_used"]]
        self.assertNotIn("girl_a", ids)
        self.assertNotIn("man_free", ids)            # 3 bands from child
        self.assertNotIn("boy_unknown_age", ids)     # an unknown age is not a match
        self.assertFalse(got["offer_generate"])

    def test_early_childhood_stages_match_the_librarys_child(self):
        for age in ("infant", "toddler", "young_child"):
            got = self.sources({"gender": "male", "age_group": age})
            self.assertEqual("boy_free", got["library_unused"][0]["adapter_id"], age)

    def test_the_library_voice_is_saved_with_the_suggestion_shape(self):
        got = self.sources({"gender": "male", "age_group": "child"})
        self.assertEqual({"type": "builtin_lora", "adapter_id": "boy_free",
                          "adapter_path": "builtin_lora/boy_free"}, got["library_unused"][0]["config"])
        self.assertEqual("lora_models/boy_used", got["library_used"][0]["config"]["adapter_path"])

    def test_a_matching_version_is_offered_first(self):
        entry = {"versions": {"young": {"type": "design", "age_group": "child"},
                              "old": {"type": "design", "age_group": "elderly"},
                              "girl": {"type": "design", "age_group": "child", "gender": "female"}}}
        got = self.sources({"gender": "male", "age_group": "toddler"}, entry)
        self.assertEqual(["young"], [v["version_id"] for v in got["versions"]])

    def test_generating_is_offered_only_when_nothing_fits(self):
        got = self.sources({"gender": "female", "age_group": "elderly"})
        self.assertEqual(([], [], [], True), (got["versions"], got["library_unused"],
                                              got["library_used"], got["offer_generate"]))

    def test_a_voice_this_character_already_uses_is_not_suggested(self):
        got = self.sources({"gender": "male", "age_group": "child"}, users={"boy_free": ["RUDY"]})
        ids = [c["adapter_id"] for c in got["library_unused"] + got["library_used"]]
        self.assertNotIn("boy_free", ids)

    def test_users_count_main_voices_and_versions(self):
        config = {"PAUL": {"type": "lora", "adapter_id": "a"},
                  "ZENITH": {"type": "custom", "versions": {"old": {"type": "builtin_lora", "adapter_id": "b"}}},
                  "LILIA": {"type": "custom", "adapter_id": "stale_custom_field"}}
        self.assertEqual({"a": ["PAUL"], "b": ["ZENITH"]}, voices_module.get_adapter_users(config))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = self.tmp.name
        self.script_path = os.path.join(tmp, "script.json")
        self.voice_path = os.path.join(tmp, "voices.json")
        self.chunks_path = os.path.join(tmp, "chunks.json")
        script = grow_up_script()
        Path(self.script_path).write_text(json.dumps(script), encoding="utf-8")
        chunks = [{"speaker": e["speaker"], "text": e["text"]} for e in script]
        Path(self.chunks_path).write_text(json.dumps(chunks), encoding="utf-8")
        Path(self.voice_path).write_text(json.dumps({
            "RUDY": {"type": "custom", "voice": "Ryan",
                     "versions": {"child": {"type": "lora", "adapter_id": "kid", "age_group": "child"}}},
            "ROXY": {"type": "lora", "adapter_id": "boy_used"}}), encoding="utf-8")
        self.patches = [patch.object(voices_module, "SCRIPT_PATH", self.script_path),
                        patch.object(voices_module, "VOICE_CONFIG_PATH", self.voice_path),
                        patch.object(voices_module, "CHUNKS_PATH", self.chunks_path),
                        patch.object(voices_module, "_build_lora_candidates",
                                     lambda: VoiceSourceTests.candidates)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def saved(self):
        return json.loads(Path(self.voice_path).read_text(encoding="utf-8"))

    def test_the_suggestion_maps_states_to_chunks_and_ranks_sources(self):
        got = asyncio.run(voices_module.get_voice_state_timeline("RUDY"))
        self.assertEqual([1, 29], [s["from_index"] for s in got["states"]])
        self.assertEqual(["Chapter 1", "Chapter 5"], [s["chapter"] for s in got["states"]])
        child = got["states"][1]["sources"]
        self.assertEqual(["child"], [v["version_id"] for v in child["versions"]])
        self.assertEqual("boy_free", child["library_unused"][0]["adapter_id"])
        self.assertEqual(["ROXY"], child["library_used"][0]["used_by"])
        self.assertEqual([], asyncio.run(voices_module.get_voice_state_timeline("ROXY"))["states"])

    def test_a_line_said_twice_maps_to_its_own_place(self):
        script = grow_up_script()
        script[5]["text"] = script[29]["text"] = "Yes."
        Path(self.script_path).write_text(json.dumps(script), encoding="utf-8")
        chunks = [{"speaker": e["speaker"], "text": e["text"]} for e in script]
        Path(self.chunks_path).write_text(json.dumps(chunks), encoding="utf-8")
        got = asyncio.run(voices_module.get_voice_state_timeline("RUDY"))
        self.assertEqual([1, 29], [s["from_index"] for s in got["states"]])

    def test_without_chunks_the_states_are_still_listed(self):
        os.remove(self.chunks_path)
        got = asyncio.run(voices_module.get_voice_state_timeline("RUDY"))
        self.assertEqual([None, None], [s["from_index"] for s in got["states"]])
        self.assertFalse(got["chunks_built"])

    def test_apply_saves_sorted_points_and_clear_removes_them(self):
        request = voices_module.VersionTimelineRequest(points=[
            {"from_index": 29, "version_id": "child"}, {"from_index": 1, "version_id": "child"}])
        res = asyncio.run(voices_module.save_version_timeline("RUDY", request))
        self.assertEqual([1, 29], [p["from_index"] for p in res["version_timeline"]])
        self.assertEqual(res["version_timeline"], self.saved()["RUDY"]["version_timeline"])
        asyncio.run(voices_module.clear_version_timeline("RUDY"))
        self.assertNotIn("version_timeline", self.saved()["RUDY"])
        self.assertEqual("custom", self.saved()["RUDY"]["type"])

    def test_an_unknown_version_is_refused_and_nothing_is_written(self):
        before = Path(self.voice_path).read_bytes()
        request = voices_module.VersionTimelineRequest(points=[
            {"from_index": 1, "version_id": "child"}, {"from_index": 29, "version_id": "nope"}])
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(voices_module.save_version_timeline("RUDY", request))
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual(before, Path(self.voice_path).read_bytes())

    def test_a_return_to_the_main_voice_is_saved(self):
        request = voices_module.VersionTimelineRequest(points=[
            {"from_index": 1, "version_id": "child"}, {"from_index": 29, "version_id": None}])
        asyncio.run(voices_module.save_version_timeline("RUDY", request))
        self.assertEqual([None], [p["version_id"] for p in self.saved()["RUDY"]["version_timeline"]][1:])

    def test_a_negative_index_is_refused_by_the_model(self):
        with self.assertRaises(ValueError):
            voices_module.VersionTimelineRequest(points=[{"from_index": -1, "version_id": "child"}])

    def test_the_voices_tab_save_model_keeps_the_timeline(self):
        dumped = voices_module.VoiceConfigItem(
            version_timeline=[{"from_index": 3, "version_id": "child"}]).model_dump()
        self.assertEqual([{"from_index": 3, "version_id": "child"}], dumped["version_timeline"])


if __name__ == "__main__":
    unittest.main()
