"""Actual Training loaders retain useful lists and reject stale/malformed replies."""
import unittest
from tests import test_training_ui_contract as ui

class TrainingListRecoveryJsTests(unittest.TestCase):
    run_js = ui.TrainingUiContractTests.run_js

    def test_upload_pending_selection_changes_and_failures_follow_native_lifecycle(self):
        self.run_js(r"""
let finished=false;process.on('beforeExit',()=>assert(finished,'upload assertions must finish'));
ctx.FormData=class{append(k,v){assert.strictEqual(k,'file');this.file=v;}};
ctx.API=vm.runInContext('API',ctx);
let fetches=0,resolveUpload,rejectUpload;
ctx.fetch=()=>{fetches++;return new Promise((resolve,reject)=>{resolveUpload=resolve;rejectUpload=reject;});};
const file={name:'new.zip'},input=element('lora-dataset-file'),button=element('btn-lora-upload'),select=element('lora-dataset-select');
button.innerHTML='Upload ZIP';
for(const scenario of ['success','changed','list-failure','transport','refused']){
 input.files=[file];input.value='chosen';select.value='old';
 ctx.API.get=async()=>{if(scenario==='list-failure')throw Error('refresh failed');return [{dataset_id:'old',sample_count:1},{dataset_id:'new',sample_count:2}];};
 const before=fetches,pending=ctx.uploadLoraDataset();assert(button.disabled);assert.strictEqual(button.textContent,'Uploading…');assert.match(element('lora-upload-status').textContent,/Uploading new.zip/);
 await ctx.uploadLoraDataset();assert.strictEqual(fetches,before+1);
 if(scenario==='changed'){select.value='other';input.files=[{name:'next.zip'}];input.value='next';}
 if(scenario==='transport'){rejectUpload(Error('Failed to fetch'));}
 else resolveUpload({ok:scenario!=='refused',status:400,json:async()=>scenario==='refused'?{detail:'invalid ZIP'}:{dataset_id:'new',sample_count:2}});
 await pending;assert.strictEqual(button.disabled,false);assert.strictEqual(button.innerHTML,'Upload ZIP');
 if(scenario==='success'){assert.strictEqual(select.value,'new');assert.match(element('lora-upload-status').textContent,/uploaded and selected/);assert.strictEqual(input.value,'');}
 if(scenario==='changed'){assert.strictEqual(select.value,'other');assert.strictEqual(input.value,'next');assert.match(element('lora-upload-status').textContent,/uploaded.*select it/);}
 if(scenario==='list-failure'){assert.strictEqual(select.value,'old');assert.match(element('lora-upload-status').textContent,/uploaded.*select it/);assert.match(element('lora-datasets-refresh-status').textContent,/Could not load/);}
 if(scenario==='transport'||scenario==='refused'){assert.strictEqual(select.value,'old');assert.strictEqual(input.value,'chosen');assert.match(element('lora-upload-status').textContent,/not confirmed|refused/);}
}
finished=true;
""")

    def test_failures_malformed_and_stale_replies_keep_lists_and_selections(self):
        self.run_js(r'''
let finished=false;process.on('beforeExit',()=>assert(finished));ctx.API=vm.runInContext('API',ctx);
for(const [kind,loader,path,select] of [['datasets','loadLoraDatasets','/api/lora/datasets','lora-dataset-select'],['models','loadLoraModels','/api/lora/models','lora-test-adapter']]){
const rows=id=>kind==='datasets'?[{dataset_id:id,sample_count:4}]:[{id,name:id}];
ctx.API.get=async url=>url===path?rows('old'):{backups:[]};await ctx[loader]();element(select).value='old';const before=element('lora-'+kind+'-list').innerHTML;const cache=ctx._loraModelsCache;
for(const value of ['error',{},[null]]){ctx.API.get=async url=>{if(url!==path)return {backups:[]};if(value==='error')throw Error('fixture API failure');return value;};await ctx[loader]();assert.strictEqual(element('lora-'+kind+'-list').innerHTML,before);assert.strictEqual(element(select).value,'old');assert(element('lora-'+kind+'-refresh-status').textContent.includes('Could not load'));assert.strictEqual(element('lora-'+kind+'-refresh-retry').hidden,false);assert.strictEqual(element('lora-'+kind+'-refresh-retry').disabled,false);if(kind==='models')assert.strictEqual(ctx._loraModelsCache,cache);}
let resolveOld;ctx.API.get=url=>url===path?new Promise(resolve=>resolveOld=resolve):Promise.resolve({backups:[]});const old=ctx[loader]();ctx.API.get=async url=>url===path?rows('new'):{backups:[]};await ctx[loader]();const current=element('lora-'+kind+'-list').innerHTML;resolveOld(rows('stale'));await old;assert.strictEqual(element('lora-'+kind+'-list').innerHTML,current);assert(current.includes('new'));assert(!current.includes('stale'));assert.strictEqual(element('lora-'+kind+'-refresh-status').textContent,'');assert.strictEqual(element('lora-'+kind+'-refresh-retry').hidden,true);
}finished=true;
''')
