from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voices

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class VoiceAutosaveMetadataTests(unittest.TestCase):
    def test_actual_http_save_keeps_server_audit_and_reference_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'voice_config.json'
            original={'ALICE':{'type':'custom','voice':'Ryan','persona_voice_audit':{'identity':True,'notes':'human checked'},'persona_ref':'persona_refs/book/generation/alice.json','future_metadata':{'value':17}},'BOB':{'preserve':True}}
            path.write_text(json.dumps(original))
            app=FastAPI();app.include_router(voices.router)
            with patch.object(voices,'VOICE_CONFIG_PATH',str(path)),TestClient(app) as client:
                response=client.post('/api/save_voice_config',json={'ALICE':{'type':'custom','voice':'Aiden','alias_of':None}})
                self.assertEqual(200,response.status_code,response.text)
            saved=json.loads(path.read_text());self.assertEqual('Aiden',saved['ALICE']['voice'])
            for key in ('persona_voice_audit','persona_ref','future_metadata'):self.assertEqual(original['ALICE'][key],saved['ALICE'][key])
            self.assertEqual(original['BOB'],saved['BOB'])

    def test_bare_default_save_keeps_explicit_backend_persona_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'annotated_script.json';script.write_text('[{"speaker":"ALICE","text":"Hello."}]');path=root/'voice_config.json'
            app=FastAPI();app.include_router(voices.router)
            with patch.object(voices,'VOICE_CONFIG_PATH',str(path)),patch.object(voices,'SCRIPT_PATH',str(script)),TestClient(app) as client:
                before=client.get('/api/voices').json();self.assertTrue(before[0]['persona_pending'])
                result=client.post('/api/save_voice_config',json={'ALICE':{'type':'custom','voice':'Ryan','seed':'0','character_style':''}});self.assertEqual(200,result.status_code,result.text)
                after=client.get('/api/voices').json();self.assertTrue(after[0]['persona_pending']);self.assertEqual('0',after[0]['config']['seed'])
                from tts import voice_is_set
                self.assertFalse(voice_is_set(after[0]['config']))

    def test_collect_keeps_audit_and_zero_seed_without_inactive_voice_fields(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const indexStart=source.indexOf('function getLoraModelsById(');const indexHelper=indexStart>=0?source.slice(indexStart,source.indexOf('async function suggestVoices(',indexStart)):'';
const code=indexHelper+source.slice(source.indexOf('function collectVoiceConfig()'),source.indexOf('function onVoiceReadyChange('));
const metadata={type:'clone',ref_audio:'prior.wav',ref_text:'prior text',seed:0,persona_voice_audit:{identity:true},persona_ref:'refs/alice.json',future_metadata:{value:17},alias_of:'OLD',ready:true};
const before=JSON.stringify(metadata);
const fields={'.alias-select':{value:''},'.voice-type:checked':{value:'custom'},'.voice-select':{value:'Ryan'},'.character-style':{value:'Warm'},'.voice-ready':{checked:false}};
const card={dataset:{voice:'ALICE'},querySelector(selector){return fields[selector]||null;}};
const context={window:{_voicesByName:{ALICE:{config:metadata}}},document:{querySelectorAll:()=>[card]}};
vm.runInNewContext(code,context);const saved=context.collectVoiceConfig().ALICE;
assert.strictEqual(saved.persona_voice_audit.identity,true);assert.strictEqual(saved.persona_ref,'refs/alice.json');assert.strictEqual(saved.future_metadata.value,17);assert.strictEqual(saved.seed,'0');assert.strictEqual(saved.type,'custom');assert.strictEqual(saved.voice,'Ryan');assert.strictEqual(saved.character_style,'Warm');assert(!('ref_audio' in saved));assert(!('ref_text' in saved));assert(!('alias_of' in saved));assert(!('ready' in saved));assert.strictEqual(JSON.stringify(metadata),before);
'''
        result=subprocess.run(['node','-e',script,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
