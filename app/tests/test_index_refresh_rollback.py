import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('refresh_indexes_test', ROOT / 'refresh_indexes.py')
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)
OUTPUTS = ['ab_test_runtime/audit/artifact_structural_audit.json',
           'ab_test_runtime/audit/legacy_attribution_audit.json',
           'LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md', 'RESULTS_INDEX.md', 'results_index.csv',
           'ab_test_runtime/audit/goal_evidence_audit.json']
SCRIPTS = ['tools/audit/audit_experiment_artifacts.py', 'tools/audit/audit_legacy_attribution.py',
           'tools/audit/collect_results.py', 'app/experiments/goal_evidence_audit.py']


class IndexRefreshRollbackTests(unittest.TestCase):
    def stage(self, root, fail_index=None, stale_index=None, script=ROOT / 'refresh_indexes.py'):
        shutil.copyfile(script, root / 'refresh_indexes.py')
        for index, relative in enumerate(SCRIPTS):
            path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
            produced = [OUTPUTS[0], *OUTPUTS[1:3], *OUTPUTS[3:5], OUTPUTS[5]]
            groups = [[produced[0]], produced[1:3], produced[3:5], [produced[5]]]
            path.write_text('import sys\nfrom pathlib import Path\n'
                + f'outputs={groups[index]!r}\n'
                + "if '--check' in sys.argv:\n"
                + f'    raise SystemExit({1 if stale_index == index else 0})\n'
                + "for name in outputs:\n"
                + "    p=Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'new index')\n"
                + f'raise SystemExit({1 if fail_index == index else 0})\n')
        before = {}
        for name in OUTPUTS[::2]:
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('old ' + name).encode()); path.chmod(0o640)
            before[name] = (path.read_bytes(), path.stat().st_mode & 0o777)
        (root / 'untouched.json').write_bytes(b'other evidence')
        return before

    def assert_restored(self, root, before):
        for name in OUTPUTS:
            path = root / name
            if name in before:
                self.assertEqual(before[name], (path.read_bytes(), path.stat().st_mode & 0o777), name)
            else:
                self.assertFalse(path.exists(), name)
        self.assertEqual(b'other evidence', (root / 'untouched.json').read_bytes())
        self.assertEqual([], list(root.glob('.index-refresh-*')))

    def test_native_failure_at_every_producer_restores_bytes_modes_and_absent_outputs(self):
        for failed in range(4):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); before = self.stage(root, fail_index=failed)
                result = subprocess.run([sys.executable, 'refresh_indexes.py'], cwd=root,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assert_restored(root, before)
                self.assertIn('prior index files restored', result.stderr)

    def test_native_failed_final_check_restores_and_success_keeps_all_new_outputs(self):
        for stale in (2, None):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); before = self.stage(root, stale_index=stale)
                result = subprocess.run([sys.executable, 'refresh_indexes.py'], cwd=root,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(3 if stale is not None else 0, result.returncode, result.stderr)
                if stale is not None:
                    self.assert_restored(root, before)
                else:
                    self.assertTrue(all((root / name).read_bytes() == b'new index' for name in OUTPUTS))
                    self.assertEqual([], list(root.glob('.index-refresh-*')))

    def test_check_only_preserves_existing_files_and_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); before = self.stage(root, stale_index=1)
            result = subprocess.run([sys.executable, 'refresh_indexes.py', '--check'], cwd=root,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(1, result.returncode)
            self.assert_restored(root, before)

    def test_rollback_failure_is_visible_retains_backup_and_restores_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); before = self.stage(root)
            real_replace = os.replace
            damaged = root / OUTPUTS[0]
            def replace(source, target):
                if Path(target) == damaged:
                    raise PermissionError('blocked restoration')
                return real_replace(source, target)
            def run(_script, check, _python):
                if not check: damaged.write_bytes(b'new index')
                return False, 'worker failed'
            stderr = io.StringIO()
            with patch.object(refresh, 'REPO', tmp), patch.object(refresh, 'run', side_effect=run), \
                 patch.object(refresh.os, 'replace', side_effect=replace), \
                 patch.object(sys, 'argv', ['refresh_indexes.py']), contextlib.redirect_stderr(stderr):
                self.assertEqual(4, refresh.main())
            backups = list(root.glob('.index-refresh-*'))
            self.assertEqual(1, len(backups))
            self.assertEqual(before[OUTPUTS[0]][0], (backups[0] / '0').read_bytes())
            self.assertIn('Index rollback FAILED', stderr.getvalue())
            self.assertIn('Recovery backups retained', stderr.getvalue())
            self.assertEqual(before[OUTPUTS[2]][0], (root / OUTPUTS[2]).read_bytes())

    def test_launch_exception_after_first_producer_restores_before_propagating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); before = self.stage(root)
            actual_run = refresh.run
            def run(script, check, python):
                if script == SCRIPTS[1]:
                    raise OSError('launch failed')
                return actual_run(script, check, python)
            with patch.object(refresh, 'REPO', tmp), patch.object(refresh, 'run', side_effect=run), \
                 patch.object(sys, 'argv', ['refresh_indexes.py']), \
                 contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(OSError, 'launch failed'):
                    refresh.main()
            self.assert_restored(root, before)
