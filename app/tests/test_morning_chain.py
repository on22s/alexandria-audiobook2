"""Actual historical driver under private Git, CPU workers and native timeouts."""
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

REPO=Path(__file__).resolve().parents[2]


class MorningChainTests(unittest.TestCase):
    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],text=True).strip()

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
        return subprocess.run(['bash',str(self.root/'run_chains/morning_20260818.sh')],cwd=self.root.parent,env=dict(self.env,**env),capture_output=True,text=True,timeout=15)

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
