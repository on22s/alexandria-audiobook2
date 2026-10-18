"""Pass 3 delivery direction (#1258): reply-aware notes and performed gaps.

All lines here are synthetic. "off" must stay the shipped instruct contract;
the other modes are opt-in and change only what Pass 3 asks for and returns.
"""
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import project
import three_pass_generate as tp
import tts
from default_prompts import load_instruct_prompts
from generate_script import LLMGenParams
from pass_quality import validate_instruct
from tests import test_three_pass_generate as tpg


def make_params(direction="off", **kwargs):
    return LLMGenParams(max_tokens=500, temperature=0.1, delivery_direction=direction, **kwargs)


def make_recording_client(payloads, seen):
    """Like test_three_pass_generate._client_returning, also recording each request."""
    responses = iter(payloads)

    def create(**kwargs):
        seen.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(next(responses))),
            finish_reason="stop")], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


PRIOR = [{"speaker": "ANNA", "text": "Are you leaving already?"},
         {"speaker": "BEN", "text": "I have to—"},
         {"speaker": "ANNA", "text": "Then go."}]
DIRECTED_ROWS = [
    {"n": 0, "answers": None, "action": "pleading", "delivery": "soft, a little fast", "gap": "normal"},
    {"n": 1, "answers": 0, "action": "dodging", "delivery": "low, hurried.", "gap": "beat"},
    {"n": 2, "answers": 1, "action": "dismissing", "delivery": "flat, clipped", "gap": "cut_off"},
]


class PromptSelectionTests(unittest.TestCase):
    def test_off_sends_the_shipped_prompt_and_custom_overrides(self):
        system, _ = tp.build_instruct_request(PRIOR, make_params())
        self.assertEqual(load_instruct_prompts()[0], system)
        custom = make_params(instruct_system_prompt="custom system")
        self.assertEqual("custom system", tp.build_instruct_request(PRIOR, custom)[0])

    def test_directed_modes_send_their_own_prompt_even_with_legacy_fallbacks(self):
        for direction, field in (("gaps", '"gap"'), ("directed", '"action"'),
                                 ("action_only", '"action"')):
            params = make_params(direction, system_prompt="legacy", user_prompt_template="{batch}")
            system, user = tp.build_instruct_request(PRIOR, params)
            self.assertEqual(load_instruct_prompts(direction)[0], system)
            self.assertIn(field, system)
            self.assertIn("Are you leaving already?", user)

    def test_off_prompt_never_asks_for_direction_fields(self):
        system, user = load_instruct_prompts()
        for field in ('"gap"', '"action"', '"answers"'):
            self.assertNotIn(field, system + user)


class ResolverTests(unittest.TestCase):
    def test_cli_beats_config_and_default_is_off(self):
        config = {"generation": {"three_pass_delivery_direction": "gaps"}}
        self.assertEqual("off", tp.resolve_delivery_direction({}))
        self.assertEqual("gaps", tp.resolve_delivery_direction(config))
        self.assertEqual("directed", tp.resolve_delivery_direction(config, "directed"))

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            tp.resolve_delivery_direction({"generation": {"three_pass_delivery_direction": "loud"}})

    def test_mode_with_a_custom_pass3_preset_is_refused(self):
        config = {"generation": {"three_pass_delivery_direction": "directed"},
                  "prompts": {"pass3_preset": "mine", "pass3_prompt_presets": [
                      {"name": "mine", "system_prompt": "s", "user_prompt": "{batch}"}]}}
        with self.assertRaises(ValueError):
            tp.resolve_delivery_direction(config)
        config["generation"]["three_pass_delivery_direction"] = "off"
        self.assertEqual("off", tp.resolve_delivery_direction(config))

    def test_saved_config_accepts_exactly_the_modes(self):
        from typing import get_args
        import config_settings
        from pass_quality import DELIVERY_DIRECTIONS
        self.assertEqual(DELIVERY_DIRECTIONS, get_args(config_settings.DeliveryDirection))
        self.assertEqual("off", config_settings.GenerationConfig().three_pass_delivery_direction)

    def test_run_params_carry_the_resolved_mode(self):
        config = {"generation": {"three_pass_delivery_direction": "action_only"}}
        settings = tp.resolve_three_pass_generation_settings(config)
        params = tp.get_three_pass_run_params(config, {}, generation_settings=settings)
        self.assertEqual("action_only", params.delivery_direction)


class ValidationTests(unittest.TestCase):
    def rows(self, changes=None):
        rows = [dict(row) for row in DIRECTED_ROWS]
        for (index, key), value in (changes or {}).items():
            rows[index][key] = value
        return rows

    def codes(self, rows, direction="directed"):
        return [f["code"] for f in validate_instruct(PRIOR, rows, direction)["findings"]]

    def test_complete_directed_rows_pass(self):
        self.assertEqual([], self.codes(self.rows()))

    def test_each_bad_field_is_named(self):
        self.assertEqual(["invalid_gap"], self.codes(self.rows({(0, "gap"): "pause"})))
        self.assertEqual(["missing_action"], self.codes(self.rows({(1, "action"): " "})))
        self.assertEqual(["missing_delivery"], self.codes(self.rows({(2, "delivery"): ""})))
        self.assertEqual(["invalid_answers"], self.codes(self.rows({(1, "answers"): 1})))
        self.assertEqual(["invalid_answers"], self.codes(self.rows({(1, "answers"): 3})))
        self.assertEqual(["invalid_answers"], self.codes(self.rows({(1, "answers"): True})))

    def test_action_only_does_not_need_delivery(self):
        self.assertEqual([], self.codes(self.rows({(2, "delivery"): ""}), "action_only"))

    def test_gaps_mode_needs_instruct_and_gap(self):
        rows = [{"n": i, "instruct": "Quiet, tense.", "gap": "normal"} for i in range(3)]
        self.assertEqual([], self.codes(rows, "gaps"))
        rows[1].pop("gap")
        rows[2]["instruct"] = ""
        self.assertEqual(["invalid_gap", "missing_instruct"], self.codes(rows, "gaps"))

    def test_off_ignores_direction_fields(self):
        rows = [{"n": i, "instruct": "Quiet.", "gap": "nonsense"} for i in range(3)]
        self.assertEqual([], self.codes(rows, "off"))


class InstructBatchTests(unittest.TestCase):
    def test_directed_rows_compose_the_instruct_and_keep_the_fields(self):
        out = tp.instruct_batch(tpg._client_returning([DIRECTED_ROWS]), "m", PRIOR,
                                make_params("directed"))
        self.assertEqual(["Pleading: soft, a little fast.", "Dodging: low, hurried.",
                          "Dismissing: flat, clipped."], [e["instruct"] for e in out])
        self.assertEqual({"answers": 0, "action": "dodging", "delivery": "low, hurried.",
                          "gap": "beat"}, out[1]["direction"])
        self.assertEqual([e["text"] for e in PRIOR], [e["text"] for e in out])

    def test_action_only_drops_the_delivery_note(self):
        out = tp.instruct_batch(tpg._client_returning([DIRECTED_ROWS]), "m", PRIOR,
                                make_params("action_only"))
        self.assertEqual(["Pleading.", "Dodging.", "Dismissing."], [e["instruct"] for e in out])

    def test_off_output_has_no_direction(self):
        rows = [{"n": i, "instruct": "Quiet.", "gap": "beat"} for i in range(3)]
        out = tp.instruct_batch(tpg._client_returning([rows]), "m", PRIOR, make_params())
        self.assertTrue(all("direction" not in e for e in out))
        self.assertEqual(["Quiet."] * 3, [e["instruct"] for e in out])

    def test_rejected_rows_retry_then_fall_back_visibly(self):
        bad = [dict(DIRECTED_ROWS[0], gap="pause")]
        seen = []
        out = tp.instruct_batch(make_recording_client([bad, bad], seen), "m", PRIOR[:1],
                                make_params("directed"), max_retries=1)
        self.assertEqual(2, len(seen))
        self.assertTrue(out[0]["instruct_unchecked"])
        self.assertEqual(tp.CHARACTER_DEFAULT_INSTRUCT, out[0]["instruct"])
        self.assertNotIn("direction", out[0])

    def test_subdivided_windows_keep_their_own_indexes(self):
        # Each half is sent with its own 0-based indexes; `answers` must be
        # checked against the half actually sent, not the original window.
        first = [dict(DIRECTED_ROWS[0])]
        second = [dict(DIRECTED_ROWS[1], n=0, answers=None), dict(DIRECTED_ROWS[2], n=1, answers=0)]
        out = (tp.instruct_batch(tpg._client_returning([first]), "m", PRIOR[:1], make_params("directed"))
               + tp.instruct_batch(tpg._client_returning([second]), "m", PRIOR[1:], make_params("directed")))
        self.assertEqual([None, None, 0], [e["direction"]["answers"] for e in out])
        out_of_half = [dict(DIRECTED_ROWS[1], n=0, answers=None), dict(DIRECTED_ROWS[2], n=1, answers=2)]
        self.assertEqual(["invalid_answers"],
                         [f["code"] for f in validate_instruct(PRIOR[1:], out_of_half, "directed")["findings"]])


class GapPauseTests(unittest.TestCase):
    def entries(self, *gaps):
        return [{"speaker": "A", "text": f"Line {i}.", "direction": {"gap": gap}}
                for i, gap in enumerate(gaps)]

    def test_a_gap_sets_the_pause_after_the_previous_line(self):
        out = tp.apply_gap_pauses(self.entries("normal", "cut_off", "beat", "trailing", "normal"))
        self.assertEqual([60, 900, 700, None, None], [e.get("pause_after") for e in out])

    def test_a_longer_existing_pause_survives(self):
        entries = self.entries("normal", "cut_off")
        entries[0]["pause_after"] = 2000
        self.assertEqual(2000, tp.apply_gap_pauses(entries)[0]["pause_after"])

    def test_input_is_not_mutated_and_reapplying_changes_nothing(self):
        entries = self.entries("normal", "beat")
        once = tp.apply_gap_pauses(entries)
        self.assertNotIn("pause_after", entries[0])
        self.assertEqual(once, tp.apply_gap_pauses(once))

    def test_entries_without_direction_and_gaps_are_counted(self):
        entries = self.entries("normal", "beat") + [{"speaker": "A", "text": "x"}, None]
        self.assertEqual(entries[2], tp.apply_gap_pauses(entries)[2])
        self.assertEqual({"normal": 1, "beat": 1, "none": 1}, tp.get_gap_counts(entries))


class RunThreePassTests(unittest.TestCase):
    SOURCE = '"Are you leaving already?" "I have to—" "Then go."'
    SEGMENTED = [{"type": "SPOKEN", "text": "Are you leaving already?"},
                 {"type": "SPOKEN", "text": "I have to—"},
                 {"type": "SPOKEN", "text": "Then go."}]
    NAMED = [{"n": 0, "speaker": "ANNA"}, {"n": 1, "speaker": "BEN"}, {"n": 2, "speaker": "ANNA"}]

    def run_book(self, direction, instruct_rows):
        client = tpg._client_returning([self.SEGMENTED, self.NAMED, instruct_rows])
        return tp.run_three_pass(client, "m", self.SOURCE, make_params(direction), chunk_size=6000,
                                 cast=tpg._load_orchestration_fixture_cast(("ANNA", "BEN")))

    def test_directed_book_carries_gap_pauses_to_tts(self):
        entries = self.run_book("directed", DIRECTED_ROWS)
        self.assertEqual([900, 60, None], [e.get("pause_after") for e in entries])
        chunks = project.group_into_chunks(entries)
        self.assertEqual(3, len(chunks))
        self.assertEqual(900, tts.get_pause_duration_ms(chunks[0], chunks[1]))
        self.assertEqual(60, tts.get_pause_duration_ms(chunks[1], chunks[2]))

    def test_off_book_has_no_gap_pauses(self):
        rows = [{"n": i, "instruct": "Quiet."} for i in range(3)]
        entries = self.run_book("off", rows)
        self.assertTrue(all("pause_after" not in e and "direction" not in e for e in entries))

    def test_fingerprint_separates_modes(self):
        prints = {direction: tp.three_pass_fingerprint(self.SOURCE, "m", 6000, make_params(direction))
                  ["settings_sha256"] for direction in ("off", "gaps", "directed", "action_only")}
        self.assertEqual(4, len(set(prints.values())))
        self.assertEqual(prints["off"], tp.three_pass_fingerprint(
            self.SOURCE, "m", 6000, replace(make_params(), delivery_direction="off"))["settings_sha256"])


class SplitEntryPauseTests(unittest.TestCase):
    def test_entry_pause_follows_only_its_last_part(self):
        parts, _ = project.split_on_unspeakable(
            {"speaker": "A", "text": "Wait <softly> here.", "instruct": "x", "pause_after": 900}, 2000)
        self.assertEqual(3, len(parts))
        self.assertEqual([None, None, 900], [p.get("pause_after") for p in parts])

    def test_scene_break_keeps_its_pause_and_the_entry_pause_moves_to_the_end(self):
        parts, _ = project.split_on_unspeakable(
            {"speaker": "A", "text": "First part. *** Second part.", "instruct": "x", "pause_after": 60}, 2000)
        self.assertEqual([2000, 60], [p.get("pause_after") for p in parts])


if __name__ == "__main__":
    unittest.main()
