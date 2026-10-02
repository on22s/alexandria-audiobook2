"""Disposable Bash chain, real flock, native replay provenance and scoped commits."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.test_gpu_job import REPO, GPU_JOB, isolated_env, copy_gpu_owner
from tests.test_chain_failure_guards import prepare_artifact_commit_fixture


class ReplayLockScopeTests(unittest.TestCase):
    def execute(self, fail_first=False, real_wrapper=False):
        with tempfile.TemporaryDirectory(prefix='replay lock fixture ') as tmp:
            root = Path(tmp); app = root / 'app/experiments'; app.mkdir(parents=True)
            python = root / 'app/env/bin/python'; python.parent.mkdir(parents=True); python.symlink_to(sys.executable)
            data = root / 'ab_test_runtime/experiments'; data.mkdir(parents=True)
            audit = root / 'ab_test_runtime/audit/artifact_structural_audit.json'; audit.parent.mkdir(parents=True)
            # This is a real kernel lock instrument, not the environment sentinel.
            probe = '''import fcntl,json,os,pathlib
fd=os.open(os.environ['GPU_LOCK'],os.O_RDWR|os.O_CREAT,0o600)
held=False
try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:held=True
finally:os.close(fd)
with open(os.environ['FIXTURE_EVENTS'],'a') as h:h.write(json.dumps({'step':STEP,'held':held})+'\\n')
assert held == EXPECTED, (STEP,held)
'''
            free = probe.replace('STEP', repr('bookkeeping')).replace('EXPECTED', 'False')
            source = Path(REPO, 'app/experiments/replay_artifact.py').read_text()
            (app / 'replay_artifact.py').write_text(source.replace('import argparse\n', free + '\nimport argparse\n', 1))
            worker = probe.replace('STEP', "'worker-' + str(pathlib.Path(__file__).stem)").replace('EXPECTED', 'True')
            for name in ('first', 'second'):
                (app / (name + '.py')).write_text(worker + '\n' +
                    f"p=pathlib.Path(__file__).parents[2]/'ab_test_runtime/experiments/{name}.json'\n" +
                    "d=json.loads(p.read_text());d['replayed']=True;p.write_text(json.dumps(d))\n" +
                    ('raise SystemExit(7)\n' if name == 'first' and fail_first else ''))
                (data / (name + '.json')).write_text(json.dumps({'provenance': {'script':name+'.py','args':{}}}))
            audit.write_text(json.dumps({'artifacts': [{'artifact': name+'.json','dirty': True} for name in ('first','second')]}))
            prepare_artifact_commit_fixture(root)
            # The worker wrapper uses a real private flock and validates inherited claims
            # using the production read-only ownership verifier. No GPU or inference.
            wrapper = root / 'gpu_job.sh'
            wrapper.write_text('''#!/bin/bash
if [ "${1:-}" = --check-lock-owner ]; then exec bash REAL_GATE "$@"; fi
name="$1"; shift
exec 9>"$GPU_LOCK"
flock -x 9 || exit 1
export ALEXANDRIA_GPU_LOCK_HELD=1 ALEXANDRIA_GPU_LOCK_PID=$$
"$@" 9>&-; rc=$?
flock -u 9
exit "$rc"
'''.replace('REAL_GATE', shlex.quote(GPU_JOB)))
            wrapper.chmod(0o755)
            # Prove artifact commits also run outside the replay lease.
            stage = root / 'run_chains/lib/stage.sh'
            stage.write_text(stage.read_text().replace('stage_commit_artifacts() {',
                'stage_commit_artifacts() {\n    "$REPO/app/env/bin/python" -c ' + shlex.quote(free) + ' || return 1',1))
            original = Path(os.environ.get('REPLAY_LOCK_SOURCE', str(Path(REPO, 'run_chains/replay_dirty_evidence_20260817.sh')))).read_text()
            original = original.replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git', 'REPO='+shlex.quote(tmp))
            original = original.replace('/tmp/replay_rest.txt',shlex.quote(str(root/'ab_test_runtime/remaining.txt')))
            chain = root / 'chain.sh'; chain.write_text(original); chain.chmod(0o755)
            env = isolated_env(str(root/'ab_test_runtime'), FIXTURE_EVENTS=str(root/'ab_test_runtime/events.jsonl'), GPU_NOTIFY='0')
            for key in ('ALEXANDRIA_GPU_LOCK_HELD','ALEXANDRIA_GPU_LOCK_PID','ALEXANDRIA_GPU_LOCK_FD'):
                env.pop(key,None)
            if real_wrapper:
                (root/'.gitignore').write_text('__pycache__/\n')
                shutil.copyfile(GPU_JOB, wrapper)
                copy_gpu_owner(str(root))
                for args in (('add','-A'),('commit','-q','-m','native CPU queue fixture')):
                    subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
            result = subprocess.run(['bash',str(chain)],cwd=root,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(1 if fail_first else 0,result.returncode,result.stdout+result.stderr+'\n'+ '\n'.join(p.read_text() for p in (root/'ab_test_runtime/logs/replay_evidence').glob('*.log')))
            events = [json.loads(line) for line in (root/'ab_test_runtime/events.jsonl').read_text().splitlines()]
            self.assertEqual([{'step':'worker-first','held':True},{'step':'worker-second','held':True}],
                [e for e in events if e['step'].startswith('worker-')])
            self.assertTrue(all(not e['held'] for e in events if e['step']=='bookkeeping'),events)
            self.assertEqual(5,len([e for e in events if e['step']=='bookkeeping']))
            self.assertTrue(all(json.loads((data/(name+'.json')).read_text())['replayed'] for name in ('first','second')))
            self.assertEqual(not fail_first,'REPLAY COMPLETE' in result.stdout)
            # Final flock is available after each wrapper has returned.
            import fcntl
            with open(env['GPU_LOCK'],'a') as handle:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_bookkeeping_and_commits_release_lock_each_replay_retains_it(self):
        self.execute()

    def test_worker_failure_is_reported_after_later_independent_replay(self):
        self.execute(fail_first=True)

    def test_production_gpu_wrapper_retains_owned_worker_cleanup_and_releases_between_cases(self):
        self.execute(real_wrapper=True)
