"""Execute the notebook installer without network or package mutations."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


class ColabInstallationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent.parent.parent
        notebook = json.loads((root / 'alexandria_colab.ipynb').read_text())
        self.source = ''.join(notebook['cells'][4]['source'])

    def run_installer(self, existing, fail_at=None):
        calls = []
        output = io.StringIO()

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if len(calls) == fail_at:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(os.path, 'exists', return_value=existing), \
             patch.object(subprocess, 'run', side_effect=run), \
             contextlib.redirect_stdout(output):
            try:
                exec(compile(self.source, 'colab-install-cell', 'exec'), {})
            except subprocess.CalledProcessError as error:
                return calls, output.getvalue(), error
        return calls, output.getvalue(), None

    def test_fresh_clone_installs_one_joint_requirement_set_in_kernel_python(self):
        calls, output, error = self.run_installer(False)
        self.assertIsNone(error)
        self.assertEqual(['git', 'clone', 'https://github.com/on22s/alexandria-audiobook2.git',
                          '/content/Alexandria'], calls[0][0])
        self.assertEqual([sys.executable, '-m', 'pip', 'install', '-r',
                          '/content/Alexandria/app/requirements.txt',
                          'qwen-tts==0.1.1', 'pyngrok'], calls[1][0])
        self.assertEqual(2, len(calls))
        self.assertTrue(all(kwargs == {'check': True} for argv, kwargs in calls))
        self.assertIn('Installation complete.', output)

    def test_existing_checkout_updates_before_installing(self):
        calls, output, error = self.run_installer(True)
        self.assertIsNone(error)
        self.assertEqual(['git', '-C', '/content/Alexandria', 'pull', '--ff-only'], calls[0][0])
        self.assertEqual(2, len(calls))
        self.assertIn('Installation complete.', output)

    def test_git_failure_stops_before_pip_and_completion(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                calls, output, error = self.run_installer(existing, fail_at=1)
                self.assertIsInstance(error, subprocess.CalledProcessError)
                self.assertEqual(1, len(calls))
                self.assertNotIn('Installation complete.', output)

    def test_pip_failure_is_not_followed_by_other_installs_or_success(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                calls, output, error = self.run_installer(existing, fail_at=2)
                self.assertIsInstance(error, subprocess.CalledProcessError)
                self.assertEqual(2, len(calls))
                self.assertIn('-r', error.cmd)
                self.assertNotIn('Installation complete.', output)


if __name__ == '__main__':
    unittest.main()
