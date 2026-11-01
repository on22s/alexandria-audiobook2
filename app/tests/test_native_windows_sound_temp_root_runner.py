import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests import native_windows_sound_temp_root_checks as runner


class NativeSoundWorkerReceiptTests(unittest.TestCase):
    def run_worker(self, store_error, loaded_identity=True):
        alias = os.path.abspath('aliased-root')
        stdout = io.StringIO()
        store = mock.Mock(side_effect=[None, store_error])
        assets = types.ModuleType('sound_design_assets')
        assets.SoundDesignAssetStore = store
        result = types.SimpleNamespace(testsRun=runner.EXPECTED_TEST_COUNT,
                                       failures=[], errors=[], skipped=[],
                                       wasSuccessful=lambda: True)
        with mock.patch.dict(sys.modules, {'sound_design_assets': assets}), \
                mock.patch.object(runner, 'derived_test_ids', return_value=['source']), \
                mock.patch.object(runner, '_source_identity_matches',
                                  side_effect=[True, loaded_identity]), \
                mock.patch.object(runner, '_loaded_test_ids', return_value=['loaded']), \
                mock.patch.object(runner.tempfile, 'tempdir', None), \
                mock.patch.object(runner.tempfile, 'gettempdir', return_value=alias), \
                mock.patch.object(runner.tempfile, 'TemporaryDirectory') as temporary, \
                mock.patch.object(runner.os.path, 'realpath',
                                  side_effect=lambda path: str(path).replace('aliased-root', 'canonical-root')), \
                mock.patch.object(runner.unittest.defaultTestLoader,
                                  'loadTestsFromNames') as loader, \
                mock.patch.object(runner.unittest, 'TextTestRunner') as executor, \
                contextlib.redirect_stdout(stdout):
            temporary.return_value.__enter__.return_value = os.path.join(alias, 'child')
            executor.return_value.run.return_value = result
            try:
                status = runner._run_modules_under_alias(alias)
            finally:
                if store_error is None or not runner._store_refusal(str(store_error)):
                    loader.assert_not_called()
                    executor.assert_not_called()
            return status, json.loads(stdout.getvalue())

    def test_identity_mismatch_receipt_reports_the_measured_false_flag(self):
        status, receipt = self.run_worker(ValueError('without aliases'), False)
        self.assertEqual(status, 1)
        self.assertEqual(receipt['outcome'], 'identity-mismatch')
        self.assertIs(receipt['source_identity'], False)
        self.assertIs(receipt['store_refused_alias'], True)

    def test_success_receipt_reports_both_measured_flags(self):
        status, receipt = self.run_worker(ValueError('without aliases'))
        self.assertEqual(status, 0)
        self.assertEqual(receipt['outcome'], 'passed')
        self.assertIs(receipt['source_identity'], True)
        self.assertIs(receipt['store_refused_alias'], True)

    def test_store_acceptance_refuses_before_loading_the_suite(self):
        status, receipt = self.run_worker(None)
        self.assertEqual(status, 1)
        self.assertEqual(receipt['outcome'], 'store-accepted-alias')

    def test_unrelated_store_error_propagates_before_loading_the_suite(self):
        with self.assertRaisesRegex(ValueError, 'unrelated failure'):
            self.run_worker(ValueError('unrelated failure'))


class NativeSoundRunnerFailureTests(unittest.TestCase):
    def setUp(self):
        self.base = Path('base')
        self.target = self.base / 'canonical-root'
        self.alias = self.base / 'aliased-root'
        roots = mock.patch.object(runner, '_create_aliased_roots',
                                  return_value=(self.base, self.target, self.alias))
        roots.start()
        self.addCleanup(roots.stop)
        hostile = mock.patch.object(runner, '_verify_hostile_junction')
        self.verify_hostile = hostile.start()
        self.addCleanup(hostile.stop)
        cleanup = mock.patch.object(runner, '_remove_aliased_roots')
        self.cleanup = cleanup.start()
        self.addCleanup(cleanup.stop)

    def run_checks(self, **subprocess_result):
        stdout = io.StringIO()
        with mock.patch.object(runner.subprocess, 'run', **subprocess_result) as launch, \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            status = runner._run_checks()
        self.cleanup.assert_called_once_with(self.base, self.alias)
        self.verify_hostile.assert_called_once_with(
            self.base / runner.HOSTILE_SEGMENT,
            self.base / (runner.HOSTILE_SEGMENT + '-aliased-root'))
        self.assertEqual(status, 1)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        receipt = json.loads(lines[0])
        self.assertEqual(receipt['alias'], str(self.alias))
        self.assertEqual(receipt['target'], str(self.target))
        self.assertEqual(launch.call_args.kwargs['timeout'], 600)
        return receipt

    def test_timeout_emits_failure_after_verified_cleanup(self):
        dumps = json.dumps
        def refuse_before_cleanup(*args, **kwargs):
            self.cleanup.assert_called_once_with(self.base, self.alias)
            return dumps(*args, **kwargs)
        with mock.patch.object(runner.json, 'dumps', side_effect=refuse_before_cleanup):
            receipt = self.run_checks(side_effect=subprocess.TimeoutExpired(
                ['fixture-worker'], 600, output=b'{"outcome":"passed"}\n'))
        self.assertEqual(receipt['outcome'], 'child-timeout')
        self.assertEqual(receipt['timeout_seconds'], 600)

    def test_cleanup_failure_aborts_without_receipt(self):
        outcomes = (
            {'return_value': subprocess.CompletedProcess(
                ['fixture-worker'], 0, '{"outcome":"passed"}\n', '')},
            {'side_effect': subprocess.TimeoutExpired(['fixture-worker'], 600)},
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.cleanup.reset_mock()
                self.cleanup.side_effect = PermissionError('fixture cleanup failure')
                stdout = io.StringIO()
                with mock.patch.object(runner.subprocess, 'run', **outcome), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex(PermissionError, 'fixture cleanup failure'):
                        runner._run_checks()
                self.cleanup.assert_called_once_with(self.base, self.alias)
                self.assertEqual(stdout.getvalue(), '')

    def test_hostile_alias_removal_failure_still_attempts_base_cleanup(self):
        outcomes = (
            {'return_value': subprocess.CompletedProcess(
                ['fixture-worker'], 0, '{"outcome":"passed"}\n', '')},
            {'side_effect': subprocess.TimeoutExpired(['fixture-worker'], 600)},
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.cleanup.reset_mock()
                stdout = io.StringIO()
                with mock.patch.object(runner.subprocess, 'run', **outcome), \
                        mock.patch.object(runner.os.path, 'lexists', return_value=True), \
                        mock.patch.object(runner.os, 'rmdir', side_effect=PermissionError(
                            'fixture hostile alias removal failure')), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex(PermissionError, 'hostile alias removal'):
                        runner._run_checks()
                self.cleanup.assert_called_once_with(self.base, self.alias)
                self.assertEqual(stdout.getvalue(), '')

    def test_success_exit_without_receipt_is_refused(self):
        receipt = self.run_checks(return_value=subprocess.CompletedProcess(
            ['fixture-worker'], 0, 'ordinary output\nnot JSON\n', ''))
        self.assertEqual(receipt['outcome'], 'no-receipt')
        self.assertIsNone(receipt['child'])

    def test_nonzero_exit_cannot_be_hidden_by_passed_receipt(self):
        receipt = self.run_checks(return_value=subprocess.CompletedProcess(
            ['fixture-worker'], 1, '{"outcome":"passed"}\n', 'fixture failure'))
        self.assertEqual(receipt['outcome'], 'child-failed')
        self.assertEqual(receipt['child_exit'], 1)

    def test_hostile_junction_is_verified_before_the_child_and_removed_first(self):
        order = []
        def verify(target, alias):
            order.append(('verify', target, alias))
        def launch(*args, **kwargs):
            order.append(('launch',))
            return subprocess.CompletedProcess(['fixture-worker'], 0, 'ordinary output\n', '')
        def cleanup(base, alias):
            order.append(('cleanup', base, alias))
        hostile_target = self.base / runner.HOSTILE_SEGMENT
        hostile_alias = self.base / (runner.HOSTILE_SEGMENT + '-aliased-root')
        with mock.patch.object(runner, '_verify_hostile_junction', side_effect=verify), \
                mock.patch.object(runner.subprocess, 'run', side_effect=launch), \
                mock.patch.object(runner, '_remove_aliased_roots', side_effect=cleanup), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            runner._run_checks()
        self.assertEqual([('verify', hostile_target, hostile_alias), ('launch',),
                          ('cleanup', self.base, self.alias)], order)


class NativeSoundRunnerJunctionTests(unittest.TestCase):
    def setUp(self):
        self.base = Path('owned-base')
        self.target = self.base / 'canonical-root'
        self.alias = self.base / 'aliased-root'

    def test_native_primitive_receives_the_target_before_the_junction(self):
        fake = mock.Mock()
        with mock.patch.dict(sys.modules, {'_winapi': fake}):
            runner._create_junction(self.target, self.alias)
        fake.CreateJunction.assert_called_once_with(str(self.target), str(self.alias))

    def test_absent_native_module_refuses_and_names_the_primitive(self):
        with mock.patch.dict(sys.modules, {'_winapi': None}):
            with self.assertRaisesRegex(RuntimeError, '_winapi[.]CreateJunction'):
                runner._create_junction(self.target, self.alias)

    def test_absent_native_callable_refuses_and_names_the_primitive(self):
        with mock.patch.dict(sys.modules, {'_winapi': mock.Mock(spec=[])}):
            with self.assertRaisesRegex(RuntimeError, '_winapi[.]CreateJunction'):
                runner._create_junction(self.target, self.alias)

    def test_native_oserror_keeps_the_real_junction_text(self):
        fake = mock.Mock()
        fake.CreateJunction.side_effect = OSError(5, 'fixture access denied')
        with mock.patch.dict(sys.modules, {'_winapi': fake}):
            with self.assertRaisesRegex(RuntimeError, 'Could not create a real junction'):
                runner._create_junction(self.target, self.alias)

    def test_symbolic_reparse_tag_is_not_accepted_as_a_junction(self):
        mount_point_tag = object()
        info = types.SimpleNamespace(st_reparse_tag=object(),
                                     st_mode=stat.S_IFDIR)
        with mock.patch.object(runner.stat, 'IO_REPARSE_TAG_MOUNT_POINT',
                               mount_point_tag, create=True), \
                mock.patch.object(runner.os.path, 'realpath', return_value='resolved-target'), \
                mock.patch.object(runner.os, 'lstat', return_value=info):
            with self.assertRaisesRegex(RuntimeError, 'not a real junction'):
                runner._assert_real_junction(self.target, self.alias)

    def test_mount_point_reparse_tag_is_accepted_as_a_junction(self):
        mount_point_tag = object()
        info = types.SimpleNamespace(st_reparse_tag=mount_point_tag,
                                     st_mode=stat.S_IFDIR)
        with mock.patch.object(runner.stat, 'IO_REPARSE_TAG_MOUNT_POINT',
                               mount_point_tag, create=True), \
                mock.patch.object(runner.os.path, 'realpath', return_value='resolved-target'), \
                mock.patch.object(runner.os, 'lstat', return_value=info):
            self.assertIsNone(runner._assert_real_junction(self.target, self.alias))

    def test_dangling_alias_is_removed_before_the_owned_tree(self):
        removed = []
        with tempfile.TemporaryDirectory(prefix=runner.BASE_PREFIX) as directory:
            base = Path(directory)
            alias = base / 'aliased-root'
            (base / 'canonical-root').mkdir()
            real_lexists = os.path.lexists
            def dangling(path):
                return True if Path(path) == alias else real_lexists(path)
            with mock.patch.object(runner.os.path, 'lexists', side_effect=dangling), \
                    mock.patch.object(runner.os, 'rmdir',
                                      side_effect=lambda path: removed.append(('rmdir', path))), \
                    mock.patch.object(runner.shutil, 'rmtree',
                                      side_effect=lambda path: removed.append(('rmtree', path))):
                runner._remove_aliased_roots(base, alias)
        self.assertEqual([('rmdir', str(alias)), ('rmtree', base)], removed)

    def test_hostile_verification_restores_the_variable_after_failure(self):
        variable = runner.HOSTILE_VARIABLE
        for previous, error in ((None, RuntimeError('Could not create a real junction')),
                                ('preserved', OSError('fixture failure'))):
            with self.subTest(previous=previous):
                with mock.patch.dict(os.environ):
                    if previous is None:
                        os.environ.pop(variable, None)
                    else:
                        os.environ[variable] = previous
                    with tempfile.TemporaryDirectory(prefix=runner.BASE_PREFIX) as directory:
                        base = Path(directory)
                        target = base / runner.HOSTILE_SEGMENT
                        alias = base / (runner.HOSTILE_SEGMENT + '-aliased-root')
                        with mock.patch.object(runner, '_create_junction',
                                               side_effect=error):
                            with self.assertRaises(type(error)):
                                runner._verify_hostile_junction(target, alias)
                        if previous is None:
                            self.assertNotIn(variable, os.environ)
                        else:
                            self.assertEqual(previous, os.environ[variable])


if __name__ == '__main__':
    unittest.main()
