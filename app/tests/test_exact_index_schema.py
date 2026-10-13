"""Pass 2 sends a schema that fixes each window's row count and indexes.

WHAT THIS VERIFIES. attribute_batch's request carries a json_schema whose
array has minItems == maxItems == the window size and whose `n` is limited to
that window's indexes, for a 1-entry and a 3-entry window, with and without
per-line traits. The shared ATTRIBUTION_RESPONSE_SCHEMA is not mutated. The
runaway it prevents was measured live: a 1-entry window answered n=0..149
until the output cap. A grammar-constrained server enforces this; here the
assertion is on what is sent. Synthetic text only.
"""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace

import three_pass_generate as tp
from generate_script import LLMGenParams


def run_window(frozen, speaker_traits=False):
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        reply = [{"n": i, "speaker": "NARRATOR" if row["type"] == "NARRATOR" else "TOM"}
                 for i, row in enumerate(frozen)]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(reply)), finish_reason="stop")],
            usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    params = LLMGenParams("system", "{batch}", 4096, 0.0, 1, structured_output="auto")
    with contextlib.redirect_stdout(io.StringIO()):
        tp.attribute_batch(client, "fixture", frozen, params, ["TOM"],
                           speaker_traits=speaker_traits)
    return sent[0]["response_format"]["json_schema"]["schema"]


ONE = [{"type": "SPOKEN", "text": "Come here,"}]
THREE = [{"type": "SPOKEN", "text": "Come here,"},
         {"type": "NARRATOR", "text": "said Tom."},
         {"type": "SPOKEN", "text": "Now."}]


class ExactIndexSchemaTests(unittest.TestCase):
    def assert_exact(self, schema, size):
        self.assertEqual(size, schema["minItems"])
        self.assertEqual(size, schema["maxItems"])
        self.assertEqual(list(range(size)), schema["items"]["properties"]["n"]["enum"])

    def test_one_entry_window_allows_exactly_one_row(self):
        self.assert_exact(run_window(ONE), 1)

    def test_three_entry_window_fixes_count_and_indexes(self):
        self.assert_exact(run_window(THREE), 3)

    def test_traits_schema_is_narrowed_too(self):
        schema = run_window(THREE, speaker_traits=True)
        self.assert_exact(schema, 3)
        self.assertIn("gender", schema["items"]["properties"])

    def test_shared_schema_is_not_mutated(self):
        original = copy.deepcopy(tp.ATTRIBUTION_RESPONSE_SCHEMA)
        run_window(THREE)
        tp.get_attribution_response_schema(True, range(2))
        self.assertEqual(original, tp.ATTRIBUTION_RESPONSE_SCHEMA)
        self.assertIs(tp.ATTRIBUTION_RESPONSE_SCHEMA, tp.get_attribution_response_schema(False))


if __name__ == "__main__":
    unittest.main()
