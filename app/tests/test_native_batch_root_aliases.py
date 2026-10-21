"""Direct batches resolve authored aliases before selecting a synthesis route."""
import contextlib
import copy
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
from tests.test_batch_style_timeline import get_engine, get_torch


class NativeBatchRootAliasTests(unittest.TestCase):
    def test_actual_native_provider_receives_canonical_custom_clone_design_and_ensemble(self):
        for kind in ('custom', 'clone', 'design', 'ensemble'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                root = Path(tmp); engine = get_engine(); seen = []
                def provider(category):
                    def synth(**kwargs):
                        seen.append((category, kwargs))
                        return [np.full(4000, .1)], 16000
                    return synth
                model = SimpleNamespace(generate_custom_voice=provider('custom'),
                                        generate_voice_clone=provider('clone'),
                                        generate_voice_design=provider('design'), _tokenize_texts=lambda texts: texts)
                prompt = [SimpleNamespace(ref_code=None, ref_text='Reference.')]
                voices = {'Alias': {'alias_of': 'Middle'}, 'Middle': {'alias': 'Canonical'},
                          'Canonical': {'type': kind, 'voice': 'Aiden', 'description': 'Canonical design', 'seed': 0},
                          'Member': {'type': 'custom', 'voice': 'Aiden'}}
                if kind == 'ensemble': voices['Canonical']['members'] = ['Member']
                rows = [dict(speaker='Alias', text='Ann arrives.', index=0, uid='same-line', instruct='Calm.')]
                before = copy.deepcopy((voices, rows))
                stack.enter_context(pronunciation.ensure_pronunciation_audition({'Ann': 'Ann Ann'}))
                stack.enter_context(patch.dict('sys.modules', {'torch': get_torch()}))
                for method in ('_init_local_custom', '_init_local_clone', '_init_local_design'):
                    stack.enter_context(patch.object(engine, method, return_value=model))
                stack.enter_context(patch.object(engine, '_get_clone_prompt', return_value=prompt))
                result = engine.generate_batch(rows, voices, tmp)
                self.assertEqual({'completed': [0], 'failed': []}, result)
                category, kwargs = seen[0]
                self.assertEqual('custom' if kind == 'ensemble' else kind, category)
                text = kwargs['text']; self.assertEqual(['Ann Ann arrives.'], text if isinstance(text, list) else [text])
                if category == 'custom': self.assertIn(kwargs['speaker'], ('Aiden', ['Aiden']))
                if category == 'clone': self.assertEqual(prompt, kwargs['voice_clone_prompt'])
                if category == 'design': self.assertIn('Canonical design', str(kwargs['instruct']))
                self.assertEqual(1, len(seen)); self.assertGreater(sf.info(root/'temp_batch_0.wav').frames, 0)
                self.assertEqual(before, (voices, rows))

    def test_copied_requests_keep_authored_speaker_uid_index_and_existing_source_identity(self):
        engine = get_engine(); seen = []
        voices = {'Alias': {'alias_of': 'Canonical'}, 'Canonical': {'type': 'custom', 'voice': 'Aiden'}}
        rows = [dict(speaker='Alias', uid='a', index=7, text='First.'),
                dict(speaker='Canonical', _source_speaker='Original', uid='b', index=2, text='Second.')]
        before = copy.deepcopy((rows, voices))
        def provider(chunks, *args):
            seen.extend(copy.deepcopy(chunks)); return {'completed': [row['index'] for row in chunks], 'failed': []}
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, '_local_batch_custom', side_effect=provider):
            self.assertEqual({'completed': [7, 2], 'failed': []}, engine.generate_batch(rows, voices, tmp))
        self.assertEqual(['Canonical', 'Canonical'], [row['speaker'] for row in seen])
        self.assertEqual(['Alias', 'Original'], [row['_source_speaker'] for row in seen])
        self.assertEqual(['a', 'b'], [row['uid'] for row in seen]); self.assertEqual([7, 2], [row['index'] for row in seen])
        self.assertEqual(before, (rows, voices))

    def test_bad_alias_roots_fail_before_native_provider_or_output(self):
        voices = {'Cycle': {'alias_of': 'Back'}, 'Back': {'alias_of': 'Cycle'}, 'Missing': {'alias_of': 'Absent'}}
        voices.update({f'Long{i}': {'alias_of': f'Long{i+1}'} for i in range(17)})
        rows = [dict(speaker=name, index=i, text='Read this.') for i, name in enumerate(('Cycle', 'Missing', 'Long0'))]
        before = copy.deepcopy((rows, voices)); engine = get_engine()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, '_local_batch_custom') as provider:
            result = engine.generate_batch(rows, voices, tmp)
            self.assertEqual([], result['completed']); self.assertEqual([0, 1, 2], [row[0] for row in result['failed']])
            provider.assert_not_called(); self.assertFalse(list(Path(tmp).iterdir()))
        self.assertEqual(before, (rows, voices))

    def test_alias_to_dynamic_narrator_resolves_focus_before_public_override(self):
        voices = {'Alias': {'alias_of': 'NARRATOR'},
                  'NARRATOR': {'type': 'custom', 'voice': 'Ryan', 'narrator_strategy': 'focus'},
                  'ALICE': {'type': 'custom', 'voice': 'Aiden'}}
        rows = [dict(speaker='Alias', focus_speaker='ALICE', text='Ann arrives.', index=0)]
        before = copy.deepcopy((rows, voices)); engine = get_engine()
        with tempfile.TemporaryDirectory() as tmp, pronunciation.ensure_pronunciation_audition({'Ann': 'Ann Ann'}), \
                patch.object(engine, 'generate_voice', return_value=True) as override:
            self.assertEqual({'completed': [0], 'failed': []}, engine.generate_batch(rows, voices, tmp))
        self.assertEqual('Ann Ann arrives.', override.call_args.args[0])
        self.assertEqual('NARRATOR', override.call_args.args[2])
        self.assertEqual('Aiden', override.call_args.args[3]['NARRATOR']['voice'])
        self.assertEqual(before, (rows, voices))

    def test_external_alias_dispatch_keeps_once_prepared_custom_and_clone_requests(self):
        for kind in ('custom', 'clone'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); engine = get_engine(); seen = []
                for name in ('ref.wav', 'provider.wav'): sf.write(root/name, np.full(4000, .1), 16000)
                engine._mode = 'external'; engine._external_parallel_workers = 1
                engine._external_urls = ['fixture']; engine._external_timeout = 5
                engine._next_external_url = lambda: 'fixture'
                def predict(*args, **kwargs):
                    seen.append(kwargs['text'] if kind == 'custom' else args[2]); return (str(root/'provider.wav'),)
                engine._external_endpoint = lambda ep: (SimpleNamespace(predict=predict), threading.Lock())
                voices = {'Alias': {'alias_of': 'Canonical'}, 'Canonical': {'type': kind, 'voice': 'Aiden',
                          'ref_audio': str(root/'ref.wav'), 'ref_text': 'Reference.', 'seed': 0}}
                rows = [dict(speaker='Alias', text='Ann arrives.', index=0)]
                before = copy.deepcopy((rows, voices))
                with pronunciation.ensure_pronunciation_audition({'Ann': 'Ann Ann'}), \
                        patch('gradio_client.handle_file', side_effect=lambda path: path):
                    self.assertEqual({'completed': [0], 'failed': []}, engine.generate_batch(rows, voices, tmp))
                self.assertEqual(['Ann Ann arrives.'], seen); self.assertEqual(before, (rows, voices))
