"""Private real Git/flock chain and promoter admission across interruption."""
import contextlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import promote_adapters
from gate_campaign import JOURNAL_NAME
from tests.regate_campaign_fixture import (
    ROOT, copy_campaign_cli, write_campaign_inputs, write_campaign_worker,
)
from tests.test_chain_failure_guards import prepare_artifact_commit_fixture
from tests.test_gpu_lock_owner import run_owned_cpu_chain


def get_cleanup_report(folder, error):
    """What still lives in a temp root that could not be removed (#1302)."""
    folder = os.path.realpath(folder)
    leftovers = sorted(os.path.relpath(os.path.join(base, name), folder)
                       for base, dirs, files in os.walk(folder) for name in dirs + files)
    processes = []
    for pid in filter(str.isdigit, os.listdir('/proc')):
        try:
            cwd = os.readlink(f'/proc/{pid}/cwd')
            with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                command = handle.read().replace(b'\0', b' ').decode(errors='replace').strip()
        except OSError:
            continue
        if cwd.startswith(folder) or folder in command:
            processes.append(f'{pid}: {command[:200]}')
    return (f'temp root cleanup failed ({error}); {len(leftovers)} path(s) remain, '
            f'{len(processes)} process(es) still use it.\nprocesses:\n  '
            + ('\n  '.join(processes) or '(none)') + '\nleftovers (first 40):\n  '
            + '\n  '.join(leftovers[:40]))


@contextlib.contextmanager
def chain_temporary_directory():
    """A temp root for a chain fixture whose failed cleanup says what is still
    writing to it. A failing test body keeps its own error; cleanup is never
    retried or ignored after a passing body, since a late writer may be a real
    orphaned process (#1302)."""
    folder = tempfile.mkdtemp()
    try:
        yield folder
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    try:
        shutil.rmtree(folder)
    except OSError as error:
        raise AssertionError(get_cleanup_report(folder, error)) from error


class RegateCampaignChainTests(unittest.TestCase):
    def prepare(self, root, rejected=False):
        gates = root / 'ab_test_runtime/experiments'
        write_campaign_inputs(root, 'a', 0.4 if rejected else 0.9)
        write_campaign_inputs(root, 'b')
        python = root / 'app/env/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        copy_campaign_cli(root)
        prefix = '''
with open('events','a') as handle:handle.write(name+'\\n')
if name=='b' and os.environ.get('MODE')=='interrupt':
 # The parent is this stage's real timeout process; its parent is our chain.
 status=Path('/proc/'+str(os.getppid())+'/stat').read_text().rsplit(')',1)[1].split()
 os.kill(int(status[1]),signal.SIGTERM)
 sys.exit(2)
if name=='b' and os.environ.get('MODE')=='error':sys.exit(7)
if name=='b' and os.environ.get('MODE')=='noop':sys.exit(0)
'''
        write_campaign_worker(root, prefix)
        source = (ROOT / 'run_chains/regate_with_provenance_20260817.sh').read_text()
        baseline = os.environ.get('REGATE_CAMPAIGN_CHAIN_BASELINE')
        if baseline:
            source = Path(baseline).read_text()
        source = source.replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git', f'REPO="{root}"')
        source = source.replace('/tmp/regate_queue.tsv', str(root / 'baseline_queue.tsv'))
        script = root / 'regate.sh'
        script.write_text(source)
        prepare_artifact_commit_fixture(root)
        tmpdir = root / 'temporary'
        tmpdir.mkdir()
        unrelated = gates / 'other-session.json'
        unrelated.write_text('base')
        self.git(root, 'add', str(unrelated))
        self.git(root, 'commit', '-qm', 'unrelated baseline')
        unrelated.write_text('staged WIP')
        self.git(root, 'add', str(unrelated))
        return script, gates

    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

    def run_chain(self, root, script, mode='success'):
        return run_owned_cpu_chain(['bash', str(script)], root,
            env={**os.environ, 'MODE': mode, 'TMPDIR': str(root / 'temporary')},
            cwd=root, capture_output=True, text=True, timeout=20)

    def admit(self, gates, name):
        with patch.object(promote_adapters, 'GATES', str(gates)), \
             patch.object(promote_adapters, 'GATE_PREFIX', 'gate_promote__'):
            return promote_adapters.gate_result(name)

    def assert_scopes(self, root):
        staged = self.git(root, 'diff', '--cached', '--name-only').splitlines()
        self.assertEqual(['ab_test_runtime/experiments/other-session.json'], staged)
        self.assertEqual([], list((root / 'temporary').glob('regate_queue.*')))
        changed = self.git(root, 'log', '--format=', '--name-only', 'HEAD~4..HEAD').splitlines()
        self.assertNotIn('ab_test_runtime/experiments/other-session.json', changed)

    def test_interrupted_mixed_set_refuses_then_restart_completes(self):
        with chain_temporary_directory() as tmp:
            root = Path(tmp)
            script, gates = self.prepare(root)
            old_b = (gates / 'gate_promote__b.json').read_bytes()
            result = self.run_chain(root, script, 'interrupt')
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(old_b, (gates / 'gate_promote__b.json').read_bytes())
            self.assertIsNone(self.admit(gates, 'a'))
            self.assertIsNone(self.admit(gates, 'b'))
            self.assertTrue((gates / JOURNAL_NAME).exists(), 'interrupted campaign must have a durable journal')
            journal = json.loads((gates / JOURNAL_NAME).read_text())
            self.assertEqual('running', journal['status'])
            self.assertIn('sha256', journal['members']['a'])
            self.assertNotIn('rc', journal['members']['b'])
            self.assertEqual(old_b, (gates / 'gate_promote__b.json').read_bytes())
            self.assertIsNone(self.admit(gates, 'a'))
            self.assertIsNone(self.admit(gates, 'b'))
            first_id = journal['campaign_id']
            result = self.run_chain(root, script)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            complete = json.loads((gates / JOURNAL_NAME).read_text())
            self.assertEqual('complete', complete['status'])
            self.assertNotEqual(first_id, complete['campaign_id'])
            self.assertIsNotNone(self.admit(gates, 'a'))
            self.assertIsNotNone(self.admit(gates, 'b'))
            self.assert_scopes(root)

    def test_interrupted_chain_waits_for_late_git_writer_before_return(self):
        from subprocess_ownership import ensure_pidfd_support
        pidfds = ensure_pidfd_support()
        with chain_temporary_directory() as tmp:
            root = Path(tmp)
            script, _ = self.prepare(root)
            write_campaign_worker(root, '''
if name=='b':
 import subprocess
 birth=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
 Path('late-writer.json').write_text(json.dumps({'pid':os.getpid(),'birth':birth}))
 status=Path('/proc/'+str(os.getppid())+'/stat').read_text().rsplit(')',1)[1].split()
 os.kill(int(status[1]),signal.SIGTERM)
 time.sleep(1)
 subprocess.run(['git','hash-object','-w','--stdin'],input=b'late fixture object',stdout=subprocess.DEVNULL,check=True,timeout=2)
 Path('late-write-done').write_text('finished')
 sys.exit(2)
''')
            try:
                result = self.run_chain(root, script, 'interrupt')
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue((root / 'late-write-done').exists(),
                                'the CPU helper returned before its interrupted writer finished')
                digest = subprocess.check_output(['git', 'hash-object', '--stdin'],
                                                 input=b'late fixture object', cwd=root).decode().strip()
                self.assertTrue((root / '.git/objects' / digest[:2] / digest[2:]).is_file())
            finally:
                # Also let the deliberately live old-code writer exit before
                # teardown, so its expected assertion failure cannot race rmtree.
                record = root / 'late-writer.json'
                if record.exists():
                    identity = json.loads(record.read_text())
                    descriptor = None
                    try:
                        fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(')', 1)[1].split()
                        if fields[19] == identity['birth']:
                            descriptor = pidfds.pidfd_open(identity['pid'], 0)
                            fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(')', 1)[1].split()
                            self.assertEqual(identity['birth'], fields[19], 'synthetic writer PID was reused')
                            if not select.select([descriptor], [], [], 5)[0]:
                                pidfds.pidfd_send_signal(descriptor, signal.SIGKILL, None, 0)
                                self.assertTrue(select.select([descriptor], [], [], 5)[0],
                                                'synthetic writer did not stop before teardown')
                    except ProcessLookupError:
                        pass
                    except FileNotFoundError:
                        pass
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)

    def test_success_and_measured_rejection_have_complete_campaigns(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected), chain_temporary_directory() as tmp:
                root = Path(tmp)
                script, gates = self.prepare(root, rejected)
                result = self.run_chain(root, script)
                self.assertEqual(1 if rejected else 0, result.returncode, result.stdout + result.stderr)
                self.assertTrue((gates / JOURNAL_NAME).exists(), 'completion needs campaign evidence')
                self.assertEqual('complete', json.loads((gates / JOURNAL_NAME).read_text())['status'])
                self.assertIs(self.admit(gates, 'a')['passed'], not rejected)
                self.assertEqual(not rejected, 'REGATE COMPLETE' in result.stdout)
                self.assert_scopes(root)

    def test_execution_failure_or_zero_exit_old_artifact_cannot_complete(self):
        for mode in ('error', 'noop'):
            with self.subTest(mode=mode), chain_temporary_directory() as tmp:
                root = Path(tmp)
                script, gates = self.prepare(root)
                result = self.run_chain(root, script, mode)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertNotIn('REGATE COMPLETE', result.stdout)
                self.assertEqual(['a', 'b'], (root / 'events').read_text().splitlines())
                self.assertIsNone(self.admit(gates, 'a'))
                self.assertEqual('running', json.loads((gates / JOURNAL_NAME).read_text())['status'])
                self.assertIsNone(self.admit(gates, 'a'))


class ChainTemporaryDirectoryTests(unittest.TestCase):
    def test_a_failed_cleanup_names_the_leftovers_and_the_live_process(self):
        with patch('shutil.rmtree', side_effect=OSError(39, 'Directory not empty')) as remove:
            with self.assertRaises(AssertionError) as caught:
                with chain_temporary_directory() as tmp:
                    Path(tmp, '.git', 'objects').mkdir(parents=True)
                    Path(tmp, '.git', 'objects', 'ab').write_text('x')
                    writer = subprocess.Popen(['sleep', '30'], cwd=tmp)
                    self.addCleanup(writer.wait)  # cleanups run last-in first-out:
                    self.addCleanup(writer.kill)  # kill, then reap
            folder = remove.call_args_list[0][0][0]
        report = str(caught.exception)
        self.assertIn('Directory not empty', report)
        self.assertIn(os.path.join('.git', 'objects', 'ab'), report)
        self.assertIn(f'{writer.pid}: sleep 30', report)
        shutil.rmtree(folder, ignore_errors=True)

    def test_a_failing_body_keeps_its_own_error(self):
        with self.assertRaises(ValueError):
            with chain_temporary_directory() as tmp:
                folder = tmp
                raise ValueError('the real failure')
        self.assertFalse(os.path.exists(folder))

    def test_a_clean_run_removes_the_root(self):
        with chain_temporary_directory() as tmp:
            Path(tmp, 'file').write_text('x')
        self.assertFalse(os.path.exists(tmp))
