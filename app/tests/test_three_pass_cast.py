"""--cast-file: a supplied cast goes on the pass-2 roster and past the name check.

Shapes are The Invisible Man's from stage 0 of the DeepSeek labelling plan (2026-09-28):
the protagonist is "the stranger" (never capitalised, so the roster gate could not admit
him and 99 of his lines came back UNKNOWN), and Silas Durgan is named once (refused by
speaker_not_in_source). One DeepSeek call listed him as THE STRANGER (also THE INVISIBLE
MAN, GRIFFIN); experiments/build_cast_list.py is that call.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import three_pass_generate as tp
from generate_script import LLMGenParams
from pass_quality import validate_attribution
from experiments.build_cast_list import parse_cast, request_cast
from tests.test_three_pass_keep_exhausted import DURGAN_LINE, DURGAN_SOURCE, _client_always

# "the stranger" only ever mid-sentence and lowercase, as in the novel; long enough
# (> MIN_SOURCE_FOR_ATTESTATION) for the name check to apply
STRANGER_SOURCE = ("Then the stranger came in. " * 200 + "Mrs. Hall spoke to Mr. Hall. " * 50
                   + 'Then the stranger said, "A fire," and sat. ' + "Mr. Hall nodded. " * 50)
SHORT_SOURCE = 'Then the stranger said, "A fire," and sat down by it.'
CAST = [{"name": "The Stranger", "aliases": ["the Invisible Man", "Griffin"]},
        {"name": "SILAS DURGAN", "aliases": []},
        {"name": "Mrs. Hall", "aliases": ["the landlady"]}]


def _cast_file(tmp, data=CAST):
    path = os.path.join(tmp, "cast.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path


def _params():
    return LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                        max_tokens=500, temperature=0.0)


class CastTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cast = tp.load_cast(_cast_file(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_cast_uppercases_maps_aliases_and_accepts_the_tool_shape(self):
        self.assertEqual(["THE STRANGER", "SILAS DURGAN", "MRS. HALL"], self.cast["names"])
        self.assertEqual("THE STRANGER", self.cast["alias_to_name"]["GRIFFIN"])
        wrapped = tp.load_cast(_cast_file(self._tmp.name, {"cast": CAST, "provenance": {}}))
        self.assertEqual(self.cast["names"], wrapped["names"])

    def test_malformed_cast_file_raises(self):
        for bad in ([], [{"aliases": ["X"]}], [{"name": "X", "aliases": "Y"}], {"people": CAST}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tp.load_cast(_cast_file(self._tmp.name, bad))

    def test_cast_names_pass_the_name_check_and_invented_ones_still_do_not(self):
        frozen = [{"type": "SPOKEN", "text": "A fire,"}]
        answer = lambda name: [{"n": 0, "speaker": name}]
        codes = lambda report: [f["code"] for f in report.get("findings", [])]
        # without a cast the lowercase-only description is refused ...
        self.assertIn("speaker_not_in_source",
                      codes(validate_attribution(frozen, answer("THE STRANGER"), STRANGER_SOURCE)))
        known = self.cast["known_names"]
        for name in ("THE STRANGER", "THE INVISIBLE MAN"):
            with self.subTest(name=name):
                self.assertTrue(validate_attribution(frozen, answer(name), STRANGER_SOURCE,
                                                     known_names=known)["passed"])
        # ... and a name in neither the cast nor the text is still refused with one
        self.assertIn("speaker_not_in_source",
                      codes(validate_attribution(frozen, answer("FUTURE ME"), STRANGER_SOURCE,
                                                 known_names=known)))

    def test_once_named_cast_member_is_accepted_without_exhausting(self):
        client, calls = _client_always([{"n": 0, "speaker": "SILAS DURGAN"}])
        with contextlib.redirect_stdout(io.StringIO()):
            out = tp.attribute_batch(client, "m", [{"type": "SPOKEN", "text": DURGAN_LINE}],
                                     _params(), roster=[], max_retries=1, on_exhaustion="fail",
                                     source_text=DURGAN_SOURCE, cast=self.cast)
        self.assertEqual("SILAS DURGAN", out[0]["speaker"])
        self.assertEqual(1, calls["attribute"])

    def test_alias_answer_comes_out_as_the_cast_name(self):
        client, _ = _client_always([{"n": 0, "speaker": "GRIFFIN"}])
        with contextlib.redirect_stdout(io.StringIO()):
            out = tp.attribute_batch(client, "m", [{"type": "SPOKEN", "text": "A fire,"}],
                                     _params(), roster=[], max_retries=1,
                                     source_text=STRANGER_SOURCE, cast=self.cast)
        self.assertEqual("THE STRANGER", out[0]["speaker"])

    def test_roster_leads_with_the_cast_and_shows_its_aliases(self):
        seen = []

        def create(**kwargs):
            schema = ((kwargs.get("response_format") or {}).get("json_schema") or {}).get("name")
            if schema == "speaker_attribution":
                seen.append(" ".join(m["content"] for m in kwargs["messages"]))
                payload = [{"n": 0, "speaker": "NARRATOR"}, {"n": 1, "speaker": "GRIFFIN"},
                           {"n": 2, "speaker": "NARRATOR"}]
            else:
                payload = [{"n": i, "instruct": "Plain."} for i in range(3)]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")],
                usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with contextlib.redirect_stdout(io.StringIO()):
            entries = tp.run_three_pass(client, "m", SHORT_SOURCE,
                              LLMGenParams(max_tokens=500, temperature=0.0, segmentation="auto"),
                              chunk_size=6000, attribute_prompt_variant="michel2_full",
                              cast=self.cast)
        self.assertTrue(seen)
        self.assertIn("THE STRANGER (also: GRIFFIN, THE INVISIBLE MAN)", seen[0])
        self.assertLess(seen[0].index("THE STRANGER (also"), seen[0].index("SILAS DURGAN"))
        # the alias answer reached the finished script as the cast name
        self.assertEqual(["THE STRANGER"], [e["speaker"] for e in entries if e["text"] == "A fire,"])

    def test_fingerprint_moves_only_when_a_cast_is_given(self):
        fp = lambda **kw: tp.three_pass_fingerprint("text", "m", 6000, **kw)
        self.assertEqual(fp(), fp(cast_sha256=None))
        self.assertNotEqual(fp(), fp(cast_sha256=self.cast["sha256"]))

    def test_tool_reply_parsing(self):
        reply = '```json\n[{"name": "the stranger", "aliases": ["Griffin", ""]}]\n```'
        self.assertEqual([{"name": "THE STRANGER", "aliases": ["GRIFFIN"]}], parse_cast(reply))
        with self.assertRaises(ValueError):
            parse_cast('{"name": "X"}')


if __name__ == "__main__":
    unittest.main()


def _reply(content, finish):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                                                    message=SimpleNamespace(content=content))])


class _ScriptedClient:
    """Returns the queued replies in order and counts the calls."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        return self.replies.pop(0)


# The shape of the real failure: five names cycled until max_tokens cut a string in half.
_LOOP = ('[' + ',\n'.join('{"name": "MISS BARRY", "aliases": []}' for _ in range(40))
         + ',\n{"name": "MISS BAR')
_GOOD = '[{"name": "ANNE SHIRLEY", "aliases": ["ANNE"]}]'


class RequestCastTests(unittest.TestCase):
    def test_truncated_reply_is_discarded_and_the_retry_is_used(self):
        client = _ScriptedClient([_reply(_LOOP, "length"), _reply(_GOOD, "stop")])
        with contextlib.redirect_stderr(io.StringIO()):
            cast, _, attempts = request_cast(client, "m", "text", 8000)
        self.assertEqual(([{"name": "ANNE SHIRLEY", "aliases": ["ANNE"]}], 2, 2),
                         (cast, attempts, client.calls))

    def test_every_attempt_truncated_raises_naming_the_cause_not_a_json_error(self):
        client = _ScriptedClient([_reply(_LOOP, "length")] * 3)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "finish_reason=length"):
                request_cast(client, "m", "text", 8000, max_attempts=3)
        self.assertEqual(3, client.calls)

    def test_a_bad_reply_that_finished_normally_raises_at_once_without_retrying(self):
        client = _ScriptedClient([_reply("not json", "stop"), _reply(_GOOD, "stop")])
        with self.assertRaises(ValueError):
            request_cast(client, "m", "text", 8000)
        self.assertEqual(1, client.calls)

    def test_the_old_path_could_not_tell_a_loop_from_an_answer(self):
        # Guards the fixture: without the finish_reason check the looped text fails only as an
        # opaque JSON error, which is the failure this change replaces.
        with self.assertRaises(ValueError):
            parse_cast(_LOOP)
