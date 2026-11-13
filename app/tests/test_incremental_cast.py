"""Synthetic regression coverage for the opt-in cast discovery contract (#1931)."""
import contextlib
import hashlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cast_list
from generate_script import LLMGenParams
from three_pass_generate import build_attribute_request, get_attribute_sizing_request, get_cast_from_data, three_pass_fingerprint
from attribution_prompt_variants import VARIANTS, build_variant_request


class Client:
    def __init__(self, replies, action=None):
        self.replies = iter(replies)
        self.requests = []
        self.action = action
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.action:
            self.action(len(self.requests))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        content, reason = reply if isinstance(reply, tuple) else (json.dumps(reply), 'stop')
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=content))], usage=None)


ALICE = [{'name': 'ALICE', 'aliases': ['THE PILOT']}]
BOB = [{'name': 'BOB', 'aliases': ['THE COOK']}]


class IncrementalCastTests(unittest.TestCase):
    def test_windows_cover_every_character_once_and_obey_bound(self):
        rng = random.Random(1931)
        for _ in range(250):
            limit = rng.randrange(3, 100)
            text = ''.join('a' + ''.join(rng.choice('a 😀界') for _ in range(rng.randrange(limit - 2)))
                           + '\n\n' for _ in range(rng.randrange(1, 20)))
            windows = list(cast_list.iter_source_windows(text, limit))
            self.assertEqual(text, ''.join(part for _, _, part in windows))
            cursor = 0
            for start, end, part in windows:
                self.assertEqual(cursor, start)
                self.assertEqual(text[start:end], part)
                self.assertTrue(0 < len(part) <= limit)
                cursor = end
            self.assertEqual(len(text), cursor)

    def test_merge_preserves_prior_aliases_and_is_idempotent(self):
        prior = ALICE + BOB
        addition = [{'name': 'THE PILOT', 'aliases': ['CAPTAIN ALICE']}]
        merged, dropped = cast_list.merge_cast_window(prior, addition)
        self.assertEqual(ALICE + BOB, prior)
        self.assertEqual(['THE PILOT', 'CAPTAIN ALICE'], merged[0]['aliases'])
        self.assertEqual((merged, []), cast_list.merge_cast_window(merged, addition))
        self.assertEqual([], dropped)

    def test_generated_merge_sequences_preserve_every_nonconflicting_identity(self):
        rng = random.Random(1931)
        for _ in range(100):
            contributions = [[{'name': f'PERSON {index}', 'aliases': [f'ALIAS {index}']}]
                             for index in range(rng.randrange(1, 15))]
            rng.shuffle(contributions)
            accumulated = []
            for contribution in contributions:
                before = json.loads(json.dumps(accumulated))
                accumulated, dropped = cast_list.merge_cast_window(accumulated, contribution)
                self.assertEqual([], dropped)
                self.assertEqual(before, accumulated[:-1])
                self.assertEqual((accumulated, []), cast_list.merge_cast_window(accumulated, contribution))
            parsed = get_cast_from_data(accumulated)
            self.assertEqual(len(contributions), len(parsed['names']))
            self.assertEqual(len(contributions), len(parsed['alias_to_name']))
            conflict = [{'name': 'ANOTHER PERSON', 'aliases': [accumulated[0]['aliases'][0]]}]
            with self.assertRaises(ValueError):
                cast_list.merge_cast_window(accumulated, conflict)

    def test_conflicting_alias_claims_refuse_rather_than_merge_people(self):
        with self.assertRaisesRegex(ValueError, 'alias|ownership|collid'):
            cast_list.merge_cast_window(ALICE, [{'name': 'BOB', 'aliases': ['THE PILOT']}])
        merged, dropped = cast_list.merge_cast_window(ALICE, [{'name': 'BOB', 'aliases': ['NARRATOR', 'UNKNOWN']}])
        self.assertEqual(ALICE + [{'name': 'BOB', 'aliases': []}], merged)
        self.assertEqual(2, len(dropped))

    def test_each_window_receives_entire_prior_roster_and_budget(self):
        client = Client([ALICE, [], BOB])
        cast, windows, dropped = cast_list.request_incremental_cast(client, 'fixture', 'a.\n\nb.\n\nc.\n\n', 4, 12000)
        self.assertEqual(ALICE + BOB, cast)
        self.assertEqual([], dropped)
        self.assertEqual([(0, 4), (4, 8), (8, 12)], [(w['start'], w['end']) for w in windows])
        for request in client.requests[1:]:
            prompt = request['messages'][0]['content']
            self.assertIn('ALICE', prompt)
            self.assertIn('THE PILOT', prompt)
            self.assertEqual(12000, request['max_tokens'])

    def test_truncated_even_valid_json_is_discarded_and_empty_window_can_retry(self):
        client = Client([(json.dumps(ALICE), 'length'), [], BOB])
        cast, windows, _ = cast_list.request_incremental_cast(client, 'fixture', 'a.\n\nb.\n\n', 4, 4000)
        self.assertEqual(BOB, cast)
        self.assertEqual(2, windows[0]['attempts'])
        self.assertEqual(['length'], windows[0]['discarded_finish_reasons'])
        self.assertNotIn('ALICE', client.requests[-1]['messages'][0]['content'])

    def run_main(self, source, out, client, *extra, config=None):
        with patch('config_settings.load_app_config', return_value=config or {}), patch('lmstudio_settings.get_active_llm_config', return_value={'model_name': 'fixture'}), patch('llm_provider.make_run_client', return_value=client), contextlib.redirect_stdout(io.StringIO()):
            return cast_list.main([str(source), '--out', str(out), *extra])

    def test_failure_cancellation_empty_result_and_stale_source_preserve_final(self):
        for failure in ('bad', 'truncated', 'cancel', 'empty', 'stale', 'identity'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
                source.write_text('Synthetic line.\n\n' * 5, encoding='utf8')
                out.write_bytes(b'previous complete cast')
                action = (lambda n: source.write_text('changed', encoding='utf8')) if failure == 'stale' else None
                replies = {'bad': [ALICE, ('oops', 'stop')], 'truncated': [(json.dumps(ALICE), 'length')] * 3, 'cancel': [ALICE, KeyboardInterrupt()], 'empty': [[]] * 10, 'stale': [ALICE] * 10, 'identity': [ALICE] * 10}[failure]
                client = Client(replies, action)
                extra = ['--incremental', '--source-chars', '20']
                if failure == 'identity':
                    extra += ['--source-sha256', '0' * 64]
                code = self.run_main(source, out, client, *extra)
                self.assertNotEqual(0, code)
                self.assertEqual(b'previous complete cast', out.read_bytes())
                if failure == 'identity':
                    self.assertEqual([], client.requests)
                else:
                    self.assertEqual({'bad': 2, 'truncated': 3, 'cancel': 2,
                                      'empty': 5, 'stale': 5}[failure], len(client.requests))

    def test_opt_in_removes_whole_book_limit_and_records_complete_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('Synthetic line.\n\n' * 5, encoding='utf8')
            client = Client([ALICE] * 20)
            with patch.object(cast_list, 'MAX_SOURCE_CHARS', 30):
                self.assertEqual(0, self.run_main(source, out, client, '--incremental', '--source-chars', '20', '--max-tokens', '12000'))
            document = json.loads(out.read_text(encoding='utf8'))
            provenance = document['provenance']
            self.assertTrue(provenance['incremental'])
            self.assertEqual(12000, provenance['max_tokens'])
            self.assertEqual(20, provenance['source_chars'])
            self.assertEqual('paragraph-v1', provenance['window_boundary_policy'])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), provenance['source_sha256'])
            self.assertEqual(provenance['prepared_source_chars'], provenance['windows'][-1]['end'])

    def test_incremental_completion_metadata_does_not_invent_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('Synthetic dialogue.', encoding='utf8')
            self.assertEqual(0, self.run_main(source, out, Client([ALICE]), '--incremental'))
            provenance = json.loads(out.read_text(encoding='utf8'))['provenance']
            self.assertIsNone(provenance['finish_reason'])
            self.assertEqual('accepted_window_responses_only', provenance['usage_scope'])
            self.assertEqual({'prompt_tokens': None, 'completion_tokens': None}, provenance['usage'])
            self.assertEqual('stop', provenance['windows'][0]['finish_reason'])

    def test_usage_aggregates_only_observed_accepted_window_responses(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
                source.write_text('a.\n\nb.\n\n', encoding='utf8')
                client = Client([ALICE, BOB])
                original_create = client.create
                def create(**kwargs):
                    response = original_create(**kwargs)
                    response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=(None if missing and len(client.requests) == 2 else 5))
                    return response
                client.chat.completions.create = create
                self.assertEqual(0, self.run_main(source, out, client, '--incremental', '--source-chars', '4'))
                usage = json.loads(out.read_text(encoding='utf8'))['provenance']['usage']
                self.assertEqual({'prompt_tokens': 20, 'completion_tokens': None if missing else 10}, usage)

    def test_interrupted_finish_reasons_preserve_previous_complete_cast(self):
        for reason in ('content_filter', None, 'tool_calls'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
                source.write_text('Synthetic dialogue.', encoding='utf8')
                out.write_bytes(b'previous complete cast')
                self.assertEqual(1, self.run_main(source, out, Client([(json.dumps(ALICE), reason)]), '--incremental'))
                self.assertEqual(b'previous complete cast', out.read_bytes())

    def test_conflict_diagnostic_reports_build_cause_without_blaming_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('a.\n\nb.\n\n', encoding='utf8')
            output = io.StringIO()
            client = Client([ALICE, [{'name': 'BOB', 'aliases': ['THE PILOT']}]])
            with patch('config_settings.load_app_config', return_value={}), patch('lmstudio_settings.get_active_llm_config', return_value={'model_name': 'fixture'}), patch('llm_provider.make_run_client', return_value=client), contextlib.redirect_stdout(output):
                code = cast_list.main([str(source), '--out', str(out), '--incremental', '--source-chars', '4'])
            self.assertEqual(1, code)
            self.assertIn('cast build refused', output.getvalue())
            self.assertNotIn('reply was not the JSON', output.getvalue())
            self.assertFalse(out.exists())

    def test_alias_sizing_requires_source_identity_when_enabled(self):
        from routers.script import get_cast_sizing_settings
        self.assertEqual({}, get_cast_sizing_settings({}, None))
        with self.assertRaisesRegex(ValueError, 'source path'):
            get_cast_sizing_settings({'cast_aliases': True}, None)

    def test_invalid_reply_shapes_are_rejected(self):
        for data in ([{'name': 4}], [{'name': 'A', 'aliases': 'B'}], [{'name': 'A', 'aliases': [5]}]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                cast_list.parse_cast_list(json.dumps(data))

    def test_full_prompt_overflow_is_refused_without_dropping_roster(self):
        client = Client([ALICE, BOB])
        from llm_request_budget import get_prompt_token_estimate
        first = cast_list.build_incremental_prompt('a.\n\n', [])
        limit = get_prompt_token_estimate([{'role': 'user', 'content': first}])
        with self.assertRaises(ValueError):
            cast_list.request_incremental_cast(client, 'fixture', 'a.\n\nb.\n\n', 4, 100, limits={'max_input_tokens': limit})
        self.assertEqual(1, len(client.requests))


    def test_real_manual_and_http_wrappers_receive_identical_validated_windows(self):
        from llm_provider import ManualClient, ConfiguredOpenAI
        from utils import atomic_json_write
        recorded = []
        with tempfile.TemporaryDirectory() as directory:
            manual = ManualClient(directory)
            replies = iter([ALICE, BOB])
            def answer(_seconds):
                request = json.loads(Path(manual.pending_path).read_text(encoding='utf8'))
                recorded.append(request)
                atomic_json_write({'id': request['id'], 'content': json.dumps(next(replies))}, manual.response_path)
            with patch('llm_provider.time.sleep', side_effect=answer):
                manual_cast, _, _ = cast_list.request_incremental_cast(manual, 'fixture', 'a.\n\nb.\n\n', 4, 12000)
            raw = Client([ALICE, BOB])
            http = ConfiguredOpenAI(raw, {}, limits={'max_output_tokens': 16000})
            http_cast, _, _ = cast_list.request_incremental_cast(http, 'fixture', 'a.\n\nb.\n\n', 4, 12000)
            self.assertEqual(manual_cast, http_cast)
            self.assertEqual([r['messages'] for r in recorded], [r['messages'] for r in raw.requests])
            self.assertTrue(all(r['params']['max_tokens'] == 12000 for r in recorded))
            self.assertFalse(Path(manual.pending_path).exists())

    def test_saved_configuration_drives_cli_defaults_and_explicit_off_wins(self):
        from cast_list_settings import get_cast_list_settings
        from config_settings import GenerationConfig
        self.assertEqual({'incremental': False, 'source_chars': 30000, 'max_tokens': 8000}, get_cast_list_settings({}))
        config = {'generation': {'cast_list_incremental': True, 'cast_list_source_chars': 20, 'cast_list_max_tokens': 12345}}
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('Synthetic line.\n\n' * 5, encoding='utf8')
            client = Client([ALICE] * 10)
            self.assertEqual(0, self.run_main(source, out, client, config=config))
            self.assertTrue(all(r['max_tokens'] == 12345 for r in client.requests))
            self.assertGreater(len(client.requests), 1)
            client = Client([ALICE])
            self.assertEqual(0, self.run_main(source, out, client, '--no-incremental', config=config))
            self.assertEqual(1, len(client.requests))
        for settings in ({'cast_list_incremental': 'true'}, {'cast_list_source_chars': True}, {'cast_list_source_chars': 0}, {'cast_list_max_tokens': -1}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                GenerationConfig(**settings)

    def test_source_change_while_waiting_for_publication_lock_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('Synthetic line.\n\n' * 5, encoding='utf8')
            out.write_bytes(b'old complete cast')
            @contextlib.contextmanager
            def changed_lock(_path):
                source.write_text('changed while waiting', encoding='utf8')
                yield
            with patch.object(cast_list, 'file_lock', changed_lock):
                code = self.run_main(source, out, Client([ALICE] * 10), '--incremental', '--source-chars', '20')
            self.assertEqual(1, code)
            self.assertEqual(b'old complete cast', out.read_bytes())
            self.assertEqual('changed while waiting', source.read_text(encoding='utf8'))


class ExperimentalPass2AliasTests(unittest.TestCase):
    def test_opt_in_without_cast_reports_missing_alias_evidence(self):
        from three_pass_generate import run_three_pass
        for enabled in (False, True):
            output = io.StringIO()
            with patch('three_pass_generate.validate_instruct_context_chars', side_effect=RuntimeError('fixture stop')), contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, 'fixture stop'):
                    run_three_pass(None, 'fixture', 'Synthetic dialogue.', LLMGenParams(cast_aliases=enabled), 3000)
            self.assertEqual(enabled, 'no cast aliases loaded' in output.getvalue())

    def test_every_variant_and_custom_preset_gets_explicit_alias_evidence(self):
        batch = [{'type': 'SPOKEN', 'text': 'A synthetic greeting.'}]
        cast = get_cast_from_data(ALICE)
        params = LLMGenParams(cast_aliases=True, cast_alias_groups=cast['alias_groups'])
        for variant in VARIANTS:
            for texts in (None, {'system': 'Synthetic instruction', 'user': 'Attribute {batch} to {roster}', 'example': ''}):
                with self.subTest(variant=variant, custom=bool(texts)):
                    actual = build_variant_request(variant, batch, params, ['ALICE'], cast['alias_groups'], surround={'before': '', 'after': ''}, texts=texts)
                    sized = get_attribute_sizing_request(batch, params, ['ALICE'], None, {'before': '', 'after': ''}, variant, texts, cast)
                    self.assertEqual(actual, sized)
                    self.assertIn('THE PILOT', actual[1])
                    self.assertEqual(1, actual[1].count('VALIDATED CAST ALIASES'))
        legacy = build_attribute_request(batch, LLMGenParams(), ['ALICE'])
        opted = build_attribute_request(batch, params, ['ALICE'])
        self.assertNotIn('THE PILOT', legacy[1])
        self.assertIn('THE PILOT', opted[1])
        self.assertEqual(legacy, build_attribute_request(batch, LLMGenParams(cast_aliases=False, cast_alias_groups=cast['alias_groups']), ['ALICE']))

    def test_actual_canonical_transport_gets_aliases_and_canonicalizes_speaker(self):
        from three_pass_generate import attribute_batch
        client = Client([[{'n': 0, 'speaker': 'THE PILOT'}]])
        cast = get_cast_from_data(ALICE)
        result = attribute_batch(client, 'fixture', [{'type': 'SPOKEN', 'text': 'A synthetic greeting.'}],
                                 LLMGenParams(cast_aliases=True, structured_output='off'), ['ALICE'], cast=cast)
        self.assertEqual('ALICE', result[0]['speaker'])
        self.assertIn('THE PILOT', client.requests[0]['messages'][-1]['content'])

    def test_checkpoint_identity_changes_only_on_opt_in(self):
        base = dict(source_text='Synthetic source.', model_name='m', chunk_size=3000)
        legacy = three_pass_fingerprint(**base, params=LLMGenParams())
        self.assertEqual(legacy, three_pass_fingerprint(**base, params=LLMGenParams(cast_alias_groups=[['ALICE', 'THE PILOT']])))
        self.assertNotEqual(legacy, three_pass_fingerprint(**base, params=LLMGenParams(cast_aliases=True)))


if __name__ == '__main__':
    unittest.main()
