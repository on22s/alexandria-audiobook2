"""Exercise object responses through the shared LLM reliability pipeline."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import generate_script


class PersonaObjectPipelineTests(unittest.TestCase):
    def test_object_response_is_validated_and_logged(self):
        payload = {"persona": "A careful storyteller", "age": "adult"}
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)),
                                     finish_reason="stop")], usage=None)
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return response

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = generate_script.LLMGenParams("system", "{chunk}", 100, 0.1, 1)
        validated = []
        attempts = []
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp, "responses.log")
            with patch.object(generate_script, "get_response_log_path",
                              return_value=str(log_path)) as get_log:
                result = generate_script.call_llm_for_object(
                    client, "model", "system", "describe the narrator", params,
                    "PERSONA narrator", validate_object=validated.append,
                    max_retries=0, attempt_observer=attempts.append)
            self.assertEqual(payload, result)
            self.assertEqual([payload], validated)
            self.assertEqual(1, len(calls))
            self.assertEqual("accepted", attempts[0]["outcome"])
            get_log.assert_called_once_with("llm_responses.log")
            self.assertIn("PERSONA narrator", log_path.read_text())
            self.assertIn(payload["persona"], log_path.read_text())
