"""Voice Design text length range (#1232) and the per-card line picker (#1232, #1251).

Every fixture sentence is synthetic.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

import core
import generate_personas as personas
from config_settings import TTSConfig
from persona_validation import (get_bounded_sample, get_design_text_candidates,
                                get_design_text_range, get_selectable_design_texts)
from routers import voices

LONG_LINE = " ".join(f"Sentence number {index} keeps the lantern lit." for index in range(40))


class DesignTextCandidateTests(unittest.TestCase):
    def test_short_interjections_are_excluded_inside_the_range(self):
        lines = ["Haa", "Alright", "Okay", "I will not leave this house before the rain stops."]
        self.assertEqual(get_design_text_candidates(lines, 12, 500),
                         ["I will not leave this house before the rain stops."])

    def test_in_range_lines_keep_source_order(self):
        lines = ["Bring the second lantern, please.", "Haa", "We leave at dawn, all of us."]
        self.assertEqual(get_design_text_candidates(lines, 12, 500),
                         ["Bring the second lantern, please.", "We leave at dawn, all of us."])

    def test_a_long_line_is_cut_at_the_last_sentence_end_that_fits(self):
        self.assertGreater(len(LONG_LINE), 900)
        [text] = get_design_text_candidates([LONG_LINE], 12, 500)
        self.assertLessEqual(len(text), 500)
        self.assertTrue(LONG_LINE.startswith(text))
        self.assertTrue(text.endswith("lit."))
        # The last boundary, not the first: far more than one sentence survives.
        self.assertGreater(len(text), 400)

    def test_nothing_in_range_returns_the_nearest_shorter_line_never_a_longer_one(self):
        lines = ["Haa", "Alright", "Okay"]
        self.assertEqual(get_design_text_candidates(lines, 12, 500), ["Alright"])
        # A line above the range is cut into it, so it is never the fallback for
        # being too long; a cut that lands below the minimum is a shorter line.
        lines = ["Hm.", "No. " + "x" * 600]
        self.assertEqual(get_design_text_candidates(lines, 12, 100), ["Hm."])

    def test_cjk_counts_characters_and_cuts_at_a_cjk_sentence_end(self):
        line = "我们明天早上出发。" * 80
        [text] = get_design_text_candidates([line], 5, 50)
        self.assertLessEqual(len(text), 50)
        self.assertTrue(text.endswith("。"))
        self.assertEqual(get_design_text_candidates(["好。", "我们明天早上出发。"], 5, 50), ["我们明天早上出发。"])

    def test_a_decimal_point_is_not_a_sentence_end(self):
        line = "The value is 3.14159 " + "and it keeps going " * 10
        self.assertFalse(get_bounded_sample(line, 60, latest_boundary=True).endswith("3."))

    def test_selectable_texts_list_every_line_once_including_short_ones(self):
        self.assertEqual(get_selectable_design_texts(["Okay", "Okay", "", "Fine then."]), ["Okay", "Fine then."])

    def test_range_from_config(self):
        self.assertEqual(get_design_text_range({}), (12, 500))
        self.assertEqual(get_design_text_range({"tts": {"design_text_min_chars": 40, "design_text_max_chars": 300}}), (40, 300))
        self.assertEqual(get_design_text_range({"tts": {"design_text_min_chars": 400, "design_text_max_chars": 30}}), (30, 30))
        self.assertEqual(get_design_text_range({"tts": {"design_text_min_chars": "x", "design_text_max_chars": 9000}}), (12, 500))


class DesignTextConfigTests(unittest.TestCase):
    def test_defaults_and_500_cap(self):
        tts = TTSConfig()
        self.assertEqual((tts.design_text_min_chars, tts.design_text_max_chars), (12, 500))
        for field, value in (("design_text_max_chars", 501), ("design_text_min_chars", 0)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                TTSConfig(**{field: value})

    def test_save_refuses_min_above_max(self):
        import asyncio
        from fastapi import HTTPException
        from config_settings import AppConfig
        from routers import system
        llm = {"base_url": "http://127.0.0.1:1/v1", "api_key": "local", "model_name": "fixture"}
        config = AppConfig.model_validate({"llm": llm, "llm_local": llm,
                                           "tts": {"design_text_min_chars": 300, "design_text_max_chars": 100}})
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(system, "CONFIG_PATH", str(Path(tmp, "config.json"))), \
             patch.object(system, "atomic_json_write") as write:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(system.save_config(config))
        self.assertEqual(raised.exception.status_code, 400)
        write.assert_not_called()

    def test_setup_round_trips_both_fields(self):
        source = Path(__file__).parent.parent.joinpath("static", "js", "app-core.js").read_text(encoding="utf-8")
        page = Path(__file__).parent.parent.joinpath("static", "index.html").read_text(encoding="utf-8")
        for field, element in (("design_text_min_chars", "design-text-min-chars"),
                               ("design_text_max_chars", "design-text-max-chars")):
            self.assertIn(f'id="{element}"', page)
            self.assertIn(f"config.tts.{field}", source)
        self.assertIn("...getDesignTextRangeInput()", source)


class PickRefTextTests(unittest.TestCase):
    def test_fallback_uses_the_range(self):
        lines = ["Okay", "The bridge is out, so we take the ford instead."]
        self.assertEqual(personas.pick_ref_text(lines), lines[1])
        self.assertEqual(personas.pick_ref_text(lines, (1, 10)), "Okay")


class AdvancedCompileTests(unittest.TestCase):
    def compile(self, sample_lines, copied, design_text_range):
        seen = {}
        def reply(*args, **kwargs):
            payload = json.JSONDecoder().raw_decode(args[3].split("Character reference:\n")[-1])[0]
            seen["reference_sample"] = payload["reference_sample"]
            return kwargs["validate_object"]({"description": "Low, steady voice.", "ref_text": copied})
        preview = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            refs = Path(tmp) / "refs"; refs.mkdir()
            Path(personas._character_ref_path(str(refs), "ALICE")).write_text(json.dumps(
                {"name": "ALICE", "voice_clues": ["low"], "sample_lines": sample_lines}))
            with patch.object(personas, "call_llm_for_object", side_effect=reply):
                self.assertTrue(personas._compile_persona(object(), "fixture", object(), {}, tmp, str(refs),
                    "ALICE", {}, None, None, preview_saver=preview, design_text_range=design_text_range))
        return seen["reference_sample"], preview.call_args.args[5]

    def test_a_copied_line_outside_the_range_is_replaced_by_an_in_range_one(self):
        lines = ["Haa", "Keep the gate shut until I return."]
        offered, designed = self.compile(lines, "Haa", (12, 500))
        self.assertEqual(offered, lines[1])
        self.assertEqual(designed, lines[1])

    def test_a_copied_line_inside_the_range_is_kept(self):
        lines = ["Keep the gate shut until I return.", "We should have left the cart behind us."]
        _, designed = self.compile(lines, lines[1], (12, 500))
        self.assertEqual(designed, lines[1])

    def test_a_fabricated_line_is_still_refused(self):
        with self.assertRaises(personas.PersonaContextRecoveryError):
            self.compile(["Keep the gate shut until I return."], "A sentence nobody said.", (12, 500))


def run_main(tmp, arguments, *, reply=None, engine=None, config=None):
    """Run generate_personas.main() in-process against a temp book."""
    heal = Mock(return_value=(False, {"context_length": 4096}, "fixture"))
    with patch.object(sys, "argv", ["generate_personas.py", *arguments]), \
         patch.object(personas, "get_runtime_data_dir", return_value=tmp), \
         patch.object(personas, "load_app_config", return_value=config or {}), \
         patch.object(personas, "get_active_llm_config", return_value={"model_name": "fixture", "base_url": "http://unused.invalid"}), \
         patch.object(personas, "ensure_ideal_settings", heal), \
         patch.object(personas, "make_run_client", return_value=object()), \
         patch.object(personas, "llm_timeout_seconds", return_value=30), \
         patch.object(personas, "TTSEngine", return_value=engine or Mock()), \
         patch.object(personas, "request_persona_with_evidence", side_effect=reply or AssertionError("LLM was called")), \
         patch.object(personas.time, "sleep"), \
         contextlib.redirect_stdout(io.StringIO()):
        personas.main()
    return heal


class FakeEngine:
    def __init__(self, folder):
        self.folder, self.texts = folder, []

    def generate_voice_design(self, description, sample_text):
        self.texts.append(sample_text)
        path = Path(self.folder, f"design_{len(self.texts)}.wav")
        path.write_bytes(b"RIFF")
        return str(path), 24000


class SimplePathTests(unittest.TestCase):
    def test_a_paraphrased_or_out_of_range_ref_text_is_replaced_by_a_real_line(self):
        script = [{"speaker": "ALICE", "text": "Okay"},
                  {"speaker": "ALICE", "text": "Bring the second lantern before the wind rises."}]
        for given in ("Okay", "Bring a lantern, the wind rises."):
            with self.subTest(given=given), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "annotated_script.json").write_text(json.dumps(script))
                Path(tmp, "voice_config.json").write_text("{}")
                engine = FakeEngine(tmp)
                run_main(tmp, ["--speaker", "ALICE"], engine=engine,
                         reply=lambda *a, **k: {"description": "Bright voice.", "ref_text": given})
                self.assertEqual(engine.texts, ["Bring the second lantern before the wind rises."])
                saved = json.loads(Path(tmp, "voice_config.json").read_text())["ALICE"]
                self.assertEqual(saved["ref_text"], "Bring the second lantern before the wind rises.")


class ChosenLineDesignTests(unittest.TestCase):
    SCRIPT = [{"speaker": "ALICE", "text": "Okay"},
              {"speaker": "ALICE", "text": "Bring the second lantern before the wind rises."},
              {"speaker": "BOB", "text": "Not one of Alice's lines at all."}]

    def book(self, tmp, voices):
        Path(tmp, "annotated_script.json").write_text(json.dumps(self.SCRIPT))
        Path(tmp, "voice_config.json").write_text(json.dumps(voices))

    def test_designs_the_chosen_line_without_the_llm_and_keeps_the_old_voice(self):
        old = {"type": "clone", "ref_audio": "designed_voices/persona/old/preview.wav", "ref_text": "Okay",
               "description": "Bright, quick voice.", "character_style": "Bright, quick voice.", "seed": 7,
               "persona_status": "approved", "ready": True}
        with tempfile.TemporaryDirectory() as tmp:
            self.book(tmp, {"ALICE": old})
            engine = FakeEngine(tmp)
            heal = run_main(tmp, ["--speaker", "ALICE", "--ref-text=Okay"], engine=engine)
            heal.assert_not_called()
            self.assertEqual(engine.texts, ["Okay"])
            saved = json.loads(Path(tmp, "voice_config.json").read_text())["ALICE"]
        self.assertEqual(saved["description"], "Bright, quick voice.")
        self.assertEqual(saved["ref_text"], "Okay")
        self.assertNotEqual(saved["ref_audio"], old["ref_audio"])
        # The replaced voice and the new one are both saved; the new one is active (#1297).
        candidate, designed = saved["candidates"]
        self.assertTrue(candidate["candidate_id"].startswith("design-"))
        self.assertEqual(candidate["ref_audio"], old["ref_audio"])
        self.assertEqual(designed["ref_audio"], saved["ref_audio"])
        self.assertEqual(saved["active_candidate"], designed["candidate_id"])
        # Character-level fields are not part of a voice candidate.
        self.assertNotIn("ready", candidate)
        self.assertNotIn("persona_status", candidate)

    def test_refuses_a_line_that_is_not_the_speakers_or_a_missing_description(self):
        cases = (({"ALICE": {"description": "Bright voice."}}, "--ref-text=Not one of Alice's lines at all."),
                 ({"ALICE": {"type": "clone"}}, "--ref-text=Okay"))
        for voices_before, argument in cases:
            with self.subTest(argument=argument), tempfile.TemporaryDirectory() as tmp:
                self.book(tmp, voices_before)
                engine = FakeEngine(tmp)
                with self.assertRaises(ValueError):
                    run_main(tmp, ["--speaker", "ALICE", argument], engine=engine)
                self.assertEqual(engine.texts, [])
                self.assertEqual(json.loads(Path(tmp, "voice_config.json").read_text()), voices_before)

    def test_a_line_starting_with_a_dash_is_one_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = [{"speaker": "ALICE", "text": "--and then the bells stopped."}]
            Path(tmp, "annotated_script.json").write_text(json.dumps(script))
            Path(tmp, "voice_config.json").write_text(json.dumps({"ALICE": {"description": "Calm voice."}}))
            engine = FakeEngine(tmp)
            run_main(tmp, ["--speaker", "ALICE", "--ref-text=--and then the bells stopped."], engine=engine)
        self.assertEqual(engine.texts, ["--and then the bells stopped."])


class DesignLineRouteTests(unittest.TestCase):
    def client(self, tmp, voices_config, config=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        Path(tmp, "script.json").write_text(json.dumps(ChosenLineDesignTests.SCRIPT))
        Path(tmp, "voice_config.json").write_text(json.dumps(voices_config))
        Path(tmp, "config.json").write_text(json.dumps(config or {}))
        app = FastAPI()
        app.include_router(voices.router)
        stack = contextlib.ExitStack()
        for name, value in (("SCRIPT_PATH", "script.json"), ("VOICE_CONFIG_PATH", "voice_config.json"),
                            ("CONFIG_PATH", "config.json")):
            stack.enter_context(patch.object(voices, name, str(Path(tmp, value))))
        return stack, TestClient(app)

    def test_lists_only_the_speakers_lines_in_range_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack, client = self.client(tmp, {})
            with stack:
                body = client.get("/api/voices/ALICE/design_lines").json()
                missing = client.get("/api/voices/NOBODY/design_lines")
        self.assertEqual([line["text"] for line in body["lines"]],
                         ["Bring the second lantern before the wind rises.", "Okay"])
        self.assertEqual([line["in_range"] for line in body["lines"]], [True, False])
        self.assertEqual((body["min_chars"], body["max_chars"]), (12, 500))
        self.assertEqual(missing.status_code, 404)

    def test_generate_refuses_bad_chosen_lines_before_claiming_the_gpu(self):
        cases = (({"ALICE": {"description": "Bright."}}, {"speaker": "ALICE", "ref_text": "Not one of Alice's lines at all."}, 422),
                 ({"ALICE": {"description": "Bright."}}, {"speaker": "ALICE", "ref_text": "Okay", "age_group": "teen"}, 422),
                 ({"ALICE": {"type": "clone"}}, {"speaker": "ALICE", "ref_text": "Okay"}, 409))
        for voices_config, payload, status in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                stack, client = self.client(tmp, voices_config)
                with stack, patch.object(voices, "check_global_gpu_lock") as check, \
                     patch.object(voices, "run_process") as run:
                    response = client.post("/api/generate_personas", json=payload)
                self.assertEqual(response.status_code, status, response.text)
                check.assert_not_called()
                run.assert_not_called()

    def test_generate_passes_the_chosen_line_as_one_argument(self):
        from types import SimpleNamespace
        state = {"persona": {"running": False, "cancel": True, "logs": []}}
        with tempfile.TemporaryDirectory() as tmp:
            stack, client = self.client(tmp, {"ALICE": {"description": "Bright."}})
            with stack, patch.object(voices, "project_manager", SimpleNamespace(engine=None)), \
                 patch.object(voices, "process_state", state), \
                 patch.object(voices, "check_global_gpu_lock") as check, \
                 patch.object(core, "process_state", state), \
                 patch.object(core, "_task_claims", {}), \
                 patch.object(core, "_gpu_leases", {}), \
                 patch.object(core, "acquire_gpu_lock", return_value=None), \
                 patch.object(core, "llm_is_on_this_gpu", return_value=True), \
                 patch.object(voices, "run_process") as run:
                response = client.post("/api/generate_personas", json={"speaker": "ALICE", "ref_text": "Okay"})
        self.assertEqual(response.status_code, 200, response.text)
        check.assert_called_once_with("persona")
        command = run.call_args.args[0]
        self.assertIn("--ref-text=Okay", command)
        self.assertNotIn("--advanced", command)


def state_book(root):
    """A synthetic two-state character whose voice and both state versions are set."""
    from speaker_traits import get_persona_state_targets
    script = []
    for age in ("teen", "adult"):
        script.append({"speaker": "NARRATOR", "text": f"The {age} scene begins."})
        script.extend({"speaker": "ARTHUR", "text": f"This is {age} line number {number}.",
                       "speaker_gender": "male", "speaker_age_group": age} for number in range(10))
    targets = get_persona_state_targets(script)["ARTHUR"]
    voices_config = {"ARTHUR": {"type": "clone", "ref_audio": "base.wav", "ref_text": "base", "description": "Base voice.",
                                "versions": {target["version_id"]: {
                                    "type": "clone", "ref_audio": f"state{target['state_number']}.wav",
                                    "ref_text": "old", "description": f"{target['age_group']} voice.",
                                    "age_group": target["age_group"], "persona_state": target}
                                    for target in targets}}}
    Path(root, "annotated_script.json").write_text(json.dumps(script))
    Path(root, "voice_config.json").write_text(json.dumps(voices_config))
    Path(root, "chunks.json").write_text(json.dumps([{"speaker": r["speaker"], "text": r["text"]} for r in script]))
    Path(root, "state.json").write_text('{"active_book_id":"book","book_generation":"one"}')
    Path(root, "config.json").write_text("{}")
    return targets, voices_config


class StateCardDesignTests(unittest.TestCase):
    """#1251: the picker and Design button on state cards act on that state only."""

    def test_cli_designs_one_state_and_leaves_the_base_and_its_sibling_alone(self):
        from book_state_transaction import get_book_snapshot, get_book_snapshot_token
        with tempfile.TemporaryDirectory() as tmp:
            targets, before = state_book(tmp)
            teen, adult = (target["version_id"] for target in targets)
            token = get_book_snapshot_token(get_book_snapshot(tmp))
            engine = FakeEngine(tmp)
            run_main(tmp, ["--speaker", "ARTHUR", "--state-version", adult, "--book-token", token,
                           "--ref-text=This is adult line number 3."], engine=engine)
            saved = json.loads(Path(tmp, "voice_config.json").read_text())["ARTHUR"]
            refused = FakeEngine(tmp)
            with self.assertRaises(ValueError):
                run_main(tmp, ["--speaker", "ARTHUR", "--state-version", adult, "--book-token", token,
                               "--ref-text=This is teen line number 3."], engine=refused)
        self.assertEqual(engine.texts, ["This is adult line number 3."])
        self.assertEqual(refused.texts, [])
        state = saved["versions"][adult]
        self.assertEqual((state["ref_text"], state["description"]), ("This is adult line number 3.", "adult voice."))
        self.assertEqual(state["persona_state"], before["ARTHUR"]["versions"][adult]["persona_state"])
        candidate, designed = state["candidates"]
        self.assertEqual(candidate["ref_audio"], "state2.wav")
        self.assertEqual(designed["ref_audio"], state["ref_audio"])
        self.assertEqual(state["active_candidate"], designed["candidate_id"])
        self.assertNotIn("persona_state", candidate)
        self.assertEqual(saved["versions"][teen], before["ARTHUR"]["versions"][teen])
        self.assertEqual({k: v for k, v in saved.items() if k != "versions"},
                         {k: v for k, v in before["ARTHUR"].items() if k != "versions"})

    def test_routes_list_and_queue_only_that_states_lines(self):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        state = {"persona": {"running": False, "cancel": True, "logs": []}}
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            targets, _ = state_book(tmp)
            adult = targets[1]["version_id"]
            for name, value in (("SCRIPT_PATH", "annotated_script.json"), ("VOICE_CONFIG_PATH", "voice_config.json"),
                                ("CHUNKS_PATH", "chunks.json"), ("CONFIG_PATH", "config.json")):
                stack.enter_context(patch.object(voices, name, str(Path(tmp, value))))
            app = FastAPI(); app.include_router(voices.router)
            client = stack.enter_context(TestClient(app))
            token = client.get("/api/voice_config/snapshot").json()["book_token"]
            listed = client.get("/api/voices/ARTHUR/design_lines", params={"version_id": adult, "book_token": token}).json()
            stale = client.get("/api/voices/ARTHUR/design_lines", params={"version_id": adult, "book_token": "old"})
            scope = {"speaker": "ARTHUR", "state_version": adult, "book_token": token}
            stack.enter_context(patch.object(voices, "project_manager", SimpleNamespace(engine=None)))
            stack.enter_context(patch.object(voices, "process_state", state))
            stack.enter_context(patch.object(core, "process_state", state))
            stack.enter_context(patch.object(core, "_task_claims", {}))
            stack.enter_context(patch.object(core, "_gpu_leases", {}))
            stack.enter_context(patch.object(core, "acquire_gpu_lock", return_value=None))
            stack.enter_context(patch.object(core, "llm_is_on_this_gpu", return_value=True))
            check = stack.enter_context(patch.object(voices, "check_global_gpu_lock"))
            run = stack.enter_context(patch.object(voices, "run_process"))
            other_state = client.post("/api/generate_personas", json={**scope, "ref_text": "This is teen line number 3."})
            check.assert_not_called()
            queued = client.post("/api/generate_personas", json={**scope, "ref_text": "This is adult line number 3."})
        self.assertEqual([line["text"] for line in listed["lines"]],
                         [f"This is adult line number {number}." for number in range(10)])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(other_state.status_code, 422, other_state.text)
        self.assertEqual(queued.status_code, 200, queued.text)
        self.assertFalse(queued.json()["advanced"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--state-version") + 1], adult)
        self.assertEqual(command[command.index("--book-token") + 1], token)
        self.assertIn("--ref-text=This is adult line number 3.", command)
        self.assertNotIn("--advanced", command)


if __name__ == "__main__":
    unittest.main()
