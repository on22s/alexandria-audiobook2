"""Contextual review can measure extra context in whole entries or characters.

WHAT THIS VERIFIES (behaviour, not return values):
- one validator clamps both review routes to the same per-mode limits;
- characters mode packs whole neighbours nearest-first from each side, never
  splits an entry, counts only entry text, and includes a single oversize
  neighbour the way Step 2's windowing does;
- the default (entries, 4) fingerprint is byte-for-byte unchanged, and changing
  the mode or value changes the fingerprint and refuses a checkpoint resume;
- the single-book route sends --context-chars for characters mode and
  --context-window for entries mode; batch uses the same cap.
"""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from generate_script import LLMGenParams
import review_script
from routers import script


# Captured from origin/main: the default (entries, 4) receipt must stay exact.
DEFAULT_FINGERPRINT = 'b19a9e494d99696271a23873ddc1c2585c8b0cc2d4f042cefc5a518b8012ce6c'


def entry(text, speaker='ALICE', instruct='calm'):
    """Synthetic fixture entry; no passage from a real book is used."""
    return {'speaker': speaker, 'text': text, 'instruct': instruct}


def args_for(context_window=4, context_chars=0):
    return SimpleNamespace(context_window=context_window, context_chars=context_chars,
                           dedupe_speakers=True, alias_registry=None, remap_voice_config=None)


def fingerprint_for(args, **config_over):
    config = {'generation': {'review_batch_size': 25}, 'llm_mode': 'local', **config_over}
    profile = {'base_url': 'http://fixture/v1', 'model_name': 'model', 'api_key': 'k'}
    params = LLMGenParams(system_prompt='system', user_prompt_template='{batch}')
    return review_script.get_completed_review_fingerprint(
        config, profile, params, args, object(), 'model')


class ContextValidatorTests(unittest.TestCase):
    def test_entries_mode_keeps_old_default_and_clamps_to_one_hundred(self):
        self.assertEqual(('entries', 4), review_script.get_validated_context_setting('entries', 4, 4))
        self.assertEqual(('entries', 100), review_script.get_validated_context_setting('entries', 101, 4))
        self.assertEqual(('entries', 1), review_script.get_validated_context_setting('entries', -3, 4))
        self.assertEqual(('entries', 100), review_script.get_validated_context_setting('junk', 500, 4),
                         'an unknown mode cannot smuggle in the characters limit')

    def test_characters_mode_clamps_to_one_million(self):
        self.assertEqual(('chars', 1000000), review_script.get_validated_context_setting('chars', 1000001, 2000))
        self.assertEqual(('chars', 1), review_script.get_validated_context_setting('chars', -1, 2000))
        self.assertEqual(('chars', 2000), review_script.get_validated_context_setting('chars', 2000, 2000))

    def test_batch_minimum_zero_is_preserved_and_non_integers_are_refused(self):
        self.assertEqual(('entries', 0), review_script.get_validated_context_setting('entries', 0, 0, minimum=0))
        self.assertEqual(('entries', 100), review_script.get_validated_context_setting('entries', 999, 0, minimum=0))
        for bad in (2.5, 'five', True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                review_script.get_validated_context_setting('chars', bad, 2000)

    def test_cli_args_default_to_entries_and_honour_characters(self):
        self.assertEqual(('entries', 4), review_script.get_review_context_from_args(args_for()))
        self.assertEqual(('chars', 5000), review_script.get_review_context_from_args(args_for(0, 5000)))
        self.assertEqual(('chars', 5000), review_script.get_review_context_from_args(args_for(4, 5000)),
                         '--context-chars overrides --context-window')
        self.assertEqual(('entries', 4), review_script.get_review_context_from_args(
            SimpleNamespace(context_window=4)))


class ContextNeighborTests(unittest.TestCase):
    def test_entries_mode_reproduces_the_old_slices_exactly(self):
        entries = [entry(f'Synthetic line {i}.') for i in range(10)]
        for start, end in ((0, 2), (5, 7), (8, 10), (0, 10)):
            for window in (1, 4, 20):
                with self.subTest(start=start, end=end, window=window):
                    before, after = review_script.get_review_context_neighbors(
                        entries, start, end, 'entries', window)
                    self.assertEqual(entries[max(0, start - window):start], before)
                    self.assertEqual(entries[end:min(len(entries), end + window)], after)

    def test_characters_mode_packs_whole_entries_nearest_first_per_side(self):
        # "aaaa"/"bbbb" are 4 chars of text; the long instruct fields are NOT
        # counted, so adding either neighbour stays within a 10-character budget.
        entries = [
            entry('aaaa', instruct='x' * 100),
            entry('bbbb', instruct='y' * 100),
            entry('TARGET ONE.', instruct='z' * 100),
            entry('TARGET TWO.', instruct='z' * 100),
            entry('cccc', instruct='x' * 100),
            entry('dddd', instruct='y' * 100),
        ]
        before, after = review_script.get_review_context_neighbors(
            entries, 2, 4, 'chars', 10)
        # Selection is nearest-first, but context is shown in source order like
        # the old +/-N slice (entries[0], entries[1]), not reversed.
        self.assertEqual([entries[0], entries[1]], before)
        self.assertEqual([entries[4], entries[5]], after)
        before, after = review_script.get_review_context_neighbors(entries, 2, 4, 'chars', 7)
        self.assertEqual([entries[1]], before, 'the next whole entry would exceed the limit')
        self.assertEqual([entries[4]], after)

    def test_each_side_is_independent_and_book_edges_are_safe(self):
        entries = [entry('a'), entry('bb'), entry('TARGET'), entry('cccc'), entry('ddddd')]
        before, after = review_script.get_review_context_neighbors(entries, 2, 3, 'chars', 3)
        self.assertEqual([entries[0], entries[1]], before, 'a and bb exactly fill the budget')
        self.assertEqual([entries[3]], after, 'cccc alone fits, ddddd would overflow')
        self.assertEqual(([], [entries[1]]),
                         review_script.get_review_context_neighbors(entries, 0, 1, 'chars', 3),
                         'start of book has no previous side')
        self.assertEqual(([entries[3]], []),
                         review_script.get_review_context_neighbors(entries, 4, 5, 'chars', 3),
                         'end of book has no next side')

    def test_single_oversize_neighbour_is_included_then_accumulation_stops(self):
        # Matches Step 2 / review_target_chars: an entry larger than the target
        # goes alone rather than being dropped.
        entries = [entry('tiny'), entry('X' * 50), entry('TARGET'), entry('Y' * 50), entry('tiny')]
        before, after = review_script.get_review_context_neighbors(entries, 2, 3, 'chars', 10)
        self.assertEqual([entries[1]], before, 'the oversize neighbour is the only context on that side')
        self.assertEqual([entries[3]], after)

    def test_text_only_and_non_dict_entries_do_not_crash(self):
        # Long instruct fields are not counted, so both short-text neighbours
        # fit; a non-dict entry counts as zero text and is kept whole.
        entries = [entry('aa', instruct='x' * 100), entry('bb', instruct='y' * 100),
                   entry('TARGET'), 'not-a-dict']
        before, after = review_script.get_review_context_neighbors(entries, 2, 3, 'chars', 10)
        self.assertEqual([entries[0], entries[1]], before)
        self.assertEqual(['not-a-dict'], after)


class ContextIdentityTests(unittest.TestCase):
    def test_default_identity_and_fingerprint_are_unchanged(self):
        self.assertEqual(DEFAULT_FINGERPRINT, fingerprint_for(args_for()))
        self.assertEqual(DEFAULT_FINGERPRINT, fingerprint_for(
            SimpleNamespace(context_window=4, dedupe_speakers=True,
                            alias_registry=None, remap_voice_config=None)))

    def test_mode_and_value_change_the_fingerprint(self):
        baseline = fingerprint_for(args_for())
        self.assertNotEqual(baseline, fingerprint_for(args_for(7)), 'entry count is identity')
        # Same window value, only the mode differs: main ignored context_chars.
        self.assertNotEqual(baseline, fingerprint_for(args_for(4, 2000)), 'characters mode is identity')
        self.assertNotEqual(fingerprint_for(args_for(4, 2000)), fingerprint_for(args_for(4, 2001)))

    def test_resume_with_a_different_context_setting_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json')
            entries = [entry('Synthetic one.'), entry('Synthetic two.'), entry('Synthetic three.')]
            source_sha = review_script._entries_fingerprint(entries)
            baseline = fingerprint_for(args_for())
            review_script.save_checkpoint(output, 1, 2, 25, 4, entries[:2],
                {'text_changed': 0, 'batches_failed': 0}, None, [2], [],
                source_sha, source_offsets=[2], execution_sha256=baseline)
            self.assertIsNotNone(review_script.load_checkpoint(output, 2, 25, 4,
                entries, execution_sha256=baseline))
            resumed = review_script.load_checkpoint(output, 2, 25, 4, entries,
                execution_sha256=fingerprint_for(args_for(4, 2000)))
            self.assertIsNone(resumed, 'switching mode must refuse the old checkpoint')


class ContextRouteTests(unittest.TestCase):
    def _post(self, payload, batch=False):
        commands = []
        state = copy.deepcopy(core.process_state)
        for row in state.values():
            row['running'] = False
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'annotated_script.json'
            config = Path(tmp) / 'config.json'
            entries = [entry('Synthetic line one.'), entry('Synthetic line two.')]
            source.write_text(json.dumps(entries))
            config.write_text(json.dumps({'llm_mode': 'local', 'generation': {'review_batch_size': 25}}))
            scripts_dir = Path(tmp) / 'scripts'
            scripts_dir.mkdir()
            (scripts_dir / 'book.json').write_text(json.dumps(entries))
            api = FastAPI()
            api.include_router(script.router)
            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(io.StringIO()))
                for module in (core, script):
                    stack.enter_context(patch.object(module, 'process_state', state))
                stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
                stack.enter_context(patch.object(core, '_gpu_leases', {}))
                stack.enter_context(patch.object(core, '_task_claims', {}))
                stack.enter_context(patch.object(core, '_saved_book_publications', {}))
                stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=False))
                stack.enter_context(patch.object(script, 'SCRIPT_PATH', str(source)))
                stack.enter_context(patch.object(script, 'CONFIG_PATH', str(config)))
                stack.enter_context(patch.object(script, 'DATA_DIR', tmp))
                stack.enter_context(patch.object(script, 'SCRIPTS_DIR', str(scripts_dir)))
                stack.enter_context(patch.object(script, 'CHARACTER_ALIASES_PATH', str(Path(tmp) / 'aliases.json')))
                stack.enter_context(patch.object(script, '_init_task_log', return_value=None))
                stack.enter_context(patch.object(script, '_write_batch_review_report', return_value=None))
                stack.enter_context(patch.object(script, 'run_process',
                    side_effect=lambda cmd, name: (commands.append(cmd), (0, []))[1]))
                stack.enter_context(patch.object(script, '_stream_subprocess_to_logs',
                    side_effect=lambda cmd, cwd, state, **kw: (commands.append(cmd), (0, []))[1]))
                client = stack.enter_context(TestClient(api))
                response = client.post('/api/review_script/batch/start' if batch
                                       else '/api/review_script_contextual', json=payload)
        return response, commands

    def test_characters_mode_reaches_the_cli_with_context_chars(self):
        response, commands = self._post({'window_size': 5000, 'context_mode': 'chars',
                                         'dedupe_speakers': False})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('chars', response.json()['context_mode'])
        self.assertEqual(1, len(commands))
        self.assertIn('--context-chars', commands[0])
        self.assertEqual('5000', commands[0][commands[0].index('--context-chars') + 1])
        self.assertNotIn('--context-window', commands[0])

    def test_default_and_out_of_range_values_use_the_one_validator(self):
        response, commands = self._post({'window_size': 4, 'dedupe_speakers': False})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('--context-window', commands[0])
        self.assertEqual('4', commands[0][commands[0].index('--context-window') + 1])
        response, commands = self._post({'window_size': 500, 'dedupe_speakers': False})
        self.assertEqual('100', commands[0][commands[0].index('--context-window') + 1])
        response, _ = self._post({'window_size': 4, 'context_mode': 'nope'})
        self.assertEqual(422, response.status_code)
        response, _ = self._post({'window_size': 'not-a-number'})
        self.assertEqual(422, response.status_code)

    def test_batch_route_uses_the_same_entry_cap(self):
        response, commands = self._post({'script_names': ['book'], 'context_window': 500,
                                         'dedupe_speakers': False, 'find_nicknames': False}, batch=True)
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(any('--context-window' in cmd and
                            cmd[cmd.index('--context-window') + 1] == '100'
                            for cmd in commands), commands)


if __name__ == '__main__':
    unittest.main()
