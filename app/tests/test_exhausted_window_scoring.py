"""An exhausted attribution window must not throw away the lines the model
answered. Measured 2026-09-11: owarimonogatari3 window 102's base arm returned a
complete array with ONE spoken line named NARRATOR; validate_attribution rejected
the whole response four identical times (temp 0) and every gold row in the
window was recorded as batch_failed."""
import unittest

from tests.test_three_pass_generate import _client_returning, LLMGenParams
import three_pass_generate as tp
from experiments.lora_serving_eval import bind_last_attempt


def _params():
    return LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                        temperature=0.0, max_tokens=64)


class PassExhaustedCarriesTheLastAttempt(unittest.TestCase):
    def test_rejected_response_travels_with_the_exception(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."},
                  {"type": "SPOKEN", "text": "No."}]
        bad = [{"n": 0, "head": "Tell me.", "speaker": "HARUHIRO"},
               {"n": 1, "head": "No.", "speaker": "NARRATOR"}]
        client = _client_returning([bad, bad])
        with self.assertRaises(tp.PassExhausted) as ctx:
            tp.attribute_batch(client, "m", frozen, _params(), roster=[],
                               max_retries=1, on_exhaustion="fail")
        self.assertEqual(bad, ctx.exception.last_entries)

    def test_no_parseable_attempt_leaves_last_entries_none(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        client = _client_returning(["not json", "still not"])
        with self.assertRaises(tp.PassExhausted) as ctx:
            tp.attribute_batch(client, "m", frozen, _params(), roster=[],
                               max_retries=1, on_exhaustion="fail")
        self.assertIsNone(ctx.exception.last_entries)

    def test_fallback_mode_is_unchanged(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        bad = [{"n": 0, "head": "Tell me.", "speaker": "NARRATOR"}]
        out = tp.attribute_batch(_client_returning([bad, bad]), "m", frozen,
                                 _params(), roster=[], max_retries=1,
                                 on_exhaustion="fallback")
        self.assertEqual("UNKNOWN", out[0]["speaker"])


class BindLastAttemptTests(unittest.TestCase):
    def test_answered_lines_are_kept_and_the_rejected_one_stays_unanswered(self):
        out = bind_last_attempt([{"n": 0, "speaker": "HARUHIRO"},
                                 {"n": 1, "speaker": "NARRATOR"},
                                 {"n": 2, "speaker": "  "}], 3)
        self.assertEqual([{"speaker": "HARUHIRO"}, {"speaker": None},
                          {"speaker": None}], out)

    def test_bad_or_out_of_range_indices_are_ignored_not_guessed(self):
        out = bind_last_attempt([{"n": "x", "speaker": "A"}, {"n": 7, "speaker": "B"},
                                 {"n": -1, "speaker": "C"}, "junk",
                                 {"n": 1, "speaker": "RANTA"}], 2)
        self.assertEqual([None, {"speaker": "RANTA"}], out)

    def test_first_binding_wins_on_duplicate_n(self):
        out = bind_last_attempt([{"n": 0, "speaker": "A"}, {"n": 0, "speaker": "B"}], 1)
        self.assertEqual([{"speaker": "A"}], out)

    def test_nothing_bindable_returns_none_so_the_window_is_batch_failed(self):
        self.assertIsNone(bind_last_attempt(None, 3))
        self.assertIsNone(bind_last_attempt([], 3))
        self.assertIsNone(bind_last_attempt([{"speaker": "A"}, {"n": 9}], 3))


if __name__ == "__main__":
    unittest.main()
