"""External selection must never silently initialize local voice models."""
import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
import tts
from routers import voice_design


class TTSBackendCapabilityTests(unittest.TestCase):
    def test_dynamic_narrator_preserves_supported_speaker_key_in_single_and_batch(self):
        for speaker in ('NARRATOR', 'Narrator'):
            for strategy in ('chapter', 'focus'):
                with self.subTest(speaker=speaker, strategy=strategy), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); engine = tts.TTSEngine({'tts': {'mode': 'local'}})
                    config = {speaker: {'type': 'custom', 'voice': 'Ryan', 'narrator_strategy': strategy,
                                       'versions': {'second': {'type': 'custom', 'voice': 'Aiden'}}},
                              'HERO': {'type': 'custom', 'voice': 'Aiden'}}
                    before = copy.deepcopy(config)
                    chunk = {'index': 0, 'speaker': speaker, 'text': 'Synthetic narration.',
                             'narrator_version': 'second', 'focus_speaker': 'HERO'}
                    seen = []
                    def render(text, instruct, name, selected, path):
                        voice = selected[name]['voice']; seen.append(voice)
                        sf.write(path, np.full(240, .25 if voice == 'Aiden' else .125), 24000)
                        return True
                    with patch.object(engine, 'generate_custom_voice', side_effect=render):
                        resolved = tts.resolve_narrator_voice_config(speaker, config, chunk)
                        self.assertTrue(engine.generate_voice(chunk['text'], '', speaker, resolved, str(root / 'single.wav')))
                        result = engine.generate_batch([chunk], config, tmp)
                    self.assertEqual({'completed': [0], 'failed': []}, result)
                    self.assertEqual(['Aiden', 'Aiden'], seen)
                    self.assertEqual(before, config)
                    for name in ('single.wav', 'temp_batch_0.wav'):
                        audio, rate = sf.read(root / name)
                        self.assertEqual(24000, rate); np.testing.assert_allclose(audio, .25)

    def test_external_timeline_versions_use_effective_backend_admission(self):
        for category in ('lora', 'design'):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as tmp:
                engine = tts.TTSEngine({'tts': {'mode': 'external'}})
                voice = {'type': 'custom', 'voice': 'Ryan',
                         'version_timeline': [{'from_index': 0, 'version_id': 'chosen'}],
                         'versions': {'chosen': {'type': category, 'adapter_path': 'fixture',
                                                'description': 'Fixture voice'}}}
                with patch.object(engine, '_local_batch_lora') as lora, \
                        patch.object(engine, 'generate_design_voice') as design, \
                        patch.object(engine, '_external_batch') as external:
                    result = engine.generate_batch(
                        [{'index': 0, 'speaker': 'A', 'text': 'Synthetic sentence.'}], {'A': voice}, tmp)
                self.assertEqual([], result['completed'])
                self.assertEqual([0], [index for index, _ in result['failed']])
                self.assertIn('local TTS mode', result['failed'][0][1])
                lora.assert_not_called(); design.assert_not_called(); external.assert_not_called()
                self.assertEqual([], list(Path(tmp).iterdir()))

    def test_external_single_and_batch_reject_lora_design_before_any_model_dispatch(self):
        for category in ('lora','design'):
            with self.subTest(category=category),tempfile.TemporaryDirectory() as tmp:
                engine=tts.TTSEngine({'tts':{'mode':'external'}});engine._clear_gpu_cache=Mock()
                local=Mock(return_value=True)
                config={'A':{'type':category,'adapter_path':'lora_models/a','description':'Quiet'}}
                chunks=[{'index':7,'speaker':'A','text':'Known line'}];before=copy.deepcopy((config,chunks))
                path=Path(tmp)/'out.wav';path.write_bytes(b'prior approved audio')
                with patch.object(engine,'generate_lora_voice',local),patch.object(engine,'generate_design_voice',local),contextlib.redirect_stdout(io.StringIO()) as logs:
                    self.assertFalse(engine.generate_voice('Known line','','A',config,str(path)))
                    result=engine.generate_batch(chunks,config,tmp)
                local.assert_not_called();self.assertEqual([],result['completed']);self.assertEqual([7],[i for i,_ in result['failed']])
                self.assertIn('local TTS mode',result['failed'][0][1]);self.assertIn('local TTS mode',logs.getvalue())
                self.assertEqual(b'prior approved audio',path.read_bytes());self.assertEqual(before,(config,chunks))

    def test_direct_external_preview_and_lora_calls_reject_before_local_initialization(self):
        engine=tts.TTSEngine({'tts':{'mode':'external'}})
        with patch.object(engine,'_init_local_design',side_effect=AssertionError('loaded local design')) as design,patch.object(engine,'_ensure_local_lora_generation',side_effect=AssertionError('loaded local LoRA')) as lora:
            with self.assertRaisesRegex(ValueError,'local TTS mode'):
                engine.generate_voice_design('quiet','known line')
            with self.assertRaisesRegex(ValueError,'local TTS mode'):
                engine.generate_design_voice('known line','',{'description':'quiet'},'/unused.wav')
            with contextlib.redirect_stdout(io.StringIO()) as logs:
                self.assertFalse(engine.generate_lora_voice('known line','',{'adapter_path':'lora_models/a'},'/unused.wav'))
            self.assertIn('local TTS mode',logs.getvalue());design.assert_not_called();lora.assert_not_called()

    def test_mixed_external_batch_preserves_supported_rows_and_reports_unsupported_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=tts.TTSEngine({'tts':{'mode':'external'}});engine._clear_gpu_cache=Mock();seen=[]
            def render(chunks,config,output,kind):
                seen.append((kind,[c['index'] for c in chunks]));return {'completed':[c['index'] for c in chunks],'failed':[]}
            config={'A':{'type':'custom','voice':'Ryan'},'B':{'type':'clone','ref_audio':'ref.wav','ref_text':'reference'},'C':{'type':'lora','adapter_path':'lora_models/a'},'D':{'type':'design','description':'quiet'}}
            chunks=[{'index':i,'speaker':speaker,'text':'Known line'} for i,speaker in enumerate(config)]
            before=copy.deepcopy((config,chunks))
            with patch.object(engine,'_external_batch',side_effect=render),patch.object(engine,'generate_lora_voice') as lora,patch.object(engine,'generate_design_voice') as design:
                result=engine.generate_batch(chunks,config,tmp)
            self.assertEqual([0,1],result['completed']);self.assertEqual([2,3],[i for i,_ in result['failed']]);self.assertEqual([('custom',[0]),('clone',[1])],seen)
            lora.assert_not_called();design.assert_not_called();self.assertEqual(before,(config,chunks))

    def test_external_ensemble_and_dynamic_narrator_cannot_hide_unsupported_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=tts.TTSEngine({'tts':{'mode':'external'}});engine._clear_gpu_cache=Mock()
            config={'GROUP':{'type':'ensemble','members':['A','B']},'A':{'type':'custom','voice':'Ryan'},
                'B':{'type':'lora','adapter_path':'lora_models/b'},'NARRATOR':{'type':'custom','narrator_strategy':'focus'}}
            with patch.object(engine,'generate_custom_voice') as custom,patch.object(engine,'generate_lora_voice') as lora:
                with self.assertRaisesRegex(ValueError,'local TTS mode'):
                    engine.generate_voice('known line','','GROUP',config,str(Path(tmp)/'single.wav'))
                result=engine.generate_batch([{'index':7,'speaker':'GROUP','text':'known line'},
                    {'index':9,'speaker':'NARRATOR','focus_speaker':'B','text':'known narration'}],config,tmp)
            self.assertEqual([],result['completed']);self.assertEqual([7,9],sorted(i for i,_ in result['failed']))
            self.assertTrue(all('local TTS mode' in error for _,error in result['failed']))
            custom.assert_not_called();lora.assert_not_called();self.assertEqual([],list(Path(tmp).iterdir()))

    def test_local_design_and_lora_still_render_known_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);engine=tts.TTSEngine({'tts':{'mode':'local'}})
            model=SimpleNamespace(generate_voice_design=lambda **kwargs:([np.full(240,.125)],24000),generate_voice_clone=lambda **kwargs:([np.full(240,.25)],24000))
            torch=SimpleNamespace(manual_seed=lambda value:None)
            with patch.dict('sys.modules',{'torch':torch}),patch.object(engine,'_init_local_design',return_value=model),patch.object(engine,'_ensure_local_lora_generation',return_value=(model,object())),patch.object(tts,'_resolve_asset_path',return_value=str(root)):
                preview,rate=engine.generate_voice_design('Quiet','known line',seed=0)
                self.assertEqual(24000,rate)
                lora=root/'lora.wav';self.assertTrue(engine.generate_lora_voice('known line','',{'adapter_path':str(root),'seed':0},str(lora)))
            for path,value in ((preview,.125),(lora,.25)):
                audio,rate=sf.read(path);self.assertEqual(24000,rate);self.assertEqual(240,len(audio));np.testing.assert_allclose(audio,value)

    def test_preview_http_returns_actionable_client_error_and_releases_its_claim(self):
        engine=tts.TTSEngine({'tts':{'mode':'external'}});api=FastAPI();api.include_router(voice_design.router)
        import core
        previous = copy.deepcopy(core.process_state['voice_design'])
        try:
            core.process_state['voice_design']['running'] = False
            with tempfile.TemporaryDirectory() as root, \
                 patch.object(core, 'DATA_DIR', root), \
                 patch.object(core, 'acquire_gpu_lock', return_value=None), \
                 patch.object(core, 'check_global_gpu_lock'), \
                 patch.object(core, 'claim_gpu_task', wraps=core.claim_gpu_task) as claim, \
                 patch.object(voice_design.project_manager, 'get_engine', return_value=engine), \
                 patch.object(engine, '_init_local_design') as load, \
                 TestClient(api, raise_server_exceptions=False) as client:
                response = client.post('/api/voice_design/preview', json={'description': 'Quiet', 'sample_text': 'known line'})
            self.assertEqual(400, response.status_code, response.text)
            self.assertIn('local TTS mode', response.json()['detail'])
            load.assert_not_called()
            claim.assert_called_once_with('voice_design')
            self.assertNotIn('voice_design', core._task_claims)
            self.assertFalse(core.process_state['voice_design']['running'])
        finally:
            core.process_state['voice_design'].clear()
            core.process_state['voice_design'].update(previous)
