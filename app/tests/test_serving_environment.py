"""HTTP settings capture and honest hardware provenance on serving campaigns."""
import copy
import json
import platform
import unittest
from unittest.mock import patch

from experiments import serving_environment as environment
from experiments.manifest import EnvironmentCaptureError
from tests import test_experiment_manifest as manifest_tests
from tests import test_campaign_reporting as campaign


class ServingEnvironmentTests(unittest.TestCase):
    _serve = manifest_tests.LlamaCppEnvironmentCaptureTest._serve
    PROPS = manifest_tests.LlamaCppEnvironmentCaptureTest.PROPS

    def test_actual_endpoint_settings_and_probed_gpu_replace_hardcoded_values(self):
        for gpu, diagnostic_backend in [('NVIDIA RTX A6000', 'cuda'), ('AMD fixture GPU', 'rocm'), (None, None)]:
            with self.subTest(gpu=gpu):
                url = self._serve(self.PROPS)
                with patch.object(environment, 'get_gpu_name_and_backend', return_value=(gpu, diagnostic_backend)) as probe:
                    result = environment.get_serving_environment(url, 'qwen/qwen3-14b')
                probe.assert_called_once_with()
                self.assertEqual(gpu, result['gpu'])
                self.assertEqual(diagnostic_backend, result['gpu_probe_backend'])
                self.assertIsNone(result['backend'])
                self.assertIn('not verified', result['gpu_observation'])
                self.assertEqual(platform.node(), result['host'])
                self.assertEqual(8192, result['context_length'])
                self.assertEqual(4, result['parallel'])
                self.assertIsNone(result['optimized'])
                self.assertEqual('llama.cpp', result['server'])
                self.assertEqual('qwen/qwen3-14b', result['verified_model'])

    def test_wrong_model_is_rejected_before_hardware_probe(self):
        with patch.object(environment, 'get_gpu_name_and_backend') as probe:
            with self.assertRaises(EnvironmentCaptureError):
                environment.get_serving_environment(self._serve(self.PROPS), 'wrong-model')
        probe.assert_not_called()

    def test_missing_or_invalid_server_settings_are_not_replaced_by_defaults(self):
        for field, value in [('total_slots', None), ('total_slots', True), ('total_slots', 0), ('n_ctx', '32768')]:
            with self.subTest(field=field, value=value):
                props = copy.deepcopy(self.PROPS)
                if field == 'n_ctx': props['default_generation_settings'][field] = value
                else: props[field] = value
                with self.assertRaises(EnvironmentCaptureError):
                    environment.get_serving_environment(self._serve(props), 'qwen3-14b')


class ServingEnvironmentChainTests(unittest.TestCase):
    setUp = campaign.CampaignReportingTests.setUp
    _write = campaign.CampaignReportingTests._write
    _run = campaign.CampaignReportingTests._run

    def test_chain_passes_captured_settings_and_gpu_to_evaluator(self):
        result = self._run('unanswered_stratification_20260830.sh')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        captured = json.loads((self.root / 'captured_environment.json').read_text())
        self.assertEqual('NVIDIA fixture GPU', captured['gpu'])
        self.assertEqual('cuda', captured['gpu_probe_backend'])
        self.assertIsNone(captured['backend'])
        self.assertEqual(8192, captured['context_length'])
        self.assertEqual(2, captured['parallel'])
        self.assertEqual('qwen/qwen3-14b', captured['verified_model'])

    def test_capture_failure_refuses_evaluation_and_complete(self):
        module = self.root / 'app/lmstudio_settings.py'
        module.write_text(module.read_text().replace('context_length=8192', 'context_length=None'))
        result = self._run('unanswered_stratification_20260830.sh')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('REFUSING unverified serving environment', result.stderr)
        self.assertNotIn('COMPLETE', result.stdout)
        self.assertFalse((self.root / 'captured_environment.json').exists())
        self.assertIn('reclaiming VRAM', result.stdout)


if __name__ == '__main__':
    unittest.main()
