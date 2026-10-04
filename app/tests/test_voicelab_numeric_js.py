"""Preserve numeric UI intent and exercise authoritative API range admission."""
from unittest.mock import patch
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voicelab as v
from tests import test_voicelab_run_state_js as run_state


class VoicelabNumericJsTests(unittest.TestCase):
    run_js = run_state.VoicelabRunStateJsTests.run_js

    def test_explicit_numbers_are_preserved_for_server_validation(self):
        self.run_js(r"""
const fields={'vl-target-loss':'target_loss','vl-max-epochs':'max_epochs','vl-lora-r':'lora_r','vl-candidate-checkpoints':'candidate_checkpoints'};
for(const [id,key] of Object.entries(fields)){
 const before=element(id).value;
 for(const value of ['0','-1','2000']){element(id).value=value;assert.strictEqual(ctx.getVoicelabRequest()[key],Number(value),id+' must not default or clamp');}
 element(id).value=before;
}
element('vl-target-loss').value=' 0.125 ';element('vl-max-epochs').value='100';element('vl-lora-r').value='1024';element('vl-candidate-checkpoints').value='0';
const body=ctx.getVoicelabRequest();assert.strictEqual(body.target_loss,.125);assert.strictEqual(body.max_epochs,100);assert.strictEqual(body.lora_r,1024);assert.strictEqual(body.candidate_checkpoints,0);
""")

    def test_bad_numeric_input_blocks_start_before_http_and_reports_field(self):
        self.run_js(r"""
let calls=0,toasts=[];ctx.API.post=async()=>{calls++;throw Error('unexpected request');};ctx.showToast=message=>toasts.push(message);
const labels={'vl-target-loss':'Target loss','vl-max-epochs':'Max epochs','vl-lora-r':'LoRA rank','vl-candidate-checkpoints':'Eval candidates'};
for(const [id,label] of Object.entries(labels)){
 const before=element(id).value;
 const values=['','  ','12oops','NaN','Infinity','-Infinity','1e999'];
 if(id!=='vl-target-loss'){values.push('1.5');}
 for(const value of values){element(id).value=value;const n=toasts.length;await ctx.startVoicelab();assert.strictEqual(calls,0,id+' '+value);assert.strictEqual(toasts.length,n+1);assert(toasts.at(-1).includes(label));assert(!element('btn-vl-start').disabled);}
 element(id).value=before;
}
""")

    def test_default_values_match_html_and_reach_preflight_unchanged(self):
        self.run_js(r"""
const html=fs.readFileSync(process.argv[2],'utf8');
for(const id of ['vl-target-loss','vl-max-epochs','vl-lora-r','vl-candidate-checkpoints']){
 const tag=html.match(new RegExp('<input[^>]*id="'+id+'"[^>]*>'))[0];element(id).value=tag.match(/value="([^"]+)"/)[1];}
let received;ctx.API.post=async(path,body)=>{assert.strictEqual(path,'/api/voicelab/preflight');received=body;return {ready:false,stages:['name']};};
await ctx.startVoicelab();assert.strictEqual(received.target_loss,4.15);assert.strictEqual(received.max_epochs,6);assert.strictEqual(received.lora_r,64);assert.strictEqual(received.candidate_checkpoints,2);
""")


class VoicelabNumericApiTests(unittest.TestCase):
    def test_out_of_range_numbers_fail_before_preflight_or_start_work(self):
        app = FastAPI()
        app.include_router(v.router)
        invalid = {'target_loss': [0, -1, 101], 'max_epochs': [0, -1, 101, 1.5],
                   'lora_r': [0, -1, 1025, 1.5], 'candidate_checkpoints': [-1, 3, 1.5]}
        with patch.object(v, '_load_voicelab_config', side_effect=AssertionError('config read')), \
             patch.object(v, '_build_voicelab_preflight', side_effect=AssertionError('preflight built')), \
             patch.object(v, 'check_global_gpu_lock', side_effect=AssertionError('GPU admission')), \
             TestClient(app) as client:
            for key, values in invalid.items():
                for value in values:
                    for endpoint in ('preflight', 'start'):
                        with self.subTest(key=key, value=value, endpoint=endpoint):
                            response = client.post('/api/voicelab/' + endpoint,
                                                   json={'stages': ['name'], key: value})
                            self.assertEqual(422, response.status_code, response.text)
                            self.assertIn(key, str(response.json()['detail']))
        for values in ({'target_loss': .01, 'max_epochs': 1, 'lora_r': 1, 'candidate_checkpoints': 0},
                       {'target_loss': 100, 'max_epochs': 100, 'lora_r': 1024, 'candidate_checkpoints': 2}):
            self.assertEqual(values, {key: getattr(v.VoiceLabRequest(**values), key) for key in values})
