"""Disposable native Bash chain and identity-file checks; no GPU/model work."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.test_identity_gate_completion import make_inputs, measured_document
from tests.test_gpu_lock_owner import run_owned_cpu_chain

REPO = Path(__file__).resolve().parents[2]
BASELINES = ('husky_baritone_40s_m_2', 'husky_baritone_40s_m_scifi',
             'husky_tenor_30s_m_literary', 'silky_baritone_30s_m_fantasy',
             'warm_baritone_30s_m_2', 'warm_baritone_30s_m_scifi')
RANKS = ('breathy_alto_50s_f_fantasy', 'husky_baritone_20s_m_supernatural',
         'silky_alto_40s_f_literary_1', 'silky_baritone_45s_m',
         'velvety_mezzo_30s_f_gothic', 'warm_alto_50s_f_gothic')


class RemainingGoalChainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'repo with spaces'
        self.experiments = self.root / 'ab_test_runtime/experiments'
        self.experiments.mkdir(parents=True)
        (self.root / 'run_chains').mkdir()
        source = Path(os.environ.get('REMAINING_CHAIN_SOURCE', REPO / 'run_chains/remaining_goal_work.sh'))
        shutil.copyfile(source, self.root / 'run_chains/remaining_goal_work.sh')
        python = self.root / 'app/env/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        scripts = self.root / 'app/experiments'
        scripts.mkdir(parents=True)
        # ASR is a fixture producer: checks verify the exact shared argument packet,
        # while the independent test_asr_completion suite covers its real score gate.
        (scripts / 'asr_backends.py').write_text('''import json,sys
from pathlib import Path
args=sys.argv[1:]
out=Path(args[args.index('--out')+1])
expected={'--limit':'50','--align-clips':'50','--lang':'ja'}
assert all(args[args.index(k)+1]==v for k,v in expected.items())
assert args[args.index('--backends')+1:args.index('--lang')]==['whisper_cpp','whisper_cpp_hybrid']
assert args[args.index('--whisper-cpp-model')+1].endswith('ggml-large-v3.bin')
if '--check-artifact' in args:
    try: assert json.loads(out.read_text())=={'fixture_complete':True}
    except Exception: sys.exit(1)
else: raise AssertionError('GPU inference must not run')
''')
        # The actual identity validator hashes known weights/PCM inputs and checks
        # scope, numeric scores, failures, and verdict before returning 0 or 3.
        (scripts / 'verify_adapter_identity.py').write_text('''import sys
from experiments.verify_adapter_identity import main
main()
''')
        wrapper = self.root / 'gpu_job.sh'
        wrapper.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
from tests.test_identity_gate_completion import measured_document
args=sys.argv[1:];stage=args[0]
root=Path(os.environ['CHAIN_FIXTURE_ROOT'])
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
if stage.startswith('baseline_heldout__') and os.environ.get('FAIL_BASELINES')=='1':sys.exit(7)
if stage=='reference_rank2_failed':sys.exit(0)
out=Path(args[args.index('--out')+1])
if stage=='asr_ja_largev3_hybrid':doc={'fixture_complete':True}
else:
    adapter=Path(args[args.index('--adapter')+1]);data=Path(args[args.index('--dataset')+1])
    doc=measured_document(adapter,data)
if stage==os.environ.get('INVALID_OUTPUT'):doc={'passed':True}
out.write_text(json.dumps(doc))
''')
        wrapper.chmod(0o755)
        for index, name in enumerate(BASELINES + RANKS):
            base = self.root / ('inputs' + str(index))
            adapter, dataset = make_inputs(base)
            if name in BASELINES:
                final_adapter = self.root / 'lora_models' / name
                final_data = self.root / 'ab_test_runtime/reference_rank1_all21' / name / 'data'
                out = self.baseline(name)
            else:
                final_adapter = self.root / 'ab_test_runtime/reference_rank2_failed' / name / 'adapter'
                final_data = final_adapter.parent / 'data'
                out = self.gate(name)
            final_adapter.parent.mkdir(parents=True, exist_ok=True)
            final_data.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(adapter), final_adapter)
            shutil.move(str(dataset), final_data)
            out.write_text(json.dumps(measured_document(final_adapter, final_data)))
        self.ja = self.experiments / 'asr_ja_largev3_hybrid.json'
        self.ja.write_text(json.dumps({'fixture_complete': True}))
        self.env = dict(os.environ, CHAIN_FIXTURE_ROOT=str(self.root),
                        PYTHONPATH=str(REPO / 'app'))

    def baseline(self, name):
        return self.experiments / ('baseline_heldout__' + name + '.json')

    def gate(self, name):
        return self.experiments / ('gate_reference_rank2__' + name + '.json')

    def run_chain(self, expected):
        result = run_owned_cpu_chain(['bash', str(self.root / 'run_chains/remaining_goal_work.sh')],
            self.root, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        self.assertEqual(expected == 0, 'REMAINING GOAL WORK COMPLETE' in result.stdout)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        return result, calls

    def test_partial_asr_is_dispatched_and_completed_before_later_stages(self):
        self.ja.write_text('{"fixture_complete":false}')
        result, calls = self.run_chain(0)
        self.assertEqual(['asr_ja_largev3_hybrid', 'reference_rank2_failed'], [row[0] for row in calls])
        self.assertEqual({'fixture_complete': True}, json.loads(self.ja.read_text()))
        self.assertIn('baseline measurement failures: 0 of 6', result.stdout)

    def test_all_failed_baseline_measurements_are_attempted_and_fail_final_status(self):
        for name in BASELINES:
            self.baseline(name).unlink()
        self.env['FAIL_BASELINES'] = '1'
        result, calls = self.run_chain(1)
        self.assertEqual(['baseline_heldout__'+name for name in BASELINES] + ['reference_rank2_failed'],
                         [row[0] for row in calls])
        self.assertIn('baseline measurement failures: 6 of 6', result.stdout)

    def test_missing_split_remains_incomplete_while_independent_work_runs(self):
        name = BASELINES[0]
        self.baseline(name).unlink()
        shutil.rmtree(self.root / 'ab_test_runtime/reference_rank1_all21' / name / 'data')
        result, calls = self.run_chain(1)
        self.assertEqual(['reference_rank2_failed'], [row[0] for row in calls])
        self.assertIn('no held-out split', result.stdout)
        self.assertIn('baseline measurement failures: 1 of 6', result.stdout)

    def test_forged_partial_or_stale_gate_is_regenerated_by_actual_validator(self):
        name = RANKS[0]
        original = json.loads(self.gate(name).read_text())
        for change in ('substring', 'scores', 'inputs', 'generation'):
            with self.subTest(change=change):
                document = copy.deepcopy(original)
                if change == 'substring': document = {'passed': True}
                elif change == 'scores': document['ecapa_scores'].pop()
                elif change == 'inputs': document['measurement']['seed'] = 99
                else: document['generation_failures'] = 1
                self.gate(name).write_text(json.dumps(document))
                calls_file = self.root / 'calls.jsonl'
                if calls_file.exists(): calls_file.unlink()
                _, calls = self.run_chain(0)
                self.assertEqual(['reference_rank2_failed', 'gate_reference_rank2__'+name],
                                 [row[0] for row in calls])
                self.assertEqual(original, json.loads(self.gate(name).read_text()))

    def test_complete_negative_baseline_is_reused_and_negative_rank_gate_fails(self):
        baseline = self.baseline(BASELINES[0])
        rank = self.gate(RANKS[0])
        for path in (baseline, rank):
            document = json.loads(path.read_text())
            document.update(ecapa_scores=[0.1]*10, median_ecapa=0.1, passed=False)
            path.write_text(json.dumps(document))
        before = (baseline.read_bytes(), rank.read_bytes())
        result, calls = self.run_chain(1)
        self.assertEqual(['reference_rank2_failed'], [row[0] for row in calls])
        self.assertEqual(before, (baseline.read_bytes(), rank.read_bytes()))
        self.assertIn('baseline measurement failures: 0 of 6', result.stdout)
        self.assertIn('rank-2 gate failures: 1 of 6', result.stdout)

    def test_zero_exit_with_incomplete_new_artifact_does_not_report_completion(self):
        original = {path: path.read_bytes() for path in self.experiments.iterdir()}
        for stage in ('asr_ja_largev3_hybrid', 'baseline_heldout__'+BASELINES[0],
                      'gate_reference_rank2__'+RANKS[0]):
            with self.subTest(stage=stage):
                for path, content in original.items():
                    path.write_bytes(content)
                calls_file = self.root / 'calls.jsonl'
                if calls_file.exists(): calls_file.unlink()
                if stage.startswith('asr_'):
                    self.ja.write_text('{"fixture_complete":false}')
                elif stage.startswith('baseline_'):
                    self.baseline(BASELINES[0]).unlink()
                else:
                    self.gate(RANKS[0]).unlink()
                self.env['INVALID_OUTPUT'] = stage
                result, calls = self.run_chain(1)
                self.assertIn(stage, [row[0] for row in calls])
                if stage.startswith('asr_'):
                    self.assertEqual([stage], [row[0] for row in calls])
                    self.assertIn('output is incomplete or stale', result.stderr)
                elif stage.startswith('baseline_'):
                    self.assertIn('baseline measurement failures: 1 of 6', result.stdout)
                else:
                    self.assertIn('rank-2 gate failures: 1 of 6', result.stdout)
