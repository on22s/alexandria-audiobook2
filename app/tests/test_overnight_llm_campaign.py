"""Whole overnight chain: leased CPU workers, aggregate errors, durable config recovery."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import unittest

from tests import test_llm_campaign_ownership as campaign
from tests.test_subprocess_finally import is_live

REPO = Path(__file__).resolve().parents[2]
CHAIN = 'overnight_2026_08_09c.sh'


class OvernightLlmCampaignTests(unittest.TestCase):
    setUpFixture = campaign.LlmCampaignOwnershipTests.setUpFixture
    setUpCampaign = campaign.LlmCampaignOwnershipTests.setUp
    launch = campaign.LlmCampaignOwnershipTests.launch
    stop_fixture_process = campaign.LlmCampaignOwnershipTests.stop_fixture_process
    cleanup_server = campaign.LlmCampaignOwnershipTests.cleanup_server
    wait_marker = campaign.LlmCampaignOwnershipTests.wait_marker

    def setUp(self):
        self.setUpCampaign()
        shutil.copy2(REPO / 'run_chains' / CHAIN, self.root / 'run_chains' / CHAIN)
        shutil.copy2(REPO / 'run_chains/lib/config_backup.sh', self.root / 'run_chains/lib/config_backup.sh')
        self.config = self.root / 'app/config.json'
        self.original = b'{"llm":{"model_name":"original"},"llm_local":{"model_name":"original local"},"custom":17}\n'
        self.config.write_bytes(self.original)
        self.backup = self.root / 'ab_test_runtime/logs/config.json.overnight_backup'
        model = self.root / 'base.gguf'; model.write_bytes(b'CPU model fixture')
        self.env['ALEXANDRIA_QWEN3_MODEL'] = str(model)
        python = self.root / 'app/env/bin/python'; python.unlink()
        python.write_text('#!/bin/bash\nif [ "${1:-}" = - ]; then exec '+sys.executable+' "$@"; fi\nexec '+sys.executable+' "$FIXTURE_ROOT/cpu_generation.py" "$@"\n')
        python.chmod(0o755)
        (self.root / 'cpu_generation.py').write_text('''import json,os,pathlib,sys,time
r=pathlib.Path(os.environ['FIXTURE_ROOT']);args=sys.argv[1:]
with (r/'calls').open('a') as f:f.write(json.dumps(args)+'\\n')
with (r/'worker_config').open('a') as f:f.write(json.dumps(json.loads((r/'app/config.json').read_text()))+'\\n')
if os.environ.get('FIXTURE_BLOCK')=='1':
 (r/'blocked').write_text(json.dumps({'owner':int(os.environ['ALEXANDRIA_GPU_LOCK_PID']),'evaluator':os.getpid()}))
 while not (r/'release').exists():time.sleep(.01)
if os.environ.get('FIXTURE_ALL_FAIL')=='1':raise SystemExit(7)
if os.environ.get('FIXTURE_FIRST_FAIL')=='1' and any('chunk_retry_probe.py' in a for a in args):raise SystemExit(7)
print('CPU completed fixture')
''')
        self.addCleanup(self.cleanup_server)

    def run_campaign(self, **flags):
        process = self.launch(CHAIN, **flags)
        stdout, stderr = process.communicate(timeout=15)
        return process.returncode, stdout, stderr

    def assert_restored(self):
        self.assertEqual(self.original, self.config.read_bytes())
        self.assertFalse(self.backup.exists())
        self.assertFalse(is_live(int((self.root / 'server.pid').read_text())))

    def test_held_lease_prevents_startup_and_config_override_then_one_load_runs_five_stages(self):
        with open(self.env['GPU_LOCK'], 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = self.launch(CHAIN)
            deadline = time.monotonic()+5;pending=Path(self.env['GPU_PENDING_DIR'])
            while (not pending.exists() or not list(pending.iterdir())) and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(pending.exists() and list(pending.iterdir()))
            self.assertEqual(self.original,self.config.read_bytes())
            self.assertFalse(self.backup.exists());self.assertFalse((self.root/'started').exists())
            fcntl.flock(lock,fcntl.LOCK_UN)
        stdout,stderr=process.communicate(timeout=15)
        self.assertEqual(0,process.returncode,stdout+stderr)
        self.assertIn('5/5 stages ok',stdout)
        self.assertEqual('start\n',(self.root/'started').read_text())
        self.assertEqual(5,len((self.root/'calls').read_text().splitlines()))
        self.assertEqual(4,len((self.root/'preflights').read_text().splitlines()))
        for line in (self.root/'worker_config').read_text().splitlines():
            config=json.loads(line);self.assertEqual('qwen3-14b',config['llm_local']['model_name']);self.assertEqual(17,config['custom'])
        self.assert_restored()

    def test_first_failed_probe_is_counted_but_other_four_attempts_continue(self):
        code,stdout,stderr=self.run_campaign(FIXTURE_FIRST_FAIL='1')
        self.assertEqual(1,code,stdout+stderr)
        self.assertIn('g31_chunk11 = failed:7',stdout);self.assertIn('4/5 stages ok',stdout)
        self.assertEqual(5,len((self.root/'calls').read_text().splitlines()))
        self.assert_restored()

    def test_all_failed_workers_are_aggregated_and_config_still_restores(self):
        code,stdout,stderr=self.run_campaign(FIXTURE_ALL_FAIL='1')
        self.assertEqual(1,code,stdout+stderr);self.assertIn('0/5 stages ok',stdout)
        self.assertEqual(5,len((self.root/'calls').read_text().splitlines()))
        self.assertNotIn('OVERNIGHT DONE',stdout);self.assert_restored()

    def test_failed_start_restores_config_without_attempting_workers(self):
        code,stdout,stderr=self.run_campaign(FIXTURE_SERVER_FAIL='1')
        self.assertEqual(1,code,stdout+stderr);self.assertEqual(self.original,self.config.read_bytes())
        self.assertFalse(self.backup.exists());self.assertFalse((self.root/'calls').exists())

    def test_malformed_config_refuses_model_start_and_preserves_original_bytes(self):
        invalid = b'not JSON'
        self.config.write_bytes(invalid)
        code, stdout, stderr = self.run_campaign()
        self.assertEqual(1, code, stdout+stderr)
        self.assertEqual(invalid, self.config.read_bytes())
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.root/'started').exists())
        self.assertFalse((self.root/'calls').exists())

    def test_stale_backup_survives_as_authoritative_input_until_restored(self):
        self.backup.write_bytes(self.original);self.config.write_text('{"llm":{"model_name":"stale override"}}')
        code,stdout,stderr=self.run_campaign()
        self.assertEqual(0,code,stdout+stderr);self.assert_restored()
        for line in (self.root/'worker_config').read_text().splitlines():self.assertEqual(17,json.loads(line)['custom'])

    def test_preflight_failure_blocks_four_gpu_workers_but_recount_still_runs(self):
        code,stdout,stderr=self.run_campaign(FIXTURE_PREFLIGHT_FAIL='1')
        self.assertEqual(1,code,stdout+stderr);self.assertIn('1/5 stages ok',stdout)
        calls=[json.loads(line) for line in (self.root/'calls').read_text().splitlines()]
        self.assertEqual(1,len(calls));self.assertIn('experiments/chunk_completion.py',calls[0])
        self.assert_restored()

    def test_failed_exit_restore_keeps_recovery_but_still_stops_server(self):
        copier = self.root/'bin/cp'
        copier.write_text('#!/bin/bash\nsrc="${@: -2:1}"\n'
            'if [ "${FIXTURE_RESTORE_FAIL:-0}" = 1 ] && [ "$src" = "$FIXTURE_ROOT/ab_test_runtime/logs/config.json.overnight_backup" ]; then exit 1; fi\n'
            'exec /bin/cp "$@"\n')
        copier.chmod(0o755)
        code, stdout, stderr = self.run_campaign(FIXTURE_RESTORE_FAIL='1')
        self.assertEqual(1,code,stdout+stderr)
        self.assertEqual(self.original,self.backup.read_bytes())
        self.assertFalse(is_live(int((self.root/'server.pid').read_text())))
        self.assertEqual(5,len((self.root/'calls').read_text().splitlines()))
        (self.root/'server.pid').unlink()
        code, stdout, stderr = self.run_campaign()
        self.assertEqual(0,code,stdout+stderr)
        self.assert_restored()

    def test_original_worker_caps_and_canonical_dispatch_are_preserved(self):
        source=(self.root/'run_chains'/CHAIN).read_text()
        self.assertEqual(1,source.count('timeout 3600'))
        self.assertEqual(3,source.count('timeout 21600'))
        self.assertIn('"$REPO/ensure_llama_server.sh"',source)
        self.assertNotIn('nohup llama-server',source)
        self.assertNotRegex(source, r'(?m)^REPO=["\x27]?/home/',
                            'campaign root must derive from its checkout')

    def test_int_and_term_restore_config_stop_workers_and_do_not_continue(self):
        for signum in (signal.SIGINT,signal.SIGTERM):
            with self.subTest(signal=signum):
                process=self.launch(CHAIN,FIXTURE_BLOCK='1');self.wait_marker('blocked')
                child=json.loads((self.root/'blocked').read_text())['evaluator']
                parent=int(Path(f'/proc/{child}/stat').read_text().rsplit(')',1)[1].split()[1])
                outer=int(Path(f'/proc/{parent}/stat').read_text().rsplit(')',1)[1].split()[1])
                shell=int(Path(f'/proc/{outer}/stat').read_text().rsplit(')',1)[1].split()[1])
                os.kill(shell,signum);(self.root/'release').touch()
                stdout,stderr=process.communicate(timeout=15)
                self.assertEqual(128+signum,process.returncode,stdout+stderr)
                self.assertFalse(is_live(child));self.assertNotIn('START g31_index18',stdout)
                self.assert_restored()
                for name in ('server.pid','release','blocked'): (self.root/name).unlink()
