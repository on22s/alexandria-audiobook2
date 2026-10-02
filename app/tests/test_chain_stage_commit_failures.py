"""Native Git/Bash failures must survive the chain's final summary."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

LIB = Path(os.environ.get('STAGE_LIB_SOURCE',str(Path(__file__).resolve().parents[2]/'run_chains/lib/stage.sh')))


class StageCommitFailureTests(unittest.TestCase):
    def git(self,repo,*args):
        return subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,check=True).stdout.strip()

    def test_add_commit_and_index_read_errors_survive_summary_and_preserve_unrelated_index(self):
        for failure,expected in [('add',128),('commit',1),('diff',2)]:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);repo=root/'repo';repo.mkdir()
                self.git(repo,'init','-q','-b','main')
                self.git(repo,'config','user.name','Fixture');self.git(repo,'config','user.email','fixture@example.com')
                artifacts=repo/'ab_test_runtime/experiments';artifacts.mkdir(parents=True)
                artifact=artifacts/'current.json';unrelated=repo/'other.txt'
                artifact.write_text('base\n');unrelated.write_text('base\n')
                self.git(repo,'add','-A');self.git(repo,'commit','-q','-m','base');head=self.git(repo,'rev-parse','HEAD')
                artifact.write_text('new measurement\n');unrelated.write_text('other session\n');self.git(repo,'add','other.txt')
                override=''
                if failure=='add':(repo/'.git/index.lock').touch()
                elif failure=='commit':
                    hook=repo/'.git/hooks/pre-commit';hook.write_text('#!/bin/sh\necho fixture commit rejected >&2\nexit 13\n');hook.chmod(0o700)
                else:override='git() { if [ "$3" = diff ]; then echo "fixture index inspection failed" >&2; return 2; fi; command git "$@"; }'
                script=f'''set -uo pipefail
source {shlex.quote(str(LIB))}
STAGE_LOG_DIR={shlex.quote(str(root/'logs'))}
{override}
stage_commit_artifacts current {shlex.quote(str(repo))} {shlex.quote(str(artifact))}
printf '%s\\n' "$?" > {shlex.quote(str(root/'commit-rc'))}
run_stage independent 2s -- touch {shlex.quote(str(root/'continued'))}
stage_summary fixture
'''
                result=subprocess.run(['bash','-c',script],capture_output=True,text=True,timeout=10)
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertEqual(str(expected)+'\n',(root/'commit-rc').read_text())
                self.assertIn('artifacts:current = failed:'+str(expected),result.stdout)
                self.assertIn('1/2 stages ok',result.stdout)
                self.assertTrue((root/'continued').exists())
                self.assertEqual(head,self.git(repo,'rev-parse','HEAD'))
                self.assertEqual('new measurement\n',artifact.read_text())
                self.assertEqual('other session',self.git(repo,'show',':other.txt'))

    def test_command_exit_124_and_fake_timeout_diagnostic_are_not_classified_as_timer_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            script=f'''set -uo pipefail
source {shlex.quote(str(LIB))}
STAGE_LOG_DIR={shlex.quote(tmp)}
run_stage application 3s -- bash -c 'echo "timeout: sending signal INT to command" >&2; exit 124'
stage_summary fixture
'''
            result=subprocess.run(['bash','-c',script],capture_output=True,text=True,timeout=5)
            self.assertEqual(1,result.returncode,result.stdout+result.stderr)
            self.assertIn('FAIL  application rc=124',result.stdout)
            self.assertNotIn('TIMEOUT application',result.stdout)
            self.assertIn('timeout: sending signal',(root/'application.log').read_text())
            self.assertEqual([],list(root.glob('*.timeout.*')))

    def test_real_timer_expiry_stops_worker_and_keeps_original_timeout_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pidfile=root/'pid'
            code=f'import os,pathlib,time;pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(30)'
            script=f'''set -uo pipefail
source {shlex.quote(str(LIB))}
STAGE_LOG_DIR={shlex.quote(tmp)}
run_stage slow 1s -- {shlex.quote(sys.executable)} -c {shlex.quote(code)}
stage_summary fixture
'''
            result=subprocess.run(['bash','-c',script],capture_output=True,text=True,timeout=5)
            self.assertEqual(1,result.returncode,result.stdout+result.stderr)
            self.assertIn('TIMEOUT slow',result.stdout)
            self.assertIn('slow = failed:124',result.stdout)
            self.assertTrue(pidfile.exists())
            with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)
            self.assertIn('sending signal INT',(root/'slow.log').read_text())
            self.assertEqual([],list(root.glob('*.timeout.*')))
