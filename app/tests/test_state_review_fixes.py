"""Regression tests for the open PR #1040 review findings (one class per task).
Synthetic fixtures only (Rule 27)."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from contextlib import ExitStack

import tts
from routers import voices as voices_module
from speaker_traits import get_entry_speaker, get_persona_state_targets
from tests import test_state_personas as state_persona_tests


def two_state_script():
    rows = [{"speaker": "NARRATOR", "text": "The story opens."}]
    rows += [{"speaker": "MIRA", "text": f"Small line {i}.", "speaker_gender": "female",
              "speaker_age_group": "child"} for i in range(12)]
    rows += [{"speaker": "OTTO", "text": "An unrelated remark."}]
    rows += [{"speaker": "NARRATOR", "text": "Years pass."}]
    rows += [{"speaker": "MIRA", "text": f"Grown line {i}.", "speaker_gender": "female",
              "speaker_age_group": "adult"} for i in range(12)]
    return rows


class ScriptFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script_path = os.path.join(self.tmp.name, "script.json")
        self.write_script(two_state_script())
        self.patches = [patch.object(voices_module, "SCRIPT_PATH", self.script_path)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_script(self, script):
        self.script = script
        Path(self.script_path).write_text(json.dumps(script), encoding="utf-8")


class T1SegmentFreshnessTests(ScriptFixture):
    def test_an_edit_outside_a_state_keeps_it_current_and_inside_makes_it_stale(self):
        before = get_persona_state_targets(self.script)["MIRA"]
        edited = copy.deepcopy(self.script)
        edited[13]["text"] = "A different unrelated remark."     # OTTO, inside the segment but not MIRA's evidence
        self.assertEqual(before, get_persona_state_targets(edited)["MIRA"])
        edited[14]["text"] = "Many years pass."                   # narration inside state 2's segment
        after = get_persona_state_targets(edited)["MIRA"]
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[1]["segment_sha256"], after[1]["segment_sha256"])
        self.assertEqual(before[1]["version_id"], after[1]["version_id"])

    def test_a_stale_unapplied_state_can_be_removed(self):
        target = get_persona_state_targets(self.script)["MIRA"][1]
        stale = {**target, "segment_sha256": "0" * 64}
        entry = {"type": "custom", "versions": {target["version_id"]: {"persona_state": stale}}}
        after = {**entry, "versions": {}}
        with self.assertRaises(HTTPException) as caught:
            voices_module.get_validated_voice_changes("MIRA", entry, after)
        self.assertEqual(409, caught.exception.status_code)
        voices_module.get_validated_voice_changes("MIRA", entry, after, allow_state_removal=True)

    def test_an_applied_state_is_still_refused(self):
        target = get_persona_state_targets(self.script)["MIRA"][1]
        entry = {"type": "custom", "versions": {target["version_id"]: {"persona_state": target}},
                 "version_timeline": [{"from_index": 3, "version_id": target["version_id"]}]}
        with self.assertRaises(HTTPException) as caught:
            voices_module.get_validated_voice_changes("MIRA", entry, {**entry, "versions": {}},
                                                      allow_state_removal=True)
        self.assertEqual(409, caught.exception.status_code)


class T17LegacySeedTests(unittest.TestCase):
    def test_an_unchanged_legacy_seed_does_not_block_a_save_but_a_new_bad_seed_does(self):
        before = {"type": "custom", "seed": "", "versions": {"old": {"seed": "1.5"}}}
        voices_module.get_validated_voice_changes("X", before, {**before, "ready": True})
        for bad in ({**before, "seed": "2.5"}, {**before, "versions": {"old": {"seed": "abc"}}}):
            with self.assertRaises(HTTPException) as caught:
                voices_module.get_validated_voice_changes("X", before, bad)
            self.assertEqual(422, caught.exception.status_code)


class T21EntrySpeakerTests(unittest.TestCase):
    def test_one_speaker_reading_everywhere(self):
        import generate_personas
        for entry, want in (({"speaker": " Mira "}, "Mira"), ({"type": "NARRATOR"}, "NARRATOR"),
                            ({"speaker": 123}, ""), ("not a dict", ""), ({}, "")):
            self.assertEqual(want, get_entry_speaker(entry))
            self.assertEqual(want, voices_module.get_script_speaker(entry))
            if isinstance(entry, dict):
                self.assertEqual(want, generate_personas._entry_speaker(entry))


class StateApiFixture(unittest.TestCase):
    fixture = state_persona_tests.StateVoiceApiTests.fixture


class T2SelectStateVersionTests(StateApiFixture):
    def test_a_state_version_is_refused_and_a_manual_one_overlays_without_bookkeeping(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            before = path.read_bytes()
            refused = client.post(f"/api/voices/ARTHUR/versions/{targets[1]['version_id']}/select")
            self.assertEqual(409, refused.status_code, refused.text)
            self.assertEqual(before, path.read_bytes())
            config = json.loads(path.read_text())
            config["ARTHUR"]["versions"]["manual"] = {"type": "custom", "voice": "Ryan", "age_group": "adult",
                                                      "candidates": [{"candidate_id": "c-manual"}]}
            config["ARTHUR"]["candidates"] = [{"candidate_id": "c-base"}]
            path.write_text(json.dumps(config))
            selected = client.post("/api/voices/ARTHUR/versions/manual/select")
            self.assertEqual(200, selected.status_code, selected.text)
            saved = json.loads(path.read_text())["ARTHUR"]
            self.assertEqual(("custom", "Ryan", "manual"), (saved["type"], saved["voice"], saved["active_version"]))
            self.assertEqual([{"candidate_id": "c-base"}], saved["candidates"])   # the version's pool does not replace the base's


class T3OverlayBookkeepingTests(unittest.TestCase):
    def test_state_bookkeeping_never_reaches_a_chunk_render_config(self):
        entry = {"type": "custom", "voice": "Ryan", "persona_status": "approved",
                 "versions": {"state_x": {"type": "lora", "adapter_id": "kid", "persona_state": {"v": 1},
                                          "persona_status": "generated", "voice_status": "generated",
                                          "persona_voice_audit": {"x": 1}}},
                 "version_timeline": [{"from_index": 0, "version_id": "state_x"}]}
        resolved = tts.voice_config_for_chunk({"A": entry}, "A", 5)["A"]
        self.assertEqual("lora", resolved["type"])
        self.assertNotIn("persona_state", resolved)
        self.assertNotIn("persona_voice_audit", resolved)
        self.assertEqual("approved", resolved["persona_status"])   # the base's own review state, not the version's


class T4SaveOverStateVersionTests(StateApiFixture):
    def test_saving_the_current_voice_into_a_state_keeps_its_state(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get("/api/voice_config/snapshot").json()["book_token"]
            version_id = targets[2]["version_id"]
            stored = json.loads(path.read_text())["ARTHUR"]["versions"][version_id]["persona_state"]
            result = client.post("/api/voices/ARTHUR/versions", json={"version_id": version_id, "book_token": token})
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual(stored, json.loads(path.read_text())["ARTHUR"]["versions"][version_id]["persona_state"])


class T5ServerOwnedStateTests(StateApiFixture):
    def test_a_client_cannot_invent_or_change_a_state_or_take_a_state_id(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            missing = [t for t in get_persona_state_targets(json.loads(Path(directory, "annotated_script.json").read_text()))["ARTHUR"]
                       if t["version_id"] not in snapshot["config"]["ARTHUR"].get("versions", {})]
            cases = []
            body = copy.deepcopy(snapshot["config"])
            body["ARTHUR"]["versions"]["invented"] = {"type": "custom", "persona_state": targets[0]}
            cases.append((body, 409))
            body = copy.deepcopy(snapshot["config"])
            body["ARTHUR"]["versions"]["state_" + "a" * 24] = {"type": "custom", "voice": "Ryan"}
            cases.append((body, 422))
            for body, status in cases:
                before = path.read_bytes()
                response = client.post("/api/voice_config/save", json={"revision": snapshot["revision"],
                                       "book_token": snapshot["book_token"], "voices": body})
                self.assertEqual(status, response.status_code, response.text)
                self.assertEqual(before, path.read_bytes())
            # A stale version cannot be made to look generated by copying the
            # current target (published in GET /api/voices) onto it.
            stored = json.loads(path.read_text())
            stored["ARTHUR"]["versions"][targets[1]["version_id"]]["persona_state"]["segment_sha256"] = "0" * 64
            path.write_text(json.dumps(stored))
            snapshot = client.get("/api/voice_config/snapshot").json()
            body = copy.deepcopy(snapshot["config"])
            body["ARTHUR"]["versions"][targets[1]["version_id"]]["persona_state"] = targets[1]
            cases = [(body, 409)]
            for body, status in cases:
                before = path.read_bytes()
                response = client.post("/api/voice_config/save", json={"revision": snapshot["revision"],
                                       "book_token": snapshot["book_token"], "voices": body})
                self.assertEqual(status, response.status_code, response.text)
                self.assertEqual(before, path.read_bytes())


class T6VoiceLibraryStateTests(unittest.TestCase):
    def test_casts_never_carry_state_versions_or_timelines_between_books(self):
        from core import _make_library_entry
        from routers.voice_library import _apply_cast_mapping
        book_a = {"type": "lora", "adapter_id": "mira", "alias_of": "X", "active_version": "state_" + "a" * 24,
                  "versions": {"manual": {"type": "custom"}, "state_" + "a" * 24: {"persona_state": {"v": "A"}}},
                  "version_timeline": [{"from_index": 4, "version_id": "state_" + "a" * 24}]}
        entry = _make_library_entry("MIRA", book_a, 30, "bookA")
        self.assertEqual({"manual": {"type": "custom"}}, entry["config"]["versions"])
        for key in ("version_timeline", "alias_of", "active_version"):
            self.assertNotIn(key, entry["config"])
        legacy = copy.deepcopy(entry)
        legacy["config"] = copy.deepcopy(book_a)          # saved before this rule
        lib = {"casts": {"c": {"members": {"MIRA": legacy}}}, "shared": {}}
        book_b = {"MIRA": {"type": "custom", "versions": {"state_" + "b" * 24: {"persona_state": {"v": "B"}}},
                           "version_timeline": [{"from_index": 9, "version_id": "state_" + "b" * 24}]}}
        result, applied = _apply_cast_mapping(lib, "c", {"MIRA": "MIRA"}, book_b)
        mira = result["MIRA"]
        self.assertEqual(["MIRA"], applied)
        self.assertEqual("lora", mira["type"])
        self.assertEqual({"manual", "state_" + "b" * 24}, set(mira["versions"]))      # B's own state kept, A's never imported
        self.assertEqual(book_b["MIRA"]["version_timeline"], mira["version_timeline"])


class T8TimelineExactSpeakerTests(ScriptFixture):
    def test_case_variant_speakers_are_not_merged_in_the_timeline_view(self):
        import asyncio
        script = two_state_script()
        for row in script[1:13]:
            row["speaker"] = "Mira"                  # a different character, spelled differently
        self.write_script(script)
        voice_path = os.path.join(self.tmp.name, "voices.json")
        Path(voice_path).write_text("{}", encoding="utf-8")
        with patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path), \
             patch.object(voices_module, "CHUNKS_PATH", os.path.join(self.tmp.name, "none.json")), \
             patch.object(voices_module, "_build_lora_candidates", lambda: []):
            for name in ("Mira", "MIRA"):
                got = asyncio.run(voices_module.get_voice_state_timeline(name))
                self.assertEqual([], got["states"], name)   # each has ONE settled state of its own


def fake_state_run(seen=None):
    def run(entries, selected, samples, config, *args, **options):
        if seen is not None:
            seen.append([row["_source_entry_index"] for row in entries])
        config[selected[0]] = {"type": "design", "description": "generated"}
        return []
    return run


class T9UnsetBaseFromCurrentFirstStateTests(unittest.TestCase):
    def test_an_unset_base_takes_a_current_state_one_even_while_a_later_state_is_pending(self):
        import generate_personas as personas
        from types import SimpleNamespace
        script = two_state_script()
        first = get_persona_state_targets(script)["MIRA"][0]
        config = {"MIRA": {"type": "custom", "versions": {first["version_id"]: {
            "type": "design", "description": "child voice", "persona_state": first}}}}
        with patch.object(personas, "_run_advanced_speaker_generation", side_effect=fake_state_run()):
            failures, result = personas.run_advanced_persona_generation(
                script, ["MIRA"], {}, config, None, "m", None, "/unused",
                SimpleNamespace(batch_size=40, new_only=True))
        self.assertEqual([], failures)
        self.assertEqual("child voice", result["MIRA"]["description"])
        self.assertEqual("custom", config["MIRA"]["type"])            # T22: the caller's dict is untouched


class T10PublisherTests(unittest.TestCase):
    def test_a_state_publication_writes_only_its_character_and_no_aliases(self):
        import generate_personas as personas
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice_config.json"
            initial = {"MIRA": {"type": "custom"}, "OTTO": {"type": "custom"}}
            path.write_text(json.dumps(initial))
            generated = copy.deepcopy(initial)
            generated["MIRA"]["versions"] = {"state_x": {"description": "done"}}
            generated["OTTO"]["alias_of"] = "MIRA"                      # a Step-2 alias decision, in memory
            generated["OTTO"]["description"] = "not finished"           # another character's unpublished work
            publisher = personas.StatePublisher(str(path), initial, None)
            publisher.publish(generated, "MIRA", "state_x")
            saved = json.loads(path.read_text())
            self.assertEqual({"state_x": {"description": "done"}}, saved["MIRA"]["versions"])
            self.assertNotIn("alias_of", saved["OTTO"])                 # committed only by the final save
            self.assertNotIn("description", saved["OTTO"])
            self.assertEqual({"type": "custom"}, publisher.base["OTTO"])


class T11CommaSpeakerTests(unittest.TestCase):
    def test_an_exact_speaker_name_with_a_comma_is_one_speaker(self):
        import generate_personas as personas
        from types import SimpleNamespace
        self.assertEqual({"Smith, Jr."}, personas.get_requested_speakers(SimpleNamespace(speaker="Smith, Jr.", speakers="")))
        self.assertEqual({"A", "B"}, personas.get_requested_speakers(SimpleNamespace(speaker="", speakers="A, B,")))

    def test_routes_pass_the_exact_speaker(self):
        source = Path(voices_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"--speakers", request.speaker', source)


class T13EvidenceQuoteTests(unittest.TestCase):
    def test_copied_quotes_with_normal_model_edits_still_count(self):
        import generate_personas as personas
        line = "\u201cI won\u2019t go back there,\u201d she said, \u201cnot   ever.\u201d"
        for quote in ('"I won\'t go back there,"', "I won't go\u2026not ever", "won't go back ... not ever",
                      "she said,  \u201cnot ever"):
            self.assertTrue(personas.is_quote_in_text(quote, line), quote)
        for quote in ("I will go back", "not ever ... I won't go", "", "..."):
            self.assertFalse(personas.is_quote_in_text(quote, line), quote)

    def test_a_shortened_quote_keeps_the_observation_and_a_borrowed_one_is_still_refused(self):
        import generate_personas as personas
        batch = [{"speaker": "MIRA", "text": "I won't go back there, not ever.", "_source_entry_index": 7}]
        kept = personas.get_validated_state_discovery(
            [{"name": "MIRA", "voice_clues": ["steady"], "evidence": [{"entry_index": 7, "quote": "I won't go...not ever"}]}],
            batch, 0, ["MIRA"])
        self.assertEqual(["steady"], kept[0]["voice_clues"])
        borrowed = personas.get_validated_state_discovery(
            [{"name": "MIRA", "voice_clues": ["x"], "evidence": [{"entry_index": 99, "quote": "I won't go"}]}],
            batch, 0, ["MIRA"])
        self.assertNotEqual(["x"], borrowed[0].get("voice_clues"))


class T18NarrationWindowTests(unittest.TestCase):
    def test_state_evidence_keeps_only_narration_near_the_characters_lines(self):
        from speaker_traits import get_persona_state_entries
        script = two_state_script()
        script[14:14] = [{"speaker": "NARRATOR", "text": f"Far scenery {i}."} for i in range(20)]
        target = get_persona_state_targets(script)["MIRA"][1]
        everything = get_persona_state_entries(script, target)
        windowed = get_persona_state_entries(script, target, narration_window=4)
        mira = [r for r in everything if r["speaker"] == "MIRA"]
        self.assertEqual(mira, [r for r in windowed if r["speaker"] == "MIRA"])
        far = [r for r in windowed if r["text"].startswith("Far scenery")]
        self.assertLessEqual(len(far), 4)
        self.assertGreater(len([r for r in everything if r["text"].startswith("Far scenery")]), 15)


class T23StateLookupTests(unittest.TestCase):
    def test_one_lookup_for_routes_and_cli(self):
        from speaker_traits import find_state_target
        targets = get_persona_state_targets(two_state_script())
        version_id = targets["MIRA"][1]["version_id"]
        self.assertEqual(targets["MIRA"][1], find_state_target(targets, "MIRA", version_id))
        self.assertIsNone(find_state_target(targets, "Mira", version_id))
        self.assertIsNone(find_state_target(targets, "MIRA", "state_" + "0" * 24))


class T12PromptBraceTests(unittest.TestCase):
    def test_braces_in_script_traits_survive_the_prompt_format(self):
        import generate_personas as personas
        from types import SimpleNamespace
        script = two_state_script()
        for row in script:
            if row.get("speaker_gender") == "female":
                row["speaker_gender"] = "f{x}"            # hand-edited / imported script value
        prompts = []
        def run(entries, selected, samples, config, *args, **options):
            prompts.append(options["advanced_prompt"])
            config[selected[0]] = {"type": "design", "description": "d"}
            return []
        with patch.object(personas, "_run_advanced_speaker_generation", side_effect=run):
            personas.run_advanced_persona_generation(script, ["MIRA"], {}, {"MIRA": {}}, None, "m", None,
                                                     "/unused", SimpleNamespace(batch_size=40))
        self.assertEqual(2, len(prompts))
        for prompt in prompts:
            rendered = personas._compile_character_prompt("ref", prompt, reference_text="ref")
            self.assertIn("f{x}", rendered)


class T14RecoveredStateIdentityTests(unittest.TestCase):
    def test_recovery_and_generation_share_the_state_fields(self):
        from speaker_traits import get_state_version_identity
        target = get_persona_state_targets(two_state_script())["MIRA"][1]
        fields = get_state_version_identity(target)
        self.assertEqual(("female", "adult", target), (fields["gender"], fields["age_group"], fields["persona_state"]))
        self.assertRegex(fields["seed"], r"^\d+$")
        self.assertNotEqual(fields["seed"], get_state_version_identity(get_persona_state_targets(two_state_script())["MIRA"][0])["seed"])


class T25CurrentFlagTests(unittest.TestCase):
    def test_voice_rows_flag_each_state_by_the_server_rule(self):
        script = two_state_script()
        first, second = get_persona_state_targets(script)["MIRA"]
        config = {"MIRA": {"type": "custom", "versions": {
            first["version_id"]: {"type": "design", "description": "d", "persona_state": first},
            second["version_id"]: {"type": "design", "description": "d",
                                   "persona_state": {**second, "segment_sha256": "0" * 64}}}}}
        row = next(r for r in voices_module.get_voice_rows(script, config) if r["name"] == "MIRA")
        self.assertEqual([True, False], [state["current"] for state in row["persona_states"]])
        self.assertTrue(row["persona_states_pending"])


class VoicesListImportanceOrderTests(unittest.TestCase):
    """Owner request 2026-10-09: more lines = more priority, in the list as in casting."""
    def test_the_voices_list_is_narrator_then_most_lines(self):
        script = ([{"speaker": "Zed", "text": f"z{i}"} for i in range(9)]
                  + [{"speaker": "Amy", "text": f"a{i}"} for i in range(2)]
                  + [{"speaker": "NARRATOR", "text": "n"}]
                  + [{"speaker": "Bob", "text": f"b{i}"} for i in range(5)]
                  + [{"speaker": "Cat", "text": ""} for _ in range(7)])            # empty rows are not lines
        rows = voices_module.get_voice_rows(script, {})
        self.assertEqual(["NARRATOR", "Zed", "Bob", "Amy", "Cat"], [row["name"] for row in rows])
        self.assertEqual([1, 9, 5, 2, 0], [row["line_count"] for row in rows])

    def test_casting_uses_the_same_order(self):
        from core import get_cast_importance_order
        source = Path(voices_module.__file__).read_text(encoding="utf-8")
        self.assertEqual(2, source.count("get_cast_importance_order("))     # Voices list + suggestion allocation
        self.assertEqual(["Narrator", "B", "A", "C"],
                         get_cast_importance_order(["A", "Narrator", "C", "B"], {"A": 3, "B": 9, "C": 3}))


class StateCardJsTests(unittest.TestCase):
    """T7, T15, T16, T26 through the real app-core.js (node)."""
    def test_state_card_fixes(self):
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        tests = Path(__file__).parent
        result = subprocess.run(["node", str(tests / "state_review_fixes_fixture.js"),
                                 str(tests.parent / "static/js/app-core.js")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("state review fixes verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
