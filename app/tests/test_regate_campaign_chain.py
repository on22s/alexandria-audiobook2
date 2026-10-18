"""Private real Git/flock chain and promoter admission across interruption."""
import contextlib
import io
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

from subprocess_ownership import run_owned_capture
import promote_adapters
from gate_campaign import JOURNAL_NAME
from tests.regate_campaign_fixture import (
    ROOT, copy_campaign_cli, write_campaign_inputs, write_campaign_worker,
)
from tests.test_chain_failure_guards import prepare_artifact_commit_fixture
from tests.test_gpu_lock_owner import run_owned_cpu_chain
from tests.test_morning_chain import write_captured_stderr


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
        try:
            result = run_owned_capture(['git', '-C', str(root), *args], text=True, timeout=15)
        except subprocess.TimeoutExpired as error:
            write_captured_stderr(error.stderr, preserve_error=True)
            raise
        write_captured_stderr(result.stderr, preserve_error=result.returncode != 0)
        result.check_returncode()
        return result.stdout.strip()

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


class RegateGitCaptureTests(unittest.TestCase):
    def test_git_waits_for_redirected_writer_before_return(self):
        worker = '''import hashlib,json,os,subprocess,sys,zlib
from pathlib import Path
from tests.test_regate_campaign_chain import RegateCampaignChainTests
root=Path(sys.argv[1])
subprocess.run(['/usr/bin/git','-C',str(root),'init','-q','--template='],check=True,timeout=5)
writer="""import json,os,subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[1])
birth=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
(root/'ready.tmp').write_text(json.dumps({'pid':os.getpid(),'birth':birth}))
os.replace(root/'ready.tmp',root/'ready.json')
time.sleep(.5)
subprocess.run(['/usr/bin/git','-C',str(root),'hash-object','-w','--stdin'],input=b'x'*32,check=True,stdout=subprocess.DEVNULL,timeout=2)
(root/'done').write_text('finished')
"""
(root/'writer.py').write_text(writer)
binary=root/'bin';binary.mkdir()
wrapper=binary/'git'
wrapper.write_text('#!'+sys.executable+'\\n'+"""import subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[2])
assert sys.argv[1]=='-C' and sys.argv[3]=='fixture-probe'
subprocess.Popen([sys.executable,str(root/'writer.py'),str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
deadline=time.monotonic()+2
while not (root/'ready.json').exists():
 if time.monotonic()>deadline:raise TimeoutError('writer handshake missing')
 time.sleep(.005)
print('fixture stdout',flush=True)
""")
wrapper.chmod(0o700)
os.environ['PATH']=str(binary)+os.pathsep+os.environ['PATH']
output=RegateCampaignChainTests.git(None,root,'fixture-probe')
assert output=='fixture stdout'
assert (root/'done').exists(),'Git capture returned before redirected writer finished'
identity=json.loads((root/'ready.json').read_text())
try:
 fields=Path('/proc',str(identity['pid']),'stat').read_text().rsplit(')',1)[1].split()
except FileNotFoundError:
 pass
else:
 assert fields[19]!=identity['birth'],'Git writer not reaped before capture returned'
digest=hashlib.sha1(b'blob 32\\0'+b'x'*32).hexdigest()
blob=root/'.git/objects'/digest[:2]/digest[2:]
assert zlib.decompress(blob.read_bytes())==b'blob 32\\0'+b'x'*32
'''
        with chain_temporary_directory() as tmp:
            root = Path(tmp)
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                               PYTHONPATH=os.pathsep.join((str(ROOT / 'app'), str(ROOT))),
                               ALEXANDRIA_DATA_DIR=str(root / 'data'),
                               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
            # The outer owner also contains the intentionally live old-code
            # writer if the discriminator fails, before removing its repository.
            result = run_owned_capture([sys.executable, '-c', worker, str(root)],
                                       env=environment, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_git_preserves_stdout_stripping_and_success_diagnostics(self):
        result = subprocess.CompletedProcess(['git'], 0, '  output\n', 'diagnostic\n')
        with patch(__name__ + '.run_owned_capture', return_value=result) as capture, \
             contextlib.redirect_stderr(io.StringIO()) as diagnostics:
            self.assertEqual('output', RegateCampaignChainTests.git(None, Path('repo'), 'status'))
        capture.assert_called_once_with(['git', '-C', 'repo', 'status'], text=True, timeout=15)
        self.assertEqual('diagnostic\n', diagnostics.getvalue())

    def test_git_command_failure_preserves_output_and_visible_diagnostics(self):
        result = subprocess.CompletedProcess(['git'], 7, 'partial output\n', 'failure diagnostic\n')
        with patch(__name__ + '.run_owned_capture', return_value=result), \
             contextlib.redirect_stderr(io.StringIO()) as diagnostics:
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                RegateCampaignChainTests.git(None, Path('repo'), 'status')
        self.assertEqual(7, caught.exception.returncode)
        self.assertEqual('partial output\n', caught.exception.output)
        self.assertEqual('failure diagnostic\n', diagnostics.getvalue())

    def test_git_timeout_keeps_its_original_error_and_diagnostics(self):
        error = subprocess.TimeoutExpired(['git'], 15, output=b'partial', stderr=b'timeout diagnostic\n')
        with patch(__name__ + '.run_owned_capture', side_effect=error), \
             contextlib.redirect_stderr(io.StringIO()) as diagnostics:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                RegateCampaignChainTests.git(None, Path('repo'), 'status')
        self.assertIs(error, caught.exception)
        self.assertEqual('timeout diagnostic\n', diagnostics.getvalue())

    def test_diagnostic_failure_does_not_mask_command_or_timeout_error(self):
        timeout = subprocess.TimeoutExpired(['git'], 15, stderr=b'failed')
        cases = [({'return_value': subprocess.CompletedProcess(['git'], 7, '', 'failed')},
                  subprocess.CalledProcessError),
                 ({'side_effect': timeout}, subprocess.TimeoutExpired)]
        for options, error_type in cases:
            with self.subTest(error=error_type.__name__), \
                 patch(__name__ + '.run_owned_capture', **options), \
                 patch.object(sys.stderr, 'write', side_effect=OSError('diagnostic replay failed')):
                with self.assertRaises(error_type):
                    RegateCampaignChainTests.git(None, Path('repo'), 'status')

    def test_diagnostic_failure_after_success_is_reported(self):
        result = subprocess.CompletedProcess(['git'], 0, 'output', 'diagnostic')
        with patch(__name__ + '.run_owned_capture', return_value=result), \
             patch.object(sys.stderr, 'write', side_effect=OSError('diagnostic replay failed')):
            with self.assertRaisesRegex(OSError, 'diagnostic replay failed'):
                RegateCampaignChainTests.git(None, Path('repo'), 'status')


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
