"""A voice suggestion must respect and clear an active narrator_strategy (#2015).

Applying a suggested narrator voice used to ignore a non-global
``narrator_strategy`` on NARRATOR: no 409, and ``clear_overrides`` did not
reset it. At render time ``tts.resolve_narrator_voice_config`` routes every
narration chunk carrying ``focus_speaker`` to the focus character's voice, so
the voice the user had just applied was never heard on those lines. The
single-voice apply already treated the strategy as masking; both apply paths
now share one verdict (``voices.get_masking_voice_overrides``).
"""
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
import tts
from project import ProjectManager
from routers import voices
from utils import atomic_json_write

class SuggestionNarratorStrategyTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, narrator=None, hero=None):
        """One synthetic book: NARRATOR (1 line) and Hero (2 lines).

        `narrator` / `hero` are extra voice-entry fields for the stored config.
        """
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as patches:
            root = Path(tmp)
            script, config = root / 'annotated_script.json', root / 'voices.json'
            library, chunks = root / 'library.json', root / 'chunks.json'
            atomic_json_write([{'speaker': 'NARRATOR', 'text': 'Synthetic narration line.'},
                               {'speaker': 'Hero', 'text': 'Synthetic first dialogue line.'},
                               {'speaker': 'Hero', 'text': 'Synthetic second dialogue line.'}], str(script))
            atomic_json_write({'NARRATOR': {'type': 'custom', 'voice': 'old-narrator', **(narrator or {})},
                               'Hero': {'type': 'custom', 'voice': 'old-hero', **(hero or {})}}, str(config))
            atomic_json_write({'shared': {}, 'favorites': [], 'casts': {'series': {'members': {}}}},
                              str(library))
            atomic_json_write([], str(chunks))
            atomic_json_write({'active_book_id': 'active-book'}, str(root / 'state.json'))
            for key, value in (('SCRIPT_PATH', str(script)), ('VOICE_CONFIG_PATH', str(config)),
                               ('VOICE_LIBRARY_PATH', str(library))):
                # core owns the library/script readers voices imports, so both
                # modules must see this fixture's files.
                patches.enter_context(patch.object(core, key, value))
                patches.enter_context(patch.object(voices, key, value))
            patches.enter_context(patch.object(voices, 'CHUNKS_PATH', str(chunks)))
            patches.enter_context(patch.object(core, 'DATA_DIR', str(root)))
            manager = ProjectManager(str(root))
            manager.voice_config_path = str(config)
            manager.chunks_path = str(chunks)
            patches.enter_context(patch.object(voices, 'project_manager', manager))
            patches.enter_context(patch.object(voices, '_script_line_counts',
                                               side_effect=lambda path=None: {'NARRATOR': 1, 'Hero': 2}))
            patches.enter_context(patch.object(voices, '_build_lora_candidates', return_value=[
                {'adapter_id': 'narrator-new', 'type': 'lora'},
                {'adapter_id': 'hero-new', 'type': 'lora'}]))
            app = FastAPI()
            app.include_router(voices.router)
            with TestClient(app) as client:
                yield root, client

    def apply(self, client, characters, *, bulk=False, cast=None, clear=None):
        suggestions = {name: {'adapter_id': 'narrator-new' if name == 'NARRATOR' else 'hero-new',
                              'book_id': 'active-book',
                              'character_style': 'calm'} for name in characters}
        body = dict(suggestions=suggestions) if bulk else dict(
            character=characters[0], suggestion=suggestions[characters[0]])
        if cast:
            body['cast'] = cast
        if clear is not None:
            body['clear_overrides'] = clear
        return client.post('/api/suggest_voices/apply_bulk' if bulk else '/api/suggest_voices/apply',
                           json=body)

    def stored(self, root):
        return json.loads((root / 'voices.json').read_text(encoding='utf-8'))

    def test_a_dynamic_strategy_refuses_naming_it_then_resets_on_confirmation(self):
        for strategy in ('character', 'focus'):
            for bulk in (False, True):
                with self.subTest(strategy=strategy, bulk=bulk), \
                        self.fixture({'narrator_strategy': strategy}) as (root, client):
                    paths = [root / 'voices.json', root / 'library.json', root / 'chunks.json']
                    before = {path: path.read_bytes() for path in paths}
                    refused = self.apply(client, ['NARRATOR'], bulk=bulk, cast='series')
                    self.assertEqual(409, refused.status_code, refused.text)
                    detail = refused.json()['detail']
                    self.assertEqual('voice_overrides', detail['code'])
                    self.assertEqual([{'character': 'NARRATOR', 'alias_of': None,
                                       'version_timeline': False,
                                       'narrator_strategy': strategy}], detail['overrides'])
                    # The refusal names what is in the way, not just aliases.
                    self.assertIn('narrator strategy', detail['message'])
                    self.assertIn('NARRATOR', detail['message'])
                    self.assertEqual(before, {path: path.read_bytes() for path in paths},
                                     'a refusal must not write')
                    applied = self.apply(client, ['NARRATOR'], bulk=bulk, cast='series', clear=True)
                    self.assertEqual(200, applied.status_code, applied.text)
                    self.assertEqual(['NARRATOR'], applied.json()['applied'])
                    self.assertEqual([{'character': 'NARRATOR',
                                       'changes': ['narrator strategy reset to global']}],
                                     applied.json()['cleared_overrides'])
                    narrator = self.stored(root)['NARRATOR']
                    self.assertEqual('global', narrator['narrator_strategy'])
                    self.assertEqual('narrator-new', narrator['adapter_id'])
                    # The narrator is one of the shared default names, so its
                    # cast entry lives in the library's shared pool.
                    library = json.loads((root / 'library.json').read_text(encoding='utf-8'))
                    member = library['shared']['narrator']
                    self.assertEqual('global', member['config']['narrator_strategy'])
                    self.assertEqual('narrator-new', member['config']['adapter_id'])

    def test_global_or_absent_strategy_is_not_an_override(self):
        for narrator in (None, {'narrator_strategy': 'global'}, {'narrator_strategy': ''}):
            with self.subTest(narrator=narrator), self.fixture(narrator) as (root, client):
                for bulk in (False, True):
                    with self.subTest(bulk=bulk):
                        response = self.apply(client, ['NARRATOR'], bulk=bulk)
                        self.assertEqual(200, response.status_code, response.text)
                        self.assertEqual([], response.json().get('cleared_overrides') or [],
                                         'nothing is cleared when no override masks the voice')
                        self.assertEqual('narrator-new', self.stored(root)['NARRATOR']['adapter_id'])

    def test_bulk_resets_a_strategy_per_book_and_leaves_clear_characters_alone(self):
        with self.fixture({'narrator_strategy': 'focus'}) as (root, client):
            refused = self.apply(client, ['NARRATOR', 'Hero'], bulk=True, cast='series')
            self.assertEqual(409, refused.status_code, refused.text)
            self.assertEqual(['NARRATOR'], [item['character'] for item
                                            in refused.json()['detail']['overrides']],
                             'only the masked character is reported')
            applied = self.apply(client, ['NARRATOR', 'Hero'], bulk=True, cast='series', clear=True)
            self.assertEqual(200, applied.status_code, applied.text)
            self.assertEqual(['NARRATOR', 'Hero'], applied.json()['applied'])
            self.assertEqual([{'character': 'NARRATOR',
                               'changes': ['narrator strategy reset to global']}],
                             applied.json()['cleared_overrides'])
            saved = self.stored(root)
            self.assertEqual('global', saved['NARRATOR']['narrator_strategy'])
            self.assertEqual('narrator-new', saved['NARRATOR']['adapter_id'])
            self.assertEqual('hero-new', saved['Hero']['adapter_id'])
            self.assertNotIn('narrator_strategy', saved['Hero'])

    def test_the_resolver_hears_the_newly_applied_narrator_voice(self):
        """The reported symptom, through the real render-time resolver."""
        narration = {'speaker': 'NARRATOR', 'focus_speaker': 'Hero'}
        with self.fixture({'narrator_strategy': 'character'}) as (root, client):
            # Rule 21: the fixture must be able to show the failure it exists to
            # catch — the uncleared config really does route narration to Hero.
            before = tts.resolve_narrator_voice_config('NARRATOR', self.stored(root), narration)
            self.assertEqual('old-hero', before['NARRATOR']['voice'])
            applied = self.apply(client, ['NARRATOR'], clear=True)
            self.assertEqual(200, applied.status_code, applied.text)
            resolved = tts.resolve_narrator_voice_config('NARRATOR', self.stored(root), narration)
            self.assertEqual('narrator-new', resolved['NARRATOR'].get('adapter_id'))
            self.assertNotEqual(resolved['NARRATOR'], resolved['Hero'])

    def test_the_shared_helper_reports_the_verdict_the_single_voice_path_used(self):
        helper = voices.get_masking_voice_overrides
        cases = [
            (None, None),
            ({}, None),
            ('not-an-entry', None),
            ({'type': 'lora', 'adapter_id': 'a'}, None),
            ({'narrator_strategy': 'global'}, None),
            ({'narrator_strategy': ''}, None),
            ({'alias_of': '   '}, None),
            ({'alias_of': 'Other'}, {'character': 'X', 'alias_of': 'Other',
                                     'version_timeline': False}),
            ({'alias': 'Other'}, {'character': 'X', 'alias_of': 'Other',
                                  'version_timeline': False}),
            ({'version_timeline': [{'from_index': 0, 'version_id': 'v1'}]},
             {'character': 'X', 'alias_of': None, 'version_timeline': True}),
            ({'narrator_strategy': 'focus'}, {'character': 'X', 'alias_of': None,
                                              'version_timeline': False,
                                              'narrator_strategy': 'focus'}),
            # Markers and history are not masks: only version_timeline is read
            # at render time (tts.active_version_id), and selecting a version
            # bakes its fields into the entry itself. The suggestion apply
            # clears these keys rather than refusing them (already pinned by
            # test_voices_regressions.SuggestionSelectionMetadataTests).
            ({'active_version': 'v1'}, None),
            ({'active_candidate': 'cand', 'ready': True,
              'versions': {'v1': {'type': 'custom', 'voice': 'Jenny'}}}, None),
        ]
        for entry, expected in cases:
            with self.subTest(entry=entry):
                self.assertEqual(expected, helper('X', entry))

    def test_the_helper_and_its_disclosures_are_pure(self):
        entry = {'alias_of': 'Other', 'version_timeline': [{'from_index': 0, 'version_id': 'v1'}],
                 'active_version': 'v1', 'narrator_strategy': 'focus'}
        snapshot = copy.deepcopy(entry)
        verdict = voices.get_masking_voice_overrides('BOB', entry)
        self.assertEqual(snapshot, entry, 'the caller entry is never mutated (Rule 17)')
        self.assertEqual(['alias cleared', 'version timeline/active version cleared',
                          'narrator strategy reset to global'],
                         voices.get_masking_override_changes(verdict))
        self.assertEqual([], voices.get_masking_override_changes(None))


if __name__ == '__main__':
    unittest.main()
