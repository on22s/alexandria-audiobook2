"""Bad controls must not become unbounded context or silently greedy calls."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams

if os.environ.get('THREE_PASS_CONTROLS_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_controls_saved', os.environ['THREE_PASS_CONTROLS_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


class ThreePassControlTests(unittest.TestCase):
    def test_invalid_windows_fail_before_rescue_dispatch(self):
        for windows in ([], (), [0], [-1], ['100'], [True], '100', 100):
            with self.subTest(windows=windows), patch.object(tp, 'segment_chunk_with_context', return_value=[]) as dispatch:
                with self.assertRaisesRegex(ValueError, 'context rescue windows'):
                    tp.rescue_chunk_with_context(None, 'fixture', ['Before.' * 100, 'Target.', 'After.' * 100], 1,
                                                 LLMGenParams(), windows=windows)
                dispatch.assert_not_called()

    def test_invalid_voting_fails_before_attribution_dispatch(self):
        for votes, temperature in ((0,0.3), (-1,0.3), (True,0.3), ('3',0.3), (1.5,0.3),
                                   (3,float('nan')), (3,float('inf')), (3,-0.1), (3,2.1), (3,'0.3'), (3,True)):
            with self.subTest(votes=votes, temperature=temperature), patch.object(tp, 'attribute_batch', return_value=[]) as dispatch:
                with self.assertRaises(ValueError):
                    tp.attribute_batch_voted(None, 'fixture', [], LLMGenParams(), [], votes=votes, vote_temperature=temperature)
                dispatch.assert_not_called()

    def test_actual_run_rejects_before_provider_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for options in ({'context_windows':[]}, {'context_windows':[0]}, {'attribution_votes':0}, {'vote_temperature':float('nan')}):
                with self.subTest(options=options), patch.object(tp, 'get_run_model_binding') as binding:
                    with self.assertRaises(ValueError):
                        tp.run_three_pass(None, 'fixture', 'Target.', LLMGenParams(), 3000,
                                          output_path=str(Path(tmp) / 'out.json'), **options)
                    binding.assert_not_called()
            self.assertEqual([], list(Path(tmp).iterdir()))

    def test_valid_windows_keep_order_and_bound_both_sides(self):
        calls = []
        def segment(client, model, chunk, before, after, params, **kwargs):
            calls.append((before,after))
            return []
        with patch.object(tp, 'segment_chunk_with_context', side_effect=segment):
            tp.rescue_chunk_with_context(None, 'fixture', ['abcdefghij', 'Target.', 'klmnopqrst'], 1,
                                         LLMGenParams(), windows=[2,5])
        self.assertEqual([('ij','kl'), ('fghij','klmno')], calls)
        with patch.object(tp, 'attribute_batch', return_value=[{'speaker':'ALICE'}]) as dispatch:
            result, confidence = tp.attribute_batch_voted(None, 'fixture', [], LLMGenParams(), [], votes=1, vote_temperature=0)
            self.assertEqual([{'speaker':'ALICE'}], result)
            self.assertEqual([1.0], confidence)
            self.assertEqual(1, dispatch.call_count)

    def test_cli_invalid_votes_stop_before_healing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.txt'; source.write_text('Target.')
            with patch.object(tp.sys, 'argv', ['three_pass_generate',str(source),'--attribution-votes','0']), \
                 patch.object(tp, 'load_app_config', return_value={'llm_local':{'model_name':'fixture'}}), \
                 patch.object(tp, 'ensure_ideal_settings') as heal, patch.object(tp, 'make_run_client') as client:
                with self.assertRaises(SystemExit) as exited:
                    tp.main()
                self.assertEqual(2, exited.exception.code)
                heal.assert_not_called()
                client.assert_not_called()
