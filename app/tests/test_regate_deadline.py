"""A real timer stops an owned CPU gate, while later adapters still run."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_chain_failure_guards import prepare_artifact_commit_fixture
from tests.test_gpu_lock_owner import run_owned_cpu_chain
from tests.regate_campaign_fixture import copy_campaign_cli


ROOT = Path(__file__).resolve().parents[2]


class RegateDeadlineTests(unittest.TestCase):
    def prepare(self, root):
        source = (ROOT / 'run_chains/regate_with_provenance_20260817.sh').read_text()
        if os.environ.get('REGATE_DEADLINE_BASELINE'):
            source = Path(os.environ['REGATE_DEADLINE_BASELINE']).read_text()
        source = source.replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                                f'REPO="{root}"').replace('/tmp/regate_queue.tsv', str(root / 'queue.tsv'))
        script = root / 'regate.sh'
        script.write_text(source)
        python = root / 'app/env/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        for name in ('a', 'b'):
            adapter = root / f'models/{name}/adapter'
            adapter.mkdir(parents=True)
            data = adapter.parent / 'data/val'
            data.mkdir(parents=True)
            (data / 'metadata.jsonl').write_text('{}\n')
            out = root / f'ab_test_runtime/experiments/gate_promote__{name}.json'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({'adapter': str(adapter), 'previous': True}))
        worker = root / 'app/experiments/verify_adapter_identity.py'
        worker.parent.mkdir(parents=True)
        worker.write_text('''import os,signal,sys,time,json
from pathlib import Path
out=Path(sys.argv[sys.argv.index('--out')+1])
name=out.stem.split('__')[-1]
def event(text):
 with open('events','a') as f:f.write(text+'\\n')
event('start '+name)
if name=='a':
 Path('worker.pid').write_text(str(os.getpid()))
 def stop(*args):
  event('stopped a');sys.exit(130)
 signal.signal(signal.SIGINT,stop)
 time.sleep(3)
else:
 try:os.kill(int(Path('worker.pid').read_text()),0)
 except ProcessLookupError:event('a reaped')
 else:raise AssertionError('previous gate still alive')
d=json.loads(out.read_text());d['measured']=True;out.write_text(json.dumps(d))
''')
        copy_campaign_cli(root)
        prepare_artifact_commit_fixture(root)
        return script

    def test_timer_reaps_worker_before_next_adapter_and_keeps_prior_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.prepare(root)
            prior = (root / 'ab_test_runtime/experiments/gate_promote__a.json').read_bytes()
            result = run_owned_cpu_chain(['bash', str(script)], root,
                env={**os.environ, 'REGATE_GATE_TIMEOUT': '1s'}, cwd=root,
                capture_output=True, text=True, timeout=15)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn('TIMEOUT regate_a', result.stdout)
            self.assertNotIn('REGATE COMPLETE', result.stdout)
            self.assertEqual(['start a', 'stopped a', 'start b', 'a reaped'],
                             (root / 'events').read_text().splitlines())
            self.assertEqual(prior, (root / 'ab_test_runtime/experiments/gate_promote__a.json').read_bytes())
            self.assertTrue(json.loads((root / 'ab_test_runtime/experiments/gate_promote__b.json').read_text())['measured'])
            pid = int((root / 'worker.pid').read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_disabled_or_invalid_deadline_refuses_before_workers(self):
        for duration in ('0s', '0', '', 'invalid'):
            with self.subTest(duration=duration), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = self.prepare(root)
                result = run_owned_cpu_chain(['bash', str(script)], root,
                    env={**os.environ, 'REGATE_GATE_TIMEOUT': duration}, cwd=root,
                    capture_output=True, text=True, timeout=15)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn('positive duration', result.stderr)
                self.assertFalse((root / 'events').exists())
