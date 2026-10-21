"""Exact scoped words must reach synthesis and matching durable publication."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import pronunciation
import tts
from project import ProjectManager, get_chunk_generation_inputs
from speech_text import get_prelexicon_speech_normalization
from chunk_status_journal import get_chunk_status_journal_path
from tests.test_batch_style_timeline import get_engine, get_torch


def rule(text='Ann Ann', uid='a', replacement='Ah-n', restrictions=None):
    prepared = get_prelexicon_speech_normalization(text)['text']
    query = text if len(text) <= 128 else 'Ann'
    anchor = pronunciation.get_occurrence_pronunciation_candidates(prepared, uid, text, query)['candidates'][0]
    # Most fixtures select the first name, not both repeated names.
    if text == 'Ann Ann':
        anchor = pronunciation.get_occurrence_pronunciation_candidates(prepared, uid, text, 'Ann')['candidates'][0]
    return {**{key: value for key, value in anchor.items() if key not in ('before', 'after')},
            'id': 'rule-' + uid, 'state': 'approved', 'replacement': replacement,
            'restrictions': restrictions or {}}


def engine_fixture():
    engine = get_engine()
    engine._device = 'cpu'
    return engine


class OccurrenceGenerationTests(unittest.TestCase):
    def test_scoped_native_public_direct_and_batch_paths_prepare_once_for_all_voice_kinds(self):
        for kind in ('custom', 'clone', 'lora', 'design', 'ensemble'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                root = Path(tmp); engine = engine_fixture(); seen = []
                model = SimpleNamespace(generate_custom_voice=self.provider(seen),
                                        generate_voice_clone=self.provider(seen),
                                        generate_voice_design=self.provider(seen), _tokenize_texts=lambda texts: texts)
                prompt = [SimpleNamespace(ref_code=None, ref_text='Reference.')]
                voices = {'A': {'type': kind, 'voice': 'Ryan', 'description': 'Calm',
                                'adapter_path': str(root/'adapter'), 'seed': 0},
                          'B': {'type': 'custom', 'voice': 'Ryan'}}
                if kind == 'ensemble': voices['A']['members'] = ['B']
                row = dict(uid='a', speaker='A', index=0, text='Ann Ann',
                           pronunciation_rules=[rule(replacement='Ann Ann')])
                context = tts.get_pronunciation_render_context(voices, row, 0, engine.get_model_settings())
                before = copy.deepcopy((voices, row))
                stack.enter_context(pronunciation.ensure_pronunciation_audition({'Ann': 'Shared'}))
                stack.enter_context(pronunciation.ensure_occurrence_pronunciation_context(row, context))
                stack.enter_context(patch.dict('sys.modules', {'torch': get_torch()}))
                for method in ('_init_local_custom', '_init_local_clone', '_init_local_design'):
                    stack.enter_context(patch.object(engine, method, return_value=model))
                stack.enter_context(patch.object(engine, '_get_clone_prompt', return_value=prompt))
                stack.enter_context(patch.object(engine, '_ensure_local_lora_generation', return_value=(model, prompt)))
                stack.enter_context(patch.object(tts, '_resolve_asset_path', side_effect=lambda p: str(root/p)))
                self.assertTrue(engine.generate_voice(row['text'], '', 'A', voices, str(root/'single.wav')))
                if kind == 'custom':
                    direct = engine.generate_custom_voice(row['text'], '', 'A', voices, str(root/'direct.wav'))
                elif kind == 'clone':
                    direct = engine.generate_clone_voice(row['text'], 'A', voices, str(root/'direct.wav'))
                elif kind == 'ensemble':
                    direct = engine.generate_ensemble_voice(row['text'], '', voices['A'], voices, str(root/'direct.wav'))
                else:
                    direct = getattr(engine, f'generate_{kind}_voice')(row['text'], '', voices['A'], str(root/'direct.wav'))
                self.assertTrue(direct)
                self.assertEqual({'completed': [0], 'failed': []}, engine.generate_batch([row], voices, tmp))
                self.assertEqual(['Ann Ann Shared.'] * 3, seen)
                self.assertEqual(before, (voices, row))

    def test_scoped_external_single_batch_and_ensemble_use_same_root_occurrence(self):
        for kind in ('custom', 'clone'):
            for ensemble in (False, True):
                with self.subTest(kind=kind, ensemble=ensemble), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); engine = engine_fixture(); seen = []
                    for name in ('reference.wav', 'provider.wav'): sf.write(root/name, np.full(2400, .2), 24000)
                    engine._mode = 'external'; engine._external_parallel_workers = 1
                    engine._external_urls = ['fixture']; engine._external_timeout = 5
                    engine._next_external_url = lambda: 'fixture'
                    def predict(*args, **kwargs):
                        seen.append(kwargs['text'] if kind == 'custom' else args[2]); return (str(root/'provider.wav'),)
                    engine._external_endpoint = lambda ep: (SimpleNamespace(predict=predict), threading.Lock())
                    voices = {'A': {'type': kind, 'voice': 'Ryan', 'ref_audio': str(root/'reference.wav'),
                                    'ref_text': 'Reference.', 'seed': 0},
                              'GROUP': {'type': 'ensemble', 'members': ['A']}}
                    speaker = 'GROUP' if ensemble else 'A'
                    row = dict(uid='a', speaker=speaker, index=0, text='Ann Ann', pronunciation_rules=[rule(replacement='Ann Ann')])
                    context = tts.get_pronunciation_render_context(voices, row, 0, engine.get_model_settings())
                    row['pronunciation_rules'][0]['restrictions'] = {key: context[key] for key in ('speaker', 'voice_sha256', 'engine_mode')}
                    with pronunciation.ensure_pronunciation_audition({'Ann': 'Shared'}), \
                            pronunciation.ensure_occurrence_pronunciation_context(row, context), \
                            patch('gradio_client.handle_file', side_effect=lambda path: path):
                        self.assertTrue(engine.generate_voice(row['text'], '', speaker, voices, str(root/'single.wav')))
                        self.assertEqual({'completed': [0], 'failed': []}, engine.generate_batch([row], voices, tmp))
                    self.assertEqual(['Ann Ann Shared.'] * 2, seen)

    def manager(self, root, text='Ann Ann', replacement='Ah-n'):
        manager = ProjectManager(str(root))
        rows = [dict(id=i, uid=uid, speaker='A', text=text, instruct='Calm.',
                     status='done', audio_path=f'voicelines/prior-{uid}.wav', audio_revision='old-' + uid)
                for i, uid in enumerate(('a', 'b'))]
        rows[0]['pronunciation_rules'] = [rule(text, replacement=replacement)]
        for row in rows:
            sf.write(root / row['audio_path'], np.full(2400, .1), 24000)
        manager.save_chunks(rows)
        Path(manager.voice_config_path).write_text(json.dumps({'A': {'type': 'custom', 'voice': 'Ryan'}}))
        return manager

    def provider(self, seen, mutate=None):
        def synth(**kwargs):
            texts = kwargs['text'] if isinstance(kwargs['text'], list) else [kwargs['text']]
            seen.extend(texts)
            if mutate:
                mutate()
            return [np.full(2400, .2) for _ in texts], 24000
        return synth

    def test_single_parallel_native_keep_uid_occurrence_and_revision_receipt(self):
        for mode in ('single', 'parallel', 'native'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); manager = self.manager(root); engine = engine_fixture(); seen = []
                lexicon = root / 'lexicon.json'; lexicon.write_text(json.dumps({'Ann': 'Shared'}))
                model = SimpleNamespace(generate_custom_voice=self.provider(seen))
                manager.engine = engine
                def prepared(text, instruct, speaker, voices, output):
                    seen.append(text); sf.write(output, np.full(2400, .2), 24000); return True
                with patch.object(pronunciation, 'DEFAULT_PATH', str(lexicon)), \
                        patch.object(engine, '_generate_voice_prepared', side_effect=prepared), \
                        patch.object(engine, '_init_local_custom', return_value=model), \
                        patch.dict('sys.modules', {'torch': get_torch()}), contextlib.redirect_stdout(io.StringIO()):
                    if mode == 'single':
                        manager._active_journal_identity = {'active_book_id': None, 'book_generation': None}
                        self.assertTrue(manager.generate_chunk_audio(0)[0])
                        self.assertTrue(manager.generate_chunk_audio(1)[0])
                    else:
                        result = (manager.generate_chunks_parallel([0, 1], max_workers=2) if mode == 'parallel'
                                  else manager.generate_chunks_batch([0, 1], batch_size=2))
                        self.assertEqual({0, 1}, set(result['completed']), result)
                        self.assertEqual([], result['failed'])
                self.assertEqual(['Ah-n Shared.', 'Shared Shared.'], sorted(seen))
                saved = ProjectManager(tmp).get_saved_chunks()
                self.assertEqual(['Ann Ann', 'Ann Ann'], [row['text'] for row in saved])
                receipt = saved[0]['pronunciation_receipt']
                self.assertEqual(saved[0]['audio_revision'], receipt['audio_revision'])
                self.assertEqual(hashlib.sha256(b'Ah-n Shared.').hexdigest(), receipt['speech_sha256'])
                self.assertEqual('applied_respelling', receipt['decisions'][0]['reason'])
                self.assertEqual('A', receipt['context']['speaker'])
                self.assertNotIn('pronunciation_receipt', saved[1])
                for row in saved:
                    audio, rate = sf.read(root / row['audio_path'])
                    self.assertEqual(24000, rate); self.assertGreater(len(audio), 0)
                    self.assertGreater(float(np.max(np.abs(audio))), .1)
                manager.save_chunks(saved)
                self.assertFalse(Path(get_chunk_status_journal_path(manager.chunks_path)).exists())
                self.assertEqual(receipt, ProjectManager(tmp).get_saved_chunks()[0]['pronunciation_receipt'])

    def test_scoped_publication_guard_uses_actual_replacement_length_and_prior_bytes(self):
        for text, replacement, expected in (('Longname' * 15, 'Ah', True), ('Ann Ann', 'Longname' * 40, False)):
            for mode in ('single', 'native'):
                with self.subTest(mode=mode, expected=expected), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); manager = self.manager(root, text, replacement); engine = engine_fixture(); seen = []
                    manager.engine = engine; prior = (root / 'voicelines/prior-a.wav').read_bytes()
                    def single(text, instruct, speaker, voices, output):
                        seen.append(text); sf.write(output, np.full(1200, .2), 24000); return True
                    def native(**kwargs):
                        seen.extend(kwargs['text']); return [np.full(1200, .2)], 24000
                    with pronunciation.ensure_literal_pronunciation_audition(), \
                            patch.object(engine, '_generate_voice_prepared', side_effect=single), \
                            patch.object(engine, '_init_local_custom', return_value=SimpleNamespace(generate_custom_voice=native)), \
                            patch.dict('sys.modules', {'torch': get_torch()}), contextlib.redirect_stdout(io.StringIO()):
                        result = manager.generate_chunk_audio(0) if mode == 'single' else manager.generate_chunks_batch([0])
                    self.assertEqual(expected, bool(result[0] if mode == 'single' else result['completed']), result)
                    self.assertTrue(seen[0].startswith(replacement))
                    if not expected:
                        self.assertEqual(prior, (root / 'voicelines/prior-a.wav').read_bytes())
                        self.assertEqual('voicelines/prior-a.wav', manager.get_saved_chunks()[0]['audio_path'])
                    self.assertFalse(list(root.glob('temp_batch_*.wav')))
                    self.assertFalse(list(root.glob('chunk_*.wav')))
                    self.assertFalse(list((root / 'voicelines').glob('.render-*')))

    def test_actual_engine_language_is_captured_through_admission_even_if_it_changes_later(self):
        for initial, later, expected in (('English', 'Japanese', False), ('Japanese', 'English', True)):
            for mode in ('single', 'native'):
                with self.subTest(initial=initial, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); manager = self.manager(root, replacement='Longname' * 40)
                    rows = manager.load_chunks(); rows[0]['pronunciation_rules'][0]['restrictions'] = {'language': 'English'}
                    manager.save_chunks(rows); engine = engine_fixture(); engine._language = initial; manager.engine = engine
                    prior = (root / 'voicelines/prior-a.wav').read_bytes(); seen = []
                    def single(text, instruct, speaker, voices, output):
                        seen.append(text); engine._language = later
                        sf.write(output, np.full(1200, .2), 24000); return True
                    def native(**kwargs):
                        seen.extend(kwargs['text']); engine._language = later
                        return [np.full(1200, .2)], 24000
                    with pronunciation.ensure_literal_pronunciation_audition(), \
                            patch.object(engine, '_generate_voice_prepared', side_effect=single), \
                            patch.object(engine, '_init_local_custom', return_value=SimpleNamespace(generate_custom_voice=native)), \
                            patch.dict('sys.modules', {'torch': get_torch()}), contextlib.redirect_stdout(io.StringIO()):
                        result = manager.generate_chunk_audio(0) if mode == 'single' else manager.generate_chunks_batch([0])
                    self.assertEqual(expected, bool(result[0] if mode == 'single' else result['completed']), result)
                    self.assertEqual(initial == 'English', seen[0].startswith('Longname'))
                    if expected:
                        receipt = manager.get_saved_chunks()[0]['pronunciation_receipt']
                        self.assertEqual(initial, receipt['context']['language'])
                        self.assertEqual('scope_mismatch:language', receipt['decisions'][0]['reason'])
                    else:
                        self.assertEqual(prior, (root / 'voicelines/prior-a.wav').read_bytes())

    def test_captured_admission_ignores_notes_and_ids_but_refuses_changed_approved_words(self):
        for change, expected in (('note', True), ('id', True), ('draft', True), ('replacement', False)):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); manager = self.manager(root); engine = engine_fixture(); manager.engine = engine
                original = copy.deepcopy(manager.get_saved_chunks()[0]); prior = (root / original['audio_path']).read_bytes()
                def synth(text, instruct, speaker, voices, output):
                    rows = manager.load_chunks()
                    if change == 'draft':
                        rows[0]['pronunciation_rules'].append({**rule(), 'id': 'new-draft', 'state': 'draft'})
                    else:
                        rows[0]['pronunciation_rules'][0][change] = 'Changed'
                    if change == 'replacement': rows[0]['status'] = 'pending'
                    manager.save_chunks(rows)
                    sf.write(output, np.full(2400, .2), 24000); return True
                with pronunciation.ensure_literal_pronunciation_audition(), \
                        patch.object(engine, '_generate_voice_prepared', side_effect=synth), contextlib.redirect_stdout(io.StringIO()):
                    result = manager.generate_chunk_audio(0)
                self.assertEqual(expected, result[0], result)
                saved = manager.get_saved_chunks()[0]
                self.assertEqual(prior, (root / original['audio_path']).read_bytes())
                if expected:
                    self.assertEqual('rule-a', saved['pronunciation_receipt']['decisions'][0]['id'])
                else:
                    self.assertIn('generation inputs changed', result[1])
                    self.assertEqual(original['audio_path'], saved['audio_path'])
                    self.assertEqual('pending', saved['status'])
                self.assertEqual(expected, get_chunk_generation_inputs(original) == get_chunk_generation_inputs(saved))

    def test_dynamic_narrator_voice_and_authored_alias_restrictions_resolve_before_native_preparation(self):
        engine = engine_fixture(); seen = []
        config = {'NARRATOR': {'type': 'custom', 'voice': 'Ryan', 'narrator_strategy': 'focus'},
                  'ALICE': {'type': 'custom', 'voice': 'Aiden'}, 'BOB': {'type': 'custom', 'voice': 'Ryan'},
                  'Alias': {'alias_of': 'ALICE'}}
        rows = [dict(uid=uid, speaker='NARRATOR', focus_speaker=focus, index=i, text='Ann Ann')
                for i, (uid, focus) in enumerate((('a', 'ALICE'), ('b', 'BOB')))]
        wanted = tts.get_pronunciation_render_context(config, rows[0], 0, {'mode': 'local', 'language': 'English'})
        for row in rows:
            row['pronunciation_rules'] = [rule(uid=row['uid'], restrictions={key: wanted[key] for key in ('speaker', 'voice_sha256')})]
        before = copy.deepcopy((rows, config))
        model = SimpleNamespace(generate_custom_voice=self.provider(seen))
        with tempfile.TemporaryDirectory() as tmp, pronunciation.ensure_literal_pronunciation_audition(), \
                patch.object(engine, '_init_local_custom', return_value=model), \
                patch.dict('sys.modules', {'torch': get_torch()}), contextlib.redirect_stdout(io.StringIO()):
            result = engine.generate_batch(rows, config, tmp)
        self.assertEqual({0, 1}, set(result['completed']), result)
        self.assertEqual(['Ah-n Ann.', 'Ann Ann.'], seen)
        self.assertEqual(before, (rows, config))
        alias = {'uid': 'alias', 'speaker': 'Alias', 'text': 'Ann Ann'}
        resolved = tts.get_pronunciation_render_context(config, alias, 0, {'mode': 'local', 'language': 'English'})
        self.assertEqual('Alias', resolved['speaker'])
        self.assertEqual(tts.get_pronunciation_render_context(config, {**alias, 'speaker': 'ALICE'}, 0,
                                                            {'mode': 'local', 'language': 'English'})['voice_sha256'],
                         resolved['voice_sha256'])

        # The authored label and resolved voice must agree at actual synthesis,
        # rather than only in the context helper's digest.
        alias.update(index=7, pronunciation_rules=[rule(uid='alias', replacement='Ann Ann',
            restrictions={key: resolved[key] for key in ('speaker', 'voice_sha256')})])
        captured = []; original = copy.deepcopy((alias, config))
        def synth(**kwargs):
            captured.append(kwargs)
            return [np.full(2400, .2)], 24000
        with tempfile.TemporaryDirectory() as tmp, pronunciation.ensure_pronunciation_audition({'Ann': 'Shared'}), \
                patch.object(engine, '_init_local_custom', return_value=SimpleNamespace(generate_custom_voice=synth)), \
                patch.dict('sys.modules', {'torch': get_torch()}):
            self.assertEqual({'completed': [7], 'failed': []}, engine.generate_batch([alias], config, tmp))
            self.assertGreater(sf.info(Path(tmp)/'temp_batch_7.wav').frames, 0)
        self.assertEqual(1, len(captured))
        self.assertEqual(['Ann Ann Shared.'], captured[0]['text'])
        self.assertEqual(['Aiden'], captured[0]['speaker'])
        self.assertEqual(original, (alias, config))
