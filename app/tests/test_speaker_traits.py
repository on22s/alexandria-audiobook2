"""Per-line speaker gender/age (#653): opt-in, normalised never retried, and a
per-character summary that follows a time skip but ignores a one-band drift."""
import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

import config_settings
import speaker_traits as st
import three_pass_generate as tp
from attribution_prompt_variants import _texts_for
from generate_script import LLMGenParams

STATIC = Path(__file__).resolve().parent.parent / "static"


def _line(gender, age, ageless=False):
    return {"speaker": "X", "speaker_gender": gender, "speaker_age_group": age,
            "speaker_ageless": ageless}


class ValuesTest(unittest.TestCase):
    def test_values_outside_the_lists_become_unknown(self):
        self.assertEqual({"speaker_gender": "unknown", "speaker_age_group": "unknown",
                          "speaker_ageless": False},
                         st.get_traits_from_answer({"gender": "BOTH", "age_group": "42"}))
        self.assertEqual({"speaker_gender": "female", "speaker_age_group": "young_child",
                          "speaker_ageless": True},
                         st.get_traits_from_answer({"gender": "FEMALE", "age_group": "Young-Child",
                                                    "ageless": "true"}))

    def test_age_is_the_voice_age_and_ageless_covers_appearance(self):
        self.assertIn("SOUNDS AND LOOKS", st.TRAITS_RULE)
        self.assertIn("child's voice even with an adult's mind", st.TRAITS_RULE)
        self.assertIn("long-lived race", st.TRAITS_RULE)

    def test_childhood_stages_follow_the_table(self):
        self.assertEqual(("infant", "toddler", "young_child", "child", "teen"),
                         st.AGE_GROUP_NAMES[:5])
        self.assertIn("TODDLER (1-3)", st.TRAITS_RULE)


class SummaryTest(unittest.TestCase):
    def test_a_time_skip_is_a_state_change(self):
        lines = [_line("male", "child")] * 12 + [_line("male", "adult")] * 14
        summary = st.get_speaker_trait_summary(lines)
        self.assertEqual("adult", summary["age_group"])
        self.assertEqual([{"gender": "male", "age_group": "child"},
                          {"gender": "male", "age_group": "adult"}], summary["states"])

    def test_a_one_band_drift_is_not(self):
        lines = [_line("female", "teen"), _line("female", "young_adult"), _line("female", "teen")]
        self.assertEqual([], st.get_speaker_trait_summary(lines)["states"])

    def test_a_gender_change_is_a_state_change(self):
        lines = [_line("male", "teen")] * 10 + [_line("female", "teen")] * 10
        self.assertEqual(2, len(st.get_speaker_trait_summary(lines)["states"]))

    def test_unknowns_do_not_break_or_start_states(self):
        lines = [_line("unknown", "unknown"), _line("male", "elderly"), _line("unknown", "unknown")]
        summary = st.get_speaker_trait_summary(lines)
        self.assertEqual(("male", "elderly", []), (summary["gender"], summary["age_group"],
                                                   summary["states"]))

    def test_ageless_is_a_flag_beside_the_age(self):
        summary = st.get_speaker_trait_summary([_line("female", "child", ageless=True)])
        self.assertEqual(("child", True), (summary["age_group"], summary["ageless"]))

    def test_stray_labels_never_become_a_state(self):
        """Paul's stray 'infant'/'teen' lines and Roxy's batch-by-batch swings
        (time_skip_traits.json): short runs of a different age must not count."""
        lines = ([_line("male", "adult")] * 10 + [_line("male", "infant")]
                 + [_line("male", "adult")] * 5 + [_line("male", "child")] * 7
                 + [_line("male", "adult")] * 10)
        summary = st.get_speaker_trait_summary(lines)
        self.assertEqual([], summary["states"])
        self.assertEqual({"gender": "male", "age_group": "adult"}, summary["current"])

    def test_a_sustained_jump_settles_and_becomes_current(self):
        lines = [_line("male", "infant")] * 10 + [_line("male", "young_child")] * 10
        summary = st.get_speaker_trait_summary(lines)
        self.assertEqual("young_child", summary["current"]["age_group"])
        self.assertEqual(2, len(summary["states"]))

    def test_established_traits_are_the_settled_current_state(self):
        named = ([dict(_line("female", "teen"), speaker="ROXY")] * 10
                 + [dict(_line("female", "adult", ageless=True), speaker="ROXY")] * 3
                 + [None, {"speaker": "NARRATOR", "text": "x"}])
        self.assertEqual({"ROXY": "female, teen, ageless"}, st.get_established_traits(named))

    def test_no_per_line_data_means_no_summary(self):
        self.assertIsNone(st.get_speaker_trait_summary([{"speaker": "X", "text": "hi"}]))


class PromptTest(unittest.TestCase):
    def test_off_sends_the_builtin(self):
        for generation in ({}, {"three_pass_speaker_traits": False}):
            self.assertIsNone(tp.resolve_attribute_prompt({"generation": generation})[1])

    def test_on_asks_every_answer_for_the_fields(self):
        variant, texts = tp.resolve_attribute_prompt({"generation": {"three_pass_speaker_traits": True}})
        sent = _texts_for(variant, texts)
        self.assertTrue(sent["system"].endswith(st.TRAITS_RULE))
        self.assertIn(st.TRAITS_FIELDS, sent["user"])
        self.assertTrue(tp.is_speaker_traits_prompt(texts))

    def test_a_prompt_without_the_answer_shape_is_left_alone(self):
        variant, texts = tp.resolve_attribute_prompt(
            {"generation": {"three_pass_speaker_traits": True}}, "default")
        self.assertFalse(tp.is_speaker_traits_prompt(texts))

    def test_off_keeps_the_checkpoint_identity_on_changes_it(self):
        params = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                              max_tokens=500, temperature=0.0)
        fp = lambda traits: tp.three_pass_fingerprint("t", "m", 3000, params, speaker_traits=traits)
        self.assertEqual(tp.three_pass_fingerprint("t", "m", 3000, params), fp(False))
        self.assertNotEqual(fp(False), fp(True))

    def test_the_schema_allows_but_does_not_require_the_fields(self):
        schema = tp.get_attribution_response_schema(True)["schema"]["items"]
        self.assertEqual(["n", "speaker"], schema["required"])
        self.assertIn("age_group", schema["properties"])
        self.assertIs(tp.ATTRIBUTION_RESPONSE_SCHEMA, tp.get_attribution_response_schema(False))


def _client(answer):
    def create(**_):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(answer)), finish_reason="stop")], usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class PassTwoTest(unittest.TestCase):
    BATCH = [{"type": "SPOKEN", "text": "Who goes there?"}, {"type": "NARRATOR", "text": "said Mr. Hall."}]
    ANSWER = [{"n": 0, "speaker": "MR. HALL", "gender": "MALE", "age_group": "MIDDLE_AGED",
               "ageless": False}, {"n": 1, "speaker": "NARRATOR", "gender": "UNKNOWN",
                                   "age_group": "UNKNOWN", "ageless": False}]

    def attribute(self, traits):
        params = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                              max_tokens=500, temperature=0.0)
        with contextlib.redirect_stdout(io.StringIO()):
            return tp.attribute_batch(_client(self.ANSWER), "m", self.BATCH, params, roster=[],
                                      max_retries=0, on_exhaustion="fail", speaker_traits=traits)

    def test_on_records_the_fields_on_spoken_lines_only(self):
        spoken, narration = self.attribute(True)
        self.assertEqual(("male", "middle_aged", False),
                         (spoken["speaker_gender"], spoken["speaker_age_group"],
                          spoken["speaker_ageless"]))
        self.assertNotIn("speaker_gender", narration)

    def test_a_spoken_line_answered_narrator_records_nothing(self):
        params = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                              max_tokens=500, temperature=0.0)
        ordered = [{"n": 0, "speaker": "NARRATOR", "gender": "MALE", "age_group": "ADULT"}]
        named = tp.get_named_from_answer([{"type": "SPOKEN", "text": "x"}], ordered,
                                         speaker_traits=True)
        self.assertNotIn("speaker_gender", named[0])

    def test_off_records_nothing(self):
        self.assertTrue(all("speaker_gender" not in entry for entry in self.attribute(False)))


class SettingAndPageTest(unittest.TestCase):
    def test_defaults_off_and_round_trips(self):
        self.assertFalse(config_settings.GenerationConfig().three_pass_speaker_traits)
        saved = config_settings.GenerationConfig(three_pass_speaker_traits=True).model_dump()
        self.assertTrue(config_settings.GenerationConfig(**saved).three_pass_speaker_traits)

    def test_switch_is_off_by_default_and_saved(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = (STATIC / "js" / "app-core.js").read_text(encoding="utf-8")
        self.assertIn('id="tp-speaker-traits">', html)
        self.assertIn("three_pass_speaker_traits: document.getElementById('tp-speaker-traits').checked", js)
        self.assertIn("getElementById('tp-speaker-traits').checked = g.three_pass_speaker_traits === true", js)

    def test_voice_rows_carry_the_summary(self):
        from routers.voices import get_voice_rows
        script = [{"speaker": "ANNA", "text": "Hi.", "speaker_gender": "female",
                   "speaker_age_group": "teen", "speaker_ageless": False},
                  {"speaker": "NARRATOR", "text": "she said."}]
        rows = {row["name"]: row for row in get_voice_rows(script, {})}
        self.assertEqual("female", rows["ANNA"]["traits"]["gender"])
        self.assertNotIn("traits", rows["NARRATOR"])

    def test_badge_shows_a_time_skip_and_escapes_model_text(self):
        script = r'''
const fs = require('fs');
const core = fs.readFileSync(process.argv[1], 'utf8');
const take = (name) => {
    const start = core.indexOf('function ' + name + '(');
    let depth = 0, i = core.indexOf('{', start);
    for (; i < core.length; i++) {
        if (core[i] === '{') { depth++; }
        if (core[i] === '}') { depth--; if (depth === 0) { break; } }
    }
    return core.slice(start, i + 1);
};
eval(take('escapeHtml') + take('getTraitBadgeHtml'));
process.stdout.write(JSON.stringify([
  getTraitBadgeHtml({gender: 'male', age_group: 'adult', ageless: false, lines: 9,
                     states: [{gender: 'male', age_group: 'child'}, {gender: 'male', age_group: 'adult'}]}),
  getTraitBadgeHtml({gender: '<img src=x onerror=1>', age_group: 'teen', ageless: true, lines: 1, states: []}),
  getTraitBadgeHtml(null)]));
'''
        skip, hostile, none = json.loads(subprocess.run(
            ["node", "-e", script, str(STATIC / "js" / "app-core.js")],
            check=True, capture_output=True, text=True).stdout)
        self.assertIn("male · child → male · adult", skip)
        self.assertNotIn("<img", hostile)
        self.assertIn("ageless", hostile)
        self.assertEqual("", none)


if __name__ == "__main__":
    unittest.main()


class StickyRosterTest(unittest.TestCase):
    def test_roster_text_is_unchanged_without_traits(self):
        from attribution_prompt_variants import roster_line
        self.assertEqual("ROXY (also: MASTER), PAUL", roster_line(["ROXY", "PAUL"], [["ROXY", "MASTER"]]))
        self.assertEqual("ROXY (also: MASTER) [female, teen, ageless], PAUL",
                         roster_line(["ROXY", "PAUL"], [["ROXY", "MASTER"]],
                                     {"ROXY": "female, teen, ageless"}))

    def test_traits_reach_the_provider_only_when_given(self):
        seen = []

        def provider(*_args, **kwargs):
            seen.append(kwargs.get("roster_traits"))
            entries = [{"n": 0, "speaker": "ROXY"}]
            kwargs["validate_entries"](entries)
            return entries
        params = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                              max_tokens=500, temperature=0.0)
        batch = [{"type": "SPOKEN", "text": "Hello."}]
        for traits in ({"ROXY": "female, teen"}, None):
            with contextlib.redirect_stdout(io.StringIO()):
                tp.attribute_batch(SimpleNamespace(), "m", batch, params, roster=["ROXY"],
                                   max_retries=0, entries_provider=provider, roster_traits=traits)
        self.assertEqual([{"ROXY": "female, teen"}, None], seen)

