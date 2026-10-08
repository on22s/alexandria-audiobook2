import asyncio
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import core
from fastapi import BackgroundTasks, HTTPException
from routers import scripts_library as sl, voices


class VoiceBookSnapshotTests(unittest.TestCase):
    @contextmanager
    def books(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); saved = root / 'scripts'; saved.mkdir()
            script = root / 'annotated_script.json'; config = root / 'voice_config.json'
            library = root / 'voice_library.json'; library.write_text('{}')
            state = copy.deepcopy(core.process_state)
            for value in state.values():
                value['running'] = False
            for owner, name, value in [
                (core, 'DATA_DIR', tmp), (core, 'SCRIPTS_DIR', str(saved)),
                (core, 'VOICE_LIBRARY_PATH', str(library)),
                (voices, 'SCRIPT_PATH', str(script)), (voices, 'VOICE_CONFIG_PATH', str(config)),
                (voices, 'VOICE_LIBRARY_PATH', str(library)),
                (voices, '_script_line_counts', lambda: core._script_line_counts(str(script))),
                (sl, 'SCRIPTS_DIR', str(saved)), (sl, 'DATA_DIR', tmp),
                (sl, 'SCRIPT_PATH', str(script)), (sl, 'VOICE_CONFIG_PATH', str(config)),
                (sl, 'CHUNKS_PATH', str(root / 'chunks.json')),
                (sl, 'AUDIOBOOK_PATH', str(root / 'audiobook.wav')),
                (sl, 'M4B_PATH', str(root / 'audiobook.m4b')), (sl, 'process_state', state)]:
                stack.enter_context(patch.object(owner, name, value))
            stack.enter_context(patch.object(voices, '_make_llm_client', side_effect=RuntimeError('offline fixture')))
            catalog = [{'adapter_id': 'fixture_voice', 'name': 'Fixture voice', 'type': 'lora',
                        'gender': 'unknown', 'age_group': 'unknown', 'description': ''}]
            def switch(book):
                (saved / f'{book}.json').write_text(json.dumps([
                    {'speaker': 'Alice', 'text': f'Synthetic sentence from {book}.', 'type': 'dialogue'}]))
                (saved / f'{book}.voice_config.json').write_text(json.dumps({
                    'Alice': {'type': 'custom', 'voice': 'Ryan', 'description': f'Persona of {book}'}}))
                sl._load_script_sync(sl.ScriptLoadRequest(name=book))
            switch('book_A')
            yield root, switch, catalog

    def test_catalog_switch_cannot_relabel_or_apply_previous_book_casting(self):
        for changed in (False, True):
            with self.subTest(changed=changed), self.books() as (root, switch, catalog):
                def get_catalog():
                    if changed:
                        switch('book_B')
                    return copy.deepcopy(catalog)
                with patch.object(voices, '_build_lora_candidates', side_effect=get_catalog):
                    result = voices._suggest_voices_impl(voices.SuggestVoicesRequest(characters=['Alice']))
                self.assertEqual('book_A', result['book_id'])
                self.assertIn('Persona of book_A', result['suggestions']['Alice']['character_style'])
                before = (root / 'voice_config.json').read_bytes()
                with patch.object(voices, '_build_lora_candidates', return_value=catalog):
                    if changed:
                        with self.assertRaises(HTTPException) as error:
                            voices._apply_voice_suggestions(result['suggestions'], None)
                        self.assertEqual(409, error.exception.status_code)
                        self.assertEqual(before, (root / 'voice_config.json').read_bytes())
                    else:
                        self.assertEqual(1, voices._apply_voice_suggestions(result['suggestions'], None)['count'])

    def test_reloading_same_named_book_rejects_old_suggestion_generation(self):
        with self.books() as (root, switch, catalog):
            with patch.object(voices, '_build_lora_candidates', return_value=catalog):
                result = voices._suggest_voices_impl(voices.SuggestVoicesRequest(characters=['Alice']))
                switch('book_A')
                before = (root / 'voice_config.json').read_bytes()
                with self.assertRaises(HTTPException) as error:
                    voices._apply_voice_suggestions(result['suggestions'], None)
            self.assertEqual(409, error.exception.status_code)
            self.assertEqual(before, (root / 'voice_config.json').read_bytes())

    def test_recovery_switch_between_validation_and_worker_save_refuses_publication(self):
        for changed in (False, 'other', 'same'):
            with self.subTest(changed=changed), self.books() as (root, switch, catalog):
                native_thread = asyncio.to_thread
                async def switch_before_worker(function, *args, **kwargs):
                    if changed:
                        switch('book_A' if changed == 'same' else 'book_B')
                    return await native_thread(function, *args, **kwargs)
                request = voices.PersonaRecoveryRequest(speaker='Alice', resume=False,
                    persona_json=json.dumps({'description': 'Recovered persona for book_A',
                                             'ref_text': 'Synthetic reference from book_A.'}))
                with patch.object(voices.asyncio, 'to_thread', side_effect=switch_before_worker):
                    if changed:
                        with self.assertRaises(HTTPException) as error:
                            asyncio.run(voices.recover_persona(BackgroundTasks(), request))
                        self.assertEqual(409, error.exception.status_code)
                        saved = json.loads((root / 'voice_config.json').read_text())
                        expected = 'Persona of book_A' if changed == 'same' else 'Persona of book_B'
                        self.assertEqual(expected, saved['Alice']['description'])
                        self.assertNotIn('ref_text', saved['Alice'])
                    else:
                        self.assertEqual('saved', asyncio.run(voices.recover_persona(BackgroundTasks(), request))['status'])
                        saved = json.loads((root / 'voice_config.json').read_text())
                        self.assertEqual('Recovered persona for book_A', saved['Alice']['description'])
