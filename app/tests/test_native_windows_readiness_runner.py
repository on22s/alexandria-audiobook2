"""Fault injection for retained, fail-closed native qualification evidence."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import native_windows_readiness_checks as gate


class NativeReadinessEvidenceTests(unittest.TestCase):
    def invoke(self, *, group='portable', suite=None, setup_error=None, git_error=None, platform='win32'):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report.json'
            with patch.object(gate.sys, 'platform', platform), \
                 patch.object(gate.subprocess, 'check_output', return_value='a'*40,
                              side_effect=git_error) as git, \
                 patch.object(gate.unittest.defaultTestLoader, 'loadTestsFromNames',
                              return_value=suite, side_effect=setup_error) as loader, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = gate.main(['--suite-group', group, '--json-report', str(report)])
            return status, json.loads(report.read_text()), git.call_count, loader.call_args

    def test_success_retains_checkout_group_and_exact_execution_count(self):
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        status, report, _, names = self.invoke(suite=suite)
        self.assertEqual(0, status)
        self.assertTrue(report['success'])
        self.assertEqual('a'*40, report['checkout_sha'])
        self.assertEqual((1, 1, 0, 0, 0), tuple(report[k] for k in
                         ('expected_tests', 'tests_run', 'failures', 'errors', 'skips')))
        self.assertEqual((gate.SUITES,), names.args)

    def test_import_exceptions_retain_failed_setup_evidence(self):
        for error in (ImportError('missing fixture dependency'), RuntimeError('module import failed')):
            with self.subTest(error=type(error).__name__):
                status, report, _, _ = self.invoke(setup_error=error)
                self.assertEqual(1, status)
                self.assertFalse(report['success'])
                self.assertEqual(1, report['errors'])
                self.assertIn(type(error).__name__, report['setup_error'])

    def test_git_failure_retains_unbound_failed_evidence(self):
        status, report, _, _ = self.invoke(git_error=subprocess.CalledProcessError(1, ['git']))
        self.assertEqual(1, status)
        self.assertIsNone(report['checkout_sha'])
        self.assertFalse(report['success'])
        self.assertIn('CalledProcessError', report['setup_error'])

    def test_neither_portable_nor_symlink_group_can_pass_with_a_skip(self):
        def skipped_probe():
            raise unittest.SkipTest('synthetic evidence protocol probe')
        for group, expected_names in (('portable', gate.SUITES), ('symlink', gate.SYMLINK_SUITES)):
            with self.subTest(group=group):
                suite = unittest.TestSuite([unittest.FunctionTestCase(skipped_probe)])
                status, report, _, names = self.invoke(group=group, suite=suite)
                self.assertEqual(1, status)
                self.assertFalse(report['success'])
                self.assertEqual((1, 1), (report['tests_run'], report['skips']))
                self.assertEqual((expected_names,), names.args)

    def test_empty_suite_cannot_certify_execution(self):
        status, report, _, _ = self.invoke(suite=unittest.TestSuite())
        self.assertEqual(1, status)
        self.assertEqual(0, report['tests_run'])
        self.assertFalse(report['success'])

    def test_wrong_host_retains_refusal_without_git_or_suite_loading(self):
        status, report, git_calls, loaded = self.invoke(platform='linux')
        self.assertEqual(2, status)
        self.assertEqual(0, git_calls)
        self.assertIsNone(loaded)
        self.assertFalse(report['success'])
        self.assertIn('nothing was verified', report['setup_error'])


class AdapterFixtureStartupTests(unittest.TestCase):
    def test_dependencies_load_before_ready_and_transaction(self):
        from tests.test_support import _run_adapter_fixture_process
        sequence = []
        def imported(name, *args, **kwargs):
            sequence.append(name)
        ready = Mock()
        ready.set.side_effect = lambda: sequence.append('ready')
        with patch('builtins.__import__', side_effect=imported):
            _run_adapter_fixture_process(ready, lambda value: sequence.append(value), ('transaction',))
        self.assertEqual(['peft', 'safetensors', 'ready', 'transaction'], sequence)

    def test_dependency_failure_never_signals_ready_or_runs_transaction(self):
        from tests.test_support import _run_adapter_fixture_process
        ready, target = Mock(), Mock()
        with patch('builtins.__import__', side_effect=ImportError('missing dependency')):
            with self.assertRaises(ImportError):
                _run_adapter_fixture_process(ready, target, ())
        ready.set.assert_not_called()
        target.assert_not_called()

    def test_startup_timeout_kills_and_joins_worker_instead_of_certifying_it(self):
        from tests.test_support import start_adapter_fixture_process
        context = Mock()
        context.Event.return_value.wait.return_value = False
        process = context.Process.return_value
        process.exitcode = -1
        with patch('multiprocessing.get_context', return_value=context), \
             patch('time.monotonic', side_effect=[0, 60, 60]):
            with self.assertRaisesRegex(AssertionError, 'timed out initializing'):
                start_adapter_fixture_process(Mock())
        process.start.assert_called_once_with()
        process.kill.assert_called_once_with()
        process.join.assert_called_once_with(5)

    def test_startup_handshake_returns_worker_for_callers_operation_deadline(self):
        from tests.test_support import start_adapter_fixture_process
        context = Mock()
        context.Event.return_value.wait.return_value = True
        with patch('multiprocessing.get_context', return_value=context):
            process = start_adapter_fixture_process(Mock())
        self.assertIs(process, context.Process.return_value)
        process.join.assert_not_called()
        process.kill.assert_not_called()


class EvidenceWriteFailureTests(unittest.TestCase):
    def test_unwritable_report_preserves_primary_refusal_and_cannot_pass(self):
        for platform, expected_status in (('win32', 1), ('linux', 2)):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
                with patch.object(gate.sys, 'platform', platform), \
                     patch.object(gate.subprocess, 'check_output', return_value='a'*40), \
                     patch.object(gate.unittest.defaultTestLoader, 'loadTestsFromNames', return_value=suite), \
                     contextlib.redirect_stdout(io.StringIO()) as output, \
                     contextlib.redirect_stderr(io.StringIO()):
                    status = gate.main(['--json-report', tmp])
                self.assertEqual(expected_status, status)
                report = json.loads(output.getvalue().split('NATIVE_WINDOWS_READINESS=')[-1])
                self.assertFalse(report['success'])
                self.assertIn('report_error', report)
                if platform == 'linux':
                    self.assertIn('nothing was verified', report['setup_error'])

    def test_exited_worker_reports_exit_instead_of_a_timeout(self):
        from tests.test_support import start_adapter_fixture_process
        context = Mock()
        context.Event.return_value.wait.return_value = False
        process = context.Process.return_value
        process.is_alive.return_value = False
        process.exitcode = 1
        with patch('multiprocessing.get_context', return_value=context):
            with self.assertRaisesRegex(AssertionError, 'exited before initialization.*exitcode=1'):
                start_adapter_fixture_process(Mock())
        process.kill.assert_not_called()
        process.join.assert_called_once_with(5)


class PortabilityDependencyEncodingTests(unittest.TestCase):
    def test_actual_workflow_reads_utf8_requirements_under_ansi_default(self):
        import textwrap
        root = Path(__file__).resolve().parents[2]
        workflow = (root / '.github/workflows/tests.yml').read_text(encoding='utf-8')
        step = workflow.split('      - name: Install CPU-only portability test dependencies', 1)[1]
        script = textwrap.dedent(step.split("@'", 1)[1].split("'@ | python -", 1)[0])
        # Match the hosted Windows ANSI default, including an invalid CP1252 byte.
        payload = (root / 'app/requirements.txt').read_bytes() + b'\n# UTF-8: \xe3\x81\x82\n'
        def read_text(path, encoding=None, **kwargs):
            self.assertEqual('requirements.txt', str(path))
            return payload.decode(encoding or 'cp1252')
        with patch.object(Path, 'read_text', autospec=True, side_effect=read_text), \
             patch.object(Path, 'write_text', autospec=True) as write:
            exec(compile(script, '<workflow dependency selection>', 'exec'), {})
        output = write.call_args.args[1].splitlines()
        self.assertIn('numpy==2.2.6', output)
        self.assertTrue(any(line.startswith('peft==') for line in output))
        self.assertFalse(any(line.startswith('torch') for line in output))


def get_native_directory_spelling(case, root):
    import os
    if os.name != 'nt':
        return root / '..' / root.name
    import ctypes
    api = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
    api.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    api.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    size = api(str(root), buffer, len(buffer))
    case.assertTrue(0 < size < len(buffer), 'Native short path lookup failed')
    alias = Path(buffer.value)
    case.assertNotEqual(alias, root, 'Native qualification requires distinct DOS short names')
    case.assertTrue(os.path.samefile(alias, root))
    return alias


class NativePathSpellingTests(unittest.TestCase):
    def test_zip_publication_accepts_native_short_directory_spelling(self):
        import alexandria_zip_publication as publication
        with tempfile.TemporaryDirectory(prefix='aa2 portability long directory ') as tmp:
            alias = get_native_directory_spelling(self, Path(tmp).resolve())
            output = alias / 'dataset.zip'
            output.write_bytes(b'original archive')
            with publication.ensure_zip_sources_readable([output]):
                self.assertEqual(b'original archive', output.read_bytes())

    def test_checkpoint_crash_fixture_matches_native_short_directory_spelling(self):
        from tests.test_adapter_checkpoint_transaction import (
            AdapterCheckpointTransactionTests, _crash_save, _files)
        with tempfile.TemporaryDirectory(prefix='aa2 checkpoint long directory ') as tmp:
            alias = get_native_directory_spelling(self, Path(tmp).resolve())
            helper = AdapterCheckpointTransactionTests()
            adapter = helper.fixture(alias)
            before = _files(adapter)
            self.assertEqual(79, helper.child(_crash_save, adapter, 'replacement'))
            helper.recover(adapter)
            self.assertEqual(before, _files(adapter))

    def test_zip_publication_refuses_links_in_each_directory_component(self):
        import stat
        from types import SimpleNamespace
        import alexandria_zip_publication as publication
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            parent = root / 'nested'
            parent.mkdir()
            output = parent / 'dataset.zip'
            output.write_bytes(b'original archive')
            original = Path.lstat
            for target in (root, parent, output):
                for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
                    with self.subTest(component=target.name, mode=mode, attributes=attributes):
                        def lstat(path):
                            if path == target:
                                return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                            return original(path)
                        with patch.object(Path, 'lstat', autospec=True, side_effect=lstat), \
                             patch.object(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400, create=True):
                            with self.assertRaisesRegex(publication.ZipPublicationError, 'symlink'):
                                with publication.ensure_zip_sources_readable([output]):
                                    self.fail('Linked directory was admitted')

    def test_zip_publication_refuses_native_directory_links(self):
        import os
        import stat
        import alexandria_zip_publication as publication
        with tempfile.TemporaryDirectory(prefix='aa2 native linked directory ') as tmp:
            root = Path(tmp).resolve()
            target = root / 'real'
            target.mkdir()
            (target / 'nested').mkdir()
            source = root / 'source.zip'
            source.write_bytes(b'original archive')
            link = root / 'linked.zip'
            if os.name == 'nt':
                made = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                                      capture_output=True, text=True)
                self.assertEqual(0, made.returncode, made.stdout + made.stderr)
                self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            else:
                link.symlink_to(target, target_is_directory=True)
                self.assertTrue(link.is_symlink())
            try:
                for output in (link, link / 'dataset.zip', link / 'nested' / 'dataset.zip'):
                    with self.subTest(output=str(output)):
                        with self.assertRaisesRegex(publication.ZipPublicationError, 'symlink'):
                            with publication.ensure_zip_sources_readable([output]):
                                self.fail('Native linked source was admitted')
                        with self.assertRaisesRegex(publication.ZipPublicationError, 'symlink'):
                            with publication.ensure_zip_sources_readable([source], destination=output):
                                self.fail('Native linked destination was admitted')
                self.assertEqual(b'original archive', source.read_bytes())
                self.assertEqual(['nested'], [path.name for path in target.iterdir()])
            finally:
                if os.name == 'nt':
                    link.rmdir()
                else:
                    link.unlink()

    def test_zip_source_aliases_share_admission_and_refuse_self_merge(self):
        import alexandria_zip_publication as publication
        with tempfile.TemporaryDirectory(prefix='aa2 zip alias collision ') as tmp:
            root = Path(tmp).resolve()
            alias = get_native_directory_spelling(self, root)
            output = root / 'dataset.zip'
            output.write_bytes(b'original archive')
            spelled = alias / output.name
            for source, destination in ((spelled, output), (output, spelled)):
                with self.subTest(source=str(source)):
                    with self.assertRaisesRegex(publication.ZipPublicationError, 'source ZIP family'):
                        with publication.ensure_zip_sources_readable([source], destination=destination):
                            self.fail('Source alias was admitted as a merge destination')
            with patch.object(publication, 'ensure_zip_family_readable',
                              wraps=publication.ensure_zip_family_readable) as admit:
                with publication.ensure_zip_sources_readable([output, spelled]):
                    self.assertEqual(1, admit.call_count)
            self.assertEqual(b'original archive', output.read_bytes())
