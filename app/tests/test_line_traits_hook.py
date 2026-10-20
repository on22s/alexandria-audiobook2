"""The experiment hook follows the product's accepted and kept call contract."""
import contextlib
import io
import unittest
from unittest.mock import patch

from experiments.character_traits import install_line_traits
from generate_script import LLMGenParams
import three_pass_generate as tp
from tests.test_speaker_traits import _client
from tests.test_three_pass_keep_exhausted import DURGAN_LINE, DURGAN_SOURCE


class LineTraitsHookTests(unittest.TestCase):
    def attribute(self, frozen, answer, mode, source=None, cast=None):
        original = tp.get_named_from_answer
        with patch.object(tp, "get_named_from_answer", original), contextlib.redirect_stdout(io.StringIO()):
            install_line_traits()
            return tp.attribute_batch(
                _client(answer), "fixture", frozen,
                LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                             max_tokens=500, temperature=0.0), roster=[],
                max_retries=0, on_exhaustion=mode, source_text=source,
                cast=cast, speaker_traits=True)

    def test_accepted_answer_keeps_experiment_traits_and_product_alias_fields(self):
        frozen = [{"type": "SPOKEN", "text": "Who goes there?"},
                  {"type": "NARRATOR", "text": "said Mr. Hall."}]
        answer = [{"n": 0, "speaker": "HALL", "gender": "MALE", "age_group": "MIDDLE_AGED"},
                  {"n": 1, "speaker": "NARRATOR", "gender": "UNKNOWN", "age_group": "UNKNOWN"}]
        out = self.attribute(frozen, answer, "fail", cast={"alias_to_name": {"HALL": "MR. HALL"}})
        self.assertEqual([entry["text"] for entry in frozen], [entry["text"] for entry in out])
        self.assertEqual("MR. HALL", out[0]["speaker"])
        self.assertEqual(("male", "middle_aged"), (out[0]["line_gender"], out[0]["line_age_group"]))
        self.assertEqual(("male", "middle_aged"), (out[0]["speaker_gender"], out[0]["speaker_age_group"]))
        self.assertEqual("unknown", out[1]["line_gender"])
        self.assertNotIn("speaker_gender", out[1])
        self.assertNotIn("attribution_unchecked", out[0])

    def test_kept_answer_preserves_frozen_text_traits_and_unchecked_reason(self):
        answer = [{"n": 0, "speaker": "SILAS DURGAN", "gender": "MALE", "age_group": "ADULT"}]
        out = self.attribute([{"type": "SPOKEN", "text": DURGAN_LINE}], answer, "keep", DURGAN_SOURCE)
        self.assertEqual(DURGAN_LINE, out[0]["text"])
        self.assertEqual("SILAS DURGAN", out[0]["speaker"])
        self.assertEqual("male", out[0]["line_gender"])
        self.assertEqual("adult", out[0]["line_age_group"])
        self.assertEqual("male", out[0]["speaker_gender"])
        self.assertEqual(["speaker_not_in_source"], out[0]["attribution_unchecked"])


if __name__ == "__main__":
    unittest.main()
