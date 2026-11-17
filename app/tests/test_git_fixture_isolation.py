"""Disposable repositories must not inherit a hook's repository authority."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import test_support, test_chain_required_inputs, test_derived_file_hooks


class GitFixtureIsolationTests(unittest.TestCase):
    def test_environment_removes_repository_and_config_injection_without_mutating_parent(self):
        poison = dict.fromkeys((
            'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
            'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
            'GIT_PREFIX', 'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES',
            'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
            'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0',
            'GIT_CONFIG_KEY_17', 'GIT_CONFIG_VALUE_17',
        ), 'poison')
        with patch.dict(os.environ, poison):
            before = os.environ.copy()
            environment = test_support.get_isolated_git_environment({'FIXTURE_MARKER': 'kept'})
            self.assertEqual(before, dict(os.environ))
        self.assertTrue(set(poison).isdisjoint(environment))
        self.assertEqual('kept', environment['FIXTURE_MARKER'])
        self.assertEqual('1', environment['GIT_CONFIG_NOSYSTEM'])
        for name in ('HOME', 'USERPROFILE', 'GIT_CONFIG_GLOBAL', 'PATH'):
            self.assertEqual(before.get(name), environment.get(name))

    def test_derived_fixture_leaves_hook_repository_byte_identical(self):
        self.assert_fixture_isolated('derived')

    def test_chain_fixture_leaves_hook_repository_byte_identical(self):
        self.assert_fixture_isolated('chain')

    def test_provenance_reader_leaves_hook_repository_byte_identical(self):
        self.assert_fixture_isolated('provenance')

    def test_benchmark_reader_leaves_hook_repository_byte_identical(self):
        self.assert_fixture_isolated('benchmark')

    def test_lifetime_scope_restores_parent_after_exception_and_allows_inner_overrides(self):
        with patch.dict(os.environ, {'GIT_DIR': 'outer hook', 'GIT_CONFIG_COUNT': '1'}):
            before = os.environ.copy()
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                with test_support.isolate_git_environment():
                    self.assertNotIn('GIT_DIR', os.environ)
                    self.assertNotIn('GIT_CONFIG_COUNT', os.environ)
                    self.assertEqual('1', os.environ['GIT_CONFIG_NOSYSTEM'])
                    with patch.dict(os.environ, {'GIT_DIR': 'deliberate inner override'}):
                        self.assertEqual('deliberate inner override', os.environ['GIT_DIR'])
                    raise RuntimeError('fixture failure')
            self.assertEqual(before, dict(os.environ))

    def test_lifetime_scope_cleans_up_even_when_setup_fails(self):
        observed = []

        class FailingFixture(unittest.TestCase):
            def setUp(self):
                test_support.isolate_git_environment(self)
                self.addCleanup(lambda: observed.append(os.environ.get('GIT_DIR')))
                raise RuntimeError('fixture setup failure')

            def runTest(self):
                self.fail('setup must fail first')

        with patch.dict(os.environ, {'GIT_DIR': 'outer hook'}):
            before = os.environ.copy()
            result = unittest.TestResult()
            FailingFixture().run(result)
            self.assertEqual(1, len(result.errors))
            self.assertIn('fixture setup failure', result.errors[0][1])
            self.assertEqual([None], observed)
            self.assertEqual(before, dict(os.environ))

    def assert_fixture_isolated(self, fixture):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / 'sentinel'
            sentinel.mkdir()
            environment = test_support.get_isolated_git_environment()
            for arguments in (('init', '-q', '-b', 'main'),
                              ('-c', 'user.name=Sentinel', '-c', 'user.email=sentinel@example.invalid',
                               '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-qm', 'sentinel')):
                subprocess.run(['git', '-C', str(sentinel), *arguments], env=environment,
                               check=True, capture_output=True)
            git_directory = sentinel / '.git'

            def snapshot():
                paths = [git_directory / 'config', git_directory / 'HEAD', git_directory / 'index',
                         git_directory / 'packed-refs', *sorted((git_directory / 'refs').rglob('*'))]
                return {str(path.relative_to(git_directory)): path.read_bytes()
                        for path in paths if path.is_file()}

            before = snapshot()
            try:
                with patch.dict(os.environ, {'GIT_DIR': str(git_directory),
                                              'GIT_WORK_TREE': str(sentinel),
                                              'GIT_INDEX_FILE': str(git_directory / 'index')}), ExitStack() as cleanup:
                    if fixture == 'derived':
                        checkout = root / 'derived'
                        checkout.mkdir()
                        test_derived_file_hooks.DerivedCommitFailureTests().make_repo(checkout)
                        expected = 'fixture base'
                    elif fixture == 'chain':
                        case = test_chain_required_inputs.ChainRequiredInputsTests()
                        cleanup.callback(case.doCleanups)
                        case.setUp()
                        cleanup.callback(case.tearDown)
                        checkout = case.root
                        expected = 'fixture baseline'
                    elif fixture == 'provenance':
                        from tests.test_experiment_provenance import ReproducibleHarnessTests
                        case = ReproducibleHarnessTests()
                        cleanup.callback(case.doCleanups)
                        case.setUp()
                        cleanup.callback(case.tearDown)
                        case.test_sound_same_commit_verifies_without_new_diagnostic_fields()
                        checkout = case.repo
                        expected = 'one'
                    else:
                        from tests import test_benchmark_environment
                        reader = test_benchmark_environment.benchmark_environment._get_local_worktree_identity
                        observed = []

                        def observe(checkout):
                            self.assert_fixture_commit(checkout, environment, 'Initial')
                            observed.append(checkout)
                            return reader(checkout)

                        with patch.object(test_benchmark_environment.benchmark_environment,
                                          '_get_local_worktree_identity', side_effect=observe):
                            case = test_benchmark_environment.BenchmarkArtifactBoundaryTests()
                            case.test_untracked_content_boundary_collision_cannot_reuse_identity()
                        self.assertEqual(3, len(observed))
                        return
                    self.assert_fixture_commit(checkout, environment, expected)
            finally:
                self.assertEqual(before, snapshot(), 'fixture modified the sentinel repository')

    def assert_fixture_commit(self, checkout, environment, expected):
        checkout = Path(checkout)
        result = subprocess.run(['git', '-C', str(checkout), 'rev-parse', '--show-toplevel'],
                                env=environment, check=True, capture_output=True, text=True)
        self.assertEqual(checkout.resolve(), Path(result.stdout.strip()).resolve())
        result = subprocess.run(['git', '-C', str(checkout), 'log', '-1', '--format=%s'],
                                env=environment, check=True, capture_output=True, text=True)
        self.assertEqual(expected, result.stdout.strip())
