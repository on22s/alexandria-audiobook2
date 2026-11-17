"""Disposable repositories must not inherit a hook's repository authority."""
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
                                              'GIT_INDEX_FILE': str(git_directory / 'index')}):
                    if fixture == 'derived':
                        checkout = root / 'derived'
                        checkout.mkdir()
                        test_derived_file_hooks.DerivedCommitFailureTests().make_repo(checkout)
                    else:
                        case = test_chain_required_inputs.ChainRequiredInputsTests()
                        self.addCleanup(case.doCleanups)
                        case.setUp()
                        checkout = case.root
            finally:
                self.assertEqual(before, snapshot(), 'fixture modified the sentinel repository')
            result = subprocess.run(['git', '-C', str(checkout), 'rev-parse', '--show-toplevel'],
                                    env=environment, check=True, capture_output=True, text=True)
            self.assertEqual(checkout.resolve(), Path(result.stdout.strip()).resolve())
            result = subprocess.run(['git', '-C', str(checkout), 'log', '-1', '--format=%s'],
                                    env=environment, check=True, capture_output=True, text=True)
            self.assertEqual('fixture base' if fixture == 'derived' else 'fixture baseline', result.stdout.strip())
