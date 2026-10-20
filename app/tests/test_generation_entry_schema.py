"""Malformed model output must be retried and cannot poison a later resume."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from chunk_quality import validate_chunk_quality
import generate_script
from tests.test_chunk_quality import _entry, generate_script_test_client


class GenerationEntrySchemaTests(unittest.TestCase):
    def test_actual_cli_regenerates_a_preexisting_poisoned_checkpoint(self):
        from tests import test_generation_model_binding as fixture
        with tempfile.TemporaryDirectory() as directory:
            harness = fixture.GenerationModelBindingTests()
            output, record = harness.run_cli(generate_script, Path(directory), False)
            fingerprint = record['fingerprint']
            generate_script.save_generation_checkpoint(str(output), fingerprint, [{
                'source_sha256':fingerprint['chunk_sha256'][0],
                'entries':[{'speaker':[], 'text':fixture.TEXT, 'instruct':'Neutral.'}],
                'quality':{'passed':True}}])
            _, recovered = harness.run_cli(generate_script, Path(directory), False)
            self.assertEqual('complete',recovered['status'])
            self.assertEqual('NARRATOR',json.loads(output.read_text())[0]['speaker'])

    def test_response_retries_malformed_fields_without_mutating_them(self):
        source = "A complete spoken sentence."
        for field, value in (("speaker", []), ("speaker", 42), ("speaker", "  "),
                             ("instruct", []), ("instruct", None), ("text", 42)):
            with self.subTest(field=field, value=value):
                bad = [_entry(source)]
                bad[0][field] = value
                snapshot = copy.deepcopy(bad)
                self.assertFalse(validate_chunk_quality(source, bad)["passed"])
                good = [_entry(source, instruct="")]
                client = generate_script_test_client([bad, good])
                result = generate_script.process_chunk(
                    client, "model", source, 1, 1,
                    generate_script.LLMGenParams("system", "{chunk}"))
                self.assertEqual(good, result)
                self.assertEqual(snapshot, bad)

    def test_numeric_text_is_not_accepted_merely_because_its_string_matches_the_source(self):
        bad = [_entry(42)]
        self.assertFalse(validate_chunk_quality('42',bad)['passed'])
        good = [_entry('42')]
        result = generate_script.process_chunk(generate_script_test_client([bad,good]),
            'model','42',1,1,generate_script.LLMGenParams('system','{chunk}'))
        self.assertEqual(good,result)

    def test_checkpoint_preserves_good_prefix_and_regenerates_poisoned_suffix(self):
        chunks = ["First spoken sentence.", "Second spoken sentence.",
                  "Third spoken sentence."]
        params = generate_script.LLMGenParams("system", "{chunk}")
        fingerprint = generate_script.get_generation_fingerprint(
            "\n".join(chunks), chunks, "model", "http://local", params, 6000)
        for field, value in (("speaker", []), ("speaker", ""), ("text", 123),
                             ("instruct", []), ("instruct", None)):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                output = str(Path(tmp, "book.json"))
                accepted = [{"chunk_number": i + 1,
                             "source_sha256": fingerprint["chunk_sha256"][i],
                             "entries": [_entry(chunk)], "quality": {"passed": True}}
                            for i, chunk in enumerate(chunks)]
                accepted[1]["entries"][0][field] = value
                generate_script.save_generation_checkpoint(output, fingerprint, accepted)
                resumed = generate_script.load_generation_checkpoint(output, fingerprint)
                self.assertEqual(accepted[:1], resumed)
                repaired = accepted[:1] + [dict(item, entries=[_entry(chunks[i])])
                                          for i, item in enumerate(accepted) if i >= 1]
                generate_script.save_generation_checkpoint(output, fingerprint, repaired)
                self.assertEqual(repaired, generate_script.load_generation_checkpoint(output, fingerprint))

    def test_missing_required_fields_and_empty_checkpoint_entries_are_rejected(self):
        for field in ("speaker", "text", "instruct"):
            with self.subTest(field=field):
                entry = _entry("A spoken sentence.")
                del entry[field]
                self.assertFalse(validate_chunk_quality("A spoken sentence.", [entry])["passed"])
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, "book.json"))
            fingerprint = {"source_sha256": "source", "settings_sha256": "settings",
                           "chunk_sha256": ["chunk"]}
            generate_script.save_generation_checkpoint(output, fingerprint, [
                {"source_sha256": "chunk", "entries": [], "quality": {"passed": True}}])
            self.assertEqual([], generate_script.load_generation_checkpoint(output, fingerprint))


if __name__ == "__main__":
    unittest.main()
