"""Synthetic generated speech must pass admission before replacing a take."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from project import ProjectManager
from tts import TTSEngine
from audio_validation import GeneratedAudioError, validate_generated_audio


def tone(seconds=1, rate=24000, hz=220):
    return (.2 * np.sin(2 * np.pi * hz * np.arange(round(seconds * rate)) / rate)).astype('float32')


class GeneratedSpeechPublicationTests(unittest.TestCase):
    def manager(self, root, text, audio):
        manager = ProjectManager(str(root))
        prior = root / 'voicelines' / 'prior.wav'
        sf.write(prior, tone(), 24000)
        manager.save_chunks([dict(id=0, uid='speech', speaker='NARRATOR', text=text,
                                  instruct='Calm.', status='done', audio_path='voicelines/prior.wav')])
        Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR': {'type': 'custom', 'voice': 'Ryan'}}))

        def single(text, instruct, speaker, voices, output):
            TTSEngine._save_wav(audio, 24000, output)
            return True

        def batch(chunks, voices, output, seed):
            for chunk in chunks:
                TTSEngine._save_wav(audio, 24000, Path(output) / f"temp_batch_{chunk['index']}.wav")
            return {'completed': [chunk['index'] for chunk in chunks], 'failed': []}

        manager.engine = SimpleNamespace(generate_voice=single, generate_batch=batch)
        return manager, prior

    def test_single_parallel_and_native_batch_reject_silent_and_grossly_short_audio(self):
        text = ' '.join(['The narrator should read every word in this long passage.'] * 12)
        for mode in ('single', 'parallel', 'batch'):
            for kind, audio in (('silent', np.zeros(24000)), ('too short', tone(.05))):
                with self.subTest(mode=mode, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    manager, prior = self.manager(root, text, audio)
                    previous = prior.read_bytes()
                    with contextlib.redirect_stdout(io.StringIO()):
                        if mode == 'single':
                            result = manager.generate_chunk_audio(0)
                        elif mode == 'parallel':
                            result = manager.generate_chunks_parallel([0], max_workers=1)
                        else:
                            result = manager.generate_chunks_batch([0], batch_size=1)
                    self.assertFalse(result[0] if mode == 'single' else result['completed'])
                    row = manager.load_chunks()[0]
                    self.assertEqual('error', row['status'])
                    self.assertIn(kind, row['error'])
                    self.assertEqual('voicelines/prior.wav', row['audio_path'])
                    self.assertEqual(previous, prior.read_bytes())
                    self.assertEqual([], list(root.glob('temp_batch_*.wav')))
                    self.assertEqual([], list(root.glob('chunk_*.wav')))
                    self.assertEqual([], list((root / 'voicelines').glob('.render-*')))

    def test_short_utterances_and_long_multilingual_controls_remain_valid(self):
        cases = [('Ah!', .05), ('はい', .05), ('Привет!', .05),
                 ('The narrator speaks these words clearly. ' * 10, 8),
                 ('これは合成音声の確認です。' * 10, 8), ('这是合成语音测试。' * 10, 8),
                 ('이것은 음성 테스트입니다. ' * 10, 8)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'speech.wav'
            for text, seconds in cases:
                with self.subTest(text=text):
                    sf.write(path, tone(seconds), 24000)
                    self.assertEqual(path, validate_generated_audio(path, expected_text=text))
            sf.write(path, np.zeros(24000), 24000)
            # Structural validation is also used for intentional non-speech files.
            self.assertEqual(path, validate_generated_audio(path))

    def test_long_compact_script_cannot_pass_as_a_tiny_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'speech.wav'
            sf.write(path, tone(.05), 24000)
            for text in ('これは合成音声です' * 10, '这是语音测试' * 10, '음성테스트' * 10):
                with self.subTest(text=text), self.assertRaisesRegex(GeneratedAudioError, 'too short'):
                    validate_generated_audio(path, expected_text=text)

    def test_duration_boundary_uses_decoded_frames_for_latin_and_compact_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'speech.wav'
            for text in ('a' * 160, '漢' * 40, '한' * 40,
                         unicodedata.normalize('NFD', '한' * 40)):
                with self.subTest(text=text):
                    sf.write(path, tone(2)[:-1], 24000)
                    with self.assertRaisesRegex(GeneratedAudioError, 'too short'):
                        validate_generated_audio(path, expected_text=text)
                    sf.write(path, tone(2), 24000)
                    self.assertEqual(path, validate_generated_audio(path, expected_text=text))

    def test_publication_measures_respelling_instead_of_raw_source_length(self):
        import pronunciation
        from tests.test_batch_style_timeline import get_engine
        with tempfile.TemporaryDirectory() as tmp:
            source = 'Longname' * 30
            manager, _ = self.manager(Path(tmp), source, tone(.05))
            engine, seen = get_engine(), []

            def render(text, instruct, speaker, voices, output):
                seen.append(text)
                TTSEngine._save_wav(tone(.05), 24000, output)
                return True

            manager.engine = engine
            with pronunciation.ensure_pronunciation_audition({source: 'Ah'}), \
                    patch.object(engine, '_generate_voice_prepared', side_effect=render), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = manager.generate_chunk_audio(0)
            self.assertTrue(result[0], result)
            self.assertEqual(['Ah.'], seen)
            self.assertEqual('done', manager.load_chunks()[0]['status'])

    def test_native_single_and_batch_share_lexicon_snapshot_across_midflight_edit(self):
        import pronunciation
        from speech_text import normalize_for_speech
        from tests.test_batch_style_timeline import get_engine,get_torch
        import sys
        long='Verylongname'*30
        for mode in ('single','batch'):
            for initial,replacement,success in (('Ah',long,True),(long,'Ah',False)):
                with self.subTest(mode=mode,initial=initial),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp)
                    lexicon=root/'lexicon.json'
                    lexicon.write_text(json.dumps({'Hero':initial}))
                    manager,prior=self.manager(root,'Hero',tone(.05))
                    old_audio=prior.read_bytes()
                    engine=get_engine()
                    seen=[]
                    def change(text):
                        seen.append(text)
                        lexicon.write_text(json.dumps({'Hero':replacement}))
                    def single(text,instruct,speaker,voices,output):
                        change(text)
                        TTSEngine._save_wav(tone(.05),24000,output)
                        return True
                    def custom(**kwargs):
                        change(kwargs['text'][0])
                        return [tone(.05) for text in kwargs['text']],24000
                    manager.engine=engine
                    with patch.object(pronunciation,'DEFAULT_PATH',str(lexicon)), \
                            patch.object(engine,'_generate_voice_prepared',side_effect=single), \
                            patch.object(engine,'_init_local_custom',return_value=SimpleNamespace(generate_custom_voice=custom)), \
                            patch.dict(sys.modules,{'torch':get_torch()}), \
                            contextlib.redirect_stdout(io.StringIO()):
                        result=manager.generate_chunk_audio(0) if mode=='single' else manager.generate_chunks_batch([0],batch_size=1)
                        self.assertEqual(success,bool(result[0] if mode=='single' else result['completed']),result)
                        self.assertEqual([initial+'.'],seen)
                        self.assertEqual(replacement+'.',normalize_for_speech('Hero'))
                    if not success:
                        self.assertEqual(old_audio,prior.read_bytes())
                        self.assertEqual('voicelines/prior.wav',manager.load_chunks()[0]['audio_path'])

    def test_snapshot_preserves_audition_and_restores_after_body_failure(self):
        import pronunciation
        from speech_text import normalize_for_speech
        with pronunciation.ensure_pronunciation_audition({'Hero':'Ah'}):
            with pronunciation.ensure_pronunciation_snapshot():
                self.assertEqual('Ah.',normalize_for_speech('Hero'))
            with self.assertRaisesRegex(RuntimeError,'body must propagate'):
                with pronunciation.ensure_pronunciation_snapshot():
                    raise RuntimeError('body must propagate')
            self.assertEqual('Ah.',normalize_for_speech('Hero'))
        with pronunciation.ensure_literal_pronunciation_audition(), \
                patch.object(pronunciation,'load_lexicon',side_effect=AssertionError('literal must not load')):
            with pronunciation.ensure_pronunciation_snapshot():
                self.assertEqual('Hero.',normalize_for_speech('Hero'))
        self.assertIsNone(pronunciation._audition_matcher.get())
        with patch.object(pronunciation,'load_lexicon',return_value={'Hero':'Pinned'}):
            with self.assertRaisesRegex(RuntimeError,'owned body must propagate'):
                with pronunciation.ensure_pronunciation_snapshot():
                    self.assertEqual('Pinned.',normalize_for_speech('Hero'))
                    raise RuntimeError('owned body must propagate')
        self.assertIsNone(pronunciation._audition_matcher.get())

    def test_snapshot_failsoft_missing_malformed_deep_json_and_matcher_reset(self):
        import pronunciation
        from speech_text import normalize_for_speech
        with tempfile.TemporaryDirectory() as tmp:
            lexicon=Path(tmp)/'lexicon.json'
            with patch.object(pronunciation,'DEFAULT_PATH',str(lexicon)):
                for content in (None,'{bad','['*2000+'0'+']'*2000):
                    with self.subTest(content='missing' if content is None else content[:8]):
                        if content is None: lexicon.unlink(missing_ok=True)
                        else: lexicon.write_text(content)
                        with pronunciation.ensure_pronunciation_snapshot():
                            lexicon.write_text(json.dumps({'Hero':'Next'}))
                            self.assertEqual('Hero.',normalize_for_speech('Hero'))
                        self.assertEqual('Next.',normalize_for_speech('Hero'))
                        self.assertIsNone(pronunciation._audition_matcher.get())
                lexicon.write_text(json.dumps({'Hero':'Cached'}))
                self.assertEqual('Cached.',normalize_for_speech('Hero'))
                with patch.object(pronunciation,'load_lexicon',side_effect=RecursionError('broken matcher')):
                    with pronunciation.ensure_pronunciation_snapshot():
                        self.assertEqual('Hero.',normalize_for_speech('Hero'))
                self.assertEqual('Cached.',normalize_for_speech('Hero'))

    def test_parallel_workers_keep_independent_snapshots_without_holding_lexicon_lock(self):
        import pronunciation
        from speech_text import normalize_for_speech
        from concurrent.futures import ThreadPoolExecutor
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            lexicon=Path(tmp)/'lexicon.json'
            lexicon.write_text(json.dumps({'Hero':'Old'}))
            captured=threading.Event()
            edited=threading.Event()
            def first():
                with pronunciation.ensure_pronunciation_snapshot():
                    captured.set()
                    if not edited.wait(5): raise RuntimeError('second worker could not acquire snapshot')
                    return normalize_for_speech('Hero')
            def second():
                if not captured.wait(5): raise RuntimeError('first worker did not capture')
                lexicon.write_text(json.dumps({'Hero':'New'}))
                try:
                    with pronunciation.ensure_pronunciation_snapshot(): return normalize_for_speech('Hero')
                finally: edited.set()
            with patch.object(pronunciation,'DEFAULT_PATH',str(lexicon)),ThreadPoolExecutor(max_workers=2) as pool:
                a,b=pool.submit(first),pool.submit(second)
                self.assertEqual(('Old.','New.'),(a.result(10),b.result(10)))
            self.assertIsNone(pronunciation._audition_matcher.get())

    def test_actual_project_parallel_workers_pin_distinct_lexicon_revisions(self):
        import pronunciation
        from speech_text import normalize_for_speech
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            lexicon=root/'lexicon.json'
            lexicon.write_text(json.dumps({'Hero':'Old'}))
            manager,_=self.manager(root,'Hero one.',tone(.05))
            rows=manager.load_chunks()
            rows.append(dict(id=1,uid='second',speaker='NARRATOR',text='Hero two.',instruct='Calm.',status='pending'))
            manager.save_chunks(rows)
            first_captured,second_captured=threading.Event(),threading.Event()
            seen=[]
            def single(text,instruct,speaker,voices,output):
                seen.append(normalize_for_speech(text))
                if text=='Hero one.':
                    first_captured.set()
                    if not second_captured.wait(5):raise RuntimeError('second worker blocked')
                else: second_captured.set()
                TTSEngine._save_wav(tone(.05),24000,output)
                return True
            manager.engine=SimpleNamespace(generate_voice=single)
            native_worker=manager._generate_captured_chunk_audio
            def ordered_worker(index,chunk):
                if index==1:
                    if not first_captured.wait(5):raise RuntimeError('first worker did not capture')
                    lexicon.write_text(json.dumps({'Hero':'New'}))
                return native_worker(index,chunk)
            with patch.object(pronunciation,'DEFAULT_PATH',str(lexicon)), \
                    patch.object(manager,'_generate_captured_chunk_audio',side_effect=ordered_worker), \
                    contextlib.redirect_stdout(io.StringIO()):
                result=manager.generate_chunks_parallel([0,1],max_workers=2)
            self.assertEqual([0,1],sorted(result['completed']))
            self.assertEqual([],result['failed'])
            self.assertEqual(['Old one.','New two.'],sorted(seen,reverse=True))
            self.assertIsNone(pronunciation._audition_matcher.get())

if __name__=='__main__':
    unittest.main()
