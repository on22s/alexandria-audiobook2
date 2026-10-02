"""Private real Git/flock chain and promoter admission across interruption."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import promote_adapters
from gate_campaign import JOURNAL_NAME
from tests.regate_campaign_fixture import (
    ROOT, copy_campaign_cli, write_campaign_inputs, write_campaign_worker,
)
from tests.test_chain_failure_guards import prepare_artifact_commit_fixture
from tests.test_gpu_lock_owner import run_owned_cpu_chain


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
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

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
        with tempfile.TemporaryDirectory() as tmp:
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

    def test_success_and_measured_rejection_have_complete_campaigns(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as tmp:
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
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script, gates = self.prepare(root)
                result = self.run_chain(root, script, mode)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertNotIn('REGATE COMPLETE', result.stdout)
                self.assertEqual(['a', 'b'], (root / 'events').read_text().splitlines())
                self.assertIsNone(self.admit(gates, 'a'))
                self.assertEqual('running', json.loads((gates / JOURNAL_NAME).read_text())['status'])
                self.assertIsNone(self.admit(gates, 'a'))
