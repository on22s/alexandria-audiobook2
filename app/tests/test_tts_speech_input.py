"""Actual TTS leaf/composite routing retains exact speech and per-output identity."""
import contextlib
import hashlib
import io
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
from config_settings import TTSConfig
from synthesis_input import ensure_speech_input_capture, get_captured_speech_input, capture_speech_input
from tests import test_synthesis_window_dispatch as window_fixture
from tests import test_audio_generation_cancellation as cancellation_fixture
from tests.test_occurrence_pronunciation_generation import rule


class TTSSpeechInputTests(unittest.TestCase):
    def test_all_builtin_single_and_split_compositions_capture_actual_prepared_input_once(self):
        fixture = window_fixture.SynthesisWindowDispatchTests()
        for kind in ('custom', 'clone', 'design', 'lora', 'ensemble'):
            for split in (False, True):
                with self.subTest(kind=kind, split=split), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with fixture.fixture(root, kind) as (engine, voices, seen, model):
                        engine._speech_verification = {'enabled': True}
                        if not split: engine._synthesis_windows = None
                        row = dict(uid='a', index=7, speaker='A', text='Ann Ann',
                                   pronunciation_rules=[rule(replacement='Ann Ann')])
                        context = tts.get_pronunciation_render_context(voices, row, 7, engine.get_model_settings())
                        output = root / 'finished.wav'
                        with pronunciation.ensure_pronunciation_audition({'Ann': 'Shared'}), \
                                pronunciation.ensure_occurrence_pronunciation_context(row, context):
                            result = engine.generate_voice_result(row['text'], 'delivery instruction', 'A', voices, str(output))
                        self.assertTrue(result.success, result.failure)
                        self.assertIsNotNone(result.speech_input)
                        self.assertEqual('Ann Ann Shared.', result.speech_input['text'])
                        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), result.speech_input['audio']['sha256'])
                        self.assertEqual('English', result.speech_input['language'])
                        self.assertNotIn('reference', result.speech_input['text'])
                        self.assertNotIn('instruction', result.speech_input['text'])

    def test_native_batch_keeps_sorted_original_indices_and_exact_provider_texts(self):
        fixture = window_fixture.SynthesisWindowDispatchTests()
        for kind in ('custom', 'clone', 'lora', 'design', 'ensemble'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with fixture.fixture(root, kind, chars=1000) as (engine, voices, seen, model):
                    engine._speech_verification = {'enabled': True}
                    rows = [dict(index=7, uid='long', speaker='A', text='Longer plain speech.'),
                            dict(index=2, uid='short', speaker='A', text='Short.')]
                    result = engine.generate_batch(rows, voices, directory, batch_seed=0)
                    self.assertEqual({2,7}, set(result['completed']))
                    self.assertEqual([], result['failed'])
                    self.assertEqual({2,7}, set(result['speech_inputs']))
                    for row in rows:
                        proof = result['speech_inputs'][row['index']]
                        self.assertEqual(row['text'], proof['text'])
                        output = root / f"temp_batch_{row['index']}.wav"
                        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), proof['audio']['sha256'])

    def test_split_batch_parents_get_complete_original_index_input(self):
        fixture = window_fixture.SynthesisWindowDispatchTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with fixture.fixture(root, chars=8) as (engine, voices, seen, model):
                engine._speech_verification = {'enabled': True}
                result = engine.generate_batch([dict(index=9, uid='parent', speaker='A', text='One. Two. Three.')],
                                               voices, directory, batch_seed=0)
                self.assertEqual([9], result['completed'])
                self.assertEqual('One. Two. Three.', result['speech_inputs'][9]['text'])

    def test_unknown_successful_override_and_disabled_capture_have_no_guessed_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = tts.TTSEngine({'tts': {'speech_verification': {'enabled': True}}})
            output = root / 'audio.wav'
            def unknown(*args):
                sf.write(output, np.full(2400,.1),24000)
                return True
            with patch.object(engine, 'generate_voice', side_effect=unknown):
                result = engine.generate_voice_result('Unknown actual input', '', 'A', {}, str(output))
            self.assertTrue(result.success); self.assertIsNone(result.speech_input)
            engine._speech_verification = None
            with patch.object(engine, 'generate_voice', side_effect=unknown), \
                    patch('synthesis_input._get_artifact', side_effect=AssertionError('off capture inspected audio')):
                result = engine.generate_voice_result('Off input', '', 'A', {}, str(output))
            self.assertTrue(result.success); self.assertIsNone(result.speech_input)

    def test_native_batch_missing_waveform_and_save_failure_do_not_lend_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = tts.TTSEngine({'tts': {'speech_verification': {'enabled': True}}})
            save = engine._save_wav
            def failing(values, rate, path):
                if 'temp_batch_3.' in str(path): raise OSError('synthetic save failure')
                return save(values, rate, path)
            with ensure_speech_input_capture(), patch.object(engine, '_save_wav', side_effect=failing):
                result, duration = engine.save_submitted_batch_waveforms([np.full(2400,.1)]*2,
                    [9,3,20], ['First.','Failed.','Missing.'],24000,directory,'custom')
                self.assertEqual([9], result['completed'])
                self.assertEqual({3,20}, {index for index,_ in result['failed']})
                self.assertEqual('First.', get_captured_speech_input(Path(directory)/'temp_batch_9.wav')['text'])
                self.assertIsNone(get_captured_speech_input(Path(directory)/'temp_batch_3.wav'))
                self.assertIsNone(get_captured_speech_input(Path(directory)/'temp_batch_20.wav'))

    def test_external_actual_client_result_promotes_matching_input_to_single_and_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'provider.wav'; sf.write(source,np.full(2400,.1),24000)
            engine = tts.TTSEngine({'tts': {'mode':'external', 'speech_verification': {'enabled':True}}})
            calls=[]
            def predict(**kwargs):
                calls.append(kwargs['text']); return (str(source),)
            with patch.object(engine,'_external_endpoint',side_effect=lambda endpoint=None:(SimpleNamespace(predict=predict),threading.Lock())):
                result=engine.generate_voice_result('Actual API words','','A',{'A':{'type':'custom'}},str(root/'single.wav'))
                self.assertTrue(result.success,result.failure)
                self.assertEqual('Actual API words.',result.speech_input['text'])
                batch=engine.generate_batch([dict(index=8,uid='batch',speaker='A',text='Batch API words')],
                                            {'A':{'type':'custom'}},directory)
                self.assertEqual([8],batch['completed'])
                self.assertEqual('Batch API words.',batch['speech_inputs'][8]['text'])
                self.assertEqual(['Actual API words.','Batch API words.'],calls)

    def test_external_late_worker_cannot_mutate_a_later_call_input(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root=Path(directory)
            engine=tts.TTSEngine({'tts':{'mode':'external','speech_verification':{'enabled':True}}})
            engine._external_timeout=.03; release=threading.Event(); done=threading.Event()
            def late(text,instruct,speaker,voices,path,cancelled=None):
                try:
                    release.wait(3); sf.write(path,np.full(2400,.1),24000,format='WAV')
                    capture_speech_input(path,text,engine._language,'late_worker_fixture')
                    return True
                finally: done.set()
            with patch.object(engine,'_external_generate_custom',side_effect=late):
                first=engine.generate_voice_result('Late words','','A',{'A':{'type':'custom'}},str(root/'old.wav'))
            self.assertFalse(first.success); self.assertIsNone(first.speech_input)
            source=root/'source.wav'; sf.write(source,np.full(2400,.1),24000)
            engine._external_timeout=1
            with patch.object(engine,'_external_endpoint',side_effect=lambda endpoint=None:(SimpleNamespace(predict=lambda **kw:(str(source),)),threading.Lock())):
                second=engine.generate_voice_result('Next words','','A',{'A':{'type':'custom'}},str(root/'new.wav'))
            release.set(); self.assertTrue(done.wait(3))
            self.assertTrue(second.success,second.failure)
            self.assertEqual('Next words.',second.speech_input['text'])
            self.assertIsNone(first.speech_input)

    def test_execution_setting_is_strict_off_default_and_excluded_from_acoustic_identity(self):
        self.assertEqual({'enabled':False},TTSConfig().speech_verification)
        off=tts.TTSEngine({'tts':{}})
        on=tts.TTSEngine({'tts':{'speech_verification':{'enabled':True}}})
        self.assertEqual(off.get_model_settings(),on.get_model_settings())
        policy=on.get_speech_verification_policy(); policy['enabled']=False
        self.assertEqual({'enabled':True},on.get_speech_verification_policy())
        for value in [True,{'enabled':1},{'enabled':'true'},{'unknown':True}]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                TTSConfig(speech_verification=value)

    def test_project_single_parallel_and_native_routes_carry_input_to_shared_publication(self):
        audio_fixture=cancellation_fixture.AudioGenerationCancellationTests()
        synthesis_fixture=window_fixture.SynthesisWindowDispatchTests()
        for mode in ('single','parallel','native'):
            with self.subTest(mode=mode),audio_fixture.fixture(1) as (root,manager,before,state):
                manager._update_chunk_fields_by_uid('line-0',text='One. Two. Three.')
                with synthesis_fixture.fixture(root,chars=8) as (engine,voices,seen,model):
                    engine._speech_verification={'enabled':True};manager.engine=engine
                    (root/'voice_config.json').write_text('{"NARRATOR":{"type":"custom","voice":"Aiden"}}')
                    received=[]; publish=manager._publish_generated_chunk_audio
                    def observe(chunk,path,speaker,**kwargs):
                        receipt=chunk.get('_speech_input');received.append(receipt)
                        self.assertIsNotNone(receipt)
                        self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),receipt['audio']['sha256'])
                        return publish(chunk,path,speaker,**kwargs)
                    with patch.object(manager,'_publish_generated_chunk_audio',side_effect=observe):
                        if mode=='single':self.assertTrue(manager.generate_chunk_audio(0)[0])
                        elif mode=='parallel':self.assertEqual([0],manager.generate_chunks_parallel([0],max_workers=1)['completed'])
                        else:self.assertEqual([0],manager.generate_chunks_batch([0],batch_size=1)['completed'])
                    self.assertEqual(1,len(received))
                    self.assertEqual('One. Two. Three.',received[0]['text'])


if __name__ == '__main__':
    unittest.main()
