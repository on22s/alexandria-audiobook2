"""Actual context campaign under a kernel lease with CPU-only server/workers."""
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

from tests import test_llm_campaign_ownership as campaign
from tests.test_subprocess_finally import is_live

CHAIN = 'pdnc_context_evidence.sh'
REPO = Path(__file__).resolve().parents[2]


class ContextCampaignOwnershipTests(unittest.TestCase):
    setUpFixture = campaign.LlmCampaignOwnershipTests.setUpFixture
    setUpCampaign = campaign.LlmCampaignOwnershipTests.setUp
    launch = campaign.LlmCampaignOwnershipTests.launch
    stop_fixture_process = campaign.LlmCampaignOwnershipTests.stop_fixture_process
    cleanup_server = campaign.LlmCampaignOwnershipTests.cleanup_server
    wait_marker = campaign.LlmCampaignOwnershipTests.wait_marker

    def setUp(self):
        self.setUpCampaign()
        shutil.copy2(REPO / 'run_chains' / CHAIN, self.root / 'run_chains' / CHAIN)
        self.model = self.root / 'model.gguf'
        self.model.write_bytes(b'CPU fixture, not loaded')
        self.env['LLAMA_PORT'] = '9123'
        self.env.update(ALEXANDRIA_RUNTIME_ROOT=str(self.root / 'ab_test_runtime'), ALEXANDRIA_QWEN3_MODEL=str(self.model))
        ensure = self.root / 'ensure_llama_server.sh'
        source = ensure.read_text()
        source = source.replace('if [ "${FIXTURE_SERVER_FAIL:-0}" = 1 ]; then exit 1; fi', '''if [ "${FIXTURE_SERVER_FAIL:-0}" = 1 ]; then exit 1; fi
python3 -c 'import os,json,pathlib;pathlib.Path(os.environ["FIXTURE_ROOT"],"start_settings").write_text(json.dumps({k:os.environ.get(k) for k in ("LLAMA_MODEL","LLAMA_CTX","LLAMA_PORT","LLAMA_ALIAS","LLAMA_THINKING")}))'
if [ -f "$FIXTURE_ROOT/server.pid" ]; then exit 0; fi''')
        ensure.write_text(source)
        from tests.test_pdnc_campaign_guards import make_pilot_fixture
        pilot_document, _ = make_pilot_fixture(self.root / 'ab_test_runtime')
        (self.root / 'pilot_template.json').write_text(json.dumps(pilot_document))
        evaluator = self.root / 'app/experiments/pdnc_context_evidence.py'
        worker = '''import json,os,pathlib,sys,time
r=pathlib.Path(os.environ['FIXTURE_ROOT']);args=sys.argv[1:]
if '--check-artifact' in args:
 p=pathlib.Path(args[args.index('--check-artifact')+1])
 try: d=json.loads(p.read_text())
 except (OSError,ValueError): raise SystemExit(1)
 raise SystemExit(0 if d.get('meta',{}).get('finished')==1 else 1)
phase=args[args.index('--phase')+1]
with (r/'evaluators').open('a') as f: f.write(phase+'\\n')
if os.environ.get('FIXTURE_EVALUATOR_BLOCK')=='1':
 (r/'blocked').write_text(json.dumps({'owner':int(os.environ['ALEXANDRIA_GPU_LOCK_PID']),'evaluator':os.getpid()}))
 while not (r/'release').exists(): time.sleep(.01)
code=int(os.environ.get('FIXTURE_EVALUATOR_RC','0'))
if code:raise SystemExit(code)
d=json.loads((r/'pilot_template.json').read_text()) if phase=='pilot' else {'meta':{'finished':1}}
p=r/'ab_test_runtime/experiments'/('pdnc_evidence__'+phase+'__local-llamacpp.json');p.write_text(json.dumps(d))
'''
        # The shell imports the production pilot-state reader before invoking
        # this CPU worker. Keep that import pure and use the actual contract.
        contract_source = REPO / 'app/experiments/pdnc_context_evidence.py'
        for name in ('pdnc_fixture.py', 'scoring.py', 'stats.py'):
            shutil.copyfile(REPO / 'app/experiments' / name, self.root / 'app/experiments' / name)
        evaluator.write_text("import runpy\nif __name__ != '__main__':\n"
            + "    globals().update(runpy.run_path(" + repr(str(contract_source)) + "))\n"
            + "else:\n" + ''.join('    '+line+'\n' for line in worker.splitlines()))
        stopper = self.root / 'app/llama_server_process.py'
        stopper.write_text(stopper.read_text().replace("r=pathlib.Path(os.environ['FIXTURE_ROOT'])", "r=pathlib.Path(os.environ['FIXTURE_ROOT'])\nimport sys\n(r/'stop_port').write_text(sys.argv[1])"))
        self.addCleanup(self.cleanup_server)

    def test_chain_uses_canonical_identity_and_lifetime_protocol(self):
        source = (self.root / 'run_chains' / CHAIN).read_text()
        self.assertIn('"$repo/ensure_llama_server.sh"', source)
        self.assertIn('ensure_llm_campaign_lease', source)
        self.assertNotIn('/usr/bin/llama-server', source)
        self.assertNotIn('adopt_check=', source)
        self.assertEqual(2, source.count('"$repo/run_chains/lib/llm_job.sh"'))

    def test_startup_waits_for_real_lease_and_both_phases_share_one_server(self):
        with open(self.env['GPU_LOCK'], 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = self.launch(CHAIN)
            deadline = time.monotonic() + 5
            pending = Path(self.env['GPU_PENDING_DIR'])
            while (not pending.exists() or not list(pending.iterdir())) and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(pending.exists() and list(pending.iterdir()))
            self.assertFalse((self.root / 'started').exists())
            fcntl.flock(lock, fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(0, process.returncode, stdout + stderr)
        self.assertEqual('start\n', (self.root / 'started').read_text())
        self.assertEqual('pilot\nconfirmatory\n', (self.root / 'evaluators').read_text())
        self.assertEqual(2, len((self.root / 'preflights').read_text().splitlines()))
        self.assertFalse(is_live(int((self.root / 'server.pid').read_text())))
        self.assertIn('CAMPAIGN_DONE', stdout)
        self.assertEqual('8090', (self.root / 'stop_port').read_text())
        self.assertEqual(dict(LLAMA_MODEL=str(self.model), LLAMA_CTX='32768', LLAMA_PORT='8090',
                              LLAMA_ALIAS='qwen3-14b', LLAMA_THINKING='0'), json.loads((self.root / 'start_settings').read_text()))

    def test_startup_preserves_requested_vram_capacity_gate(self):
        process = self.launch(CHAIN, REQUIRE_VRAM_GB='999')
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(7, process.returncode, stdout + stderr)
        self.assertFalse((self.root / 'started').exists())
        self.assertFalse((self.root / 'evaluators').exists())

    def test_failed_start_never_runs_a_worker(self):
        process = self.launch(CHAIN, FIXTURE_SERVER_FAIL='1')
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(2, process.returncode, stdout + stderr)
        self.assertFalse((self.root / 'evaluators').exists())
        self.assertNotIn('MODEL_READY', stdout)

    def test_failed_preflight_and_worker_codes_are_preserved(self):
        for flags, expected in (({'FIXTURE_PREFLIGHT_FAIL':'1'}, 6), ({'FIXTURE_EVALUATOR_RC':'7'}, 7)):
            with self.subTest(flags=flags):
                process = self.launch(CHAIN, **flags)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(expected, process.returncode, stdout + stderr)
                self.assertNotIn('CAMPAIGN_DONE', stdout)
                self.assertFalse(is_live(int((self.root / 'server.pid').read_text())))
                (self.root / 'server.pid').unlink()
        self.assertEqual('pilot\n', (self.root / 'evaluators').read_text())

    def test_forged_owner_cannot_start_or_adopt_server(self):
        process = self.launch(CHAIN, ALEXANDRIA_GPU_LOCK_HELD='1', ALEXANDRIA_GPU_LOCK_PID=str(os.getpid()), ALEXANDRIA_GPU_LOCK_FD='9')
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(4, process.returncode, stdout + stderr)
        self.assertFalse((self.root / 'started').exists())

    def test_cleanup_failure_changes_campaign_status(self):
        process = self.launch(CHAIN, FIXTURE_CLEANUP_FAIL='1')
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(4, process.returncode, stdout + stderr)
        self.assertFalse(is_live(int((self.root / 'server.pid').read_text())))
        self.assertTrue((self.root / 'stops').exists())

    def run_signal(self, sent_signal, reused=False):
        external = None
        if reused:
            external = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            self.addCleanup(self.stop_fixture_process, external)
            (self.root / 'server.pid').write_text(str(external.pid))
        process = self.launch(CHAIN, FIXTURE_EVALUATOR_BLOCK='1')
        self.wait_marker('blocked')
        child = json.loads((self.root / 'blocked').read_text())['evaluator']
        server = int((self.root / 'server.pid').read_text())
        if sent_signal is None:
            (self.root / 'release').touch()
        else:
            # Signal the supervised worker shell, not its unrelated test parent.
            # Resolve its exact parent PID from the captured evaluator.
            parent = int(Path(f'/proc/{child}/stat').read_text().rsplit(')',1)[1].split()[1])
            # parent is timeout; the actual chain shell is its parent.
            shell = int(Path(f'/proc/{parent}/stat').read_text().rsplit(')',1)[1].split()[1])
            os.kill(shell, sent_signal)
            (self.root / 'release').touch()
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(0 if sent_signal is None else 128+sent_signal, process.returncode, stdout + stderr)
        self.assertFalse(is_live(server), 'configured server survived owned campaign cleanup')
        self.assertFalse(is_live(child), 'dependent evaluator survived campaign exit')
        if sent_signal is not None: self.assertNotIn('CAMPAIGN_DONE', stdout)
        if external is not None: external.wait(timeout=5)
