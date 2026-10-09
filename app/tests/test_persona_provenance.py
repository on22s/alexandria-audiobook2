import json
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import generate_personas as personas


class PersonaProvenanceTests(unittest.TestCase):
    def test_actual_saved_book_switch_restores_reference_and_preview(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import scripts_library as lib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);saved = root / 'scripts';saved.mkdir()
            (root / 'state.json').write_text('{"active_book_id":"first book"}')
            script = [{'speaker':'ALICE','text':'Original speaker evidence.'}]
            (root / 'annotated_script.json').write_text(json.dumps(script))
            source = root / 'source.wav'
            with wave.open(str(source), 'wb') as wav:
                wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0' * 2400)
            engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(source),None))
            config = {}
            def source_backed_reply(*args, **kwargs):
                if not kwargs.get('label', '').startswith('PERSONA COMPILE'):
                    return {}
                reference = json.loads(args[3].split('Character reference:\n', 1)[1])
                return {'description': 'Warm voice.', 'ref_text': reference['reference_sample']}
            with patch.object(personas, 'call_llm_for_object', side_effect=source_backed_reply), patch.object(personas.time,'sleep'):
                self.assertEqual([], personas.run_advanced_persona_generation(script, ['ALICE'],
                    {'ALICE':['Original speaker evidence.']}, config, object(), 'fixture', engine, tmp,
                    SimpleNamespace(batch_size=1)))
            (root / 'voice_config.json').write_text(json.dumps(config))
            files = [root / config['ALICE'][key] for key in ('persona_ref','ref_audio')]
            original_bytes = [p.read_bytes() for p in files]
            (saved / 'second.json').write_text('[{"speaker":"BOB","text":"Other book."}]')
            (saved / 'second.voice_config.json').write_text('{"BOB":{"voice":"Ryan"}}')
            app = FastAPI();app.include_router(lib.router)
            with ExitStack() as stack:
                for name,value in (('DATA_DIR',tmp), ('SCRIPTS_DIR',str(saved)),
                    ('SCRIPT_PATH',str(root / 'annotated_script.json')),
                    ('VOICE_CONFIG_PATH',str(root / 'voice_config.json')),
                    ('CHUNKS_PATH',str(root / 'chunks.json')), ('AUDIOBOOK_PATH',str(root / 'audiobook.mp3')),
                    ('M4B_PATH',str(root / 'audiobook.m4b')), ('process_state',{})):
                    stack.enter_context(patch.object(lib,name,value))
                client = stack.enter_context(TestClient(app))
                for action,book in (('save','first'),('load','second'),('load','first')):
                    response = client.post('/api/scripts/' + action, json={'name':book})
                    self.assertEqual(200,response.status_code,response.text)
            restored = json.loads((root / 'voice_config.json').read_text())
            self.assertEqual(config,restored)
            for key,expected in zip(('persona_ref','ref_audio'),original_bytes):
                self.assertEqual(expected,(root / restored['ALICE'][key]).read_bytes())
            ref = json.loads((root / restored['ALICE']['persona_ref']).read_text())
            self.assertEqual(['Original speaker evidence.'],ref['sample_lines'])

    def test_shared_object_call_retains_raw_payload_and_compile_normalizes_it(self):
        import generate_script as gs
        from persona_validation import validate_compiled_persona_payload
        payload = {'description':'  Calm warm voice.  ', 'ref_text':'  Hello there, friend.  '}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)),
                                                           finish_reason='stop')],usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response))))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);ref_dir=root/'refs';ref_dir.mkdir()
            source=root/'source.wav'
            with wave.open(str(source),'wb') as wav:
                wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*2400)
            engine=Mock();engine.generate_voice_design.return_value=(str(source),None)
            def actual_call(*args,**kwargs):
                result=gs.call_llm_for_object(*args,**kwargs)
                self.assertEqual(payload,result)
                return result
            config={}
            with patch.object(gs,'get_response_log_path',return_value=str(root/'response.log')), \
                 patch.object(personas,'call_llm_for_object',side_effect=actual_call), \
                 patch.object(personas,'validate_compiled_persona_payload',wraps=validate_compiled_persona_payload) as validate, \
                 patch.object(personas.time,'sleep'):
                self.assertTrue(personas._compile_persona(client,'fixture',engine,config,tmp,str(ref_dir),'ALICE',
                                   {'ALICE':['Hello there, friend.']},'Return JSON',None,context_length=4096))
                self.assertEqual(3,validate.call_count)
            engine.generate_voice_design.assert_called_once_with(description='Calm warm voice.',sample_text='Hello there, friend.')
            self.assertEqual('Calm warm voice.',config['ALICE']['description'])
