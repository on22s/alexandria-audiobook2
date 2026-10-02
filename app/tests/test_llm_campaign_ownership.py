"""Real campaign scripts/queue/leases with CPU server and evaluator providers."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest

from tests.test_gpu_job import REPO
from tests import test_stage_gpu_reclamation as reclamation
from tests.test_subprocess_finally import is_live


CHAINS = ('goal_13_confirmation_20260830.sh', 'unanswered_stratification_20260830.sh')
STEMS = 'emma mansfieldpark northangerabbey persuasion senseandsensibility ahandfulofdust thegambler themysteriousaffairatstyles'.split()


class LlmCampaignOwnershipTests(unittest.TestCase):
    setUpFixture = reclamation.StageGpuReclamationTests.setUp
    wait_marker = reclamation.StageGpuReclamationTests.wait_marker

    def setUp(self):
        self.setUpFixture()
        for chain in CHAINS:
            shutil.copy(Path(REPO) / 'run_chains' / chain, self.root / 'run_chains' / chain)
        for folder in ('app/env/bin', 'app/experiments', 'app/fixtures',
                       'ab_test_runtime/logs', 'ab_test_runtime/experiments',
                       'ab_test_runtime/distill/gguf/new_20260824'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        (self.root / 'app/env/bin/python').symlink_to(sys.executable)
        for name in ('serving_results.py', 'serving_environment.py', 'manifest.py'):
            shutil.copyfile(Path(REPO) / 'app/experiments' / name, self.root / 'app/experiments' / name)
        (self.root / 'app/lmstudio_settings.py').write_text("def get_lmstudio_status(model): return {}\n"
            "def get_current_status(mode,url,model): return dict(available=True,loaded=True,context_length=8192,parallel=2,optimized=None,runtime='llama.cpp')\n"
            "def get_gpu_name_and_backend(): return ('NVIDIA fixture GPU', 'cuda')\n")
        shutil.copy(Path(REPO) / 'app/experiments/pdnc_results.py', self.root / 'app/experiments/pdnc_results.py')
        (self.root / 'ab_test_runtime/distill/gguf/new_20260824/adapter_author_heldout_balanced.gguf').write_bytes(b'fixture')
        for stem in STEMS:
            (self.root / f'app/fixtures/attribution_gold_pdnc_{stem}.json').write_text(json.dumps({
                'book': stem, 'entries': [{'id': stem+'-0', 'expected_speaker': 'Alice', 'line': 'Hello'}]}))
        ensure = self.root / 'ensure_llama_server.sh'
        ensure.write_text('''#!/bin/bash
echo start >> "$FIXTURE_ROOT/started"
bash "$FIXTURE_WRAPPER" --check-lock-owner "$ALEXANDRIA_GPU_LOCK_PID" "$ALEXANDRIA_GPU_LOCK_FD" || exit 4
if [ "${FIXTURE_SERVER_FAIL:-0}" = 1 ]; then exit 1; fi
python3 -c 'import os,pathlib,time;pathlib.Path(os.environ["FIXTURE_ROOT"],"server.pid").write_text(str(os.getpid()));time.sleep(60)' &
for i in {1..100}; do test -s "$FIXTURE_ROOT/server.pid" && exit 0; sleep .01; done
exit 1
''')
        ensure.chmod(0o755)
        self.preflight = self.root / 'app/experiments/llm_preflight.py'
        self.preflight.write_text('''import os,pathlib,subprocess
r=pathlib.Path(os.environ['FIXTURE_ROOT'])
subprocess.run(['bash',os.environ['FIXTURE_WRAPPER'],'--check-lock-owner',os.environ['ALEXANDRIA_GPU_LOCK_PID'],os.environ['ALEXANDRIA_GPU_LOCK_FD']],check=True)
assert (r/'server.pid').exists(), 'preflight ran before startup'
with (r/'preflights').open('a') as f: f.write('checked\\n')
raise SystemExit(1 if os.environ.get('FIXTURE_PREFLIGHT_FAIL') == '1' else 0)
''')
        evaluator = '''import json,os,pathlib,sys,time
r=pathlib.Path(os.environ['FIXTURE_ROOT'])
assert (r/'server.pid').exists()
with (r/'evaluators').open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('FIXTURE_EVALUATOR_BLOCK') == '1':
 (r/'blocked').write_text(json.dumps({'owner':int(os.environ['ALEXANDRIA_GPU_LOCK_PID']),'evaluator':os.getpid()}))
 while not (r/'release').exists(): time.sleep(.01)
rc=int(os.environ.get('FIXTURE_EVALUATOR_RC','0'))
if rc: raise SystemExit(rc)
args=sys.argv[1:]
if '--out' not in args: (r/'captured_environment.json').write_text(os.environ['EXPERIMENT_ENV'])
if '--out' in args:
 out=pathlib.Path(args[args.index('--out')+1]);fx=json.loads(pathlib.Path(args[args.index('--fixtures')+1]).read_text())
 rows=[{'id':e['id'],'expected':e['expected_speaker'],'predicted':e['expected_speaker'],'correct':True} for e in fx['entries']]
 doc={fx['book']:{a:{'n':len(rows),'correct':len(rows),'rows':rows} for a in ('base','lora')}}
else:
 out=r/'ab_test_runtime/experiments'/('lora_serving_eval__'+args[args.index('--tag')+1]+'.json')
 doc={'meta': {'validation': 'ok', 'finished': 1}, 'summary': {'base': {'n': 1, 'correct': 0}, 'lora': {'n': 1, 'correct': 1}}, 'rows': [dict(arm=a, id='index18:0', line='Hello', expected='ALICE', correct=(a == 'lora'), candidates=['ALICE'], in_candidates=True, predicted=('ALICE' if a == 'lora' else '')) for a in ('base', 'lora')]}
out.write_text(json.dumps(doc))
'''
        for name in ('pdnc_eval.py', 'lora_serving_eval.py'):
            (self.root / 'app/experiments' / name).write_text(evaluator)
        (self.root / 'app/llama_server_process.py').write_text('''import os,pathlib,select,signal,subprocess
r=pathlib.Path(os.environ['FIXTURE_ROOT'])
subprocess.run(['bash',os.environ['FIXTURE_WRAPPER'],'--check-lock-owner',os.environ['ALEXANDRIA_GPU_LOCK_PID'],os.environ['ALEXANDRIA_GPU_LOCK_FD']],check=True)
p=r/'server.pid'
if p.exists():
 try: fd=os.pidfd_open(int(p.read_text()))
 except ProcessLookupError: raise SystemExit(0)
 try:
  signal.pidfd_send_signal(fd,signal.SIGTERM)
  assert select.select([fd],[],[],5)[0], 'captured CPU server did not stop'
 finally: os.close(fd)
with (r/'stops').open('a') as f: f.write('stopped\\n')
raise SystemExit(4 if os.environ.get('FIXTURE_CLEANUP_FAIL') == '1' else 0)
''')
        self.env.update(ALLOW_DIRTY_TREE='1', REQUIRE_LLM='1',
                        LLM_PREFLIGHT_PYTHON=sys.executable)
        # These campaigns commit their measured artifacts between GPU stages.
        # Use a real disposable repository rather than hiding Git failures.
        marker=self.root/'ab_test_runtime/experiments/_fixture.txt'
        marker.write_text('fixture baseline\n')
        subprocess.run(['git','init','-q','-b','main',str(self.root)],check=True)
        for key,value in [('user.name','Fixture'),('user.email','fixture@example.com'),
                          ('core.hooksPath',str(self.root/'.git/no-hooks'))]:
            subprocess.run(['git','-C',str(self.root),'config',key,value],check=True)
        subprocess.run(['git','-C',str(self.root),'add','-f',str(marker)],check=True)
        subprocess.run(['git','-C',str(self.root),'commit','-q','-m','fixture baseline'],check=True)

    def launch(self, chain, **extra):
        process = subprocess.Popen(['bash', str(self.root / 'run_chains' / chain)],
            cwd=self.root, env=dict(self.env, **extra), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_fixture_process, process)
        return process

    def stop_fixture_process(self, process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=26)

    def cleanup_server(self):
        p = self.root / 'server.pid'
        if p.exists():
            pid = int(p.read_text())
            if is_live(pid):
                os.kill(pid, signal.SIGTERM)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def test_server_start_waits_for_real_gpu_lease_and_entire_campaign_reuses_one_load(self):
        for chain in CHAINS:
            with self.subTest(chain=chain):
                for name in ('started', 'server.pid', 'preflights', 'evaluators'):
                    (self.root / name).unlink(missing_ok=True)
                with open(self.env['GPU_LOCK'], 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    process = self.launch(chain)
                    try:
                        deadline = time.monotonic() + 5
                        pending = Path(self.env['GPU_PENDING_DIR'])
                        while not (self.root / 'started').exists() and (not pending.exists() or not list(pending.iterdir())) and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertFalse((self.root / 'started').exists(), 'server mutation preceded GPU lease')
                        self.assertTrue(pending.exists() and list(pending.iterdir()))
                        time.sleep(.1)
                        self.assertFalse((self.root / 'started').exists(), 'server mutation preceded GPU lease')
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        stdout, stderr = process.communicate(timeout=15)
                        server_survived = (self.root / 'server.pid').exists() and is_live(int((self.root / 'server.pid').read_text()))
                        self.cleanup_server()
                self.assertEqual(0, process.returncode, stdout + stderr)
                self.assertEqual('start\n', (self.root / 'started').read_text())
                count = 8 if chain == CHAINS[0] else 1
                self.assertEqual(count, len((self.root / 'preflights').read_text().splitlines()))
                self.assertEqual(count, len((self.root / 'evaluators').read_text().splitlines()))
                self.assertFalse(server_survived, 'owned CPU server survived campaign before fixture cleanup')
                if chain == CHAINS[0]:
                    self.assertIn('9/9 stages ok', stdout)
                else:
                    self.assertIn('COMPLETE', stdout)

    def test_forged_inherited_marker_is_refused_before_server_start(self):
        for chain in CHAINS:
            with self.subTest(chain=chain):
                process = self.launch(chain, ALEXANDRIA_GPU_LOCK_HELD='1',
                    ALEXANDRIA_GPU_LOCK_PID=str(os.getpid()), ALEXANDRIA_GPU_LOCK_FD='9')
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(4, process.returncode, stdout + stderr)
                self.assertFalse((self.root / 'started').exists())
                self.assertFalse((self.root / 'evaluators').exists())

    def test_failed_preflight_blocks_each_experiment_and_propagates_failure(self):
        for chain in CHAINS:
            with self.subTest(chain=chain):
                (self.root / 'preflights').unlink(missing_ok=True)
                process = self.launch(chain, FIXTURE_PREFLIGHT_FAIL='1')
                try:
                    stdout, stderr = process.communicate(timeout=15)
                finally:
                    self.cleanup_server()
                self.assertEqual(1 if chain == CHAINS[0] else 6, process.returncode, stdout + stderr)
                self.assertFalse((self.root / 'evaluators').exists())
                self.assertEqual(8 if chain == CHAINS[0] else 1,
                                 len((self.root / 'preflights').read_text().splitlines()))
                self.assertNotIn('COMPLETE', stdout)
                (self.root / 'server.pid').unlink()

    def test_failed_start_does_not_preflight_or_evaluate(self):
        for chain in CHAINS:
            with self.subTest(chain=chain):
                process = self.launch(chain, FIXTURE_SERVER_FAIL='1')
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(1, process.returncode, stdout + stderr)
                self.assertFalse((self.root / 'preflights').exists())
                self.assertFalse((self.root / 'evaluators').exists())

    def test_stratification_evaluator_failure_is_preserved(self):
        process = self.launch(CHAINS[1], FIXTURE_EVALUATOR_RC='7')
        try:
            stdout, stderr = process.communicate(timeout=10)
        finally:
            self.cleanup_server()
        self.assertEqual(7, process.returncode, stdout + stderr)
        self.assertNotIn('COMPLETE', stdout)

    def test_goal13_resume_and_evaluator_arguments_are_preserved(self):
        artifact = self.root / 'ab_test_runtime/experiments/pdnc_eval__goal13_heldout_emma.json'
        row = {'id': 'emma-0', 'expected': 'Alice', 'predicted': 'Alice', 'correct': True}
        original = json.dumps({'emma': {a: {'n': 1, 'correct': 1, 'rows': [row]} for a in ('base', 'lora')}})
        artifact.write_text(original)
        process = self.launch(CHAINS[0], GOAL13_LIMIT='13', GOAL13_BATCH='7', LLAMA_PORT='8123')
        try:
            stdout, stderr = process.communicate(timeout=15)
        finally:
            self.cleanup_server()
        self.assertEqual(0, process.returncode, stdout + stderr)
        self.assertEqual(original, artifact.read_text())
        self.assertIn('SKIP goal13_heldout_emma', stdout)
        calls = [json.loads(row) for row in (self.root / 'evaluators').read_text().splitlines()]
        self.assertEqual(7, len(calls))
        self.assertEqual(7, len((self.root / 'preflights').read_text().splitlines()))
        for call in calls:
            self.assertEqual('13', call[call.index('--limit') + 1])
            self.assertEqual('7', call[call.index('--batch') + 1])
            self.assertEqual('http://127.0.0.1:8123/v1', call[call.index('--base_url') + 1])

    def test_cleanup_failure_changes_successful_campaign_exit_status(self):
        process = self.launch(CHAINS[1], FIXTURE_CLEANUP_FAIL='1')
        try:
            stdout, stderr = process.communicate(timeout=10)
        finally:
            self.cleanup_server()
        self.assertEqual(4, process.returncode, stdout + stderr)
        self.assertTrue((self.root / 'evaluators').exists())
        self.assertTrue((self.root / 'stops').exists())

    def test_wrapper_death_keeps_lease_over_server_and_evaluator_until_reaped(self):
        process = self.launch(CHAINS[1], FIXTURE_EVALUATOR_BLOCK='1')
        owner = None
        try:
            self.wait_marker('blocked')
            identities = json.loads((self.root / 'blocked').read_text())
            owner = identities['owner']
            server = int((self.root / 'server.pid').read_text())
            os.kill(owner, signal.SIGSTOP)
            process.kill()
            process.wait(timeout=5)
            self.assertTrue(is_live(server))
            self.assertTrue(is_live(identities['evaluator']))
            with open(self.env['GPU_LOCK'], 'a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if owner is not None:
                os.kill(owner, signal.SIGCONT)
            stdout, stderr = process.communicate(timeout=26)
            if owner is not None:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    pid, _ = os.waitpid(owner, os.WNOHANG)
                    if pid:
                        break
                    time.sleep(.01)
                else:
                    self.fail('campaign owner did not reap its descendants')
            self.cleanup_server()
        self.assertFalse(is_live(identities['evaluator']))
        self.assertFalse(is_live(server))
        with open(self.env['GPU_LOCK'], 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
