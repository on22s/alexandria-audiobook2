"""Execute the notebook installer without network or package mutations."""
import ast
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

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            class Process:
                stdout = io.StringIO('command diagnostic\n')

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    self.stdout.close()

                def wait(self):
                    return 1 if len(calls) == fail_at else 0

            return Process()

        with patch.object(os.path, 'exists', return_value=existing), \
             patch.object(subprocess, 'Popen', side_effect=popen), \
             patch.object(subprocess, 'run', side_effect=AssertionError('Inherited output is not displayed by Colab')), \
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
        self.assertTrue(all(kwargs == {'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT,
                                      'encoding': 'utf-8', 'errors': 'replace'}
                            for argv, kwargs in calls))
        self.assertIn('Installation complete.', output)
        self.assertEqual(2, output.count('command diagnostic'))

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
                self.assertIn('command diagnostic', output)
                self.assertNotIn('Installation complete.', output)

    def test_pip_failure_is_not_followed_by_other_installs_or_success(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                calls, output, error = self.run_installer(existing, fail_at=2)
                self.assertIsInstance(error, subprocess.CalledProcessError)
                self.assertEqual(2, len(calls))
                self.assertIn('-r', error.cmd)
                self.assertEqual(2, output.count('command diagnostic'))
                self.assertNotIn('Installation complete.', output)

    def get_checked_install_command(self):
        functions = [node for node in ast.parse(self.source).body
                     if isinstance(node, ast.FunctionDef)
                     and node.name == 'run_checked_install_command']
        self.assertEqual(1, len(functions), 'Installer must route subprocess output through Python')
        namespace = {'subprocess': subprocess}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'colab-command-helper', 'exec'), namespace)
        return namespace['run_checked_install_command']

    def test_real_child_stdout_and_stderr_reach_python_output_in_order(self):
        run_command = self.get_checked_install_command()
        output = io.StringIO()
        script = "import os; os.write(1,b'child stdout\\n'); os.write(2,b'child stderr\\n')"
        with contextlib.redirect_stdout(output):
            run_command([sys.executable, '-c', script])
        self.assertEqual('child stdout\nchild stderr\n', output.getvalue())

    def test_real_child_failure_displays_diagnostics_and_preserves_exit_code(self):
        run_command = self.get_checked_install_command()
        output = io.StringIO()
        script = "import os,sys; os.write(1,b'package diagnostic\\n'); os.write(2,b'failure\\xff\\n'); sys.exit(9)"
        command = [sys.executable, '-c', script]
        with contextlib.redirect_stdout(output), self.assertRaises(subprocess.CalledProcessError) as caught:
            run_command(command)
        self.assertEqual(9, caught.exception.returncode)
        self.assertEqual(command, caught.exception.cmd)
        self.assertEqual('package diagnostic\nfailure\ufffd\n', output.getvalue())

    def test_interrupt_kills_and_reaps_the_running_child(self):
        run_command = self.get_checked_install_command()
        children = []
        real_popen = subprocess.Popen
        interruption = KeyboardInterrupt('synthetic notebook interruption')

        def start(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        class InterruptingOutput(io.StringIO):
            def write(self, value):
                if 'owned-ready' in value:
                    raise interruption
                return super().write(value)

        try:
            with patch.object(subprocess, 'Popen', side_effect=start), \
                 contextlib.redirect_stdout(InterruptingOutput()), \
                 self.assertRaises(KeyboardInterrupt) as caught:
                run_command([sys.executable, '-c',
                             "import os,time; os.write(1,b'owned-ready\\n'); time.sleep(20)"])
            self.assertIs(interruption, caught.exception)
            self.assertEqual(1, len(children))
            self.assertIsNotNone(children[0].poll(), 'Interrupted installer left its child running')
            self.assertNotEqual(0, children[0].returncode)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_cleanup_error_does_not_replace_the_original_output_error(self):
        run_command = self.get_checked_install_command()
        children = []
        real_popen = subprocess.Popen
        original = ValueError('synthetic closed notebook output')

        def start(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append((child, child.wait))
            def fail_wait(*_args, **_kwargs):
                raise OSError('synthetic wait failure')
            child.wait = fail_wait
            return child

        class FailingOutput(io.StringIO):
            def write(self, value):
                if 'owned-ready' in value:
                    raise original
                return super().write(value)

        try:
            with patch.object(subprocess, 'Popen', side_effect=start), \
                 contextlib.redirect_stdout(FailingOutput()), \
                 self.assertRaises(ValueError) as caught:
                run_command([sys.executable, '-c',
                             "import os,time; os.write(1,b'owned-ready\\n'); time.sleep(20)"])
            self.assertIs(original, caught.exception)
            self.assertIsInstance(caught.exception.__cause__, OSError)
        finally:
            for child, wait in children:
                if child.poll() is None:
                    child.kill()
                wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
