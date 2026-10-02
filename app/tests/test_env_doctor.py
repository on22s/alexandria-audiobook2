import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from env_doctor import evaluate_env


class EvaluateEnvTests(unittest.TestCase):
    def test_all_required_present_is_ok(self):
        spec = {"required": ["torch", "numpy"], "optional": [], "version_hint": {}}
        probe = {"torch": "2.10.0+rocm7.0", "numpy": "1.26.0"}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        statuses = {r["package"]: r["status"] for r in rows}
        self.assertEqual({"torch": "OK", "numpy": "OK"}, statuses)

    def test_required_missing_fails(self):
        spec = {"required": ["torch", "peft"], "optional": [], "version_hint": {}}
        probe = {"torch": "2.10.0+rocm7.0", "peft": None}
        rows, ok = evaluate_env(spec, probe)
        self.assertFalse(ok)
        peft_row = next(r for r in rows if r["package"] == "peft")
        self.assertEqual("MISSING", peft_row["status"])

    def test_optional_missing_passes_with_note(self):
        spec = {
            "required": ["torch"],
            "optional": ["pyannote.audio"],
            "version_hint": {},
        }
        probe = {"torch": "2.10.0+rocm7.0", "pyannote.audio": None}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        opt_row = next(r for r in rows if r["package"] == "pyannote.audio")
        self.assertEqual("OPTIONAL-MISSING", opt_row["status"])
        self.assertIn("diarization unavailable", opt_row["note"])

    def test_optional_missing_without_note_still_passes(self):
        spec = {"required": [], "optional": ["seaborn"], "version_hint": {}}
        probe = {"seaborn": None}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        self.assertEqual("OPTIONAL-MISSING", rows[0]["status"])
        self.assertEqual("", rows[0]["note"])

    def test_interpreter_not_found_fails_all_required(self):
        spec = {
            "required": ["torch", "librosa"],
            "optional": ["matplotlib"],
            "version_hint": {},
        }
        rows, ok = evaluate_env(spec, None)
        self.assertFalse(ok)
        required_rows = [r for r in rows if r["expected"] == "required"]
        self.assertTrue(all(r["status"] == "MISSING" for r in required_rows))
        optional_rows = [r for r in rows if r["expected"] == "optional"]
        self.assertTrue(all(r["status"] == "OPTIONAL-MISSING" for r in optional_rows))
        self.assertFalse(ok)

    def test_version_hint_mismatch_adds_note_but_still_ok(self):
        spec = {
            "required": ["torch"],
            "optional": [],
            "version_hint": {"torch": "2.10.0+rocm"},
        }
        probe = {"torch": "2.4.0+cu121"}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        self.assertEqual("OK", rows[0]["status"])
        self.assertIn("expected version prefix", rows[0]["note"])

    def test_version_hint_match_has_no_note(self):
        spec = {
            "required": ["torch"],
            "optional": [],
            "version_hint": {"torch": "2.10.0+rocm"},
        }
        probe = {"torch": "2.10.0+rocm7.0"}
        rows, ok = evaluate_env(spec, probe)
        self.assertTrue(ok)
        self.assertEqual("", rows[0]["note"])


if __name__ == "__main__":
    unittest.main()


class InterpreterImportHealthTests(unittest.TestCase):
    def test_real_probe_and_doctor_reject_installed_package_with_broken_import(self):
        import contextlib
        import io
        import json
        import os
        import tempfile
        from unittest.mock import patch
        import env_doctor as doctor
        cases = (("__version__ = '9.9'", True, "1.2.3", True),
                 ("raise OSError('fixture native extension failed')", True, None, False),
                 ("__version__ = '9.9'", False, "9.9", True))
        for source, metadata, expected, healthy in cases:
            with self.subTest(source=source, metadata=metadata), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                package = root / "fixture_runtime"
                package.mkdir()
                module = package / "__init__.py"
                module.write_text(source)
                if metadata:
                    dist = root / "fixture_runtime-1.2.3.dist-info"
                    dist.mkdir()
                    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: fixture_runtime\nVersion: 1.2.3\n")
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                spec = {"fixture_env": {"path": sys.executable, "required": ["fixture_runtime"], "optional": [], "version_hint": {}}}
                with patch.dict(os.environ, {"PYTHONPATH": tmp, "PYTHONDONTWRITEBYTECODE": "1"}), \
                     patch.object(doctor, "ENV_SPECS", spec), \
                     contextlib.redirect_stdout(io.StringIO()) as log:
                    probe = doctor.probe_interpreter(sys.executable, ["fixture_runtime"])
                    self.assertEqual({"fixture_runtime": expected}, probe)
                    code = doctor.main(["--json"])
                report = json.loads(log.getvalue())
                self.assertEqual(0 if healthy else 1, code)
                self.assertIs(healthy, report["ok"])
                row = report["envs"]["fixture_env"]["rows"][0]
                self.assertEqual(expected, row["found"])
                self.assertEqual("OK" if healthy else "MISSING", row["status"])
                after = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                self.assertEqual(before, after)


class VoiceLabInterpreterResolutionTests(unittest.TestCase):
    def test_doctor_and_actual_app_loader_resolve_same_config_without_mutation(self):
        import copy
        import io
        import json
        import os
        import tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch
        import core
        import env_doctor as doctor
        for override in (None, '/fixture/env/python', ''):
            for payload in (None, {}, {'rocm_python': '/fixture/saved/python'},
                            {'rocm_python': ''}, {'rocm_python': None}, {'rocm_python': 7}, [], '{'):
                with self.subTest(override=override, payload=payload), tempfile.TemporaryDirectory() as tmp:
                    config = Path(tmp, 'voicelab_config.json')
                    if payload is not None:
                        config.write_text(payload if payload == '{' else json.dumps(payload))
                    before = config.read_bytes() if config.exists() else None
                    env = dict(os.environ)
                    env.pop('ALEXANDRIA_ROCM_PYTHON', None)
                    if override is not None:
                        env['ALEXANDRIA_ROCM_PYTHON'] = override
                    defaults = copy.deepcopy(core.VOICELAB_DEFAULTS)
                    defaults['rocm_python'] = override if override is not None else sys.executable
                    with patch.dict(os.environ, env, clear=True), \
                         patch.object(core, 'VOICELAB_CONFIG_PATH', str(config)), \
                         patch.object(core, 'VOICELAB_DEFAULTS', defaults), \
                         patch.object(doctor, 'VOICELAB_CONFIG_PATH', str(config)):
                        expected = core._load_voicelab_config()['rocm_python']
                        self.assertEqual(expected, doctor.resolve_rocm_python_path())
                        spec = {'rocm_python': {'path': None, 'required': ['fixture'], 'optional': [], 'version_hint': {}}}
                        with patch.object(doctor, 'probe_interpreter', return_value={'fixture': '1'}) as probe, \
                             patch.object(doctor, 'ENV_SPECS', spec), redirect_stdout(io.StringIO()) as output:
                            code = doctor.main(['--json'])
                        report = json.loads(output.getvalue())
                        self.assertEqual(expected, report['envs']['rocm_python']['path'])
                        if expected:
                            probe.assert_called_once_with(expected, ['fixture'])
                            self.assertEqual(0, code)
                        else:
                            probe.assert_not_called()
                            self.assertEqual(1, code)
                    self.assertEqual(before, config.read_bytes() if config.exists() else None)


class QwenRuntimeRequirementTests(unittest.TestCase):
    def test_native_probe_and_real_spec_require_qwen_import_and_distribution_version(self):
        import contextlib
        import io
        import json
        import os
        import tempfile
        from unittest.mock import patch
        import env_doctor as doctor
        for healthy in (True, False):
            with self.subTest(healthy=healthy), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                # Isolate all declared imports without initializing real ML modules.
                packages = [*doctor.ENV_SPECS['app_env']['required'], 'qwen-tts']
                for package in packages:
                    module = doctor._IMPORT_NAME_OVERRIDES.get(package, package)
                    (root / (module + '.py')).write_text("__version__ = 'fixture'\n")
                (root / 'qwen_tts.py').write_text('' if healthy else
                    "raise ModuleNotFoundError('qwen_tts unavailable')\n")
                dist = root / 'qwen_tts-0.1.1.dist-info'; dist.mkdir()
                (dist / 'METADATA').write_text('Metadata-Version: 2.1\nName: qwen-tts\nVersion: 0.1.1\n')
                spec = {'app_env': {**doctor.ENV_SPECS['app_env'], 'path': sys.executable}}
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                output = io.StringIO()
                with patch.dict(os.environ, {'PYTHONPATH': tmp, 'PYTHONDONTWRITEBYTECODE': '1'}), \
                     patch.object(doctor, 'ENV_SPECS', spec), contextlib.redirect_stdout(output):
                    result = doctor.main(['--json'])
                report = json.loads(output.getvalue())
                self.assertEqual(0 if healthy else 1, result)
                self.assertIs(healthy, report['ok'])
                qwen = next(row for row in report['envs']['app_env']['rows'] if row['package'] == 'qwen-tts')
                self.assertEqual('OK' if healthy else 'MISSING', qwen['status'])
                self.assertEqual('0.1.1' if healthy else None, qwen['found'])
                self.assertEqual(before, {p.relative_to(root): p.read_bytes()
                                         for p in root.rglob('*') if p.is_file()})
