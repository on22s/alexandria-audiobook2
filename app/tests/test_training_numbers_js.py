"""Actual training request builder must preserve numeric inputs for API validation."""
import json
from pathlib import Path
import subprocess
import unittest

from pydantic import ValidationError
from routers.lora import LoraTrainingRequest

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-training.js'


class TrainingNumericRequestJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import lora
        cls.api = FastAPI()
        cls.api.include_router(lora.router)
        cls.client = TestClient(cls.api)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def assert_api_rejects_before_gpu_claim(self, request, field):
        from unittest.mock import patch
        with patch('routers.lora.claim_gpu_task') as claim, \
             patch('routers.lora.run_process') as process:
            response = self.client.post('/api/lora/train', json=request)
        self.assertEqual(422, response.status_code, response.text)
        self.assertTrue(any(error['loc'] == ['body', field] for error in response.json()['detail']))
        claim.assert_not_called()
        process.assert_not_called()

    def run_inputs(self, inputs):
        script=r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const handler=source.slice(source.indexOf('window.startLoraTraining ='),source.indexOf('function pollLoraTraining('));
const fields={
 'lora-adapter-name':'voice','lora-dataset-select':'dataset',
 'lora-epochs':'5','lora-lr':'1e-6','lora-batch-size':'1',
 'lora-rank':'32','lora-alpha':'128','lora-grad-accum':'8','lora-language':'english',
 ...JSON.parse(process.argv[2])};
const elements=Object.fromEntries(Object.entries(fields).map(([id,value])=>[id,{value,style:{},disabled:false}]));
const posts=[],toasts=[],polls=[];
const context={window:{},document:{getElementById(id){return elements[id]||(elements[id]={style:{},innerHTML:'',disabled:false});}},
 API:{post:async(path,data)=>{posts.push({path,data:JSON.parse(JSON.stringify(data))});return{};}},
 showToast(message,kind){toasts.push({message,kind});},pollLoraTraining(value){polls.push(value);}};
vm.runInNewContext(handler,context);
context.window.startLoraTraining().then(()=>{console.log(JSON.stringify({posts,toasts,polls}));}).catch(error=>{console.error(error);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',script,str(SOURCE),json.dumps(inputs)],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual([],report['toasts'])
        self.assertEqual(1,len(report['posts']))
        self.assertEqual('/api/lora/train',report['posts'][0]['path'])
        return report['posts'][0]['data']

    def test_exponent_notation_survives_all_integer_controls(self):
        mapping={'lora-epochs':'epochs','lora-batch-size':'batch_size','lora-rank':'lora_r',
                 'lora-alpha':'lora_alpha','lora-grad-accum':'gradient_accumulation_steps'}
        for element,field in mapping.items():
            with self.subTest(field=field):
                request=self.run_inputs({element:'1e1'})
                self.assertEqual(10,request[field])
                self.assertEqual(10,getattr(LoraTrainingRequest(**request),field))

    def test_zero_blank_malformed_and_fractional_integer_inputs_do_not_start_with_defaults(self):
        mapping={'lora-epochs':'epochs','lora-batch-size':'batch_size','lora-rank':'lora_r',
                 'lora-alpha':'lora_alpha','lora-grad-accum':'gradient_accumulation_steps','lora-lr':'lr'}
        for element,field in mapping.items():
            for raw in ('0','', '4garbage', 'NaN','Infinity'):
                with self.subTest(field=field,raw=raw):
                    request=self.run_inputs({element:raw})
                    self.assertIn(request[field],(0,None))
                    with self.assertRaises(ValidationError):
                        LoraTrainingRequest(**request)
                    self.assert_api_rejects_before_gpu_claim(request,field)
            if field!='lr':
                with self.subTest(field=field,raw='1.5'):
                    request=self.run_inputs({element:'1.5'})
                    self.assertEqual(1.5,request[field])
                    with self.assertRaises(ValidationError):
                        LoraTrainingRequest(**request)
                    self.assert_api_rejects_before_gpu_claim(request,field)

    def test_normal_values_and_learning_rate_exponent_are_unchanged(self):
        request=self.run_inputs({'lora-lr':'5e-6'})
        parsed=LoraTrainingRequest(**request)
        self.assertEqual(5,parsed.epochs)
        self.assertEqual(5e-6,parsed.lr)
        self.assertEqual(1,parsed.batch_size)
        self.assertEqual(32,parsed.lora_r)
        self.assertEqual(128,parsed.lora_alpha)
        self.assertEqual(8,parsed.gradient_accumulation_steps)


class TrainingUploadPlaybackJsTests(unittest.TestCase):
    def run_js(self, scenario):
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const mode=process.argv[2];
const upload=source.slice(source.indexOf('let loraDatasetUploadPending ='),source.indexOf('window.deleteLoraDataset ='));
const preview=source.slice(source.indexOf('window.playLoraPreview ='),source.indexOf('window.testLoraModel ='));
const toasts=[],posts=[],unhandled=[];
process.on('unhandledRejection',error=>unhandled.push(error.message));
const file={name:mode},input={files:[file],value:mode};
const button={innerHTML:'Play',disabled:false,title:'Generate preview',classList:{replace(a,b){posts.push([a,b]);}}};
let refreshes=0,resolvePlay,rejectPlay;
const played=new Promise((resolve,reject)=>{resolvePlay=resolve;rejectPlay=reject;});
const context={window:{},document:{getElementById(id){return id==='lora-dataset-file'?input:button;}},
 FormData:class{constructor(){this.fields=[];}append(key,value){this.fields.push([key,value]);}},
 fetch:async(path,options)=>{assert.strictEqual(options.body.fields[0][1],file);posts.push({path,method:options.method});return{ok:true,json:async()=>({dataset_id:'fixture',sample_count:3})};},
 loadLoraDatasets(){refreshes++;},showToast(message,kind){toasts.push({message,kind});},
 API:{post:async(path,data)=>{posts.push({path,data});return{audio_url:'/fixture.wav'};}},
 Audio:class{constructor(url){posts.push({url});}play(){return played;}},Date};
const guidanceCore=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInNewContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm('))+upload+preview,context);
(async()=>{
 if(mode.startsWith('play-')){
  const pending=context.window.playLoraPreview('voice/name');
  await new Promise(resolve=>setImmediate(resolve));
  const before={disabled:button.disabled,html:button.innerHTML};
  if(mode==='play-failure'){rejectPlay(new Error('Playback denied'));}else{resolvePlay();}
  await pending;await new Promise(resolve=>setImmediate(resolve));
  console.log(JSON.stringify({toasts,posts,unhandled,button,before}));
 }else{
  await context.window.uploadLoraDataset();
  console.log(JSON.stringify({toasts,posts,refreshes,input}));
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(SOURCE), scenario], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_playback_rejection_is_visible_and_button_recovers(self):
        result = self.run_js('play-failure')
        self.assertTrue(result['before']['disabled'])
        self.assertEqual([], result['unhandled'])
        self.assertEqual(1, len(result['toasts']))
        self.assertEqual('error', result['toasts'][0]['kind'])
        self.assertIn('Check that the adapter has preview audio', result['toasts'][0]['message'])
        self.assertIn('Details: Playback denied', result['toasts'][0]['message'])
        self.assertFalse(result['button']['disabled'])
        self.assertEqual('Play', result['button']['innerHTML'])
        self.assertEqual('Generate preview', result['button']['title'])
        self.assertFalse(any(isinstance(post,list) for post in result['posts']))

    def test_successful_playback_updates_cached_button_only_after_play_starts(self):
        result = self.run_js('play-success')
        self.assertTrue(result['before']['disabled'])
        self.assertEqual([], result['toasts'])
        self.assertEqual([], result['unhandled'])
        self.assertFalse(result['button']['disabled'])
        self.assertEqual('Play', result['button']['innerHTML'])
        self.assertEqual('Play preview', result['button']['title'])
        self.assertIn(['btn-outline-secondary','btn-outline-success'], result['posts'])
        self.assertEqual('/api/lora/preview/voice%2Fname', result['posts'][0]['path'])


class ZipUploadSuffixJsTests(unittest.TestCase):
    def test_actual_upload_handler_preserves_file_and_accepts_case_insensitive_suffix(self):
        program = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const elements = {};
const requests = [], messages = [], refreshes = [];
const context = {
 console, document:{getElementById:id => elements[id] || (elements[id]={value:'',innerHTML:''})},
 FormData:class {constructor(){this.parts=[];} append(name,file){this.parts.push({name,file});}},
 showToast:(...args)=>messages.push(args), escapeHtml:value=>value,
 API:{get:async url=>{refreshes.push(url);return[];}},
 fetch:async (url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({dataset_id:'MixedCase',sample_count:1})};}
};
context.window=context;vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
 const outcomes=[];
 for(const name of ['dataset.zip','DATASET.ZIP','MixedCase.ZiP','dataset.zip.exe','dataset.tar','datasetzip']){
  const file={name,size:1};
  const input=elements['lora-dataset-file']={files:[file],value:name};
  const before=requests.length;
  await context.uploadLoraDataset();
  outcomes.push({name,uploaded:requests.length>before});
  if(requests.length>before){
   const request=requests[before];
   assert.strictEqual(request.url,'/api/lora/upload_dataset');assert.strictEqual(request.options.method,'POST');
   assert.strictEqual(request.options.body.parts.length,1);
   assert.strictEqual(request.options.body.parts[0].name,'file');
   assert.strictEqual(request.options.body.parts[0].file,file);
   assert.strictEqual(file.name,name);assert.strictEqual(input.value,'');
  }else{assert.strictEqual(input.value,name);}
 }
 assert.deepStrictEqual(outcomes.map(value=>value.uploaded),[true,true,true,false,false,false]);
 assert.strictEqual(requests.length,3);assert.strictEqual(refreshes.length,3);
 assert.strictEqual(messages.filter(message=>message[1]==='success').length,3);
 assert.strictEqual(messages.filter(message=>message[1]==='warning').length,3);
 elements['lora-dataset-file']={files:[],value:''};await context.uploadLoraDataset();
 assert.strictEqual(requests.length,3);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', program, str(SOURCE)], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
