"""Structured attribution output (issue #522 s9.1): the attribution pass asks
for a {n, speaker} JSON schema, a server that rejects response_format is
remembered for the run, and any other error keeps the normal retry path."""
import json
import unittest
from types import SimpleNamespace

import generate_script as gs
import three_pass_generate as tp
from generate_script import LLMGenParams


def _reply(entries):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(entries)), finish_reason="stop")],
        usage=None)


class _Client:
    def __init__(self, reject=None, base_url="http://srv/v1"):
        self.base_url = base_url
        self.calls = []
        self._reject = reject
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._reject and "response_format" in kwargs:
            raise self._reject
        return _reply([{"n": 0, "head": "Yes", "speaker": "ELENA"}])


class _ApiError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def _params(**over):
    return LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                        attribute_system_prompt="a", **over)


class StructuredOutput(unittest.TestCase):
    def setUp(self):
        gs._SCHEMA_REJECTED_BY.clear()

    def test_attribution_sends_the_schema_by_default(self):
        client = _Client()
        tp.attribute_batch(client, "m", [{"type": "SPOKEN", "text": "Yes."}], _params(), roster=[])
        rf = client.calls[0]["response_format"]
        self.assertEqual("json_schema", rf["type"])
        self.assertTrue(rf["json_schema"]["strict"])
        self.assertEqual(["n", "speaker"], rf["json_schema"]["schema"]["items"]["required"])

    def test_off_never_sends_it(self):
        client = _Client()
        tp.attribute_batch(client, "m", [{"type": "SPOKEN", "text": "Yes."}],
                           _params(structured_output="off"), roster=[])
        self.assertNotIn("response_format", client.calls[0])

    def test_rejection_falls_back_once_and_sticks_for_the_server(self):
        client = _Client(reject=_ApiError(400, "response_format is not supported"))
        frozen = [{"type": "SPOKEN", "text": "Yes."}]
        tp.attribute_batch(client, "m", frozen, _params(), roster=[])
        # first window: schema attempt, then the same request free-form
        self.assertIn("response_format", client.calls[0])
        self.assertNotIn("response_format", client.calls[1])
        self.assertEqual(client.calls[0]["messages"], client.calls[1]["messages"])
        # second window on the same server: no schema attempt at all
        tp.attribute_batch(client, "m", frozen, _params(), roster=[])
        self.assertEqual(3, len(client.calls))
        self.assertNotIn("response_format", client.calls[2])
        # a different server still gets the schema
        other = _Client(base_url="http://other/v1")
        tp.attribute_batch(other, "m", frozen, _params(), roster=[])
        self.assertIn("response_format", other.calls[0])

    def test_other_errors_are_not_mistaken_for_schema_rejection(self):
        self.assertFalse(gs.is_schema_rejection(_ApiError(400, "context length exceeded")))
        self.assertFalse(gs.is_schema_rejection(_ApiError(500, "json_schema grammar failed")))
        self.assertTrue(gs.is_schema_rejection(_ApiError(400, "unknown field: json_schema")))
        self.assertTrue(gs.is_schema_rejection(_ApiError(422, "grammar: invalid")))
        self.assertTrue(gs.is_schema_rejection(TypeError("create() got an unexpected keyword argument 'response_format'")))
        self.assertFalse(gs.is_schema_rejection(TypeError("unsupported operand")))

    def test_a_non_schema_400_keeps_the_schema_and_the_retry_path(self):
        client = _Client(reject=_ApiError(400, "context length exceeded"))
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, "m", [{"type": "SPOKEN", "text": "Yes."}],
                               _params(api_retry_limit=0), roster=[], max_retries=0)
        self.assertEqual(set(), gs._SCHEMA_REJECTED_BY)
        self.assertIn("response_format", client.calls[-1])


if __name__ == "__main__":
    unittest.main()
