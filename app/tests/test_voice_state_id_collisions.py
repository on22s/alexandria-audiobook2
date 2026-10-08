"""Actual state selection packets cannot overwrite distinct long adapter IDs."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from routers import voices
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class VoiceStateIdCollisionTests(unittest.TestCase):
    def test_long_ids_and_existing_versions_remain_distinct_after_actual_route_writes(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const a='x'.repeat(90)+'A',b='x'.repeat(90)+'B',base=('adult-'+a).slice(0,80),original={};original[base]={type:'lora',adapter_id:'unrelated',adapter_path:'old',age_group:'adult'};
const candidates=[a,b].map(adapter_id=>({adapter_id,config:{type:'lora',adapter_id,adapter_path:'lora_models/'+adapter_id}}));
const rows=candidates.map((candidate,i)=>({dataset:{fromIndex:String(i*10),age:'adult'},querySelector:()=>({value:'library:'+candidate.adapter_id})}));
const card={dataset:{voice:'Alice'},querySelectorAll:()=>rows},button={closest:()=>card},packets=[];
const c={window:null,_voicesByName:{Alice:{config:{versions:original}}},_voiceStateSuggestions:{Alice:{states:[{sources:{library_unused:candidates}}]}},applyVoiceStateSave:async(_button,_speaker,_label,save)=>save(()=>true),API:{post:async(path,body)=>{packets.push({path,body});}},loadVoices:async()=>{},showToast(){},showActionError:(...args)=>{throw Error(args);}};c.window=c;vm.createContext(c);const start=source.indexOf('async function applyVoiceStates(');vm.runInContext(source.slice(start,source.indexOf('async function clearVoiceStates(',start)),c);
(async()=>{await c.applyVoiceStates(button);const writes=packets.filter(p=>p.path.endsWith('/versions'));assert.strictEqual(writes.length,2);assert.notStrictEqual(writes[0].body.version_id,writes[1].body.version_id);assert(writes.every(p=>p.body.version_id.length<=80&&p.body.version_id!==base));assert.strictEqual(Object.keys(original).length,1);
c._voicesByName.Alice.config.versions={...original,...Object.fromEntries(writes.map(p=>[p.body.version_id,{...p.body.config,age_group:p.body.age_group}]))};packets.length=0;await c.applyVoiceStates(button);assert.strictEqual(packets.filter(p=>p.path.endsWith('/versions')).length,0);const points=packets.at(-1).body.points;assert.deepStrictEqual(Array.from(points,p=>p.version_id),writes.map(p=>p.body.version_id));console.log(JSON.stringify({writes,points,original}));})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, 'annotated_script.json'); config = Path(tmp, 'voice_config.json')
            script.write_text('[{"speaker":"Alice","text":"Synthetic"}]')
            config.write_text(json.dumps({'Alice': {'versions': data['original']}}))
            with patch.object(voices, 'SCRIPT_PATH', str(script)), patch.object(voices, 'VOICE_CONFIG_PATH', str(config)):
                for packet in data['writes']:
                    asyncio.run(voices.save_voice_version('Alice', voices.VoiceVersionRequest(**packet['body'])))
                asyncio.run(voices.save_version_timeline('Alice', voices.VersionTimelineRequest(points=data['points'])))
            saved = json.loads(config.read_text())['Alice']
            self.assertEqual(len(saved['versions']), 3)
            for packet, point in zip(data['writes'], saved['version_timeline']):
                self.assertEqual(saved['versions'][point['version_id']]['adapter_id'], packet['body']['config']['adapter_id'])
            for key, value in data['original'].items():
                self.assertEqual(saved['versions'][key], value)
