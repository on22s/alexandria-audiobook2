"""cast_list.py: the whole-book cast request the Script tab runs (#653).

The measured reason it exists is in the module docstring; these pin the parts
the app relies on: a list is found by the source's bytes, a list generation
would refuse is never written, and the model is sent the text generation sees.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cast_list
from three_pass_generate import get_prepared_source

# Mojibake the shared source repair fixes ("Itâ€™s" -> "It’s"), so the prepared
# text differs from the file and a CLI that sent the raw file would be caught.
SOURCE = ('"Itâ€™s late," said the detective. ' * 40
          + "Mr. Oakley went out. " * 30)
CAST = [{"name": "THE DETECTIVE", "aliases": []},
        {"name": "MAURICE OAKLEY", "aliases": ["MR. OAKLEY"]}]


class _Client:
    def __init__(self, content=json.dumps(CAST), finish_reason="stop"):
        self.prompts = []
        self.content, self.finish_reason = content, finish_reason
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=self.finish_reason,
                                     message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


class CastListTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.source = os.path.join(self.dir, "book.txt")
        with open(self.source, "w", encoding="utf-8") as handle:
            handle.write(SOURCE)

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, client, *extra):
        out = os.path.join(self.dir, "lists", "cast.json")
        with patch("config_settings.load_app_config", return_value={}), \
                patch("lmstudio_settings.get_active_llm_config", return_value={"model_name": "m"}), \
                patch("llm_provider.make_run_client", return_value=client):
            code = cast_list.main([self.source, "--out", out, *extra])
        return code, out

    def test_path_is_keyed_by_bytes_not_name(self):
        renamed = os.path.join(self.dir, "renamed.txt")
        with open(renamed, "w", encoding="utf-8") as handle:
            handle.write(SOURCE)
        self.assertEqual(cast_list.get_cast_list_path(self.source, self.dir),
                         cast_list.get_cast_list_path(renamed, self.dir))
        with open(renamed, "a", encoding="utf-8") as handle:
            handle.write(".")
        self.assertNotEqual(cast_list.get_cast_list_path(self.source, self.dir),
                            cast_list.get_cast_list_path(renamed, self.dir))

    def test_a_list_generation_would_refuse_is_not_written(self):
        path = os.path.join(self.dir, "lists", "cast.json")
        for bad in ([], [{"aliases": ["X"]}], [{"name": "X", "aliases": "Y"}]):
            with self.assertRaises(ValueError):
                cast_list.save_cast_list(path, bad)
            self.assertFalse(os.path.exists(path))

    def test_documented_bare_output_saves_valid_cast_and_rejects_invalid_replacement(self):
        previous = os.getcwd()
        os.chdir(self.dir)
        try:
            for path in ('cast.json', './relative.json', 'nested/cast.json'):
                with self.subTest(path=path):
                    cast_list.save_cast_list(path, CAST, {'model': 'fixture'})
                    with open(path, encoding='utf-8') as stream:
                        self.assertEqual({'cast': CAST, 'provenance': {'model': 'fixture'}}, json.load(stream))
                    with open(path, 'rb') as stream:
                        before = stream.read()
                    with self.assertRaises(ValueError):
                        cast_list.save_cast_list(path, [])
                    with open(path, 'rb') as stream:
                        self.assertEqual(before, stream.read())
        finally:
            os.chdir(previous)

    def test_model_is_sent_the_text_generation_sees(self):
        client = _Client()
        code, out = self.run_main(client)
        self.assertEqual(0, code)
        prepared, _ = get_prepared_source(self.source, True)
        self.assertNotEqual(SOURCE, prepared)          # the fixture really differs
        self.assertEqual([cast_list.PROMPT + prepared], client.prompts)
        with open(out, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(CAST, written["cast"])
        self.assertEqual("m", written["provenance"]["model"])

    def test_source_over_the_limit_is_refused_before_any_request(self):
        client = _Client()
        with patch.object(cast_list, "MAX_SOURCE_CHARS", 100):
            code, out = self.run_main(client)
        self.assertEqual(1, code)
        self.assertEqual([], client.prompts)
        self.assertFalse(os.path.exists(out))

    def test_unparseable_reply_writes_nothing(self):
        code, out = self.run_main(_Client(content="Here are the characters: Holmes."))
        self.assertEqual(1, code)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
