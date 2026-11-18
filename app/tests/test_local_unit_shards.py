"""Local readiness keeps dirty-source fidelity, ownership and exact coverage."""
from tests.test_support import get_isolated_git_environment, isolate_git_environment
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace


APP = Path(__file__).resolve().parent.parent


def receipt(shard, identifiers, **changes):
    report = dict(schema_version=1, shard=shard, status='passed', exit_code=0, complete=True,
                  tests_ran=len(identifiers), tests_completed=len(identifiers),
                  failures=0, errors=0, skips=0, expected_failures=0,
                  unexpected_successes=0, test_ids=identifiers)
    report.update(changes)
    return report


class LocalAggregationTests(unittest.TestCase):
    def setUp(self):
        self.local = importlib.import_module('local_unit_shards')
        self.identifiers = ['tests.test_first.Example.test_value',
                            'tests.test_second.Example.test_value']

    def aggregate(self, reports):
        return self.local.aggregate_unit_reports(reports, self.identifiers, 2)

    def test_missing_test_dependencies_are_named_before_shards_start(self):
        with patch.object(self.local.metadata, 'version',
                          side_effect=self.local.metadata.PackageNotFoundError), \
             patch.object(self.local, 'start_owned_subprocess') as start:
            errors = self.local.get_unit_test_dependency_errors()
            self.assertIn('EbookLib==0.20 (installed: missing)', errors)
            self.assertTrue(any('beautifulsoup4==' in error for error in errors))
            self.assertTrue(any('uv==' in error for error in errors))
            with self.assertRaisesRegex(RuntimeError, 'Unit test dependencies unavailable:.*EbookLib'):
                self.local.run_local_unit_tests(APP.parent, workers=2)
        start.assert_not_called()

    def test_wrong_test_dependency_version_is_refused(self):
        with patch.object(self.local.metadata, 'version', return_value='0.0.0'):
            errors = self.local.get_unit_test_dependency_errors()
        self.assertTrue(errors)
        self.assertTrue(all('installed: 0.0.0' in error for error in errors))

    def test_windows_environment_keeps_hidden_drive_state_and_clears_git_routing(self):
        import windows_subprocess_owner
        environment = {'=C:': r'C:\original', 'Path': 'native-path',
                       'gIt_iNdEx_fIlE': 'foreign-index', 'PythonPath': 'previous-source'}
        with patch.object(self.local, 'sys', SimpleNamespace(platform='win32')), \
             patch.object(windows_subprocess_owner, '_inherited_windows_environment', return_value=environment):
            result = self.local.get_git_environment()
        self.assertEqual(r'C:\original', result['=C:'])
        self.assertEqual('native-path', result['PATH'])
        self.assertEqual('previous-source', result['PYTHONPATH'])
        self.assertNotIn('GIT_INDEX_FILE', result)
        self.assertEqual('foreign-index', environment['gIt_iNdEx_fIlE'])
        with patch.object(self.local.os, 'cpu_count', return_value=128):
            self.assertEqual(2, self.local.get_default_workers())

    def test_exact_identities_and_counts_sum(self):
        reports = [receipt(f'{number}/2', [identifier])
                   for number, identifier in enumerate(self.identifiers, 1)]
        result = self.aggregate(reports)
        self.assertEqual('passed', result['status'])
        self.assertEqual((2, 0, 0, 0), tuple(result[key] for key in
                         ('tests_ran', 'failures', 'errors', 'skips')))
        self.assertTrue(result['counts_complete'])
        self.assertEqual(self.identifiers, result['test_ids'])

    def test_failed_shard_fails_readiness_but_retains_its_counts(self):
        result = self.aggregate([
            receipt('1/2', self.identifiers[:1], status='failed', exit_code=1,
                    failures=2, errors=1, skips=1),
            receipt('2/2', self.identifiers[1:])])
        self.assertEqual('failed', result['status'])
        self.assertEqual((2, 2, 1, 1), tuple(result[key] for key in
                         ('tests_ran', 'failures', 'errors', 'skips')))
        self.assertTrue(result['counts_complete'])
        self.assertTrue(result['problems'])
        self.assertFalse(any('shards ran' in problem for problem in result['problems']))

    def test_missing_shard_is_not_a_successful_partial_run(self):
        result = self.aggregate([receipt('1/2', self.identifiers[:1])])
        self.assertEqual('failed', result['status'])
        self.assertEqual(1, result['tests_ran'])
        self.assertFalse(result['counts_complete'])
        self.assertTrue(any('expected 2 shard reports' in item for item in result['problems']))

    def test_equal_count_duplicate_omission_cannot_pass(self):
        result = self.aggregate([receipt('1/2', self.identifiers[:1]),
                                 receipt('2/2', self.identifiers[:1])])
        self.assertEqual('failed', result['status'])
        self.assertTrue(any('more than once' in item for item in result['problems']))
        self.assertTrue(any('not executed' in item for item in result['problems']))

    def test_crash_timeout_skip_and_forged_exit_fail_closed(self):
        for changes in (dict(status='crashed', exit_code=7, complete=False),
                        dict(status='timed_out', exit_code=-1, complete=False),
                        dict(exit_code=7), dict(skips=1), dict(unexpected_successes=1),
                        dict(errors=True), dict(tests_completed=0), dict(test_ids='invalid'),
                        dict(schema_version=2), dict(schema_version=True)):
            with self.subTest(changes=changes):
                result = self.aggregate([receipt('1/2', self.identifiers[:1], **changes),
                                         receipt('2/2', self.identifiers[1:])])
                self.assertEqual('failed', result['status'])
                self.assertTrue(result['problems'])

    def test_missing_receipt_counter_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            payload = receipt('1/1', self.identifiers[:1])
            del payload['errors']
            path = directory / 'tests.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            report = self.local.get_worker_report(dict(
                receipt=path, directory=directory, spec='1/1', worktree=directory / 'worktree',
                log_path=directory / 'output.log', process=SimpleNamespace(returncode=0)))
            self.assertIsNone(report['errors'])
            result = self.local.aggregate_unit_reports([report], self.identifiers[:1], 1)
            self.assertEqual('failed', result['status'])
            self.assertFalse(result['counts_complete'])


class RegisteredWindowsSkipTests(unittest.TestCase):
    """A native Windows shard passes on skips only when the registry names them.

    Measured 2026-11-17 on a clean native Windows run of this tree: 14,498
    tests, 2 failures, 0 errors, 1,064 skips, and all six shards reported
    'failed' - five of them with 0 failures, 0 errors and exit code 0. These
    controls pin the rule both shard layers now share.
    """

    def setUp(self):
        self.local = importlib.import_module('local_unit_shards')
        self.registry = importlib.import_module('skip_registry')
        self.identifiers = ['tests.test_first.Example.test_value',
                            'tests.test_second.Example.test_value']

    def sites(self, locations=()):
        """The verdict's view of these rows, through the registry's own reader.

        A decorator row authorizes its location; a `...:runtime:<digest>` row
        pins the exact message it may skip with; a `...:runtime-prefix:<digest>`
        row pins only the stable prefix that message must start with.
        """
        parsed = []
        for entry in locations:
            location, declaration = entry if isinstance(entry, tuple) else (entry, None)
            parsed.append(self.registry.get_verdict_site(location, declaration))
        return frozenset(parsed)

    def runtime_row(self, module_file, qualname, message):
        return (f'{module_file}:{qualname}:runtime:'
                + self.registry.get_reason_key(message))

    def runtime_prefix_row(self, module_file, qualname, prefix):
        return (f'{module_file}:{qualname}:runtime-prefix:'
                + self.registry.get_reason_key(prefix))

    def aggregate(self, reports, platform='win32', locations=()):
        return self.local.aggregate_unit_reports(
            reports, self.identifiers, 2, platform=platform,
            registered_sites=self.sites(locations))

    def test_registry_rows_cover_a_whole_class_or_one_whole_name(self):
        """A class row covers its methods; a name matches only at a dot boundary."""
        sites = self.sites({'test_first.py:Example:0', 'test_second.py:Example.test_value:0'})
        self.assertTrue(self.registry.is_registered_skip(self.identifiers[0], sites))
        self.assertTrue(self.registry.is_registered_skip(self.identifiers[1], sites))
        method_only = self.sites({'test_second.py:Example.test_value:0'})
        self.assertFalse(self.registry.is_registered_skip(
            'tests.test_second.Example.test_value_other', method_only))
        self.assertFalse(self.registry.is_registered_skip(
            'tests.test_second.ExampleOther.test_value', method_only))
        self.assertFalse(self.registry.is_registered_skip(
            'tests.test_third.Example.test_value', sites))

    def test_subtest_parameters_keep_the_test_id_registered(self):
        """A subTest id is still its test, whatever the parameters look like."""
        sites = self.sites({'test_first.py:Example.test_value:0'})
        for identifier in ("tests.test_first.Example.test_value (kind='link')",
                           'tests.test_first.Example.test_value [symlink escape]',
                           "tests.test_first.Example.test_value (name='../adapter/linked')"):
            self.assertTrue(self.registry.is_registered_skip(identifier, sites), identifier)
        self.assertFalse(self.registry.is_registered_skip(
            'tests.test_first.Example.test_value_other (kind=1)', sites))
        self.assertFalse(self.registry.is_registered_skip('synthetic id', sites))

    def test_runtime_rows_authorize_only_their_exact_message(self):
        """A runtime row covers one reviewed message, not whatever a host reports."""
        message = 'symlink creation not permitted (synthetic WinError 1314)'
        row = self.runtime_row('test_first.py', 'Example', message)
        skipped = receipt('1/2', self.identifiers[:1], skips=1,
                          skip_ids=self.identifiers[:1],
                          skip_reasons={self.identifiers[0]: message})
        result = self.aggregate([skipped, receipt('2/2', self.identifiers[1:])], locations={row})
        self.assertEqual('passed', result['status'], result['problems'])
        self.assertEqual((1, 0), (result['registered_skips'], result['unregistered_skips']))
        other = receipt('1/2', self.identifiers[:1], skips=1, skip_ids=self.identifiers[:1],
                        skip_reasons={self.identifiers[0]: message + ' (another host)'},
                        status='failed')
        result = self.aggregate([other, receipt('2/2', self.identifiers[1:])], locations={row})
        self.assertEqual('failed', result['status'])
        self.assertEqual((0, 1), (result['registered_skips'], result['unregistered_skips']))
        unrecorded = receipt('1/2', self.identifiers[:1], skips=1,
                             skip_ids=self.identifiers[:1], status='failed')
        result = self.aggregate([unrecorded, receipt('2/2', self.identifiers[1:])],
                                locations={row})
        self.assertEqual('failed', result['status'])
        self.assertEqual(1, result['unregistered_skips'],
                         'a receipt with no recorded message cannot authorize a runtime row')

    def test_runtime_prefix_rows_authorize_only_the_pinned_prefix(self):
        """A measured WinError reason with a temp path matches on its stable prefix."""
        prefix = 'native symlink unavailable here: '
        measured = prefix + '[WinError 1314] privilege: '
        measured += "'C:\\Temp\\tmpXXXX\\real.json' -> 'C:\\Temp\\tmpXXXX\\timing.json'"
        row = self.runtime_prefix_row('test_first.py', 'Example', prefix)
        declaration = f"reason={prefix!r}; runtime skip; synthetic prefix row"
        locations = {(row, declaration)}
        skipped = receipt('1/2', self.identifiers[:1], skips=1,
                          skip_ids=self.identifiers[:1],
                          skip_reasons={self.identifiers[0]: measured})
        result = self.aggregate([skipped, receipt('2/2', self.identifiers[1:])], locations=locations)
        self.assertEqual('passed', result['status'], result['problems'])
        self.assertEqual((1, 0), (result['registered_skips'], result['unregistered_skips']))
        # A message with a different prefix is still unregistered even though
        # the row does not pin the varying temp-path suffix.
        wrong = receipt('1/2', self.identifiers[:1], skips=1, skip_ids=self.identifiers[:1],
                        skip_reasons={self.identifiers[0]: 'a different message ' + measured},
                        status='failed')
        result = self.aggregate([wrong, receipt('2/2', self.identifiers[1:])], locations=locations)
        self.assertEqual('failed', result['status'])
        self.assertEqual((0, 1), (result['registered_skips'], result['unregistered_skips']))
        # A receipt that records no message cannot authorize a prefix row either.
        unrecorded = receipt('1/2', self.identifiers[:1], skips=1,
                             skip_ids=self.identifiers[:1], status='failed')
        result = self.aggregate([unrecorded, receipt('2/2', self.identifiers[1:])],
                                locations=locations)
        self.assertEqual('failed', result['status'])
        self.assertEqual(1, result['unregistered_skips'])

    def test_empty_or_missing_prefix_never_authorizes_a_skip(self):
        """An empty prefix would match every message, so it is refused at parse and at verdict."""
        row = self.runtime_prefix_row('test_first.py', 'Example', '')
        with self.assertRaisesRegex(ValueError, 'empty prefix'):
            self.registry.parse_skip_registry_row(f"{row} = {'0' * 64}  # reason=''; synthetic")
        self.assertIsNone(self.registry.get_declared_reason(None))
        test_id = self.identifiers[0]
        for value in ('', None):
            site = ('test_first.py', 'Example', self.registry.MATCH_PREFIX, value)
            self.assertFalse(self.registry.is_registered_skip(test_id, (site,), 'any message'))
        # A prefix row with no declaration recovers no prefix and fails closed.
        site = self.registry.get_verdict_site(row)
        self.assertFalse(self.registry.is_registered_skip(test_id, (site,), 'any message'))

    def test_worker_report_carries_skip_reasons_to_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            payload = receipt('1/2', self.identifiers[:1], skips=1,
                              skip_ids=self.identifiers[:1],
                              skip_reasons={self.identifiers[0]: 'synthetic message'})
            path = directory / 'tests.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            report = self.local.get_worker_report(dict(
                receipt=path, directory=directory, spec='1/2', worktree=directory / 'worktree',
                log_path=directory / 'output.log', process=SimpleNamespace(returncode=0)))
        self.assertEqual({self.identifiers[0]: 'synthetic message'}, report['skip_reasons'])

    def test_native_windows_passes_when_every_skip_is_registered(self):
        """A class row and a method row both authorize a skip; counts stay intact."""
        reports = [receipt('1/2', self.identifiers[:1], skips=1, skip_ids=self.identifiers[:1]),
                   receipt('2/2', self.identifiers[1:], skips=1, skip_ids=self.identifiers[1:])]
        result = self.aggregate(reports, locations={'test_first.py:Example:0',
                                                    'test_second.py:Example.test_value:0'})
        self.assertEqual('passed', result['status'], result['problems'])
        self.assertEqual((2, 2, 0), (result['skips'], result['registered_skips'],
                                     result['unregistered_skips']))
        self.assertTrue(result['counts_complete'])
        self.assertEqual(self.identifiers, result['test_ids'])

    def test_native_windows_fails_and_names_an_unregistered_skip(self):
        """One unregistered skip fails the shard and the problem carries its id."""
        reports = [receipt('1/2', self.identifiers[:1], skips=1, skip_ids=self.identifiers[:1]),
                   receipt('2/2', self.identifiers[1:], skips=1, skip_ids=self.identifiers[1:],
                           status='failed')]
        result = self.aggregate(reports, locations={'test_first.py:Example:0'})
        self.assertEqual('failed', result['status'])
        self.assertEqual((1, 1), (result['registered_skips'], result['unregistered_skips']))
        self.assertTrue(any(f'unregistered skip: {self.identifiers[1]}' in problem
                            for problem in result['problems']))

    def test_other_platforms_still_fail_on_a_registered_skip(self):
        """Linux keeps the original rule: any skip fails, registered or not."""
        reports = [receipt('1/2', self.identifiers[:1], skips=1, skip_ids=self.identifiers[:1],
                           status='failed'),
                   receipt('2/2', self.identifiers[1:])]
        result = self.aggregate(reports, platform='linux',
                                locations={'test_first.py:Example:0'})
        self.assertEqual('failed', result['status'])
        self.assertEqual((1, 0, 1), (result['skips'], result['registered_skips'],
                                     result['unregistered_skips']))

    def test_native_windows_failure_error_or_unexpected_success_still_fails(self):
        """Registered skips excuse skips only, never a failure, error or surprise pass."""
        for changes in (dict(status='failed', exit_code=1, failures=1),
                        dict(status='failed', exit_code=1, errors=1),
                        dict(status='failed', unexpected_successes=1)):
            with self.subTest(changes=changes):
                reports = [receipt('1/2', self.identifiers[:1], skips=1,
                                   skip_ids=self.identifiers[:1], **changes),
                           receipt('2/2', self.identifiers[1:])]
                result = self.aggregate(reports, locations={'test_first.py:Example:0'})
                self.assertEqual('failed', result['status'])
                self.assertTrue(result['problems'])

    def test_worker_receipt_records_each_skipped_test_id(self):
        """A real ci_env run writes skip_ids and reasons for a skipped test.

        The suite is synthetic and lives in a temporary package, so no skip
        site enters this repository's census; the id it produces is
        deliberately unregistered, which also shows the worker refuses an
        unregistered skip on either platform while unittest itself exits 0.
        """
        app = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'tests'
            package.mkdir()
            (package / '__init__.py').write_text('', encoding='utf-8')
            (package / 'test_ci_env_skip_receipt.py').write_text(
                "import unittest\n\n\n"
                "class Example(unittest.TestCase):\n"
                "    @unittest.skip('synthetic registered-skip receipt probe')\n"
                "    def test_skipped(self):\n"
                "        pass\n\n"
                "    def test_runs(self):\n"
                "        self.assertTrue(True)\n", encoding='utf-8')
            receipt_path = root / 'tests.json'
            environment = get_isolated_git_environment()
            environment.update(PYTHONPATH=str(app), PYTHONUTF8='1')
            result = subprocess.run(
                [sys.executable, '-m', 'ci_env', 'discover', '-s', '.', '-p', 'test_*.py',
                 '-v', '--test-report', str(receipt_path)],
                cwd=str(root), env=environment, capture_output=True, text=True, timeout=300)
            payload = json.loads(receipt_path.read_text(encoding='utf-8'))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        skipped_id = 'tests.test_ci_env_skip_receipt.Example.test_skipped'
        self.assertEqual(1, payload['skips'])
        self.assertEqual([skipped_id], payload['skip_ids'])
        self.assertIn('synthetic registered-skip receipt probe', payload['skip_reasons'][skipped_id])
        self.assertEqual('failed', payload['status'],
                         'an unregistered skip must not pass a shard')


class LocalWorktreeTests(unittest.TestCase):
    def setUp(self):
        isolate_git_environment(self)
        self.local = importlib.import_module('local_unit_shards')
        fixture_root = Path(tempfile.gettempdir()).resolve() / 'alexandria-local-units'
        fixture_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='fixture-', dir=fixture_root)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / 'repository with spaces'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.hooksPath', str(self.root / 'no-hooks'))
        app = self.repo / 'app'
        tests = app / 'tests'
        tests.mkdir(parents=True)
        for name in ('ci_env.py', 'skip_registry.py', 'unit_test_sharding.py', 'utils.py',
                     'loaded_module_code.py'):
            shutil.copyfile(APP / name, app / name)
        shutil.copyfile(APP.parent / 'gpu_stats.py', self.repo / 'gpu_stats.py')
        (tests / '__init__.py').write_text('', encoding='utf-8')
        (self.repo / '.gitattributes').write_text('*.txt text eol=lf\n', encoding='utf-8')
        (self.repo / '.gitignore').write_text('private.txt\n__pycache__/\n', encoding='utf-8')
        self.identifiers = []
        for name in ('first', 'second'):
            self.write_test(name, "self.assertEqual(b'base\\n', Path('payload.txt').read_bytes())")
            self.identifiers.append(f'tests.test_{name}.Example.test_value')
        (tests / 'unit_test_inventory.json').write_text(json.dumps({
            f'test_{name}': [identifier] for name, identifier in
            zip(('first', 'second'), self.identifiers)}), encoding='utf-8')
        (tests / 'module_durations.json').write_text(json.dumps({
            'test_first': 60, 'test_second': 60}), encoding='utf-8')
        (app / 'payload.txt').write_bytes(b'base\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'Synthetic baseline')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.checkout_ownership = {}

    def git(self, *arguments):
        return subprocess.run(['git', *arguments], cwd=self.repo, check=True,
                              capture_output=True, text=True, timeout=30, env=get_isolated_git_environment()).stdout

    def write_test(self, name, body):
        (self.repo / 'app/tests' / f'test_{name}.py').write_text(
            'import os,subprocess,sys,time,unittest\nfrom pathlib import Path\n'
            'class Example(unittest.TestCase):\n    def test_value(self):\n'
            '        ' + body + '\n', encoding='utf-8')

    def registered(self):
        return self.git('worktree', 'list', '--porcelain')

    def add_checkout(self, snapshot, path):
        ownership = dict(path=str(path), admin_name=None, snapshot_commit=snapshot['commit'],
                         token=self.local.uuid.uuid4().hex + ':' + self.local.secrets.token_hex(32))
        self.checkout_ownership[str(path)] = ownership
        return self.local.add_snapshot_worktree(self.repo, snapshot, path, ownership=ownership)

    def remove_checkout(self, path):
        self.local.remove_snapshot_worktree(self.repo, path, self.root,
                                           ownership=self.checkout_ownership.get(str(path)))

    def git_integrity(self):
        common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
        index = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
        return (common.joinpath('config').read_bytes(), index.read_bytes(),
                self.git('rev-parse', 'HEAD'), self.git('show-ref'))

    def test_rapid_same_size_edit_is_captured_without_trusting_copied_index_stats(self):
        self.git('config', 'core.checkStat', 'minimal')
        self.git('config', 'core.trustctime', 'false')
        payload = self.repo / 'app/payload.txt'
        for attempt in range(5):
            payload.write_bytes(b'base\n')
            self.git('add', 'app/payload.txt')
            indexed_second = int(payload.stat().st_mtime)
            payload.write_bytes(b'next\n')
            if int(payload.stat().st_mtime) == indexed_second:
                break
        else:
            self.fail('Could not exercise a same-second edit')
        self.assertIn('app/payload.txt', self.git('diff', '--name-only'))
        before = self.git_integrity()
        checkout = self.root / 'checkout'
        try:
            snapshot = self.local.create_working_tree_snapshot(self.repo, self.root / 'snapshot')
            self.add_checkout(snapshot, checkout)
            self.assertEqual(b'next\n', (checkout / 'app/payload.txt').read_bytes())
            captured = subprocess.check_output(
                ['git', 'show', snapshot['commit'] + ':app/payload.txt'], cwd=self.repo, env=get_isolated_git_environment())
            self.assertEqual(b'next\n', captured)
        finally:
            self.remove_checkout(checkout)
        self.assertEqual(before, self.git_integrity())

    def create_stale_run(self, *, created=True):
        artifacts = self.root / 'reports'
        artifacts.mkdir()
        manifest = self.local.register_run(self.repo, artifacts, self.identifiers, 1)
        record = json.loads(manifest.read_text(encoding='utf-8'))
        path = artifacts / 'worktrees/shard-1'
        if created:
            snapshot = self.local.create_working_tree_snapshot(self.repo, self.root / 'snapshot')
            options = {}
            if record.get('ownership'):
                record['snapshot_commit'] = snapshot['commit']
                record['ownership'][0]['snapshot_commit'] = snapshot['commit']
                manifest.write_text(json.dumps(record), encoding='utf-8')
                options = dict(ownership=record['ownership'][0], manifest=manifest)
            self.local.add_snapshot_worktree(self.repo, snapshot, path, **options)
            record = json.loads(manifest.read_text(encoding='utf-8'))
        child = subprocess.Popen([sys.executable, '-c', 'pass'], env=get_isolated_git_environment())
        child.wait(timeout=10)
        record['owner_pid'] = child.pid
        manifest.write_text(json.dumps(record), encoding='utf-8')
        return manifest, path, record

    def assert_recovery_preserves(self, marker, before, manifest):
        integrity = self.git_integrity()
        try:
            self.local.cleanup_stale_runs(self.repo)
        except (ValueError, RuntimeError):
            pass
        self.assertTrue(marker.is_file(), 'recovery deleted foreign worktree data')
        self.assertEqual(b'foreign uncommitted data', marker.read_bytes())
        self.assertEqual(before, self.registered())
        self.assertEqual(integrity, self.git_integrity())
        self.assertTrue(manifest.exists(), 'refused recovery must retain ownership evidence')

    def test_stale_recovery_preserves_replacement_branch_at_unchanged_recorded_path(self):
        manifest, path, record = self.create_stale_run()
        self.git('worktree', 'remove', '--force', str(path))
        self.git('worktree', 'add', '-b', 'foreign-branch', str(path), self.head)
        marker = path / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        self.assert_recovery_preserves(marker, self.registered(), manifest)

    def test_stale_recovery_preserves_recreated_detached_worktree_with_copied_token(self):
        manifest, path, record = self.create_stale_run()
        ownership = record['ownership'][0]
        admin = self.local.get_worktree_admin(self.repo, path, record['snapshot_commit'])
        token = (admin / self.local.OWNER_TOKEN_FILE).read_bytes()
        self.git('worktree', 'remove', '--force', str(path))
        self.git('worktree', 'add', '--detach', str(path), record['snapshot_commit'])
        replacement = self.local.get_worktree_admin(self.repo, path, record['snapshot_commit'])
        self.assertEqual(ownership['admin_name'], replacement.name)
        (replacement / self.local.OWNER_TOKEN_FILE).write_bytes(token)
        marker = path / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        self.assert_recovery_preserves(marker, self.registered(), manifest)

    def test_stale_recovery_never_overwrites_retargeted_foreign_reports(self):
        manifest, path, record = self.create_stale_run(created=False)
        foreign = self.root / 'foreign-reports'
        (foreign / 'shard-1').mkdir(parents=True)
        expected = {'aggregate.json': b'foreign aggregate\n',
                    'shard-1/shard.json': b'foreign shard\n',
                    'shard-1/output.log': b'foreign log\n'}
        for name, content in expected.items():
            (foreign / name).write_bytes(content)
        record['artifact_dir'] = str(foreign)
        record['worktrees'] = [str(foreign / 'worktrees/shard-1')]
        record['ownership'][0]['path'] = record['worktrees'][0]
        manifest.write_text(json.dumps(record), encoding='utf-8')
        before = self.git_integrity()
        recovered = self.local.cleanup_stale_runs(self.repo)
        self.assertEqual(expected, {name: (foreign / name).read_bytes() for name in expected})
        self.assertEqual(set(expected), {str(name.relative_to(foreign)).replace(chr(92), '/')
                                        for name in foreign.rglob('*') if name.is_file()})
        self.assertEqual(before, self.git_integrity())
        self.assertEqual(1, len(recovered))
        recovered_path = Path(recovered[0])
        self.assertNotEqual(foreign, recovered_path)
        self.addCleanup(shutil.rmtree, recovered_path)
        self.assertEqual('failed', json.loads((recovered_path / 'aggregate.json').read_text())['status'])

    def test_stale_recovery_preserves_recreated_artifacts_with_copied_marker(self):
        manifest, path, record = self.create_stale_run(created=False)
        artifacts = Path(record['artifact_dir'])
        expected = b'foreign aggregate must remain byte-identical'
        (artifacts / 'aggregate.json').write_bytes(expected)
        saved = self.root / 'original-reports'
        artifacts.rename(saved)
        shutil.copytree(saved, artifacts)
        recovered = self.local.cleanup_stale_runs(self.repo)
        self.assertEqual(1, len(recovered))
        recovered_path = Path(recovered[0])
        if recovered_path != artifacts:
            self.addCleanup(shutil.rmtree, recovered_path)
        self.assertEqual(expected, (artifacts / 'aggregate.json').read_bytes())
        self.assertNotEqual(artifacts, recovered_path)
        self.assertEqual('failed', json.loads((recovered_path / 'aggregate.json').read_text())['status'])

    def test_stale_recovery_preserves_foreign_worktree_after_consistent_path_edit(self):
        manifest, path, record = self.create_stale_run()
        artifacts = self.root / 'another-tool'
        foreign = artifacts / 'worktrees/shard-1'
        self.git('worktree', 'add', '--detach', str(foreign), self.head)
        marker = foreign / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        record.update(artifact_dir=str(artifacts), worktrees=[str(foreign)])
        manifest.write_text(json.dumps(record), encoding='utf-8')
        self.assert_recovery_preserves(marker, self.registered(), manifest)
        self.assertTrue((path / '.git').is_file())

    def test_stale_recovery_preserves_independent_repository_at_never_created_path(self):
        manifest, path, record = self.create_stale_run(created=False)
        path.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(path)], check=True, env=get_isolated_git_environment())
        config = (path / '.git/config').read_bytes()
        marker = path / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        self.assert_recovery_preserves(marker, self.registered(), manifest)
        self.assertEqual(config, (path / '.git/config').read_bytes())

    def test_stale_recovery_preserves_foreign_admin_index_when_artifacts_point_at_common(self):
        manifest, path, record = self.create_stale_run(created=False)
        foreign = self.root / 'foreign/shard-1'
        self.git('worktree', 'add', '--detach', str(foreign), self.head)
        common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
        admin = common / 'worktrees/shard-1'
        index = (admin / 'index').read_bytes()
        marker = admin / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        record.update(artifact_dir=str(common), worktrees=[str(admin)])
        manifest.write_text(json.dumps(record), encoding='utf-8')
        self.assert_recovery_preserves(marker, self.registered(), manifest)
        self.assertEqual(index, (admin / 'index').read_bytes())

    def test_force_added_ignored_file_is_included_in_snapshot(self):
        ignored = self.repo / 'private.txt'
        ignored.write_bytes(b'force-added source')
        self.git('add', '-f', 'private.txt')
        before = self.git_integrity()
        snapshot = self.local.create_working_tree_snapshot(self.repo, self.root / 'snapshot')
        captured = subprocess.check_output(['git', 'show', snapshot['commit'] + ':private.txt'], cwd=self.repo, env=get_isolated_git_environment())
        self.assertEqual(b'force-added source', captured)
        checkout = self.root / 'checkout'
        try:
            self.add_checkout(snapshot, checkout)
            self.assertEqual(b'force-added source', (checkout / 'private.txt').read_bytes())
        finally:
            self.remove_checkout(checkout)
        self.assertEqual(before, self.git_integrity())

    def test_staged_executable_mode_survives_filemode_false_snapshot(self):
        self.git('config', 'core.filemode', 'false')
        self.git('update-index', '--chmod=+x', 'app/payload.txt')
        before = self.git_integrity()
        snapshot = self.local.create_working_tree_snapshot(self.repo, self.root / 'snapshot')
        entry = self.git('ls-tree', snapshot['commit'], 'app/payload.txt')
        self.assertTrue(entry.startswith('100755 '), entry)
        checkout = self.root / 'checkout'
        try:
            self.add_checkout(snapshot, checkout)
            materialized = subprocess.check_output(['git', 'ls-files', '--stage', 'app/payload.txt'], cwd=checkout, env=get_isolated_git_environment())
            self.assertTrue(materialized.startswith(b'100755 '), materialized)
        finally:
            self.remove_checkout(checkout)
        self.assertEqual(before, self.git_integrity())

    def test_reused_live_pid_with_different_birth_recovers_only_verified_worktree(self):
        manifest, path, record = self.create_stale_run()
        record.update(owner_pid=os.getpid(), owner_start_time='previous-process-start')
        manifest.write_text(json.dumps(record), encoding='utf-8')
        self.local.cleanup_stale_runs(self.repo)
        self.assertFalse(path.exists(), 'a reused PID must not preserve the dead run')
        self.assertFalse(manifest.exists())
        self.assertTrue(self.repo.is_dir())

    def test_recovery_requires_detached_commit_admin_name_and_secret_token(self):
        manifest, path, original = self.create_stale_run()
        admin = self.local.get_worktree_admin(self.repo, path, original['snapshot_commit'])
        marker = path / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        for kind in ('token', 'admin-name', 'commit'):
            with self.subTest(identity=kind):
                record = json.loads(json.dumps(original))
                if kind == 'token':
                    (admin / self.local.OWNER_TOKEN_FILE).write_text('foreign-token', encoding='ascii')
                elif kind == 'admin-name':
                    record['ownership'][0]['admin_name'] = 'another-admin'
                else:
                    record['snapshot_commit'] = self.head
                    record['ownership'][0]['snapshot_commit'] = self.head
                manifest.write_text(json.dumps(record), encoding='utf-8')
                self.assert_recovery_preserves(marker, self.registered(), manifest)
                (admin / self.local.OWNER_TOKEN_FILE).write_text(original['token'], encoding='ascii')

    def test_recovery_refuses_junction_or_symlink_artifact_directory(self):
        manifest, path, record = self.create_stale_run(created=False)
        artifacts = Path(record['artifact_dir'])
        (artifacts / self.local.OWNER_TOKEN_FILE).unlink()
        artifacts.rmdir()
        target = self.root / 'other-tool'
        target.mkdir()
        marker = target / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        if sys.platform == 'win32':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(artifacts), str(target)],
                           check=True, capture_output=True, env=get_isolated_git_environment())
            self.addCleanup(artifacts.rmdir)
        else:
            artifacts.symlink_to(target, target_is_directory=True)
            self.addCleanup(artifacts.unlink)
        self.assert_recovery_preserves(marker, self.registered(), manifest)

    def test_owner_creation_time_matches_live_process_and_preserves_unknown_access(self):
        started = self.local.get_process_start_time(os.getpid())
        self.assertTrue(started)
        self.assertTrue(self.local.is_owner_alive(os.getpid(), started))
        self.assertFalse(self.local.is_owner_alive(os.getpid(), started + '-previous'))
        with patch.object(self.local, 'get_process_start_time', side_effect=PermissionError('synthetic')):
            self.assertTrue(self.local.is_owner_alive(os.getpid(), started))

    def test_recovery_refuses_artifacts_outside_fixed_tool_root(self):
        manifest, path, record = self.create_stale_run(created=False)
        with tempfile.TemporaryDirectory(prefix='foreign-tool-') as temporary:
            artifacts = Path(temporary).resolve()
            foreign = artifacts / 'worktrees/shard-1'
            self.git('worktree', 'add', '--detach', str(foreign), self.head)
            try:
                marker = foreign / 'uncommitted'
                marker.write_bytes(b'foreign uncommitted data')
                record.update(artifact_dir=str(artifacts), worktrees=[str(foreign)])
                record['ownership'][0]['path'] = str(foreign)
                manifest.write_text(json.dumps(record), encoding='utf-8')
                self.assert_recovery_preserves(marker, self.registered(), manifest)
            finally:
                self.git('worktree', 'remove', '--force', str(foreign))

    def test_recovery_validates_all_shards_before_removing_any_owned_worktree(self):
        manifest, owned, record = self.create_stale_run()
        foreign = owned.parent / 'shard-2'
        foreign.mkdir()
        marker = foreign / 'uncommitted'
        marker.write_bytes(b'foreign uncommitted data')
        record['shard_count'] = 2
        record['worktrees'].append(str(foreign))
        record['ownership'].append(dict(path=str(foreign), admin_name=None,
            token=record['token'], snapshot_commit=record['snapshot_commit']))
        manifest.write_text(json.dumps(record), encoding='utf-8')
        self.assert_recovery_preserves(marker, self.registered(), manifest)
        self.assertTrue((owned / '.git').is_file(), 'a later refusal must preserve earlier valid shards')

    def test_killed_coordinator_worktrees_are_recovered_without_touching_live_or_unowned_trees(self):
        self.run_killed_coordinator_recovery(automatic=False)

    def test_next_run_recovers_killed_coordinator_worktrees(self):
        self.run_killed_coordinator_recovery(automatic=True)

    def run_killed_coordinator_recovery(self, *, automatic):
        markers = [self.root / ('active-' + name) for name in ('first', 'second')]
        for name, marker in zip(('first', 'second'), markers):
            self.write_test(name, f"Path({str(marker)!r}).touch(); time.sleep(120)")
        unrelated = self.root / 'unrecorded-checkout'
        self.git('worktree', 'add', '--detach', str(unrelated), self.head)
        self.addCleanup(self.git, 'worktree', 'remove', '--force', str(unrelated))
        before, integrity = self.registered(), self.git_integrity()
        reports = self.root / 'killed-reports'
        code = ('from pathlib import Path; import sys; import local_unit_shards as local; '
                'local.run_local_unit_tests(Path(sys.argv[1]), workers=2, shard_count=2, '
                'report_dir=Path(sys.argv[2]), timeout=180)')
        def cleanup_command():
            return subprocess.run(
                [sys.executable, '-m', 'local_unit_shards', '--cleanup-stale', '--repo', str(self.repo)],
                cwd=APP, env={**self.local.get_git_environment(), 'PYTHONPATH': str(APP)},
                capture_output=True, text=True, timeout=60)
        log = (self.root / 'coordinator.log').open('wb')
        process = subprocess.Popen([sys.executable, '-c', code, str(self.repo), str(reports)],
                                   cwd=APP, env={**self.local.get_git_environment(),
                                                 'PYTHONPATH': str(APP), **self.local.CPU_DEVICE_MASK},
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while not all(marker.exists() for marker in markers) and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(.05)
            self.assertTrue(all(marker.exists() for marker in markers),
                            (self.root / 'coordinator.log').read_text(encoding='utf-8'))
            live = self.registered()
            self.assertNotEqual(before, live)
            live_cleanup = cleanup_command()
            self.assertEqual(0, live_cleanup.returncode, live_cleanup.stderr)
            self.assertEqual(live, self.registered(), 'a live coordinator must never be cleaned')
            process.kill()
            process.wait(timeout=20)
            if automatic:
                for name in ('first', 'second'):
                    self.write_test(name, 'self.assertTrue(True)')
                result = self.run_units()
                self.assertEqual('passed', result['status'], result['problems'])
            else:
                cleanup = cleanup_command()
                self.assertEqual(0, cleanup.returncode, cleanup.stderr)
            self.assertEqual(before, self.registered())
            if not automatic:
                self.assertIn('Recovered stale local unit run:', cleanup.stdout)
            self.assertTrue((unrelated / 'app/payload.txt').is_file())
            self.assertFalse(any((reports / 'worktrees').iterdir()))
            self.assertEqual(integrity, self.git_integrity())
            saved = json.loads((reports / 'aggregate.json').read_text(encoding='utf-8'))
            self.assertEqual('failed', saved['status'])
            self.assertFalse(saved['counts_complete'])
            self.assertEqual(2, saved['tests_ran'])
            self.assertEqual(set(self.identifiers), set(saved['test_ids']))
            self.assertEqual([], self.local.cleanup_stale_runs(self.repo))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=20)
            log.close()
            if hasattr(self.local, 'cleanup_stale_runs'):
                self.local.cleanup_stale_runs(self.repo)
            else:
                for number in (1, 2):
                    self.local.remove_snapshot_worktree(
                        self.repo, reports / 'worktrees' / f'shard-{number}', reports / 'worktrees')

    def test_stale_recovery_rejects_unrecorded_paths_before_removing_anything(self):
        artifacts = self.root / 'reports'
        artifacts.mkdir()
        unrelated = self.root / 'unrecorded'
        unrelated.mkdir()
        marker = unrelated / 'keep'
        marker.touch()
        child = subprocess.Popen([sys.executable, '-c', 'pass'], env=get_isolated_git_environment())
        child.wait(timeout=10)
        manifest = self.local.register_run(self.repo, artifacts, self.identifiers, 1)
        record = json.loads(manifest.read_text(encoding='utf-8'))
        record.update(owner_pid=child.pid, worktrees=[str(unrelated)])
        manifest.write_text(json.dumps(record), encoding='utf-8')
        integrity = self.git_integrity()
        with self.assertRaisesRegex(ValueError, 'Unsafe local unit recovery paths'):
            self.local.cleanup_stale_runs(self.repo)
        self.assertTrue(marker.exists())
        self.assertTrue(manifest.exists(), 'failed recovery must retain its manifest')
        self.assertEqual(integrity, self.git_integrity())

    def test_stale_manifest_before_first_checkout_keeps_unknown_counts_and_log_paths(self):
        artifacts = self.root / 'reports'
        artifacts.mkdir()
        child = subprocess.Popen([sys.executable, '-c', 'pass'], env=get_isolated_git_environment())
        child.wait(timeout=10)
        manifest = self.local.register_run(self.repo, artifacts, self.identifiers, 2)
        record = json.loads(manifest.read_text(encoding='utf-8'))
        record['owner_pid'] = child.pid
        manifest.write_text(json.dumps(record), encoding='utf-8')
        integrity = self.git_integrity()
        self.assertEqual([str(artifacts)], self.local.cleanup_stale_runs(self.repo))
        result = json.loads((artifacts / 'aggregate.json').read_text(encoding='utf-8'))
        self.assertEqual('failed', result['status'])
        self.assertFalse(result['counts_complete'])
        self.assertEqual(0, result['tests_ran'])
        for shard in result['shards']:
            self.assertIsNone(shard['tests_ran'])
            self.assertFalse(shard['complete'])
            self.assertTrue(Path(shard['log_path']).is_file())
        self.assertFalse(manifest.exists())
        self.assertEqual(integrity, self.git_integrity())

    def test_stale_recovery_preserves_unrecorded_prunable_registration(self):
        self.git('config', 'gc.worktreePruneExpire', 'now')
        unrelated = self.root / 'unrecorded-prunable'
        self.git('worktree', 'add', '--detach', str(unrelated), self.head)
        self.addCleanup(self.git, 'worktree', 'remove', '--force', str(unrelated))
        self.assertTrue(unrelated.resolve().is_relative_to(self.root.resolve()))
        shutil.rmtree(unrelated)
        before, integrity = self.registered(), self.git_integrity()
        artifacts = self.root / 'reports'
        artifacts.mkdir()
        child = subprocess.Popen([sys.executable, '-c', 'pass'], env=get_isolated_git_environment())
        child.wait(timeout=10)
        manifest = self.local.register_run(self.repo, artifacts, self.identifiers, 1)
        record = json.loads(manifest.read_text(encoding='utf-8'))
        record['owner_pid'] = child.pid
        manifest.write_text(json.dumps(record), encoding='utf-8')
        self.local.cleanup_stale_runs(self.repo)
        self.assertEqual(before, self.registered(), 'prune diagnostics on stderr must not be ignored')
        self.assertEqual(integrity, self.git_integrity())

    def run_units(self, **options):
        return self.local.run_local_unit_tests(
            self.repo, workers=options.pop('workers', 2), shard_count=2,
            report_dir=self.root / 'reports', timeout=options.pop('timeout', 60), **options)

    def test_empty_inventory_module_does_not_change_discovery_assignment(self):
        tests = self.repo / 'app/tests'
        inventory = json.loads((tests / 'unit_test_inventory.json').read_text(encoding='utf-8'))
        inventory['test_empty'] = []
        (tests / 'unit_test_inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
        (tests / 'test_empty.py').write_text('', encoding='utf-8')
        (tests / 'module_durations.json').write_text(json.dumps({
            'test_first': 60, 'test_second': 60, 'test_empty': 120}), encoding='utf-8')
        result = self.run_units()
        self.assertEqual('passed', result['status'], result['problems'])
        self.assertTrue(result['counts_complete'])
        self.assertEqual(2, result['tests_ran'])
        self.assertEqual(set(self.identifiers), set(result['test_ids']))

    def test_import_failure_in_empty_inventory_module_is_still_refused(self):
        tests = self.repo / 'app/tests'
        inventory = json.loads((tests / 'unit_test_inventory.json').read_text(encoding='utf-8'))
        inventory['test_broken'] = []
        (tests / 'unit_test_inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
        (tests / 'test_broken.py').write_text("raise ImportError('synthetic discovery failure')\n",
                                            encoding='utf-8')
        result = self.run_units()
        self.assertEqual('failed', result['status'])
        self.assertFalse(result['counts_complete'])
        self.assertEqual(1, result['errors'])
        self.assertTrue(any('_FailedTest' in identifier for identifier in result['test_ids']))
        self.assertTrue(any('executed identities differ' in problem for problem in result['problems']))

    def test_dirty_staged_untracked_and_crlf_inputs_are_frozen_without_index_or_ref_changes(self):
        payload = self.repo / 'app/payload.txt'
        payload.write_bytes(b'staged\n')
        self.git('add', 'app/payload.txt')
        payload.write_bytes(b'working\r\n')
        (self.repo / 'app/needed.py').write_text("VALUE = 'untracked'\n", encoding='utf-8')
        (self.repo / 'private.txt').write_text('not copied', encoding='utf-8')
        for name in ('first', 'second'):
            self.write_test(name, "self.assertTrue(all(os.environ.get(key) == '' for key in "
                            "('CUDA_VISIBLE_DEVICES','HIP_VISIBLE_DEVICES','ROCR_VISIBLE_DEVICES'))); "
                            "self.assertEqual(str(Path(sys.executable).parent), "
                            "os.environ['PATH'].split(os.pathsep)[0]); "
                            "from needed import VALUE; self.assertEqual('untracked', VALUE); "
                            "self.assertEqual(b'working\\r\\n', Path('payload.txt').read_bytes()); "
                            "self.assertFalse(Path('../private.txt').exists()); "
                            "self.assertFalse(Path('shared-marker').exists()); Path('shared-marker').touch()")
        index = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
        before_index, before_refs, before_worktrees = index.read_bytes(), self.git('show-ref'), self.registered()
        result = self.run_units()
        self.assertEqual('passed', result['status'], result['problems'])
        self.assertEqual(2, result['tests_ran'])
        self.assertEqual(set(self.identifiers), set(result['test_ids']))
        self.assertEqual(before_index, index.read_bytes())
        self.assertEqual(before_refs, self.git('show-ref'))
        self.assertEqual(before_worktrees, self.registered())
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD').strip())
        self.assertFalse((self.repo / 'app/shared-marker').exists())
        for shard in result['shards']:
            self.assertTrue(Path(shard['log_path']).is_file())
            self.assertFalse(Path(shard['worktree']).exists())

    def test_native_crlf_overlay_does_not_create_false_dirty_git_status(self):
        self.git('config', 'core.autocrlf', 'true')
        self.git('config', 'core.longpaths', 'false')
        (self.repo / 'native.log').write_bytes(b'native\r\nsource\r\n')
        long_name = 'long-' + 'x' * 145 + '.log'
        (self.repo / long_name).write_bytes(b'long path\r\n')
        self.git('add', 'native.log', long_name)
        self.git('commit', '-qm', 'Native newline fixture')
        snapshot = self.local.create_working_tree_snapshot(self.repo, self.root / 'snapshot')
        checkout = self.root / ('nested-' + 'x' * 110) / 'checkout'
        try:
            self.add_checkout(snapshot, checkout)
            self.assertEqual(b'native\r\nsource\r\n', (checkout / 'native.log').read_bytes())
            self.assertEqual(b'long path\r\n', (checkout / long_name).read_bytes())
            status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=checkout, env=get_isolated_git_environment())
            self.assertEqual(b'', status, 'faithful CRLF bytes must not appear spuriously dirty')
        finally:
            self.remove_checkout(checkout)

    def test_failure_still_runs_other_shard_and_removes_every_worktree(self):
        self.write_test('first', "self.fail('synthetic failure')")
        before = self.registered()
        result = self.run_units()
        self.assertEqual(('failed', 2, 1, 0, 0), tuple(result[key] for key in
                         ('status', 'tests_ran', 'failures', 'errors', 'skips')))
        self.assertTrue(result['counts_complete'])
        self.assertEqual(before, self.registered())
        self.assertTrue((self.root / 'reports/aggregate.json').is_file())

    def test_worker_hook_installer_cannot_write_its_shared_config_but_fixture_install_still_works(self):
        from tests.test_support import get_bash_executable
        installer = self.root / 'install_git_hooks.sh'
        installer.write_text((APP.parent / 'tools/install_git_hooks.sh').read_text(encoding='utf-8'),
                             encoding='utf-8')
        common = self.git('rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        config = Path(common) / 'config'
        before = config.read_bytes()
        snapshot = self.local.create_working_tree_snapshot(self.repo, self.root / 'snapshot')
        checkout = self.root / 'checkout'
        environment = {**self.local.get_git_environment(), 'AA2_LOCAL_UNIT_COMMON_DIR': common}
        try:
            self.add_checkout(snapshot, checkout)
            result = subprocess.run([get_bash_executable(), str(installer)], cwd=checkout,
                                    env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(before, config.read_bytes())
            independent = self.root / 'independent'
            independent.mkdir()
            subprocess.run(['git', 'init', '-q', str(independent)], check=True, env=get_isolated_git_environment())
            result = subprocess.run([get_bash_executable(), str(installer)], cwd=independent,
                                    env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(b'.githooks', subprocess.check_output(
                ['git', 'config', '--get', 'core.hooksPath'], cwd=independent, env=get_isolated_git_environment()).strip())
            self.assertEqual(before, config.read_bytes())
            installed = (independent / '.git/config').read_bytes()
            installed_stat = (independent / '.git/config').stat()
            again = subprocess.run([get_bash_executable(), str(installer)], cwd=independent,
                                   env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, again.returncode, again.stdout + again.stderr)
            self.assertEqual(installed, (independent / '.git/config').read_bytes())
            after_stat = (independent / '.git/config').stat()
            self.assertEqual((installed_stat.st_ino, installed_stat.st_mtime_ns),
                             (after_stat.st_ino, after_stat.st_mtime_ns))
        finally:
            self.remove_checkout(checkout)

    def test_worktree_commands_wait_for_the_common_kernel_lock(self):
        from utils import file_lock
        common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
        ready = self.root / 'waiting'
        code = ("import sys;from pathlib import Path;import local_unit_shards as local;"
                "Path(sys.argv[2]).touch();local.run_git(Path(sys.argv[1]),'worktree','list','--porcelain')")
        process = None
        try:
            with file_lock(common / 'alexandria-local-unit-git', timeout=10):
                process = self.local.start_owned_subprocess(
                    [sys.executable, '-c', code, str(self.repo), str(ready)], cwd=APP,
                    env=self.local.get_git_environment(), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=os.name == 'posix',
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists(), 'child must reach the protected Git command')
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=3)
            self.assertEqual(0, process.wait(timeout=15))
        finally:
            if process is not None:
                if process.poll() is None:
                    self.local.stop_owned_subprocess(process, timeout=2, force_after_grace=os.name == 'nt')
                control = getattr(process, '_alexandria_control', None)
                if control is not None:
                    control.close()

    def test_serial_path_uses_no_worktrees_and_matches_identity_and_failure_counts(self):
        self.write_test('first', "self.fail('synthetic failure')")
        with patch.object(self.local, 'create_working_tree_snapshot', side_effect=AssertionError('serial snapshot')):
            result = self.run_units(workers=0)
        self.assertEqual(('failed', 2, 1, 0, 0), tuple(result[key] for key in
                         ('status', 'tests_ran', 'failures', 'errors', 'skips')))
        self.assertEqual(set(self.identifiers), set(result['test_ids']))

    def test_readiness_floor_fails_in_the_persistent_artifact_too(self):
        result = self.run_units(workers=0, minimum_tests=3)
        self.assertEqual('failed', result['status'])
        self.assertTrue(any('floor' in problem for problem in result['problems']))
        saved = json.loads((self.root / 'reports/aggregate.json').read_text(encoding='utf-8'))
        self.assertEqual('failed', saved['status'])

    def test_insufficient_disk_refuses_before_snapshot_or_explicitly_falls_back_to_serial(self):
        with patch.object(self.local.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), \
             patch.object(self.local, 'create_working_tree_snapshot', side_effect=AssertionError('disk admission')) as snapshot:
            refused = self.run_units()
        snapshot.assert_not_called()
        self.assertEqual('failed', refused['status'])
        self.assertTrue(any('disk' in item.lower() for item in refused['problems']))
        report_directory = (self.root / 'reports').resolve()
        self.assertTrue(report_directory.is_relative_to(self.root.resolve()))
        shutil.rmtree(report_directory)
        with patch.object(self.local.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), \
             patch.object(self.local, 'create_working_tree_snapshot', side_effect=AssertionError('disk admission')) as snapshot:
            fallback = self.run_units(serial_on_low_disk=True, timeout=None)
        snapshot.assert_not_called()
        self.assertEqual('passed', fallback['status'], fallback['problems'])
        self.assertEqual(0, fallback['workers'])
        self.assertEqual(7200, fallback['timeout_seconds'])
        self.assertIn('disk', fallback['fallback_reason'].lower())

    def test_timeout_leaves_partial_count_artifact_and_no_registered_worktree(self):
        self.write_test('first', "time.sleep(30)")
        before = self.registered()
        result = self.run_units(timeout=.5)
        self.assertEqual('failed', result['status'])
        self.assertFalse(result['counts_complete'])
        self.assertTrue(any(shard['status'] == 'timed_out' for shard in result['shards']))
        self.assertEqual(before, self.registered())
        self.assertTrue((self.root / 'reports/aggregate.json').exists())

    def test_ctrl_c_reaps_owned_processes_and_removes_every_registered_worktree(self):
        self.write_test('first', "time.sleep(30)")
        self.write_test('second', "time.sleep(30)")
        before, processes = self.registered(), []
        spawn = self.local.start_owned_subprocess
        def recorded(*args, **kwargs):
            process = spawn(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(self.local, 'start_owned_subprocess', side_effect=recorded), \
             patch.object(self.local, 'get_owned_exit_result', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_units()
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertEqual(before, self.registered())
        report = json.loads((self.root / 'reports/aggregate.json').read_text(encoding='utf-8'))
        self.assertEqual('failed', report['status'])
        self.assertFalse(report['counts_complete'])

    def test_process_crash_retains_started_count_and_runs_the_other_shard(self):
        self.write_test('first', "os._exit(7)")
        before = self.registered()
        result = self.run_units()
        self.assertEqual('failed', result['status'])
        self.assertFalse(result['counts_complete'])
        self.assertEqual(2, result['tests_ran'])
        self.assertEqual(before, self.registered())
        self.assertTrue(any(shard['exit_code'] == 7 for shard in result['shards']))

    def test_source_mutation_during_snapshot_is_refused_without_touching_user_index(self):
        capture = self.local.capture_source_files
        calls = []
        def changed(*args, **kwargs):
            result = capture(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                (self.repo / 'app/payload.txt').write_text('changed', encoding='utf-8')
            return result
        index = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'index').strip())
        original = index.read_bytes()
        with patch.object(self.local, 'capture_source_files', side_effect=changed):
            result = self.run_units()
        self.assertEqual('failed', result['status'])
        self.assertTrue(any('changed' in problem for problem in result['problems']))
        self.assertEqual(original, index.read_bytes())
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD').strip())

    def test_checkout_failure_keeps_a_failed_shard_artifact_and_cleans_prior_workers(self):
        add = self.local.add_snapshot_worktree
        before, calls = self.registered(), []
        def failed(repo, snapshot, path, **options):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('synthetic checkout failure')
            return add(repo, snapshot, path, **options)
        with patch.object(self.local, 'add_snapshot_worktree', side_effect=failed):
            result = self.run_units()
        self.assertEqual('failed', result['status'])
        self.assertFalse(result['counts_complete'])
        self.assertEqual([], result['remaining_worktrees'])
        self.assertEqual(before, self.registered())
        failed_report = json.loads((self.root / 'reports/shard-2/shard.json').read_text(encoding='utf-8'))
        self.assertEqual('crashed', failed_report['status'])
        self.assertIn('synthetic checkout failure', Path(failed_report['log_path']).read_text(encoding='utf-8'))

    def test_wrong_shard_identities_are_failed_in_the_persistent_shard_artifact(self):
        collect = self.local.get_worker_report
        def wrong(job):
            result = collect(job)
            if result['shard'] == '1/2':
                result['test_ids'] = self.identifiers[:1]
            return result
        with patch.object(self.local, 'get_worker_report', side_effect=wrong):
            result = self.run_units()
        self.assertEqual('failed', result['status'])
        self.assertEqual([], result['remaining_worktrees'])
        saved = json.loads((self.root / 'reports/shard-1/shard.json').read_text(encoding='utf-8'))
        self.assertEqual('failed', saved['status'])


class LocalVerifierRouteTests(unittest.TestCase):
    def test_ready_defaults_to_windows_workers_and_serial_on_other_hosts(self):
        import verify_release
        local = importlib.import_module('local_unit_shards')
        for platform, expected in (('win32', 3), ('linux', 0), ('darwin', 0)):
            with self.subTest(platform=platform), \
                 patch.object(verify_release, 'sys', SimpleNamespace(
                     platform=platform, executable=sys.executable, stdout=sys.stdout, stderr=sys.stderr)), \
                 patch.object(local, 'get_default_workers', return_value=3), \
                 patch.object(verify_release, 'compile_python_files', return_value=None), \
                 patch.object(verify_release, 'run_report_command', return_value=None), \
                 patch.object(verify_release, 'run_unit_test_report', return_value={}) as units, \
                 patch.object(verify_release, 'run_api_suite', return_value={}):
                self.assertEqual(0, verify_release.main(['--local-units']))
            self.assertEqual(expected, units.call_args.kwargs['workers'])

    def test_successful_local_units_run_each_nonunit_gate_once(self):
        import verify_release
        local = importlib.import_module('local_unit_shards')
        commands = []
        result = dict(status='passed', tests_ran=600)
        with patch.object(verify_release, 'compile_python_files', return_value=None) as compiler, \
             patch.object(verify_release, 'run_report_command', side_effect=lambda *args, **kwargs: commands.append(args[0])), \
             patch.object(local, 'run_local_unit_tests', return_value=result) as units, \
             patch.object(verify_release, 'run_api_suite', return_value={}) as api:
            self.assertEqual(0, verify_release.main(['--unit-workers', '2']))
        compiler.assert_called_once()
        units.assert_called_once()
        api.assert_called_once()
        self.assertEqual(6, len(commands))
        self.assertEqual(len(commands), len(set(commands)))

    def test_local_discovery_runs_nonunit_gates_once_and_preserves_failed_counts(self):
        import verify_release
        local = importlib.import_module('local_unit_shards')
        result = dict(status='failed', tests_ran=600, failures=1, errors=2, skips=3,
                      artifact_dir='owned-artifacts', problems=['synthetic failed shard'])
        commands = []
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(verify_release, 'compile_python_files', return_value=None), \
             patch.object(verify_release, 'run_report_command', side_effect=lambda *args, **kwargs: commands.append(args[0])), \
             patch.object(local, 'run_local_unit_tests', return_value=result) as units:
            destination = Path(tmp) / 'release.json'
            code = verify_release.main(['--local-units', '--unit-workers', '2',
                                        '--json-report', str(destination)])
            saved = json.loads(destination.read_text(encoding='utf-8'))
        self.assertEqual(1, code)
        self.assertEqual(len(commands), len(set(commands)))
        self.assertEqual(5, len(commands))
        self.assertEqual(2, units.call_args.kwargs['workers'])
        self.assertEqual(verify_release.MINIMUM_UNIT_TESTS, units.call_args.kwargs['minimum_tests'])
        self.assertEqual('unit_tests', saved['failure']['gate'])
        self.assertEqual(result, saved['gates'][-1]['result'])

    def test_ci_shard_keeps_legacy_path_even_when_ready_selects_local_default(self):
        import verify_release
        with patch.object(verify_release, 'run_unit_test_report', return_value={}) as units:
            self.assertEqual(0, verify_release.main(['--local-units', '--shard', '2/6']))
        self.assertEqual({}, units.call_args.kwargs)
        self.assertEqual(['--shard', '2/6'], units.call_args.args[1][-2:])

    def test_invalid_local_options_fail_before_running_gates(self):
        import verify_release
        for options in (['--unit-workers', '-1'], ['--unit-workers', '1', '--shard', '1/6'],
                        ['--unit-shard-timeout', 'nan'], ['--unit-shard-timeout', '0']):
            with self.subTest(options=options), patch.object(verify_release, 'run_report_gate') as gate:
                with self.assertRaises(SystemExit):
                    verify_release.main(options)
                gate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
