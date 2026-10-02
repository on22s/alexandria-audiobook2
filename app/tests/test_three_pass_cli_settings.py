"""Verify CLI sample/full execution policy and real planning prompt construction."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from attribution_prompt_variants import builtin_texts

if os.environ.get('THREE_PASS_CLI_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_cli_saved', os.environ['THREE_PASS_CLI_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


class ThreePassCliSettingsTests(unittest.TestCase):
    def run_cli(self, tmp, config, preflight, extra):
        source = Path(tmp) / 'source.txt'
        source.write_text('Alice stood. "Yes." Alice smiled. "No." Alice left. "Goodbye."', encoding='utf-8')
        output = Path(tmp) / ('sample.json' if preflight else 'full.json')
        calls, plans = [], []
        estimate = tp.build_three_pass_request_preflight
        count = tp.get_three_pass_planned_calls

        def plan(*args, **kwargs):
            report = estimate(args[0], args[1], args[2].context_length, 1, params=args[2])
            plans.append((args, kwargs, report))
            return count(*args, **kwargs)

        def run(client, model, text, params, chunk_size, **kwargs):
            calls.append((text, params, chunk_size, kwargs))
            return [{'speaker':'NARRATOR', 'text':text, 'instruct':'Neutral.'}]

        argv = ['three_pass_generate', str(source), '--output', str(output),
                '--chunk-size', '6000', '--segmentation', 'quotes'] + extra
        if preflight:
            argv.append('--preflight')
        with patch.object(tp.sys, 'argv', argv), \
             patch.object(tp, 'load_app_config', return_value=config), \
             patch.object(tp, 'ensure_ideal_settings', return_value=(None, {'context_length':32768}, 'fixture')), \
             patch.object(tp, 'make_run_client', return_value=object()), \
             patch.object(tp, 'run_three_pass', side_effect=run), \
             patch.object(tp, 'get_three_pass_planned_calls', side_effect=plan):
            if preflight:
                with self.assertRaises(SystemExit) as exited:
                    tp.main()
                self.assertEqual(0, exited.exception.code)
            else:
                tp.main()
        self.assertTrue(calls)
        for text, _, _, options in calls:
            artifact = json.loads(Path(options['output_path']).read_text())
            self.assertEqual(text, artifact[0]['text'])
        if preflight:
            manifest = json.loads(Path(str(output) + '.preflight_manifest.json').read_text())
            self.assertEqual('complete', manifest['status'])
            self.assertEqual(len(calls), len(manifest['samples']))
        return calls, plans

    def test_samples_and_full_run_receive_same_selected_controls_and_plan_votes(self):
        config = {'llm_mode':'local', 'llm_local':{'model_name':'fixture', 'base_url':'http://fixture/v1'},
                  'generation':{'max_tokens':4096, 'context_rescue_windows':[100,200],
                                'context_rescue_retries':4}}
        original = copy.deepcopy(config)
        extra = ['--attribute-batch-size','1','--attribute-context-chars','5000',
                 '--prompt-variant','michel2_full','--attribution-votes','3',
                 '--vote-temperature','0.7','--reasoning-effort','none']
        with tempfile.TemporaryDirectory() as tmp:
            full, full_plans = self.run_cli(tmp, config, False, extra)
            samples, sample_plans = self.run_cli(tmp, config, True, extra)
        expected = {'attribute_batch_size':1, 'attribute_context_chars':5000,
                    'attribute_prompt_variant':'michel2_full', 'attribute_prompt_texts':None,
                    'attribution_votes':3, 'vote_temperature':0.7,
                    'thinking_mode':'none', 'context_windows':(100,200), 'context_rescue_retries':4}
        for _, params, size, options in full + samples:
            self.assertEqual(6000, size)
            self.assertEqual('none', params.reasoning_effort)
            for key, value in expected.items():
                self.assertIn(key, options)
                self.assertEqual(value, options[key], key)
            self.assertEqual(9, options['planned_calls'][2])
        for args, _, report in full_plans + sample_plans:
            with self.assertRaises(TypeError):
                args[1]['attribute_batch_size'] = 100
            self.assertEqual(1, args[1]['attribute_batch_size'])
            self.assertEqual(5000, args[1]['attribute_context_chars'])
            self.assertEqual(9, len([r for r in report['requests'] if r['stage'] == 'attribute']))
        self.assertEqual(original, config)

    def test_selected_preset_texts_reach_samples_and_estimated_prompt(self):
        texts = dict(builtin_texts('michel2'), system='Custom attribution system ' * 100)
        config = {'llm_mode':'local', 'llm_local':{'model_name':'fixture'},
                  'generation':{'max_tokens':4096}, 'prompts':{'attribution_preset':'custom'},
                  'prompt_presets':[{'name':'custom', 'variant':'michel2',
                                    'system_prompt':texts['system'], 'user_prompt':texts['user'],
                                    'example':texts['example']}]}
        with tempfile.TemporaryDirectory() as tmp:
            calls, plans = self.run_cli(tmp, config, True, [])
        for _, _, _, options in calls:
            self.assertIn('attribute_prompt_variant', options)
            self.assertEqual('michel2', options['attribute_prompt_variant'])
            self.assertEqual(texts, options['attribute_prompt_texts'])
        for _, _, report in plans:
            attributed = [r for r in report['requests'] if r['stage'] == 'attribute']
            self.assertTrue(attributed)
            self.assertGreater(attributed[0]['prompt_tokens'], len(texts['system']) // 3)
