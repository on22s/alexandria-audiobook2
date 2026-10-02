import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_experiment_infra import stage_index_scripts, run_index_script

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools/audit/collect_results.py'


class ResultsIndexImportTests(unittest.TestCase):
    def test_import_does_not_parse_arguments_read_artifacts_or_write_outputs(self):
        import builtins
        import argparse
        spec = importlib.util.spec_from_file_location('collector_import_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'argv', ['unrelated', '--foreign-argument']), \
             patch.object(argparse.ArgumentParser, 'parse_args', side_effect=AssertionError('parsed')), \
             patch.object(builtins, 'open', side_effect=AssertionError('file read or write')), \
             patch.object(subprocess, 'run', side_effect=AssertionError('subprocess')):
            spec.loader.exec_module(module)
            self.assertTrue(module._only_added_commit_filled_in(
                'artifact,added_commit\nx,\n', 'artifact,added_commit\nx,abc\n'))
        self.assertTrue(callable(module.main))

    def test_native_cli_groups_new_books_from_metadata_and_preserves_row_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_index_scripts(tmp)
            audit = root / 'ab_test_runtime/audit'; audit.mkdir(parents=True)
            experiments = root / 'ab_test_runtime/experiments'; experiments.mkdir()
            for name in ('artifact_structural_audit.json', 'legacy_attribution_audit.json'):
                (audit / name).write_text('{"artifacts":[]}')
            cases = [
                ('fifth', {'gold_path': 'app/fixtures/attribution_gold_newbook.json'}, 'newbook'),
                ('provisional', {'gold_path': 'app/fixtures/attribution_gold_fifth-volume_provisional.json'}, 'fifth-volume'),
                ('explicit', {'book': 'recorded-book', 'gold_path': 'attribution_gold_other.json'}, 'recorded-book'),
                ('historical', {'gold_path': 'app/fixtures/attribution_gold_random.json'}, 'mushoku16'),
                ('directory', {'gold_path': '/grimgar03/attribution_gold_newbook.json'}, 'newbook'),
                ('windows', {'gold_path': r'C:\fixtures\attribution_gold_newbook.json'}, 'newbook'),
                ('row', {'gold_path': 'attribution_gold_first.json'}, 'row-book'),
            ]
            for name, metadata, _expected in cases:
                metadata.update(experiment='probe', git={})
                rows = [{'arm':'baseline', 'correct':True}]
                if name == 'row': rows[0]['id'] = 'row-book:line-1'
                (experiments / (name + '.json')).write_text(json.dumps({'meta':metadata,'rows':rows}))
            command = [sys.executable, 'tools/audit/collect_results.py']
            result = run_index_script(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            with (root / 'results_index.csv').open() as handle:
                indexed = {row['artifact']:row for row in csv.DictReader(handle)}
            for name, _metadata, expected in cases:
                self.assertEqual(expected, indexed[name + '.json']['book'], name)
            checked = run_index_script([*command, '--check'], cwd=root, capture_output=True, text=True)
            self.assertEqual(0, checked.returncode, checked.stderr)

class ArtifactMembershipFailureTests(unittest.TestCase):
    def test_git_faults_refuse_membership_instead_of_admitting_local_evidence(self):
        from tools.audit import audit_experiment_artifacts as audit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'local-only.json').write_text('{"rows":[]}')
            before = (root / 'local-only.json').read_bytes()
            faults = [PermissionError('permission denied'), FileNotFoundError('no git'),
                      subprocess.TimeoutExpired(['git', 'ls-files'], 10)]
            for fault in faults:
                with self.subTest(fault=type(fault).__name__), \
                     patch.object(audit.subprocess, 'run', side_effect=fault):
                    with self.assertRaisesRegex(RuntimeError, 'Cannot determine tracked'):
                        audit.indexable_artifacts(tmp)
            failed = subprocess.CompletedProcess(['git'], 128, b'', b'index unavailable')
            with patch.object(audit.subprocess, 'run', return_value=failed):
                with self.assertRaisesRegex(RuntimeError, 'Git exited 128: index unavailable'):
                    audit.indexable_artifacts(tmp)
            self.assertEqual(before, (root / 'local-only.json').read_bytes())

    def test_native_cli_git_timeout_preserves_both_existing_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_index_scripts(tmp)
            audit = root / 'ab_test_runtime/audit'; audit.mkdir(parents=True)
            experiments = root / 'ab_test_runtime/experiments'; experiments.mkdir()
            for name in ('artifact_structural_audit.json', 'legacy_attribution_audit.json'):
                (audit / name).write_text('{"artifacts":[]}')
            (experiments / 'private-local.json').write_text('{"rows":[]}')
            originals = {'results_index.csv': b'prior csv\n', 'RESULTS_INDEX.md': b'prior md\n'}
            structural = audit / 'artifact_structural_audit.json'
            originals[str(structural.relative_to(root))] = structural.read_bytes()
            for name, data in originals.items(): (root / name).write_bytes(data)
            harness = '''import runpy, subprocess, sys
real_run = subprocess.run
def run(command, *args, **kwargs):
    if command[:2] == ["git", "ls-files"]:
        raise subprocess.TimeoutExpired(command, 10)
    return real_run(command, *args, **kwargs)
subprocess.run = run
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
            for script in ('tools/audit/collect_results.py', 'tools/audit/audit_experiment_artifacts.py'):
                result = subprocess.run([sys.executable, '-c', harness, script], cwd=root,
                                        capture_output=True, text=True, timeout=20)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('Cannot determine tracked artifact membership', result.stderr)
                for name, data in originals.items():
                    self.assertEqual(data, (root / name).read_bytes(), (script, name))
                self.assertEqual('{"rows":[]}', (experiments / 'private-local.json').read_text())
