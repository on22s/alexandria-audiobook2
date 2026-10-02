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
            with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": directory}), \
                 patch("llm_provider.make_llm_client", return_value=_Client()) as make_client, \
                 patch.object(sys, "argv", ["repair_source_llm.py", source, "--report", report]):
                repair_source_llm.main()

            self.assertEqual(active, make_client.call_args.args[0])
            self.assertEqual(repair_source_llm.llm_timeout_seconds(),
                             make_client.call_args.args[1])
            self.assertEqual(1, json.loads(Path(report).read_text(encoding="utf-8"))["runs_resolved"])


if __name__ == "__main__":
    unittest.main()


class RepairRuntimeConfigTests(unittest.TestCase):
    def test_cli_uses_each_runtime_config_and_legacy_default_without_modifying_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            app_dir.mkdir()
            source = root / "damaged.txt"
            source.write_text("caf�", encoding="utf-8")
            profiles = {}
            for name, folder in (("legacy", app_dir), ("first", root / "first"), ("second", root / "second")):
                folder.mkdir(exist_ok=True)
                profile = {"base_url": "https://" + name + ".example/v1", "model_name": name + "-model",
                           "provider_headers": {"x-profile": name}, "api_key": "fixture"}
                config = folder / "config.json"
                config.write_text(json.dumps({"llm_mode": "remote", "llm_remote": profile}))
                profiles[name] = (folder, config, profile, config.read_bytes())
            for name in ("first", "second", "legacy"):
                folder, config, profile, before = profiles[name]
                output = root / (name + "_repaired.txt")
                report = root / (name + "_report.json")
                data_dir = "" if name == "legacy" else str(folder)
                with self.subTest(name=name), patch.object(repair_source_llm, "APP", str(app_dir)), \
                     patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": data_dir}), \
                     patch("llm_provider.make_llm_client", return_value=_Client()) as client, \
                     patch.object(sys, "argv", ["repair_source_llm.py", str(source), "--apply",
                                               "--out", str(output), "--report", str(report)]):
                    repair_source_llm.main()
                    self.assertEqual(profile, client.call_args.args[0])
                    self.assertEqual("café", output.read_text(encoding="utf-8"))
                    data = json.loads(report.read_text())
                    self.assertEqual((1, 0, 0), (data["runs_resolved"], data["runs_refused"], data["runs_unanswered"]))
                    self.assertEqual(before, config.read_bytes())
                    self.assertEqual("caf�", source.read_text(encoding="utf-8"))
            for folder, config, profile, before in profiles.values():
                self.assertEqual(before, config.read_bytes())


class DuplicateRepairDecisionTests(unittest.TestCase):
    def test_last_decision_has_one_disposition_and_repaired_bytes_match_report(self):
        import copy
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        cases = (([{"n": 0, "chars": "é"}, {"n": 0, "chars": "A"}], 0, 1, "caf�"),
                 ([{"n": 0, "chars": "A"}, {"n": 0, "chars": "é"}], 1, 0, "café"),
                 ([{"n": 0, "chars": "é"}, {"n": 0, "chars": "à"}], 1, 0, "cafà"))
        for reply, resolved, refused, expected in cases:
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as tmp:
                source, output, report = [Path(tmp) / name for name in ("source.txt", "fixed.txt", "report.json")]
                source.write_text("caf� and naïve", encoding="utf-8")
                before = copy.deepcopy(reply)
                config = Path(tmp) / "config.json"
                config.write_text(json.dumps({"llm_local": {"model_name": "fixture"}}))
                response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
                with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": tmp}), \
                     patch("llm_provider.make_llm_client", return_value=client), \
                     patch.object(sys, "argv", ["repair_source_llm.py", str(source), "--apply", "--out", str(output), "--report", str(report)]), \
                     redirect_stdout(io.StringIO()):
                    repair_source_llm.main()
                data = json.loads(report.read_text())
                self.assertEqual((1, resolved, refused, 0), tuple(data[k] for k in ("runs_total", "runs_resolved", "runs_refused", "runs_unanswered")))
                self.assertEqual(refused, sum(data["refusal_reasons"].values()))
                self.assertEqual(expected + " and naïve", output.read_text(encoding="utf-8"))
                self.assertEqual(1 - resolved, data["replacement_chars_after"])
                self.assertEqual("caf� and naïve", source.read_text(encoding="utf-8"))
                self.assertEqual(before, reply)
