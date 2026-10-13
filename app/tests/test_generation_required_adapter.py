"""Native generation startup honors a required serving adapter before work."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attribution_adapter import AdapterError
import generate_script as generation


class ClientBoundaryReached(RuntimeError):
    pass


class RequiredGenerationAdapterTests(unittest.TestCase):
    def run_startup(self, spec, served, probe_error=None):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_text('Alice said, "Hello there."\n\nBob replied, "Good morning."')
            output = root / 'saved.json'
            output.write_text('[{"speaker":"ALICE","text":"Previous good script."}]')
            checkpoint = root / 'saved.json.generation_checkpoint.json'
            checkpoint.write_text('{"preserve":"unfinished work"}')
            shard = root / 'saved.json.generation_checkpoint.json.shards'
            shard.mkdir()
            (shard / 'part.json').write_text('{"preserve":"completed shard"}')
            quality = root / 'saved.json.generation_quality.json'
            quality.write_text('{"preserve":"prior quality evidence"}')
            config = {'llm_mode': 'remote', 'llm_remote': {
                'base_url': 'http://fixture.invalid/v1', 'model_name': 'fixture'},
                'generation': {'chunk_size': 3000, 'max_tokens': 4096}}
            if spec is not None:
                config['llm_remote']['attribution_adapter'] = spec
            original_config = copy.deepcopy(config)
            before = {str(path.relative_to(root)): path.read_bytes()
                      for path in root.rglob('*') if path.is_file()}
            stack.enter_context(patch.object(generation.sys, 'argv', [
                'generate_script.py', str(source), '--output', str(output)]))
            stack.enter_context(patch.object(generation, 'get_runtime_data_dir', return_value=directory))
            stack.enter_context(patch.object(generation, 'load_app_config', return_value=config))
            stack.enter_context(patch('attribution_adapter.served_adapters', return_value=served,
                                      side_effect=probe_error))
            heal = stack.enter_context(patch.object(generation, 'ensure_ideal_settings',
                return_value=(True, {}, 'Synthetic settings boundary')))
            client = stack.enter_context(patch.object(generation, 'make_run_client',
                side_effect=ClientBoundaryReached('No model requests allowed')))
            work = stack.enter_context(patch.object(generation, 'process_chunk'))
            log = io.StringIO()
            error = None
            with contextlib.redirect_stdout(log):
                try:
                    generation.main()
                except Exception as caught:
                    error = caught
            self.assertEqual(before, {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob('*') if path.is_file()})
            self.assertEqual(original_config, config)
            work.assert_not_called()
            return error, client.call_count, heal.call_count, log.getvalue()

    def test_required_missing_disabled_wrong_unverifiable_or_invalid_scale_stops_before_client(self):
        spec = {'path': '/synthetic/required.gguf', 'require': True, 'scale': 1}
        cases = ([], [{'path': spec['path'], 'scale': 0}],
                 [{'path': '/another/required.gguf', 'scale': 1}], None,
                 [{'path': spec['path'], 'scale': 'invalid'}])
        for served in cases:
            with self.subTest(served=served):
                error, calls, heals, _ = self.run_startup(spec, served)
                self.assertIsInstance(error, AdapterError)
                self.assertEqual(0, calls)
                self.assertEqual(0, heals)
        error, calls, heals, _ = self.run_startup({**spec, 'scale': 'invalid'}, [])
        self.assertIsInstance(error, AdapterError)
        self.assertEqual((0, 0), (calls, heals))

    def test_required_serving_adapter_proceeds_to_existing_client_boundary(self):
        spec = {'path': '/synthetic/required.gguf', 'require': True}
        error, calls, heals, log = self.run_startup(spec, [{'path': spec['path'], 'scale': 1}])
        self.assertIsInstance(error, ClientBoundaryReached)
        self.assertEqual((1, 1), (calls, heals))
        self.assertNotIn('WARNING: adapter check failed', log)

    def test_optional_refusals_still_warn_and_proceed(self):
        for served in ([], None, [{'path': '/synthetic/optional.gguf', 'scale': 0}]):
            with self.subTest(served=served):
                error, calls, heals, log = self.run_startup({
                    'path': '/synthetic/optional.gguf', 'require': False}, served)
                self.assertIsInstance(error, ClientBoundaryReached)
                self.assertEqual((1, 1), (calls, heals))
                self.assertIn('WARNING:', log)

    def test_unconfigured_adapter_and_optional_probe_exception_keep_existing_behavior(self):
        error, calls, heals, log = self.run_startup(None, [])
        self.assertIsInstance(error, ClientBoundaryReached)
        self.assertEqual((1, 1), (calls, heals))
        self.assertIn('attribution_adapter=none', log)
        error, calls, heals, log = self.run_startup({
            'path': '/synthetic/optional.gguf'}, [], RuntimeError('fixture probe failed'))
        self.assertIsInstance(error, ClientBoundaryReached)
        self.assertEqual((1, 1), (calls, heals))
        self.assertIn('WARNING: adapter check failed: fixture probe failed', log)
