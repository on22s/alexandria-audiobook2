import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import plan_dialogue_continuity as experiment
from tests import test_dialogue_continuity as fixtures


class ContinuityExperimentTests(unittest.TestCase):
    def source(self, root):
        path = root / 'input.json'
        path.write_text(json.dumps({'version': 1, 'narrator_speakers': ['NARRATOR'],
                                   'entries': fixtures.DialogueContinuityTests().rows()}))
        return path

    def test_actual_cli_exports_bound_exact_text_plan_without_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.source(root); output = root / 'plan.json'; before = source.read_bytes()
            result = subprocess.run([sys.executable, '-m', 'experiments.plan_dialogue_continuity',
                '--input', str(source), '--out', str(output)], cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('planned', json.loads(result.stdout)['status'])
            packet = json.loads(output.read_bytes())
            self.assertEqual(hashlib.sha256(before).hexdigest(), packet['source_sha256'])
            self.assertEqual(64, len(packet['planner_loaded_code_sha256']))
            self.assertEqual(['I will go,', 'Anna said,', 'when the rain stops.'],
                             [r['deterministic']['text'] for r in packet['plan']['rows']])
            self.assertEqual(before, source.read_bytes())

    def test_existing_output_input_alias_and_invalid_input_preserve_prior_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.source(root); before = source.read_bytes()
            output = root / 'prior.json'; output.write_bytes(b'Prior plan')
            with self.assertRaises(FileExistsError):
                experiment.save_continuity_experiment_plan(source, output)
            with self.assertRaises(ValueError):
                experiment.save_continuity_experiment_plan(source, source)
            self.assertEqual(before, source.read_bytes()); self.assertEqual(b'Prior plan', output.read_bytes())
            for raw in (b'[' * 2000 + b']' * 2000, b'{}', b'\xff', b'{"version":true}'):
                with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                    experiment.get_continuity_experiment_packet(raw)

    def test_bare_relative_cli_output_succeeds_and_preserves_existing_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.source(root)
            script = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
                      'from experiments.plan_dialogue_continuity import main; raise SystemExit(main())')
            command = [sys.executable, '-c', script, str(Path(__file__).resolve().parents[1]),
                       '--input', 'input.json', '--out', 'plan.json']
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('planned', json.loads(result.stdout)['status'])
            self.assertTrue((root / 'plan.json').is_file())
            if os.name != 'nt':
                output = root / 'dangling.json'; target = root / 'missing.json'
                output.symlink_to(target)
                with self.assertRaises(FileExistsError):
                    experiment.save_continuity_experiment_plan(root / 'input.json', output)
                self.assertTrue(output.is_symlink())
                self.assertFalse(target.exists())

    def test_changed_input_and_failed_publication_do_not_leave_a_partial_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.source(root); output = root / 'plan.json'
            build = experiment.get_continuity_experiment_packet
            def changed(raw):
                result = build(raw); source.write_bytes(b'Changed input'); return result
            with patch.object(experiment, 'get_continuity_experiment_packet', side_effect=changed):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    experiment.save_continuity_experiment_plan(source, output)
            self.assertFalse(output.exists()); self.assertEqual(b'Changed input', source.read_bytes())
            source = self.source(root); replace = os.replace
            def fail(src, dst, *args, **kwargs):
                if Path(dst) == output:
                    raise OSError('fixture final plan publication failure')
                return replace(src, dst, *args, **kwargs)
            with patch('os.replace', side_effect=fail), self.assertRaises(OSError):
                experiment.save_continuity_experiment_plan(source, output)
            self.assertFalse(output.exists())

    def test_actual_output_limit_refuses_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.source(root); output = root / 'plan.json'
            with patch.object(experiment, 'MAX_PLAN_BYTES', 10), self.assertRaisesRegex(ValueError, '16 MiB'):
                experiment.save_continuity_experiment_plan(source, output)
            self.assertFalse(output.exists())
