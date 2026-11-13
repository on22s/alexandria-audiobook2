"""Experimental prompt presets preserve payloads, defaults and run identities."""
import asyncio
import json
import random
import unittest
from unittest.mock import patch

import compact_pass1 as compact
import config_settings
import three_pass_generate as tp
from default_prompts import load_instruct_prompts
from routers import system
from tests import test_prompt_preset_transactions_js as js_fixtures

FAMILIES = ('pass1_compact', 'pass3_gaps', 'pass3_directed')


def config_for(family, system='Synthetic system {literal braces}', user=None):
    field = 'units' if family == 'pass1_compact' else 'batch'
    return {'prompts': {family + '_preset': 'synthetic', family + '_prompt_presets': [
        {'name': 'synthetic', 'description': 'kept', 'system_prompt': system,
         'user_prompt': user if user is not None else 'Synthetic input:\n{' + field + '}'}]}}


def params_for(config):
    return tp.get_three_pass_run_params(config, {}, generation_settings=tp.resolve_three_pass_generation_settings(config))


class ExperimentalPromptStudioTests(unittest.TestCase):
    def test_schema_retains_each_family_and_defaults_off(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                saved = config_settings.PromptConfig(**config_for(family)['prompts']).model_dump()
                self.assertEqual('synthetic', saved[family + '_preset'])
                self.assertEqual('kept', saved[family + '_prompt_presets'][0]['description'])
                self.assertEqual('default', config_settings.PromptConfig().model_dump()[family + '_preset'])
        self.assertEqual('ordinary', config_settings.GenerationConfig().three_pass_pass1_format)
        self.assertEqual('off', config_settings.GenerationConfig().three_pass_delivery_direction)

    def test_invalid_templates_and_blank_instructions_rejected_at_schema_boundary(self):
        for family in FAMILIES:
            for key, value in [('system_prompt', '   '), ('user_prompt', ''), ('user_prompt', 'no payload'),
                               ('user_prompt', '{unknown}'), ('user_prompt', '{batch.__class__}'),
                               ('user_prompt', '{units!r}'), ('user_prompt', '{batch:>20}'),
                               ('user_prompt', '{'), ('system_prompt', 'x' * 100001)]:
                data = config_for(family)['prompts']
                data[family + '_prompt_presets'][0][key] = value
                with self.subTest(family=family, key=key, value=value[:30]), self.assertRaises(ValueError):
                    config_settings.PromptConfig(**data)

    def test_missing_duplicate_or_invalid_active_selection_refuses_before_requests(self):
        for family in FAMILIES:
            for defect in ('missing', 'duplicate', 'invalid'):
                config = config_for(family)
                preset = config['prompts'][family + '_prompt_presets'][0]
                if defect == 'missing': config['prompts'][family + '_prompt_presets'] = []
                elif defect == 'duplicate': config['prompts'][family + '_prompt_presets'].append(dict(preset))
                else: preset['user_prompt'] = 'missing required payload'
                config['generation'] = {'three_pass_pass1_format': 'compact'} if family == 'pass1_compact' else {'three_pass_delivery_direction': family.removeprefix('pass3_')}
                with self.subTest(family=family, defect=defect), self.assertRaises(ValueError):
                    params_for(config)

    def test_compact_custom_prompt_reaches_actual_shared_request_path(self):
        config = config_for('pass1_compact')
        config['generation'] = {'three_pass_pass1_format': 'compact'}
        params = params_for(config)
        with patch('generate_script.call_llm_for_entries', return_value=[]) as call:
            compact.segment_chunk_compact(object(), 'synthetic', 'First. Second.', params)
        self.assertEqual('Synthetic system {literal braces}', call.call_args.args[2])
        self.assertEqual('Synthetic input:\nN1: First.\nN2: Second.', call.call_args.args[3])
        self.assertEqual(0, call.call_args.kwargs['max_retries'])

    def test_generated_compact_units_are_present_verbatim_once(self):
        rng = random.Random(1937)
        for count in range(1, 35):
            units = [f'unit-{i} ' + ''.join(rng.choices('abc雪é{}[]', k=27)) for i in range(count)]
            system_prompt, user = compact.build_compact_prompt(units, system_prompt='Literal {example}', user_prompt_template='Before\n{units}\nAfter')
            self.assertEqual('Literal {example}', system_prompt)
            self.assertEqual(['Before'] + [f'N{i}: {text}' for i, text in enumerate(units, 1)] + ['After'], user.splitlines())

    def test_each_pass3_mode_sends_its_own_selected_family_with_frozen_batch(self):
        prior = [{'speaker': 'SYNTHETIC', 'text': 'A {literal} line 雪.'}]
        for mode in ('gaps', 'directed', 'action_only'):
            family = 'pass3_gaps' if mode == 'gaps' else 'pass3_directed'
            config = config_for(family)
            config['generation'] = {'three_pass_delivery_direction': mode}
            sys_prompt, user = tp.build_instruct_request(prior, params_for(config))
            self.assertEqual('Synthetic system {literal braces}', sys_prompt)
            rows = json.loads(user.removeprefix('Synthetic input:\n'))
            self.assertEqual([{'n': 0, **prior[0]}], rows)

    def test_default_experimental_requests_and_identities_are_unchanged(self):
        self.assertEqual((compact.COMPACT_SYSTEM, 'Label every line.\n\nN1: A.'), compact.build_compact_prompt(['A.']))
        for mode in ('off', 'gaps', 'directed', 'action_only'):
            config = {'generation': {'three_pass_delivery_direction': mode}}
            params = params_for(config)
            sys_prompt, user = tp.build_instruct_request([], params)
            expected = load_instruct_prompts(mode)
            self.assertEqual((expected[0], expected[1].format(batch='[]')), (sys_prompt, user))
            inactive = config_for('pass1_compact'); inactive.update(config)
            self.assertEqual(tp.three_pass_fingerprint('source', 'm', 3000, params=params),
                             tp.three_pass_fingerprint('source', 'm', 3000, params=params_for(inactive)))

    def test_changed_active_prompt_changes_resume_identity(self):
        for family in FAMILIES:
            config = config_for(family)
            config['generation'] = {'three_pass_pass1_format': 'compact'} if family == 'pass1_compact' else {'three_pass_delivery_direction': family.removeprefix('pass3_')}
            first = tp.three_pass_fingerprint('source', 'm', 3000, params=params_for(config))
            config['prompts'][family + '_prompt_presets'][0]['system_prompt'] += ' changed'
            second = tp.three_pass_fingerprint('source', 'm', 3000, params=params_for(config))
            self.assertNotEqual(first, second, family)

    def test_defaults_endpoint_exposes_real_experimental_text(self):
        defaults = asyncio.run(system.get_default_prompts())
        self.assertEqual(compact.COMPACT_SYSTEM, defaults['pass1_compact_system_prompt'])
        self.assertEqual('Label every line.\n\n{units}', defaults['pass1_compact_user_prompt'])
        for mode in ('gaps', 'directed'):
            expected = load_instruct_prompts(mode)
            self.assertEqual(expected, (defaults[f'pass3_{mode}_system_prompt'], defaults[f'pass3_{mode}_user_prompt']))

    def test_editor_save_switch_failure_and_delete_are_available_for_every_family(self):
        js_fixtures.PromptPresetTransactionTests.run_case(self, r"""
for(const pass of ['pass1_compact','pass3_gaps','pass3_directed']){
 run('passPromptDefaults['+JSON.stringify(pass)+']={system_prompt:"default S",user_prompt:"default U"}');
 context.renderPassPromptPresets(pass,[], 'default');
 assert.strictEqual(element(pass+'-system-prompt').value,'default S');
 element(pass+'-system-prompt').value='custom S';element(pass+'-user-prompt').value='custom U';
 mode='success';await context.window.savePassPromptPreset(pass);
 assert.strictEqual(persisted.prompts[pass+'_preset'],'new');
 assert.strictEqual(persisted.prompts[pass+'_prompt_presets'][0].user_prompt,'custom U');
 context.applyPassPromptPreset(pass,-1);assert.strictEqual(element(pass+'-system-prompt').value,'default S');
 await context.onPromptPresetChange(pass,0);assert.strictEqual(element(pass+'-user-prompt').value,'custom U');
 const before=snapshot();mode='fail';element(pass+'-user-prompt').value='unsaved';await context.window.savePassPromptPreset(pass);
 assert.strictEqual(snapshot(),before);assert.strictEqual(element(pass+'-user-prompt').value,'unsaved');
 mode='success';await context.window.deletePassPromptPreset(pass);assert.strictEqual(persisted.prompts[pass+'_preset'],'default');
}
""")

    def test_saved_settings_round_trip_and_older_clients_keep_all_families(self):
        import tempfile
        from tests.test_config_save_keeps_unsent_fields import KeepUnsentFieldsTests
        profile = {'base_url': 'http://localhost:1/v1', 'api_key': 'fixture', 'model_name': 'fixture'}
        config = {'llm': profile, 'llm_local': profile, 'tts': {'mode': 'local'}, 'prompts': {}}
        for family in FAMILIES: config['prompts'].update(config_for(family)['prompts'])
        with tempfile.TemporaryDirectory() as directory:
            saved = KeepUnsentFieldsTests._save(self, directory, config)
            older = KeepUnsentFieldsTests._save(self, directory, {'llm': profile, 'llm_local': profile, 'tts': {'mode': 'local'}, 'prompts': {'system_prompt': 'older client'}})
            for family in FAMILIES:
                self.assertEqual(config['prompts'][family + '_preset'], saved['prompts'][family + '_preset'])
                self.assertEqual(saved['prompts'][family + '_prompt_presets'], older['prompts'][family + '_prompt_presets'])
                for mode in (['compact'] if family == 'pass1_compact' else ['gaps'] if family == 'pass3_gaps' else ['directed', 'action_only']):
                    older['generation'] = {'three_pass_pass1_format': mode} if mode == 'compact' else {'three_pass_delivery_direction': mode}
                    params = params_for(older)
                    self.assertEqual('Synthetic system {literal braces}', params.compact_system_prompt if mode == 'compact' else params.instruct_system_prompt)

    def test_http_and_real_manual_requests_use_identical_custom_text_and_validation(self):
        import tempfile
        import time
        from llm_provider import ManualClient, apply_manual_cancellation
        from tests.test_manual_llm import _answer_when_pending
        from tests.test_delivery_direction import make_recording_client
        cases = [('compact', 'pass1_compact'), ('gaps', 'pass3_gaps'), ('directed', 'pass3_directed'), ('action_only', 'pass3_directed')]
        for mode, family in cases:
            config = config_for(family)
            config['generation'] = {'three_pass_pass1_format': mode} if mode == 'compact' else {'three_pass_delivery_direction': mode}
            params = params_for(config)
            prior = [{'speaker': 'SYNTHETIC', 'text': 'A synthetic line.'}]
            reply = [{'n': 1, 'type': 'NARRATOR'}] if mode == 'compact' else ([{'n': 0, 'instruct': 'Quiet.', 'gap': 'normal'}] if mode == 'gaps' else [{'n': 0, 'answers': None, 'action': 'stating', 'delivery': 'quiet', 'gap': 'normal'}])
            invoke = lambda client: compact.segment_chunk_compact(client, 'fixture', prior[0]['text'], params) if mode == 'compact' else tp.instruct_batch(client, 'fixture', prior, params, max_retries=0)
            http_seen = []
            expected = invoke(make_recording_client([reply], http_seen))
            self.assertTrue(expected)
            with tempfile.TemporaryDirectory() as directory:
                client = ManualClient(directory); client.POLL_SECONDS = .01
                original = client.chat.completions.create; manual_seen = []
                def record(**kwargs):
                    manual_seen.append(kwargs['messages']); return original(**kwargs)
                client.chat.completions.create = record
                answer = _answer_when_pending(client, json.dumps(reply))
                deadline = time.monotonic() + 5
                with apply_manual_cancellation(lambda: time.monotonic() > deadline):
                    actual = invoke(client)
                answer.join(2); self.assertFalse(answer.is_alive())
                self.assertEqual(expected, actual)
                self.assertEqual(http_seen, manual_seen)
                self.assertEqual('Synthetic system {literal braces}', manual_seen[0][0]['content'])
                self.assertEqual(prior[0]['text'], actual[0]['text'])

    def test_pass3_sizing_uses_the_custom_prompt_and_refuses_an_over_budget_request(self):
        from dataclasses import replace
        prior = [{'speaker': 'SYNTHETIC', 'text': 'A synthetic line.'}]
        for mode, family in [('gaps', 'pass3_gaps'), ('directed', 'pass3_directed'), ('action_only', 'pass3_directed')]:
            config = config_for(family, system='synthetic ' * 1000)
            config['generation'] = {'three_pass_delivery_direction': mode}
            params = replace(params_for(config), max_input_tokens=100)
            self.assertFalse(tp.does_instruct_batch_fit_context(prior, params))
            self.assertTrue(tp.does_instruct_batch_fit_context(prior, replace(params, max_input_tokens=20000)))

    def test_batch_preflight_and_sizing_cache_use_selected_experimental_text(self):
        from routers import script
        for mode, family in [('compact', 'pass1_compact'), ('gaps', 'pass3_gaps'), ('directed', 'pass3_directed'), ('action_only', 'pass3_directed')]:
            config = config_for(family)
            config['generation'] = {'three_pass_pass1_format': mode, 'three_pass_segmentation': 'llm'} if mode == 'compact' else {'three_pass_delivery_direction': mode, 'three_pass_segmentation': 'quotes'}
            settings = tp.resolve_three_pass_generation_settings(config)
            source = '"A synthetic line."'
            first = script.get_batch_script_sizing_identity(source, settings, [1000], 32000)
            if mode == 'compact':
                with patch.object(compact, 'build_compact_prompt', wraps=compact.build_compact_prompt) as builder:
                    report = tp.build_three_pass_request_preflight(source, settings, 32000, 1)
                self.assertTrue(any(request['stage'] == 'segment_compact' for request in report['requests']))
                self.assertTrue(builder.called)
                self.assertEqual('Synthetic system {literal braces}', builder.call_args.args[1])
            else:
                with patch.object(tp, 'build_instruct_request', wraps=tp.build_instruct_request) as builder:
                    tp.build_three_pass_request_preflight(source, settings, 32000, 1)
                self.assertTrue(builder.called)
                self.assertEqual(mode, builder.call_args.args[1].delivery_direction)
                self.assertEqual('Synthetic system {literal braces}', builder.call_args.args[1].instruct_system_prompt)
            config['prompts'][family + '_prompt_presets'][0]['system_prompt'] += ' changed'
            changed = tp.resolve_three_pass_generation_settings(config)
            self.assertNotEqual(first, script.get_batch_script_sizing_identity(source, changed, [1000], 32000))

    def test_cli_samples_and_full_run_receive_selected_experimental_prompts(self):
        import tempfile
        from tests.test_three_pass_cli_settings import ThreePassCliSettingsTests
        for mode, family in [('compact', 'pass1_compact'), ('gaps', 'pass3_gaps'), ('directed', 'pass3_directed'), ('action_only', 'pass3_directed')]:
            config = config_for(family)
            config.update(llm_mode='local', llm_local={'model_name': 'fixture', 'base_url': 'http://fixture/v1'})
            config['generation'] = {'max_tokens': 4096}
            config['generation']['three_pass_pass1_format' if mode == 'compact' else 'three_pass_delivery_direction'] = mode
            for preflight in (False, True):
                with self.subTest(mode=mode, preflight=preflight), tempfile.TemporaryDirectory() as directory:
                    calls, _ = ThreePassCliSettingsTests.run_cli(self, directory, config, preflight, [])
                    for _, params, _, _ in calls:
                        if mode == 'compact':
                            system_text, user = compact.build_compact_prompt(['Synthetic unit.'], params.compact_system_prompt, params.compact_user_prompt_template)
                        else:
                            system_text, user = tp.build_instruct_request([{'speaker': 'SYNTHETIC', 'text': 'Synthetic unit.'}], params)
                        self.assertEqual('Synthetic system {literal braces}', system_text)
                        self.assertIn('Synthetic unit.', user)
