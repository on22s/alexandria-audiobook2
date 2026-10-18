"""Actual historical driver under private Git, CPU workers and native timeouts."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from subprocess_ownership import run_owned_capture

REPO=Path(__file__).resolve().parents[2]


def write_captured_stderr(stderr,*,preserve_error=False):
    try:
        if stderr:
            sys.stderr.write(stderr.decode(errors='replace') if isinstance(stderr, bytes) else stderr)
    except Exception:
        if not preserve_error:
            raise


class MorningChainTests(unittest.TestCase):
    def git(self,*args):
        try:
            result=run_owned_capture(['git','-C',str(self.root),*args],text=True,timeout=15)
        except subprocess.TimeoutExpired as error:
            write_captured_stderr(error.stderr,preserve_error=True)
            raise
        write_captured_stderr(result.stderr,preserve_error=result.returncode!=0)
        result.check_returncode()
        return result.stdout.strip()

    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='morning chain ');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        for folder in ('run_chains/lib','app/experiments','app/env/bin','ab_test_runtime/experiments','ab_test_runtime/reports/overnight_20260818','bin'):(self.root/folder).mkdir(parents=True)
        for relative in ('run_chains/lib/stage.sh','run_chains/lib/server_cleanup.sh','app/experiments/respelling_completion.py'):shutil.copyfile(REPO/relative,self.root/relative)
        source=Path(os.environ.get('MORNING_CHAIN_SOURCE',str(REPO/'run_chains/morning_20260818.sh')))
        shutil.copyfile(source,self.root/'run_chains/morning_20260818.sh')
        self.git('init','-q','-b','main');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.com');self.git('config','core.hooksPath',str(self.root/'no-hooks'))
        self.folder=self.root/'ab_test_runtime/experiments'
        for name in ('staged-wip.json','unstaged-wip.json'):(self.folder/name).write_text('base\n')
        sensitive=self.root/'private.txt';sensitive.write_text('base private\n')
        self.git('add','-A');self.git('commit','-q','-m','fixture base')
        sensitive.write_text('staged private\n');(self.folder/'staged-wip.json').write_text('staged WIP\n');self.git('add','private.txt','ab_test_runtime/experiments/staged-wip.json')
        (self.folder/'unstaged-wip.json').write_text('unstaged WIP\n');(self.folder/'untracked-wip.json').write_text('untracked WIP\n')
        self.write('app/env/bin/python','#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n')
        self.write('bin/date','#!/bin/sh\nif [ "$1" = -d ]; then echo "$FIXTURE_DEADLINE"; else exec /usr/bin/date "$@"; fi\n')
        self.write('gpu_job.sh','#!/bin/bash\necho "$1" >> "$FIXTURE_ROOT/jobs"\nshift\nexec "$@"\n')
        self.write('ensure_llama_server.sh','#!/bin/bash\ntouch "$FIXTURE_ROOT/server_called"\nexit "${FIXTURE_SERVER_RC:-0}"\n')
        self.write('run_chains/unseen_books.sh','#!/bin/bash\ntouch "$FIXTURE_ROOT/unseen_called"\nmkdir -p "$FIXTURE_ROOT/ab_test_runtime/unseen_books"\necho generated > "$FIXTURE_ROOT/ab_test_runtime/unseen_books/book.json"\nexit "${FIXTURE_UNSEEN_RC:-0}"\n')
        self.write('app/experiments/measure_respellings.py', '''import json,os,pathlib,sys
args=sys.argv;limit=int(args[args.index('--limit')+1]);out=pathlib.Path(args[args.index('--out')+1]);mode=os.environ.get('FIXTURE_MODE','success')
rows=limit-1 if mode=='partial' else limit
out.write_text(json.dumps({'status':'partial' if mode=='partial' else 'complete','candidates_considered':limit,'results':[{'term':'term'+str(i)} for i in range(rows)]}))
raise SystemExit(7 if mode=='fail_full' else 0)
''')
        self.write('app/experiments/pair_e_row.py','import os,pathlib,sys\np=pathlib.Path(os.environ["FIXTURE_ROOT"])/"scored"\nwith p.open("a") as f:f.write(sys.argv[1]+"\\n")\nraise SystemExit(4 if os.environ.get("FIXTURE_MODE")=="score_fail" else 0)\n')
        self.env=dict(os.environ,FIXTURE_ROOT=str(self.root),FIXTURE_DEADLINE=str(int(time.time())+20000),PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH'])

    def write(self,relative,source):
        path=self.root/relative;path.write_text(source);path.chmod(0o755)

    def run_chain(self,**env):
        return run_owned_capture(['bash',str(self.root/'run_chains/morning_20260818.sh')],cwd=self.root.parent,env=dict(self.env,**env),text=True,timeout=15)

    def write_late_git_writer(self):
        actual_git=shutil.which('git')
        (self.root/'late-writer.py').write_text('''import json,os,subprocess,sys,time
from pathlib import Path
root=Path(__file__).parent
birth=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
(root/'late-writer.json').write_text(json.dumps({'pid':os.getpid(),'birth':birth}))
time.sleep(1)
subprocess.run([sys.argv[1],'-C',str(root),'hash-object','-w','--stdin'],input=b'late fixture object',stdout=subprocess.DEVNULL,check=True,timeout=2)
(root/'late-write-done').write_text('finished')
''')
        (self.root/'late-launcher.py').write_text('''import subprocess,sys,time
from pathlib import Path
root=Path(__file__).parent
subprocess.Popen([sys.executable,str(root/'late-writer.py'),sys.argv[1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
deadline=time.monotonic()+2
while not (root/'late-writer.json').exists():
 if time.monotonic()>deadline:raise RuntimeError('writer did not start')
 time.sleep(.01)
print('fixture stdout');print('fixture stderr',file=sys.stderr)
''')
        command='#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(self.root/'late-launcher.py'))+' '+shlex.quote(actual_git)+'\n'
        self.write('bin/git',command)
        self.write('run_chains/morning_20260818.sh',command)

    def ensure_late_writer_stopped(self):
        # Also retire the deliberate old-code counterexample before rmtree.
        import select
        import signal
        from subprocess_ownership import ensure_pidfd_support
        record=self.root/'late-writer.json'
        if not record.exists():
            return
        identity=json.loads(record.read_text());pidfds=ensure_pidfd_support();descriptor=None
        try:
            path=Path('/proc',str(identity['pid']),'stat')
            self.assertEqual(identity['birth'],path.read_text().rsplit(')',1)[1].split()[19])
            descriptor=pidfds.pidfd_open(identity['pid'],0)
            self.assertEqual(identity['birth'],path.read_text().rsplit(')',1)[1].split()[19])
            if not select.select([descriptor],[],[],5)[0]:
                pidfds.pidfd_send_signal(descriptor,signal.SIGKILL,None,0)
                self.assertTrue(select.select([descriptor],[],[],5)[0])
        except (FileNotFoundError,ProcessLookupError):
            pass
        finally:
            if descriptor is not None:os.close(descriptor)

    def assert_late_writer_finished(self):
        self.assertTrue((self.root/'late-write-done').exists(),
                        'capture returned before redirected Git writer finished')
        identity=json.loads((self.root/'late-writer.json').read_text())
        self.assertFalse(Path('/proc',str(identity['pid'])).exists(),'owned writer was not reaped')
        content=self.root/'late-content';content.write_bytes(b'late fixture object')
        digest=self.git('hash-object',str(content))
        self.assertTrue((self.root/'.git/objects'/digest[:2]/digest[2:]).is_file())

    def test_chain_capture_waits_for_redirected_late_git_writer(self):
        self.write_late_git_writer()
        try:
            result=self.run_chain()
            self.assertEqual((0,'fixture stdout\n','fixture stderr\n'),
                             (result.returncode,result.stdout,result.stderr))
            # Use the real Git again for object verification.
            (self.root/'bin/git').unlink()
            self.assert_late_writer_finished()
        finally:
            self.ensure_late_writer_stopped()

    def test_git_capture_waits_for_redirected_late_git_writer(self):
        self.write_late_git_writer();diagnostic=io.StringIO()
        try:
            with patch.dict(os.environ,PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH']):
                with contextlib.redirect_stderr(diagnostic):
                    self.assertEqual('fixture stdout',self.git('status'))
            self.assertEqual('fixture stderr\n',diagnostic.getvalue())
            self.assert_late_writer_finished()
        finally:
            self.ensure_late_writer_stopped()

    def test_git_replays_decoded_stderr_on_success_and_before_nonzero_error(self):
        for code in (0,7):
            with self.subTest(code=code):
                self.write('bin/git',"#!/bin/sh\nprintf 'fixture output\\n'; printf 'fixture diagnostic\\n' >&2; exit "+str(code)+'\n')
                diagnostic=io.StringIO()
                with patch.dict(os.environ,PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH']):
                    with contextlib.redirect_stderr(diagnostic):
                        if code:
                            with self.assertRaises(subprocess.CalledProcessError) as caught:
                                self.git('status')
                            self.assertEqual((7,'fixture output\n','fixture diagnostic\n'),
                                             (caught.exception.returncode,caught.exception.stdout,caught.exception.stderr))
                        else:
                            self.assertEqual('fixture output',self.git('status'))
                self.assertEqual('fixture diagnostic\n',diagnostic.getvalue())

    def test_stderr_replay_failure_preserves_original_git_error_or_timeout(self):
        class BrokenSink(io.StringIO):
            def write(self,value):raise OSError('secondary diagnostic failure')
        timeout=subprocess.TimeoutExpired(['git'],15,stderr=b'partial diagnostic')
        for result in (subprocess.CompletedProcess(['git'],7,'output','diagnostic'),timeout):
            with self.subTest(result=type(result).__name__):
                kwargs={'side_effect':result} if isinstance(result,Exception) else {'return_value':result}
                with patch(__name__+'.run_owned_capture',**kwargs):
                    with patch.object(sys,'stderr',BrokenSink()):
                        with self.assertRaises((subprocess.CalledProcessError,subprocess.TimeoutExpired)) as caught:
                            self.git('status')
                if isinstance(result,Exception):self.assertIs(timeout,caught.exception)
                else:self.assertEqual((7,'output','diagnostic'),
                                      (caught.exception.returncode,caught.exception.stdout,caught.exception.stderr))

    def test_stderr_replay_failure_after_success_remains_an_error(self):
        class BrokenSink(io.StringIO):
            def write(self,value):raise OSError('diagnostic failure')
        with patch(__name__+'.run_owned_capture',return_value=subprocess.CompletedProcess(['git'],0,'output','diagnostic')):
            with patch.object(sys,'stderr',BrokenSink()):
                with self.assertRaisesRegex(OSError,'diagnostic failure'):self.git('status')

    def test_chain_preserves_cwd_env_nonzero_and_completed_process(self):
        self.write('run_chains/morning_20260818.sh',
                   '#!/bin/sh\nprintf "%s\\n" "$FIXTURE_CHECK"; pwd; printf "diagnostic\\n" >&2; exit 7\n')
        result=self.run_chain(FIXTURE_CHECK='explicit environment')
        self.assertIsInstance(result,subprocess.CompletedProcess)
        self.assertEqual((7,'explicit environment\n'+str(self.root.parent)+'\n','diagnostic\n'),
                         (result.returncode,result.stdout,result.stderr))

    def test_git_and_chain_keep_fifteen_second_bound_and_retire_on_timeout(self):
        for name in ('git','chain'):
            with self.subTest(name=name):
                self.write_late_git_writer()
                real_capture=run_owned_capture
                def bounded_capture(command,*,timeout,**kwargs):
                    self.assertEqual(15,timeout)
                    return real_capture(command,timeout=.5,**kwargs)
                diagnostic=io.StringIO()
                try:
                    with patch(__name__+'.run_owned_capture',side_effect=bounded_capture):
                        with contextlib.redirect_stderr(diagnostic):
                            if name=='git':
                                with patch.dict(os.environ,PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH']):
                                    with self.assertRaises(subprocess.TimeoutExpired):self.git('status')
                            else:
                                with self.assertRaises(subprocess.TimeoutExpired):self.run_chain()
                    identity=json.loads((self.root/'late-writer.json').read_text())
                    self.assertFalse(Path('/proc',str(identity['pid'])).exists())
                    self.assertFalse((self.root/'late-write-done').exists())
                    if name=='git':self.assertEqual('fixture stderr\n',diagnostic.getvalue())
                finally:
                    self.ensure_late_writer_stopped()
                (self.root/'late-writer.json').unlink()

    def test_success_commits_only_each_generated_output_preserving_other_sessions_index_and_work(self):
        head=self.git('rev-parse','HEAD');index=self.git('diff','--cached');result=self.run_chain()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(index,self.git('diff','--cached'))
        names=set(self.git('diff','--name-only',head,'HEAD').splitlines())
        self.assertEqual({'ab_test_runtime/experiments/respelling_e_row__ay_n'+str(n)+'.json' for n in (800,1200,1600)},names)
        self.assertEqual('base private',self.git('show','HEAD:private.txt'))
        self.assertEqual('unstaged WIP\n',(self.folder/'unstaged-wip.json').read_text())
        self.assertEqual('untracked WIP\n',(self.folder/'untracked-wip.json').read_text())
        self.assertIn('untracked-wip.json',self.git('ls-files','--others'))
        self.assertTrue((self.root/'unseen_called').exists())

    def test_server_failure_prevents_dependent_launch_and_fails_summary(self):
        result=self.run_chain(FIXTURE_DEADLINE=str(int(time.time())+6000),FIXTURE_SERVER_RC='7')
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertTrue((self.root/'server_called').exists());self.assertFalse((self.root/'unseen_called').exists())
        self.assertIn('server = failed:7',result.stdout);self.assertIn('unseen_books = skipped',result.stdout)

    def test_failed_or_partial_generation_is_not_scored_and_independent_blocks_still_attempt(self):
        for mode in ('fail_full','partial','score_fail'):
            with self.subTest(mode=mode):
                for marker in ('jobs','scored'):
                    p=self.root/marker
                    if p.exists():p.unlink()
                for p in self.folder.glob('respelling_e_row__ay_n*.json'):p.unlink()
                result=self.run_chain(FIXTURE_MODE=mode)
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertEqual(3,len((self.root/'jobs').read_text().splitlines()))
                if mode=='score_fail':self.assertEqual(3,len((self.root/'scored').read_text().splitlines()))
                else:self.assertFalse((self.root/'scored').exists())
                self.assertTrue((self.root/'unseen_called').exists())

    def test_empty_partial_without_count_is_remeasured_instead_of_skipped(self):
        for n in (800,1200,1600):
            (self.folder/('respelling_e_row__ay_n'+str(n)+'.json')).write_text(json.dumps({'status':'partial','results':[]}))
        result=self.run_chain()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertTrue((self.root/'jobs').exists(),'partial files were all skipped')
        self.assertEqual(3,len((self.root/'jobs').read_text().splitlines()))
        for n in (800,1200,1600):self.assertEqual(n,len(json.loads((self.folder/('respelling_e_row__ay_n'+str(n)+'.json')).read_text())['results']))
