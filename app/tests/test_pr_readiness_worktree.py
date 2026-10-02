import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mark_pr_ready as ready


class PrReadinessWorktreeTests(unittest.TestCase):
    def make_repo(self, root):
        subprocess.run(['git', 'init', '--quiet', str(root)], check=True)
        (root / 'tracked.py').write_text('value = 1\n')
        (root / '.gitignore').write_text('runtime/\n')
        subprocess.run(['git', 'add', '.'], cwd=root, check=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', 'fixture'], cwd=root, check=True)

    def invoke(self, root):
        real_run = subprocess.run
        def local_git(command, **kwargs):
            self.assertEqual('git', command[0], 'readiness must refuse before any GitHub call')
            return real_run(command, cwd=root, capture_output=True, text=True)
        fake_verifier = subprocess.CompletedProcess([], 1, '', '')
        with patch.object(ready, 'run', side_effect=local_git), \
             patch.object(ready.subprocess, 'run', return_value=fake_verifier) as verifier, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            result = ready.main([])
        return result, verifier.call_count, errors.getvalue()

    def test_untracked_python_and_nested_config_refuse_before_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.make_repo(root)
            for name in ('untracked.py', 'nested/config.json'):
                with self.subTest(name=name):
                    path = root / name; path.parent.mkdir(exist_ok=True)
                    path.write_text('fixture')
                    result, calls, error = self.invoke(root)
                    self.assertEqual(1, result)
                    self.assertEqual(0, calls)
                    self.assertIn('worktree changes', error)
                    path.unlink()

    def test_tracked_changes_still_refuse_before_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.make_repo(root)
            (root / 'tracked.py').write_text('value = 2\n')
            result, calls, _ = self.invoke(root)
            self.assertEqual(1, result); self.assertEqual(0, calls)

    def test_clean_and_ignored_runtime_trees_reach_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.make_repo(root)
            for ignored in (False, True):
                with self.subTest(ignored=ignored):
                    if ignored:
                        (root / 'runtime').mkdir(); (root / 'runtime' / 'fixture.log').write_text('fixture')
                    result, calls, error = self.invoke(root)
                    self.assertEqual(1, result)
                    self.assertEqual(1, calls)
                    self.assertIn('local release verification failed', error)
