"""Cast instruction presets affect real requests without editing source or roster payloads."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cast_list
import config_settings
from tests import test_incremental_cast as fixtures
from tests import test_prompt_preset_transactions_js as js_fixtures

PRESET = {'name': 'synthetic', 'whole_book_prompt': 'Find synthetic speakers. {literal JSON}\n',
          'incremental_prompt': 'Find supported synthetic aliases.\n'}
CONFIG = {'prompts': {'cast_preset': 'synthetic', 'cast_prompt_presets': [PRESET]}}


class CastPromptStudioTests(unittest.TestCase):
    def test_schema_retains_cast_preset_and_bounds_text(self):
        prompts = config_settings.PromptConfig(**CONFIG['prompts']).model_dump()
        self.assertEqual('synthetic', prompts['cast_preset'])
        self.assertEqual(PRESET['whole_book_prompt'], prompts['cast_prompt_presets'][0]['whole_book_prompt'])
        for text in ('', '   ', 'x' * 100001):
            with self.subTest(text_length=len(text)), self.assertRaises(ValueError):
                config_settings.PromptConfig(cast_prompt_presets=[{**PRESET, 'whole_book_prompt': text}])

    def run_main(self, source, out, client, *args, config=None):
        return fixtures.IncrementalCastTests.run_main(self, source, out, client, *args, config=config)

    def test_selected_whole_book_prompt_and_provenance_reach_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory)/'book.txt', Path(directory)/'cast.json'
            source.write_text('Synthetic source {with braces}.', encoding='utf8')
            client = fixtures.Client([fixtures.ALICE])
            self.assertEqual(0, self.run_main(source, out, client, config=CONFIG))
            self.assertEqual(PRESET['whole_book_prompt'] + source.read_text(), client.requests[0]['messages'][0]['content'])
            evidence = json.loads(out.read_text())['provenance']
            self.assertEqual(hashlib.sha256(PRESET['whole_book_prompt'].encode()).hexdigest(), evidence['prompt_sha256'])
            self.assertEqual('synthetic', evidence['prompt_preset'])

    def test_incremental_custom_instruction_keeps_all_source_and_prior_aliases(self):
        client = fixtures.Client([fixtures.ALICE, fixtures.BOB])
        cast, windows, _ = cast_list.request_incremental_cast(client, 'fixture', 'abcdefgh', 4, 8000,
            instruction=PRESET['incremental_prompt'])
        self.assertEqual(fixtures.ALICE + fixtures.BOB, cast)
        self.assertEqual([(0, 4), (4, 8)], [(w['start'], w['end']) for w in windows])
        for index, request in enumerate(client.requests):
            prompt = request['messages'][0]['content']
            self.assertTrue(prompt.startswith(PRESET['incremental_prompt']))
            self.assertTrue(prompt.endswith(('abcd', 'efgh')[index]))
        self.assertIn('THE PILOT', client.requests[1]['messages'][0]['content'])

    def test_missing_selected_preset_refuses_before_model_and_preserves_old_cast(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory)/'book.txt', Path(directory)/'cast.json'
            source.write_text('Synthetic source.', encoding='utf8')
            out.write_bytes(b'previous complete cast')
            client = fixtures.Client([fixtures.ALICE])
            self.assertEqual(1, self.run_main(source, out, client,
                config={'prompts': {'cast_preset': 'missing'}}))
            self.assertEqual([], client.requests)
            self.assertEqual(b'previous complete cast', out.read_bytes())

    def test_default_requests_keep_exact_legacy_text(self):
        client = fixtures.Client([fixtures.ALICE])
        cast_list.request_cast_list(client, 'fixture', 'synthetic', 8000)
        self.assertEqual(cast_list.PROMPT + 'synthetic', client.requests[0]['messages'][0]['content'])
        self.assertEqual(cast_list.INCREMENTAL_PROMPT + 'VALIDATED ROSTER (canonical names with aliases):\n[]\n\nSOURCE WINDOW:\nsynthetic',
                         cast_list.build_incremental_prompt('synthetic', []))

    def test_cast_presets_save_switch_and_preserve_inflight_edits(self):
        js_fixtures.PromptPresetTransactionTests.run_case(self, r"""
run("passPromptDefaults.cast={whole_book_prompt:'default whole',incremental_prompt:'default incremental'};renderPassPromptPresets('cast',[], 'default');");
assert.strictEqual(element('cast-whole-book-prompt').value,'default whole');
element('cast-whole-book-prompt').value='custom whole';element('cast-incremental-prompt').value='custom incremental';
mode='success';await context.window.savePassPromptPreset('cast');
assert.strictEqual(persisted.prompts.cast_preset,'new');
assert.strictEqual(persisted.prompts.cast_prompt_presets[0].whole_book_prompt,'custom whole');
run("applyPassPromptPreset('cast',-1)");assert.strictEqual(element('cast-whole-book-prompt').value,'default whole');
await context.onPromptPresetChange('cast',0);assert.strictEqual(element('cast-incremental-prompt').value,'custom incremental');
const before=snapshot();mode='fail';element('cast-whole-book-prompt').value='unsaved';await context.window.savePassPromptPreset('cast');
assert.strictEqual(snapshot(),before);assert.strictEqual(element('cast-whole-book-prompt').value,'unsaved');
mode='hold';const saving=context.window.savePassPromptPreset('cast');await entered;
element('cast-whole-book-prompt').value='typed while saving';release();await saving;
assert.strictEqual(element('cast-whole-book-prompt').value,'typed while saving');
mode='success';await context.window.deletePassPromptPreset('cast');
assert.strictEqual(persisted.prompts.cast_preset,'default');assert.strictEqual(persisted.prompts.cast_prompt_presets.length,0);
""")

    def test_saved_cast_presets_round_trip_and_survive_older_config_clients(self):
        from tests.test_config_save_keeps_unsent_fields import KeepUnsentFieldsTests
        profile = {'base_url': 'http://localhost:1/v1', 'api_key': 'fixture', 'model_name': 'fixture'}
        with tempfile.TemporaryDirectory() as directory:
            payload = {'llm': profile, 'llm_local': profile, 'tts': {'mode': 'local'}, **CONFIG}
            saved = KeepUnsentFieldsTests._save(self, directory, payload)
            self.assertEqual([{**PRESET, 'description': ''}], saved['prompts']['cast_prompt_presets'])
            saved = KeepUnsentFieldsTests._save(self, directory, {'llm': profile, 'llm_local': profile, 'tts': {'mode': 'local'},
                'prompts': {'system_prompt': 'older client edits attribution'}})
            self.assertEqual('synthetic', saved['prompts']['cast_preset'])
            self.assertEqual(PRESET, {k: v for k, v in saved['prompts']['cast_prompt_presets'][0].items() if k != 'description'})

    def test_selected_incremental_preset_reaches_cli_and_preserves_old_cast_on_bad_reply(self):
        config = {**CONFIG, 'generation': {'cast_list_incremental': True, 'cast_list_source_chars': 4}}
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory)/'book.txt', Path(directory)/'cast.json'
            source.write_text('abcdefgh', encoding='utf8')
            client = fixtures.Client([fixtures.ALICE, fixtures.BOB])
            self.assertEqual(0, self.run_main(source, out, client, config=config))
            evidence = json.loads(out.read_text())['provenance']
            self.assertEqual(hashlib.sha256(PRESET['incremental_prompt'].encode()).hexdigest(), evidence['prompt_sha256'])
            self.assertTrue(evidence['complete'])
            self.assertTrue(all(r['messages'][0]['content'].startswith(PRESET['incremental_prompt']) for r in client.requests))
            previous = out.read_bytes()
            self.assertEqual(1, self.run_main(source, out, fixtures.Client([fixtures.ALICE, 'malformed']), config=config))
            self.assertEqual(previous, out.read_bytes())

    def test_custom_manual_and_http_requests_match_with_complete_roster(self):
        from llm_provider import ManualClient, ConfiguredOpenAI
        from utils import atomic_json_write
        recorded = []
        with tempfile.TemporaryDirectory() as directory:
            manual = ManualClient(directory)
            replies = iter([fixtures.ALICE, fixtures.BOB])
            def answer(_seconds):
                request = json.loads(Path(manual.pending_path).read_text(encoding='utf8'))
                recorded.append(request)
                atomic_json_write({'id': request['id'], 'content': json.dumps(next(replies))}, manual.response_path)
            with patch('llm_provider.time.sleep', side_effect=answer):
                cast_list.request_incremental_cast(manual, 'fixture', 'abcdefgh', 4, 8000,
                    instruction=PRESET['incremental_prompt'])
            raw = fixtures.Client([fixtures.ALICE, fixtures.BOB])
            cast_list.request_incremental_cast(ConfiguredOpenAI(raw, {}), 'fixture', 'abcdefgh', 4, 8000,
                instruction=PRESET['incremental_prompt'])
            self.assertEqual([r['messages'] for r in recorded], [r['messages'] for r in raw.requests])
            self.assertIn('THE PILOT', recorded[-1]['messages'][0]['content'])

    def test_custom_instruction_counts_toward_full_prompt_budget(self):
        client = fixtures.Client([fixtures.ALICE])
        with self.assertRaises(ValueError):
            cast_list.request_incremental_cast(client, 'fixture', 'abcd', 4, 8000,
                instruction='Synthetic long instruction. ' * 200, limits={'max_input_tokens': 50})
        self.assertEqual([], client.requests)

    def test_ambiguous_or_invalid_selected_presets_refuse(self):
        for presets in ([PRESET, PRESET], [{**PRESET, 'incremental_prompt': ' '}], []):
            with self.subTest(presets=presets), self.assertRaises(ValueError):
                cast_list.get_cast_prompt_settings({'prompts': {'cast_preset': 'synthetic', 'cast_prompt_presets': presets}})

    def test_whole_book_prompt_budget_refuses_before_model_and_preserves_cast(self):
        import contextlib
        import io
        for limits in ({'max_input_tokens': 50}, {'context_length': 8100}, {'max_output_tokens': 7999}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as directory:
                source, out = Path(directory)/'book.txt', Path(directory)/'cast.json'
                source.write_text('Synthetic source.', encoding='utf8')
                out.write_bytes(b'previous complete cast')
                client = fixtures.Client([fixtures.ALICE])
                config = {'prompts': {'cast_preset': 'synthetic', 'cast_prompt_presets': [
                    {**PRESET, 'whole_book_prompt': 'Synthetic instructions. ' * 100}]}}
                profile = {'model_name': 'fixture', **limits}
                with patch('config_settings.load_app_config', return_value=config), \
                        patch('lmstudio_settings.get_active_llm_config', return_value=profile), \
                        patch('llm_provider.make_run_client', return_value=client), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(1, cast_list.main([str(source), '--out', str(out)]))
                self.assertEqual([], client.requests)
                self.assertEqual(b'previous complete cast', out.read_bytes())

    def test_cast_preview_displays_names_and_keeps_instruction_text_private(self):
        from config_change_preview import get_preview_value
        self.assertEqual('alpha', get_preview_value('alpha', 'cast_preset'))
        self.assertEqual('bravo', get_preview_value('bravo', 'cast_preset'))
        self.assertNotIn('synthetic private instruction',
                         get_preview_value('synthetic private instruction', 'whole_book_prompt'))

    def test_defaults_endpoint_exposes_both_actual_instructions(self):
        import asyncio
        from routers.system import get_default_prompts
        defaults = asyncio.run(get_default_prompts())
        self.assertEqual(cast_list.PROMPT, defaults['cast_whole_book_prompt'])
        self.assertEqual(cast_list.INCREMENTAL_PROMPT, defaults['cast_incremental_prompt'])


if __name__ == '__main__':
    unittest.main()
