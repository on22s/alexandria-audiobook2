"""Mixed Goal1.3 uses the same validated rows and campaign lease as confirmation."""
import json
import subprocess
import shlex
import sys
import unittest
from unittest.mock import patch

from tests import test_llm_campaign_ownership as campaign


MIXED = 'goal_13_mixed_multin_20260912.sh'


class MixedPdncCampaignTests(unittest.TestCase):
    setUpCampaign = campaign.LlmCampaignOwnershipTests.setUp
    setUpFixture = campaign.LlmCampaignOwnershipTests.setUpFixture
    launch = campaign.LlmCampaignOwnershipTests.launch
    stop_fixture_process = campaign.LlmCampaignOwnershipTests.stop_fixture_process
    cleanup_server = campaign.LlmCampaignOwnershipTests.cleanup_server
    wait_marker = campaign.LlmCampaignOwnershipTests.wait_marker

    def setUp(self):
        replacement = patch.object(campaign, 'CHAINS', (MIXED,))
        replacement.start()
        self.addCleanup(replacement.stop)
        self.setUpCampaign()
        (self.root / 'ab_test_runtime/distill/gguf/qwen3-14b-mixed-multin-r16-20260912.f16.gguf').write_bytes(b'fixture')

    test_start_waits_for_real_lease_and_reuses_one_load = campaign.LlmCampaignOwnershipTests.test_server_start_waits_for_real_gpu_lease_and_entire_campaign_reuses_one_load
    test_forged_owner_refuses_startup = campaign.LlmCampaignOwnershipTests.test_forged_inherited_marker_is_refused_before_server_start
    test_failed_preflight_blocks_all_experiments = campaign.LlmCampaignOwnershipTests.test_failed_preflight_blocks_each_experiment_and_propagates_failure
    test_failed_start_does_not_evaluate = campaign.LlmCampaignOwnershipTests.test_failed_start_does_not_preflight_or_evaluate

    def get_artifact(self):
        return self.root / 'ab_test_runtime/experiments/pdnc_eval__goal13mm_heldout_emma.json'

    def run_campaign(self, **environment):
        process = self.launch(MIXED, **environment)
        try:
            stdout, stderr = process.communicate(timeout=15)
        finally:
            self.cleanup_server()
        return process.returncode, stdout, stderr

    def test_corrupt_or_incomplete_cache_is_regenerated_with_current_rows(self):
        count = 0
        for cached in ('{"emma":', json.dumps({'emma': {'base': {'n': 1, 'correct': 1}}})):
            with self.subTest(cached=cached):
                self.get_artifact().write_text(cached)
                code, stdout, stderr = self.run_campaign()
                self.assertEqual(0, code, stdout + stderr)
                self.assertNotIn('SKIP goal13mm_heldout_emma', stdout)
                calls = (self.root / 'evaluators').read_text().splitlines()
                self.assertEqual(8 if count == 0 else count + 1, len(calls))
                count = len(calls)
                document = json.loads(self.get_artifact().read_text())
                self.assertEqual(1, len(document['emma']['lora']['rows']))
                self.assertIn('DEV MINUS HELD-OUT', stdout)
                (self.root / 'server.pid').unlink()

    def test_actual_run_book_does_not_skip_nonempty_unfinished_json(self):
        source = (self.root / 'run_chains' / MIXED).read_text()
        start = source.index('run_book() {')
        function = source[start:source.index('\n}\n', start) + 3]
        for cached in ('{"emma":', '{"emma":{"base":{"n":1}}}'):
            with self.subTest(cached=cached):
                self.get_artifact().write_text(cached)
                script = ('REPO=' + shlex.quote(str(self.root)) + '\n'
                    'runtime=' + shlex.quote(str(self.root / 'ab_test_runtime')) + '\n'
                    'python=' + shlex.quote(sys.executable) + '\nLIMIT=100000; BATCH=25; PORT=8090\n'
                    'stage_note() { echo "$*"; }\n'
                    'run_stage() { echo EVALUATED; }\n'
                    'stage_commit_artifacts() { true; }\n' + function + '\nrun_book heldout emma\n')
                result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=5)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn('EVALUATED', result.stdout)
                self.assertNotIn('SKIP', result.stdout)

    def test_valid_cache_is_preserved_and_mixed_arguments_remain_correct(self):
        row = dict(id='emma-0', expected='Alice', predicted='Alice', correct=True)
        original = json.dumps({'emma': {a: dict(n=1, correct=1, rows=[row]) for a in ('base', 'lora')}}, indent=2)
        self.get_artifact().write_text(original)
        code, stdout, stderr = self.run_campaign(GOAL13_LIMIT='13', GOAL13_BATCH='7', LLAMA_PORT='8123')
        self.assertEqual(0, code, stdout + stderr)
        self.assertEqual(original, self.get_artifact().read_text())
        self.assertIn('SKIP goal13mm_heldout_emma', stdout)
        calls = [json.loads(row) for row in (self.root / 'evaluators').read_text().splitlines()]
        self.assertEqual(7, len(calls))
        for call in calls:
            self.assertEqual('13', call[call.index('--limit') + 1])
            self.assertEqual('7', call[call.index('--batch') + 1])
            self.assertEqual('http://127.0.0.1:8123/v1', call[call.index('--base_url') + 1])
            self.assertIn('pdnc_eval__goal13mm_', call[call.index('--out') + 1])

    def test_success_without_new_artifacts_cannot_produce_a_comparison(self):
        (self.root / 'app/experiments/pdnc_eval.py').write_text('raise SystemExit(0)\n')
        code, stdout, stderr = self.run_campaign()
        self.assertEqual(1, code, stdout + stderr)
        self.assertIn('goal13mm_summary = failed:1', stdout)
        self.assertIn('8/9 stages ok', stdout)
        self.assertNotIn('DEV MINUS', stdout)


if __name__ == '__main__':
    unittest.main()
