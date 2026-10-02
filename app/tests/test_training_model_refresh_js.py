"""Actual model refresh request ordering, backup indexing, and favorite DOM ownership."""
import unittest
from tests import test_training_ui_contract as ui


class TrainingModelRefreshTests(unittest.TestCase):
    run_js = ui.TrainingUiContractTests.run_js

    def test_independent_requests_start_before_either_settles_and_backup_failure_is_optional(self):
        self.run_js(r"""
const requests=[],pending={};
ctx.API=vm.runInContext('API',ctx);
ctx.API.get=path=>{requests.push(path);return new Promise((resolve,reject)=>{pending[path]={resolve,reject};});};
const refresh=ctx.loadLoraModels();
assert.deepStrictEqual(requests,['/api/lora/models','/api/lora/backups']);
pending['/api/lora/backups'].reject(new Error('offline'));
pending['/api/lora/models'].resolve([{id:'a',name:'A'}]);await refresh;
assert.strictEqual(ctx._loraModelsCache[0].rollback_backup,null);
assert(element('lora-models-list').innerHTML.includes('A'));
const html=element('lora-models-list').innerHTML;
const second=ctx.loadLoraModels();pending['/api/lora/models'].reject(new Error('models offline'));
pending['/api/lora/backups'].resolve({backups:[]});await second;
assert.strictEqual(element('lora-models-list').innerHTML,html);
""")

    def test_backup_index_reads_each_identity_once_preserves_first_match_and_inputs(self):
        self.run_js(r"""
let reads=0;const backups=[];
for(let i=0;i<200;i++){backups.push({get adapter_id(){reads++;return 'id'+i;},size_bytes:i});}
backups.push({get adapter_id(){reads++;return 'id199';},size_bytes:999});
const models=Array.from({length:200},(_,i)=>Object.freeze({id:'id'+i,name:'Model'+i}));
ctx.API=vm.runInContext('API',ctx);ctx.API.get=async path=>path.endsWith('/models')?models:{backups};
await ctx.loadLoraModels();
assert.strictEqual(reads,backups.length,'backup identity reads='+reads);
assert.strictEqual(ctx._loraModelsCache[199].rollback_backup.size_bytes,199);
assert.strictEqual(ctx._loraModelsCache[0].rollback_backup,backups[0]);
assert(models.every(m=>!Object.hasOwn(m,'rollback_backup')));
""")

    def test_favorite_updates_existing_button_and_cache_without_gets_or_replacing_table(self):
        self.run_js(r"""
const id='voice #日本語',icon={className:'far fa-star'},classes=new Set(['text-muted']);
const star={dataset:{adapterId:id},disabled:false,title:'Mark as favorite',classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name)},querySelector:()=>icon};
const other={dataset:{adapterId:'other'}};ctx.document.querySelectorAll=()=>[star,other];
const original=Object.freeze({id,name:'Voice',favorite:false});ctx._loraModelsCache=[original];
const table=element('lora-models-list');table.innerHTML='keep table and focus';
ctx.API=vm.runInContext('API',ctx);ctx.API.get=async()=>{throw new Error('unexpected GET');};
let settle,calls=0;ctx.API.post=(path,body)=>{calls++;assert.strictEqual(path,'/api/voice_library/favorites/'+encodeURIComponent(id));return new Promise(r=>settle=r);};
const pending=ctx.onToggleFavoriteAdapter(id,star);assert(star.disabled);
await ctx.onToggleFavoriteAdapter(id,star);assert.strictEqual(calls,1);
settle({favorite:true});await pending;
assert.strictEqual(table.innerHTML,'keep table and focus');assert(!star.disabled);
assert(classes.has('text-warning'));assert(!classes.has('text-muted'));assert.strictEqual(icon.className,'fas fa-star');
assert(star.title.startsWith('Favorite'));assert(ctx._loraModelsCache[0].favorite);assert(!original.favorite);
ctx.API.post=async()=>({favorite:false});await ctx.onToggleFavoriteAdapter(id,star);
assert(!ctx._loraModelsCache[0].favorite);assert.strictEqual(icon.className,'far fa-star');assert.strictEqual(star.title,'Mark as favorite');
ctx.API.post=async()=>{throw new Error('denied');};await ctx.onToggleFavoriteAdapter(id,star);
assert(!star.disabled);assert(!ctx._loraModelsCache[0].favorite);assert(toasts.at(-1)[0].includes('denied'));assert.strictEqual(table.innerHTML,'keep table and focus');
""")
