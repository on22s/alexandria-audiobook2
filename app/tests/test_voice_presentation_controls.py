"""Manual vocal presentation survives saving without mutating narrative labels."""
import copy
import json
from pathlib import Path
from contextlib import ExitStack
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from routers import voices
from tests import test_voice_followups_733 as audio_fixtures
from tests import test_state_voices_js as ui_fixtures


class VoicePresentationControlTests(unittest.TestCase):
    def test_save_and_reload_hint_preserves_script_audio_and_voice_choice(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root,manager,client,path,config,rows=audio_fixtures.VoiceTimelineAudioTests().fixture(tmp,stack)
            original_script=(root/'annotated_script.json').read_bytes()
            before=Path(manager.chunks_path).read_bytes()
            snap=client.get('/api/voice_config/snapshot').json()
            body=copy.deepcopy(snap['config']);body['A']['voice_presentation']='lower, resonant female voice'
            response=client.post('/api/voice_config/save',json={'voices':body,'book_token':snap['book_token'],'revision':snap['revision']})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual('lower, resonant female voice',json.loads(path.read_text())['A']['voice_presentation'])
            self.assertEqual('Ryan',json.loads(path.read_text())['A']['voice'])
            self.assertEqual(before,Path(manager.chunks_path).read_bytes())
            self.assertEqual(original_script,(root/'annotated_script.json').read_bytes())
            restored=client.get('/api/voice_config/snapshot').json()
            self.assertEqual('lower, resonant female voice',restored['config']['A']['voice_presentation'])
            body=restored['config'];body['A']['voice_presentation']=''
            self.assertEqual(200,client.post('/api/voice_config/save',json={'voices':body,'book_token':restored['book_token'],'revision':restored['revision']}).status_code)
            self.assertEqual('',json.loads(path.read_text())['A']['voice_presentation'])
            self.assertEqual(before,Path(manager.chunks_path).read_bytes())

    def test_hint_is_bounded_and_profile_extension_is_pure(self):
        config={'voice_presentation':'  soft, light male voice  ','gender':'female'}
        original=copy.deepcopy(config)
        result=voices.get_voice_matching_profile('Original persona.',config)
        self.assertIn('soft, light male voice',result)
        self.assertIn('Original persona.',result)
        self.assertEqual(original,config)
        self.assertEqual('Original',voices.get_voice_matching_profile('Original',{}))
        with self.assertRaises(ValueError): voices.VoiceConfigItem(voice_presentation='x'*501)

    def test_continuity_shortcut_available_without_gender_reveal(self):
        data=ui_fixtures.suggestion();html=ui_fixtures.VoiceStatesJsTests().render(data)
        self.assertIn('Choose Main voice throughout',html)
        self.assertIn('Review, then Apply',html)
        self.assertNotIn('Possible identity reveal:',html)

    def test_actual_suggestion_prompt_has_hint_but_initial_traits_do_not(self):
        captured={}
        def create(**kwargs):
            captured['prompt']=kwargs['messages'][1]['content']
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"characters": []}'),finish_reason='stop')])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);source=root/'script.json';source.write_text('[{"speaker":"Hero","text":"Hello."}]')
            cfg=root/'voice_config.json';cfg.write_text(json.dumps({'Hero':{'description':'A female adult character.','voice_presentation':'low, masculine resonance'}}))
            for name,value in [('SCRIPT_PATH',str(source)),('VOICE_CONFIG_PATH',str(cfg)),
                ('_build_lora_candidates',lambda **kwargs:[{'adapter_id':'v','name':'Voice','gender':'female','age_group':'adult','description':'low resonant','type':'lora'}]),
                ('_make_llm_client',lambda **kwargs:(client,'fixture')),('get_current_status',lambda *a,**k:{'context_length':8192})]:
                stack.enter_context(patch.object(voices,name,value))
            original=voices._infer_character_traits
            with patch.object(voices,'_infer_character_traits',wraps=original) as infer:
                voices._suggest_voices_impl(voices.SuggestVoicesRequest())
            self.assertNotIn('masculine',infer.call_args.args[1])
            self.assertIn('User-confirmed voice presentation: low, masculine resonance',captured['prompt'])
            self.assertEqual('low, masculine resonance',json.loads(cfg.read_text())['Hero']['voice_presentation'])
