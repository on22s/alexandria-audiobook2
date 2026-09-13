"""The source-repair CLI uses the same configured provider client as the app."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import repair_source_llm


class _Message:
    content = '[{"n": 0, "chars": "é"}]'


class _Choice:
    message = _Message()


class _Response:
    choices = [_Choice()]


class _Client:
    class chat:
        class completions:
            @staticmethod
            def create(**_kwargs):
                return _Response()


class RepairSourceLLMTests(unittest.TestCase):
    def test_cli_passes_the_active_profile_to_the_shared_client_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "damaged.txt")
            config_path = os.path.join(directory, "config.json")
            report = os.path.join(directory, "report.json")
            Path(source).write_text("caf�", encoding="utf-8")
            active = {
                "base_url": "https://provider.example/v1",
                "api_key": "token",
                "model_name": "repair-model",
                "provider_headers": {"x-provider": "enabled"},
                "reasoning_effort": "low",
            }
            Path(config_path).write_text(json.dumps({
                "llm_mode": "remote", "llm": {"model_name": "stale"},
                "llm_remote": active,
            }), encoding="utf-8")
            real_join = os.path.join

            def join(*parts):
                if parts == (repair_source_llm.APP, "config.json"):
                    return config_path
                return real_join(*parts)

            with patch.object(repair_source_llm.os.path, "join", side_effect=join), \
                 patch("llm_provider.make_llm_client", return_value=_Client()) as make_client, \
                 patch.object(sys, "argv", ["repair_source_llm.py", source, "--report", report]):
                repair_source_llm.main()

            self.assertEqual(active, make_client.call_args.args[0])
            self.assertEqual(repair_source_llm.llm_timeout_seconds(),
                             make_client.call_args.args[1])
            self.assertEqual(1, json.loads(Path(report).read_text(encoding="utf-8"))["runs_resolved"])


if __name__ == "__main__":
    unittest.main()
