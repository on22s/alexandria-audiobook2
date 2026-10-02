"""Validate the ownership instrument with real temporary kernel flock fixtures."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import gpu_guard

ROOT=Path(__file__).resolve().parents[2]
GPU_JOB=ROOT/'gpu_job.sh'

OWNER_WORKER=r"""
import fcntl,json,os,pathlib,subprocess,sys
root,temporary,mode,nested=sys.argv[1:]
lock=pathlib.Path(temporary)/'gpu.lock';other=pathlib.Path(temporary)/'other.lock'
path=other if mode=='wrong-file' else lock
fd=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
if fd!=9:os.dup2(fd,9);os.close(fd)
if mode!='unlocked':fcntl.flock(9,fcntl.LOCK_SH if mode=='shared' else fcntl.LOCK_EX)
env=dict(os.environ,GPU_LOCK=str(lock),ALEXANDRIA_GPU_LOCK_HELD='1',ALEXANDRIA_GPU_LOCK_PID=str(os.getpid()))
cli=['bash',root+'/gpu_job.sh','--check-lock-owner',str(os.getpid())]
if nested=='yes':
 import shlex
 cli=['bash','-c','bash -c '+shlex.quote(' '.join(shlex.quote(x) for x in cli)+'; rc=$?; exit "$rc"')+'; rc=$?; exit "$rc"']
result=subprocess.run(cli,env=env,capture_output=True,text=True,close_fds=True,timeout=5)
print(json.dumps({'rc':result.returncode,'stderr':result.stderr,'fdinfo':pathlib.Path('/proc/self/fdinfo/9').read_text()}))
if mode=='exclusive':
 env['PYTHONPATH']=root+'/app'
 code="import os;from experiments.gpu_guard import is_queue_lock_held_by_us,require_free_gpu,acquire_gpu_lock;"+"\ntry:os.fstat(9);raise AssertionError('fd9 inherited')\nexcept OSError:pass\nassert is_queue_lock_held_by_us();require_free_gpu('CPU fixture');assert acquire_gpu_lock() is None;print('GUARD_ACCEPTED_FD9_CLOSED')"
 result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,close_fds=True,timeout=5)
 print(json.dumps({'guard_rc':result.returncode,'stdout':result.stdout,'stderr':result.stderr}))
"""


def run_owned_cpu_chain(command, root, env=None, **kwargs):
    """Run a model-free chain fixture under a real temporary fd9 owner."""
    root = Path(root)
    wrapper = root / "gpu_job.sh"
    if not wrapper.exists():
        wrapper.write_text('#!/bin/bash\nexec bash ' + str(GPU_JOB) + ' "$@"\n')
        wrapper.chmod(0o755)
    environment = dict(os.environ if env is None else env)
    environment["GPU_LOCK"] = str(root / "owned_cpu_fixture.lock")
    environment["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
    owner = ('exec 9>"$GPU_LOCK"; flock -x 9; '
             'export ALEXANDRIA_GPU_LOCK_PID=$$; '
             '"$@" 9>&-; rc=$?; exit "$rc"')
    return subprocess.run(["bash", "-c", owner, "owned-cpu-fixture"] + command,
                          env=environment, **kwargs)


class KernelGpuLockOwnershipTests(unittest.TestCase):
    def test_real_exclusive_owner_accepts_direct_nested_descendants_and_closed_fd9(self):
        for nested in ('no','yes'):
            with self.subTest(nested=nested),tempfile.TemporaryDirectory() as tmp:
                Path(tmp,'gpu.lock').touch()
                result=subprocess.run([sys.executable,'-c',OWNER_WORKER,str(ROOT),tmp,'exclusive',nested],capture_output=True,text=True,timeout=15)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                lines=[json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(0,lines[0]['rc'],lines[0])
                self.assertIn('FLOCK',lines[0]['fdinfo']);self.assertIn('WRITE',lines[0]['fdinfo'])
                self.assertEqual(0,lines[1]['guard_rc'],lines[1]);self.assertIn('GUARD_ACCEPTED_FD9_CLOSED',lines[1]['stdout'])
                self.assertEqual({'gpu.lock'},{path.name for path in Path(tmp).iterdir()})

    def test_live_ancestor_with_unlocked_shared_or_wrong_file_fd9_is_rejected(self):
        for mode in ('unlocked','shared','wrong-file'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                Path(tmp,'gpu.lock').touch()
                result=subprocess.run([sys.executable,'-c',OWNER_WORKER,str(ROOT),tmp,mode,'no'],capture_output=True,text=True,timeout=10)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                observed=json.loads(result.stdout)
                self.assertNotEqual(0,observed['rc'],observed)
                self.assertIn('cannot verify inherited GPU lock',observed['stderr'])
                if mode=='shared':self.assertIn('READ',observed['fdinfo'])

    def test_missing_malformed_nonancestor_and_exited_owner_claims_refuse_without_queue_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock=Path(tmp,'gpu.lock');lock.touch()
            dead=subprocess.Popen([sys.executable,'-c','pass']);dead.wait()
            live=subprocess.Popen([sys.executable,'-c',"import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR);os.dup2(fd,9);fcntl.flock(9,fcntl.LOCK_EX);print('held',flush=True);time.sleep(30)",str(lock)],stdout=subprocess.PIPE,text=True)
            self.assertEqual('held',live.stdout.readline().strip())
            self.assertIn('FLOCK',Path('/proc/'+str(live.pid)+'/fdinfo/9').read_text())
            try:
                for owner in ('','not-a-pid','-1','0','01',str(dead.pid),str(live.pid)):
                    with self.subTest(owner=owner):
                        result=subprocess.run(['bash',str(GPU_JOB),'--check-lock-owner',owner],env={**os.environ,'GPU_LOCK':str(lock),'GPU_QLOG':str(Path(tmp,'queue.log'))},capture_output=True,text=True,timeout=5)
                        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
                        self.assertIn('cannot verify inherited GPU lock',result.stderr)
                        self.assertFalse(Path(tmp,'queue.log').exists())
                self.assertEqual({'gpu.lock'},{path.name for path in Path(tmp).iterdir()})
            finally:live.terminate();live.wait(timeout=5);live.stdout.close()

    def test_python_guard_does_not_accept_forged_live_pid_or_marker_without_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,'gpu.lock');path.touch()
            for owner in ('',str(os.getpid()),'invalid'):
                with self.subTest(owner=owner),patch.dict(os.environ,{'ALEXANDRIA_GPU_LOCK_HELD':'1','ALEXANDRIA_GPU_LOCK_PID':owner,'GPU_LOCK':str(path)}):
                    with self.assertRaisesRegex(RuntimeError,'inherited GPU lock'):
                        gpu_guard.require_free_gpu('forged claim',str(path))
                    with self.assertRaisesRegex(RuntimeError,'inherited GPU lock'):
                        gpu_guard.acquire_gpu_lock(str(path))

    def test_existing_shell_guard_blocks_all_forged_claims_before_gpu_body(self):
        chains=[]
        pattern=r'(?m)^if \[ "\$\{ALEXANDRIA_GPU_LOCK_HELD:-[^}]*\}" != 1 \]; then\n.*?^fi\n'
        for path in sorted((ROOT/'run_chains').glob('*.sh')):
            matches=list(re.finditer(pattern,path.read_text(),re.S))
            if matches:chains.append((path.name,matches[0].group()))
        pdnc = (ROOT/'run_chains/pdnc_context_evidence.sh').read_text()
        self.assertIn('source "$repo/run_chains/lib/llm_campaign.sh"', pdnc)
        self.assertIn('ensure_llm_campaign_lease "pdnc_${intervention}"', pdnc)
        campaign = (ROOT/'run_chains/lib/llm_campaign.sh').read_text()
        chains.append(('pdnc_context_evidence.sh', campaign + '\nensure_llm_campaign_lease fixture true || exit $?\n'))
        replay = (ROOT/'run_chains/replay_dirty_evidence_20260817.sh').read_text()
        replay_guard = re.search(r'(?m)^if \[ "\$\{ALEXANDRIA_GPU_LOCK_HELD:-[^}]*\}" = 1 \]; then\n.*?^fi\n', replay, re.S)
        self.assertIsNotNone(replay_guard)
        chains.append(('replay_dirty_evidence_20260817.sh', replay_guard.group()))
        managed = (ROOT/'run_chains/lib/managed_server.sh').read_text()
        self.assertIn('ensure_managed_server_lease muse_local_reasoninglow_20260914',
                      (ROOT/'run_chains/muse_local_reasoninglow_20260914.sh').read_text())
        managed = managed[managed.index('MANAGED_SERVER_PID='):]
        chains.append(('muse_local_reasoninglow_20260914.sh', campaign + '\n' + managed +
                       '\nensure_managed_server_lease fixture true || exit $?\n'))
        self.assertEqual(14,len(chains))
        for name,block in chains:
            with self.subTest(chain=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);wrapper=root/'gpu_job.sh'
                wrapper.write_text('#!/bin/bash\nif [ "${1:-}" = "--check-lock-owner" ]; then exec bash '+str(GPU_JOB)+' "$@"; fi\nprintf "QUEUED\\n" > "$FIXTURE_QUEUE"\nexit 77\n');wrapper.chmod(0o755)
                probe=root/'probe.sh';probe.write_text('REPO='+str(root)+'\nrepo='+str(root)+'\nruntime_root='+str(root)+'\n'+block+'printf "GPU_BODY_REACHED\\n"\n')
                env={**os.environ,'GPU_LOCK':str(root/'gpu.lock'),'FIXTURE_QUEUE':str(root/'queue.log'),'ALEXANDRIA_GPU_LOCK_HELD':'1','ALEXANDRIA_GPU_LOCK_PID':str(os.getpid())}
                (root/'gpu.lock').touch()
                result=subprocess.run(['bash',str(probe)],env=env,capture_output=True,text=True,timeout=5)
                self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertNotIn('GPU_BODY_REACHED',result.stdout);self.assertFalse((root/'queue.log').exists())
                env.pop('ALEXANDRIA_GPU_LOCK_HELD');env.pop('ALEXANDRIA_GPU_LOCK_PID')
                result=subprocess.run(['bash',str(probe)],env=env,capture_output=True,text=True,timeout=5)
                if name == 'replay_dirty_evidence_20260817.sh':
                    # Orchestration enters CPU bookkeeping first; the native replay
                    # fixture separately proves every actual worker acquires a lease.
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertFalse((root/'queue.log').exists())
                    self.assertIn('GPU_BODY_REACHED',result.stdout)
                else:
                    self.assertEqual(77,result.returncode,result.stdout+result.stderr)
                    self.assertEqual('QUEUED\n',(root/'queue.log').read_text());self.assertNotIn('GPU_BODY_REACHED',result.stdout)

    def test_readonly_default_probe_does_not_create_runtime_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'gpu_job.sh';script.write_bytes(GPU_JOB.read_bytes())
            env=dict(os.environ);env.pop('GPU_LOCK',None)
            result=subprocess.run(['bash',str(script),'--check-lock-owner',str(os.getpid())],env=env,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(0,result.returncode);self.assertIn('authoritative lock is unreadable',result.stderr)
            self.assertEqual({'gpu_job.sh'},{path.name for path in root.iterdir()})

    def test_ancestor_waiting_for_exclusive_lock_is_not_accepted_as_owner(self):
        import signal,time
        source=r"""
import fcntl,json,os,pathlib,subprocess,sys,time
job,lock,report=sys.argv[1:]
fd=os.open(lock,os.O_RDWR)
if fd!=9:os.dup2(fd,9);os.close(fd)
owner=os.getpid()
child=os.fork()
if child==0:
 os.close(9)
 deadline=time.monotonic()+3;waiting=''
 while time.monotonic()<deadline:
  for line in pathlib.Path('/proc/locks').read_text().splitlines():
   tokens=line.split()
   if '->' in tokens and 'FLOCK' in tokens and tokens[tokens.index('FLOCK')+3]==str(owner):waiting=line;break
  if waiting:break
  time.sleep(0.02)
 result=subprocess.run(['bash',job,'--check-lock-owner',str(owner)],env=dict(os.environ,GPU_LOCK=lock),capture_output=True,text=True,close_fds=True,timeout=5)
 pathlib.Path(report).write_text(json.dumps({'waiting':waiting,'rc':result.returncode,'stderr':result.stderr,'fdinfo':pathlib.Path('/proc/'+str(owner)+'/fdinfo/9').read_text()}))
 os._exit(0)
fcntl.flock(9,fcntl.LOCK_EX)
os.waitpid(child,0)
"""
        with tempfile.TemporaryDirectory() as tmp:
            lock=Path(tmp,'gpu.lock');report=Path(tmp,'report.json')
            with lock.open('w') as held:
                fcntl.flock(held,fcntl.LOCK_EX)
                process=subprocess.Popen([sys.executable,'-c',source,str(GPU_JOB),str(lock),str(report)],start_new_session=True)
                try:
                    deadline=time.monotonic()+5
                    while not report.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(0.02)
                    self.assertTrue(report.exists(),'temporary waiting-owner fixture did not report')
                    observed=json.loads(report.read_text());self.assertIn('->',observed['waiting'])
                    self.assertNotEqual(0,observed['rc'],observed)
                    self.assertIn('no acquired exclusive flock',observed['stderr'])
                finally:
                    if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
                    process.wait(timeout=5)


    def test_real_queue_accepts_its_cpu_child_after_closing_fd9(self):
        from tests.test_gpu_job import isolated_env
        code = """
import os
from experiments.gpu_guard import require_free_gpu,acquire_gpu_lock
try:
    os.fstat(9)
    raise AssertionError('queue command inherited fd9')
except OSError:
    pass
require_free_gpu('CPU queue ownership fixture')
assert acquire_gpu_lock() is None
print('REAL_QUEUE_OWNER_ACCEPTED_FD9_CLOSED')
"""
        with tempfile.TemporaryDirectory() as tmp:
            env=isolated_env(tmp,ALLOW_DIRTY_TREE='1',GPU_NOTIFY='0',REQUIRE_LLM='0')
            env['PYTHONPATH']=str(ROOT/'app')
            result=subprocess.run(['bash',str(GPU_JOB),'ownership-cpu-fixture',sys.executable,'-c',code],env=env,cwd=ROOT,capture_output=True,text=True,timeout=15)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertIn('REAL_QUEUE_OWNER_ACCEPTED_FD9_CLOSED',result.stdout)
            log=Path(env['GPU_QLOG']).read_text()
            self.assertRegex(log,r'QUEUED\s+ownership-cpu-fixture')
            self.assertRegex(log,r'START\s+ownership-cpu-fixture')
            self.assertRegex(log,r'OK\s+ownership-cpu-fixture')
            self.assertEqual([],list(Path(env['GPU_PENDING_DIR']).iterdir()))
