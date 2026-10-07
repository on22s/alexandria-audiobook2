"""Execute actual chunk rendering and request coordination on changing snapshots."""
import os
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(os.environ.get('EDITOR_POLL_JS_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const fields={},timers=new Map(),errors=[],updates=[];let timerId=0,draws=0,playing=false;
const element=id=>fields[id]||(fields[id]={value:'',style:{},children:[]});
const body=element('chunks-table-body');let html='';
Object.defineProperty(body,'innerHTML',{get:()=>html,set:value=>{html=value;draws++;body.children=[...value.matchAll(/<tr data-id="(\d+)"/g)].map(match=>({dataset:{id:match[1]},children:[{},{},{},{},{},{}]}));if(!body.children.length){body.children=[{children:[{}]}];}}});
const ctx={window:null,currentBookFilename:'A',console:{log:()=>{},error:(...args)=>errors.push(args)},document:{getElementById:element,querySelector:()=>null,querySelectorAll:()=>[]},API:{},Date,
 setTimeout:(callback,delay)=>{const id=++timerId;timers.set(id,{callback,delay});return id;},clearTimeout:id=>timers.delete(id),
 refreshEditorIntegrity:async()=>{},invalidateEditorIntegrity:()=>{},isAudioPlaying:()=>playing,buildSpeakerSelect:chunk=>chunk.speaker||'',_driftBadge:()=>'',_driftKey:drift=>JSON.stringify(drift),updateChunkRow:chunk=>updates.push(chunk.id)};ctx.window=ctx;
vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
run(source.slice(source.indexOf('function escapeHtml('),source.indexOf('// Parse a numeric input')));
run(source.slice(source.indexOf('let isPlayingSequence ='),source.indexOf('function buildSpeakerSelect(')));
const start=source.includes('function ensureChunkRefresh(')?source.indexOf('function ensureChunkRefresh('):source.indexOf('async function loadChunks(');
run(source.slice(start,source.indexOf('window.toggleChunkExpand =',start)));
// Delivery UI is exercised with native route replies in its own suite.
ctx.refreshDeliveryReview=async()=>null;
let getFixture;
Object.defineProperty(ctx.API,'get',{set:value=>{getFixture=value;},get:()=>async url=>{
 const data=await getFixture(url);
 if(!Array.isArray(data)){return data;}
 const old=run('cachedChunks');
 const full=!url.includes('?revision=')||old.length!==data.length||data.some((row,i)=>old[i].id!==row.id);
 return {revision:'fixture-'+JSON.stringify(data),full,total:data.length,running_count:data.filter(row=>row.status==='generating').length,changed_ids:data.map(row=>row.id),chunks:data};
}});
const chunk=(id,status='pending')=>({id,status,text:'line '+id,speaker:'Narrator'});
const ids=()=>body.children.map(row=>row.dataset.id);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const turn=()=>new Promise(resolve=>setImmediate(resolve));
'''


class ChunkRefreshJsTests(unittest.TestCase):
    def test_cancel_reconciles_optimistic_rows_without_forcing_redraw_or_losing_controls(self):
        self.run_js(r'''
const updateStart=source.indexOf('function updateChunkRow(chunk)');run(source.slice(updateStart,source.indexOf('function ensureChunkRefresh(',updateStart)));
const cancelStart=source.indexOf('window.cancelRender =');run(source.slice(cancelStart,source.indexOf('window.startRender =',cancelStart)));
const batchStart=source.indexOf('async function _runBatchRender(');run(source.slice(batchStart,source.indexOf('window.renderAll =',batchStart)));
const server=[{...chunk(1),uid:'one'},{...chunk(2),uid:'two'}];ctx.API.get=async url=>({revision:'v1',full:!url.includes('?revision='),total:2,running_count:0,changed_ids:[],chunks:url.includes('?revision=')?[]:server});await ctx.loadChunks(true);
const rows=body.children;for(const row of rows){const set=new Set();row.classList={add:k=>set.add(k),remove:k=>set.delete(k),contains:k=>set.has(k),toggle(k,on){if(on){set.add(k);}else{set.delete(k);}}};row.badge={innerText:'pending',insertAdjacentHTML(){}};row.draft={value:'Unsubmitted draft'};const action={querySelector:selector=>selector==='button'?{}:null};row.querySelector=selector=>selector==='.badge'?row.badge:selector==='.chunk-actions'?action:null;}
ctx.applyDriftFilter=()=>{};ctx.document.querySelector=selector=>rows.find(row=>selector.includes('data-id="'+row.dataset.id+'"'))||null;
ctx.document.querySelectorAll=selector=>selector==='#chunks-table-body tr.table-info'?rows.filter(row=>row.classList.contains('table-info')):[];
ctx.ensureEditorRenderSnapshot=async()=>run('cachedChunks');ctx.API.post=async()=>({});ctx.showToast=()=>{};let poll;ctx._startPolling=(key,fetch,options)=>poll=options;
ctx.cancelTask=async(path,options)=>{assert.equal(path,'/api/cancel_audio');await options.onSuccess();};
await ctx._runBatchRender('/fixture',false,{label:'fixture',describeStart:()=>''});assert(rows.every(row=>row.badge.innerText==='generating'&&row.classList.contains('table-info')));const count=draws;
await ctx.cancelRender();assert(rows.every(row=>row.badge.innerText==='pending'&&!row.classList.contains('table-info')));assert.strictEqual(draws,count);assert.strictEqual(body.children,rows);assert(rows.every(row=>row.draft.value==='Unsubmitted draft'));
await poll.onDone({audio:{running:false},chunks:server});assert(rows.every(row=>row.badge.innerText==='pending'));assert(run('cachedChunks').every(row=>row.status==='pending'));
''')

    def test_batch_completion_uses_starting_book_and_row_uids(self):
        self.run_js(r'''
const bookStart=source.indexOf("let currentBookFilename = ''");run(source.slice(bookStart,source.indexOf('function getCurrentBookName(',bookStart)));
run(source.slice(source.indexOf('function getBatchOutcome('),source.indexOf('function pollReviewBatch()')));
const cancelStart=source.indexOf('window.cancelRender =');run(source.slice(cancelStart,source.indexOf('window.startRender =',cancelStart)));
const batchStart=source.indexOf('async function _runBatchRender(');run(source.slice(batchStart,source.indexOf('window.renderAll =',batchStart)));
const toasts=[],drifts=[];let poll;ctx.showToast=(text,tone)=>toasts.push({text,tone});ctx.runDriftCheck=rows=>drifts.push(Array.from(rows));ctx.showActionError=error=>{throw Error(error);};ctx.API.post=async()=>({});
ctx._startPolling=(key,fetch,options)=>{assert.equal(key,'render_batch');poll={fetch,...options};};
for(const change of ['book','uid','none']){
toasts.length=drifts.length=0;ctx.applyCurrentBookFilename('book-A.json');ctx.ensureEditorRenderSnapshot=async()=>[1,2,3,4,5,6].map(id=>({...chunk(id),uid:'A-'+id}));
await ctx._runBatchRender('/fixture',false,{label:'fixture',describeStart:()=>''});assert(poll);
if(change==='book'){ctx.applyCurrentBookFilename('book-B.json');}
const finished=[1,2,3,4,5,6].map(id=>({...chunk(id,'done'),uid:(change==='none'?'A-':'B-')+id}));
await poll.onDone({audio:{running:false},chunks:finished});
if(change==='book'){assert.equal(drifts.length,0);assert(!toasts.some(row=>row.text.startsWith('Batch complete')));assert(toasts.some(row=>row.text.includes('previous book')));}
else if(change==='uid'){assert.equal(drifts.length,0);assert(toasts.some(row=>row.text.includes('0 succeeded')&&row.text.includes('6 unfinished')));}
else{assert.deepStrictEqual(drifts,[[1,2,3,4,5,6]]);assert.equal(toasts[0].text,'Batch complete: 6 succeeded, 0 failed, 0 cancelled, 0 unfinished');}
}
''')

    def test_expanded_completion_updates_generated_action_container_without_replacing_pause_label(self):
        self.run_js(r'''
const updateStart=source.indexOf('function updateChunkRow(chunk)');run(source.slice(updateStart,source.indexOf('function ensureChunkRefresh(',updateStart)));
const expandStart=source.indexOf('window.toggleChunkExpand =');run(source.slice(expandStart,source.indexOf('window.insertChunkAfter =',expandStart)));
ctx.applyDriftFilter=()=>{};ctx.generateChunk=()=>{};ctx.document.createElement=()=>({style:{}});
function classes(text){const set=new Set(text.split(/\s+/));return{contains:k=>set.has(k),add:k=>set.add(k),remove:k=>set.delete(k),toggle(k,force){const on=force===undefined?!set.has(k):force;if(on){set.add(k);}else{set.delete(k);}return on;}};}
for(const expanded of [false,true]){
ctx.API.get=async()=>({revision:'generating',full:true,total:1,changed_ids:[1],chunks:[{...chunk(1,'generating'),uid:'one',pause_after:500}]});await ctx.loadChunks(true);
const markup=body.innerHTML,pauseClass=markup.match(/class="(chunk-pause-row[^"]*)"/)[1],actionClass=markup.match(/<div class="([^"]*align-items-center gap-2[^"]*)"/)[1];
let pauseAudio=false,actionAudio='',progress={},button=null;const pauseLabel={textContent:'Pause after (ms):'};Object.defineProperty(pauseLabel,'outerHTML',{set:value=>{pauseAudio=value.includes('<audio');}});
const noAudio={};Object.defineProperty(noAudio,'outerHTML',{set:value=>{actionAudio=value;}});
const pause={classList:classes(pauseClass),querySelector:selector=>selector==='.text-muted'?pauseLabel:null};
const action={classList:classes(actionClass),querySelector:selector=>selector==='button'?button:selector==='.progress'?progress:selector==='.text-muted'?noAudio:null,replaceChild(newNode,oldNode){assert.strictEqual(oldNode,progress);button=newNode;progress=null;}};
const badge={insertAdjacentHTML(){}};const row={classList:classes('chunk-row'),querySelector(selector){if(selector==='.badge'){return badge;}if(selector==='.drift-badge'){return null;}return[pause,action].find(node=>node.classList.contains(selector.slice(1)))||null;},querySelectorAll(selector){return selector==='.chunk-pause-row'?[pause]:[];}};
ctx.document.querySelector=()=>row;if(expanded){ctx.toggleChunkExpand({closest:()=>row});assert(pause.classList.contains('d-flex'));}
assert(ctx.updateChunkRow({...chunk(1,'done'),uid:'one',audio_path:'synthetic.wav'}));assert.strictEqual(badge.innerText,'done');assert(button&&button.innerHTML.includes('Gen'));assert.strictEqual(progress,null);assert(actionAudio.includes('src="/synthetic.wav?t='));assert.strictEqual(pauseAudio,false);assert.strictEqual(pauseLabel.textContent,'Pause after (ms):');
}
''')

    def test_managed_refresh_does_not_paint_prior_book_response_while_new_read_waits(self):
        self.run_js(r'''
const bookStart=source.indexOf("let currentBookFilename = ''");run(source.slice(bookStart,source.indexOf('function getCurrentBookName(',bookStart)));
const reads=[];ctx.API.get=()=>{const gate=deferred();reads.push(gate);return gate.promise;};
const snapshot=book=>({revision:'revision-'+book,full:true,total:1,running_count:0,changed_ids:[1],chunks:[{...chunk(1),uid:'uid-'+book,text:'Synthetic text from '+book}]});
ctx.applyCurrentBookFilename('book-A.json');const a=ctx.ensureChunkRefresh(true);
ctx.applyCurrentBookFilename('book-B.json');const b=ctx.ensureChunkRefresh(true);assert.strictEqual(reads.length,1);assert.strictEqual(a,b);
reads[0].resolve(snapshot('A'));await turn();assert.strictEqual(reads.length,2);
assert(!body.innerHTML.includes('Synthetic text from A'));assert.strictEqual(run('cachedChunks.length'),0);assert.strictEqual(run('chunkSnapshotRevision'),null);
reads[1].resolve(snapshot('B'));await Promise.all([a,b]);assert(body.innerHTML.includes('Synthetic text from B'));assert.strictEqual(run('cachedChunks[0].uid'),'uid-B');assert.strictEqual(run('chunkSnapshotRevision'),'revision-B');
''')

    def test_forced_redraw_retains_only_same_chunk_and_audio(self):
        self.run_js(r'''let reply={revision:'v1',full:true,total:1,changed_ids:[1],chunks:[{...chunk(1,'done'),uid:'one',audio_path:'audio.wav'}]};ctx.API.get=async()=>reply;await ctx.loadChunks(true);
let replaced=0,plays=0,paused=0,metadata,removed=0;const player={dataset:{id:'1'},paused:false,ended:false,currentTime:8,isConnected:true,setAttribute:(key,value)=>{assert.equal(key,'onplay');assert.equal(value,'stopOthers(2)');},addEventListener:(event,fn)=>{assert.equal(event,'loadedmetadata');metadata=fn;},removeEventListener:()=>removed++,load:()=>{player.currentTime=0;metadata();},play:()=>{plays++;return Promise.resolve();},pause:()=>paused++};
ctx.document.querySelectorAll=()=>[player];body.querySelector=()=>({replaceWith:p=>{assert.equal(p,player);replaced++;player.currentTime=0;}});
reply={revision:'v2',full:true,total:1,changed_ids:[2],chunks:[{...chunk(2,'done'),uid:'one',audio_path:'audio.wav'}]};await ctx.loadChunks(true);assert.equal(replaced,1);assert.equal(plays,1);assert.equal(player.currentTime,8);assert.equal(player.dataset.id,'2');
for(const change of ['uid','path','missing','book']){replaced=plays=0;run("cachedChunks=[{id:2,uid:'one',audio_path:'audio.wav'}]");reply={revision:'next',full:true,total:1,changed_ids:[2],chunks:[{...chunk(2,'done'),uid:change==='uid'?'other':'one',audio_path:change==='path'?'replacement.wav':change==='missing'?null:'audio.wav'}]};ctx.API.get=async()=>{if(change==='book'){ctx.currentBookFilename='B';}return reply;};ctx.currentBookFilename='A';await ctx.loadChunks(true);assert.equal(replaced,0,change);assert.equal(plays,0,change);}
''')

    def test_saved_local_edit_refetches_server_normalization_when_revision_is_unchanged(self):
        self.run_js(r'''run(source.slice(source.indexOf('const pendingChunkEdits ='),source.indexOf('async function ensureEditorRenderSnapshot()')));
const urls=[];const saved={...chunk(1),uid:'one',pause_after:0};ctx.API.get=async url=>{urls.push(url);return {revision:'v1',full:!url.includes('?revision='),total:1,changed_ids:url.includes('?revision=')?[]:[1],chunks:url.includes('?revision=')?[]:[saved]};};
await ctx.loadChunks();ctx.API.post=async()=>saved;await ctx.applyChunkEdits(1,{pause_after:-10});await ctx.loadChunks();
assert.strictEqual(urls.at(-1),'/api/chunks/status');assert.strictEqual(run('cachedChunks[0].pause_after'),0);''')

    def test_duplicate_delta_ids_refuse_before_replacing_cached_rows_and_recover_full(self):
        self.run_js(r'''const urls=[];let reply={revision:'v1',full:true,total:2,changed_ids:[1,2],chunks:[{...chunk(1),uid:'one'},{...chunk(2),uid:'two'}]};
ctx.API.get=async url=>{urls.push(url);return reply;};await ctx.loadChunks();
reply={revision:'v2',full:false,total:2,changed_ids:[1,1],chunks:[{...chunk(1,'done'),uid:'one'},{...chunk(1,'error'),uid:'one'}]};
await assert.rejects(ctx.ensureChunkRefresh(),/refresh required/);assert.strictEqual(run('chunkSnapshotRevision'),null);assert.strictEqual(run('cachedChunks[0].status'),'pending');
reply={revision:'v3',full:true,total:2,changed_ids:[1,2],chunks:[{...chunk(1,'done'),uid:'one'},{...chunk(2),uid:'two'}]};await ctx.loadChunks();assert.strictEqual(urls.at(-1),'/api/chunks/status');assert.strictEqual(run('cachedChunks[0].status'),'done');''')

    def test_reused_id_with_changed_uid_refuses_delta_and_preserves_old_identity(self):
        self.run_js(r'''let reply={revision:'v1',full:true,total:1,changed_ids:[1],chunks:[{...chunk(1),uid:'original'}]};
ctx.API.get=async()=>reply;await ctx.loadChunks();reply={revision:'v2',full:false,total:1,changed_ids:[1],chunks:[{...chunk(1,'done'),uid:'replacement'}]};
await assert.rejects(ctx.ensureChunkRefresh(),/refresh required/);assert.strictEqual(run('chunkSnapshotRevision'),null);assert.strictEqual(run('cachedChunks[0].uid'),'original');assert.strictEqual(run('cachedChunks[0].status'),'pending');''')

    def test_full_snapshot_redraws_same_ids_and_status_when_text_changes(self):
        self.run_js(r'''let reply={revision:'v1',full:true,total:1,changed_ids:[1],chunks:[{...chunk(1),uid:'one',text:'Original text'}]};
ctx.API.get=async()=>reply;await ctx.loadChunks();const before=draws;reply={revision:'v2',full:true,total:1,changed_ids:[1],chunks:[{...chunk(1),uid:'one',text:'Changed server text'}]};
await ctx.loadChunks();assert.ok(draws>before);assert.match(body.innerHTML,/Changed server text/);assert.strictEqual(run('cachedChunks[0].text'),'Changed server text');''')

    def run_js(self, code):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_same_length_replacements_reorders_and_playback_restore_correct_rows(self):
        self.run_js(r'''
let snapshot=[chunk(1),chunk(2)];ctx.API.get=async()=>snapshot;await ctx.loadChunks();assert.deepStrictEqual(ids(),['1','2']);
snapshot=[chunk(3),chunk(4)];await ctx.loadChunks();assert.deepStrictEqual(ids(),['3','4']);
snapshot=[chunk(4),chunk(3)];await ctx.loadChunks();assert.deepStrictEqual(ids(),['4','3']);
const before=draws;snapshot=[chunk(4,'done'),chunk(3)];await ctx.loadChunks();assert.strictEqual(draws,before);assert.deepStrictEqual(updates,[4]);
playing=true;snapshot=[chunk(8),chunk(9)];await ctx.loadChunks();assert.deepStrictEqual(ids(),['4','3']);playing=false;await ctx.loadChunks();assert.deepStrictEqual(ids(),['8','9']);
snapshot=[];await ctx.loadChunks();assert.match(body.innerHTML,/No chunks found/);assert.strictEqual(run('cachedChunks.length'),0);assert.deepStrictEqual(errors,[]);
''')

    def test_overlapping_reads_are_serialized_and_forced_freshness_is_not_lost(self):
        self.run_js(r'''
const first=deferred(),second=deferred();let reads=0,active=0,maxActive=0;
ctx.API.get=async()=>{const gate=++reads===1?first:second;maxActive=Math.max(maxActive,++active);try{return await gate.promise;}finally{active--;}};
const a=ctx.loadChunks(),b=ctx.loadChunks(),c=ctx.loadChunks(true);assert.strictEqual(reads,1);
first.resolve([chunk(1)]);await turn();assert.strictEqual(reads,2);playing=true;second.resolve([chunk(2)]);await Promise.all([a,b,c]);assert.strictEqual(maxActive,1);assert.strictEqual(reads,2);assert.deepStrictEqual(ids(),['2']);
ctx.API.get=async()=>{throw Error('fixture unavailable');};await ctx.loadChunks();assert.strictEqual(errors.length,1);
playing=false;ctx.API.get=async()=>[chunk(3,'generating')];await ctx.loadChunks();assert.deepStrictEqual(ids(),['3']);assert.strictEqual(timers.size,1);await ctx.loadChunks();assert.strictEqual(timers.size,1);
ctx.API.get=async()=>[chunk(3,'done')];await ctx.loadChunks();assert.strictEqual(timers.size,0);
''')

    def test_failed_refresh_keeps_rows_and_shows_persistent_retry_until_recovery(self):
        self.run_js(r'''
ctx.API.get=async()=>[chunk(1)];await ctx.loadChunks();const before=body.innerHTML;ctx.API.get=async()=>{throw Error('<internal exception>');};await ctx.loadChunks();assert.strictEqual(body.innerHTML,before);assert.strictEqual(run('cachedChunks[0].id'),1);assert.match(element('chunk-load-status').innerHTML,/Could not load editor chunks/);assert.match(element('chunk-load-status').innerHTML,/loadChunks\(true\)/);assert(!element('chunk-load-status').innerHTML.includes('<internal exception>'));
ctx.API.get=async()=>[chunk(2)];await ctx.loadChunks(true);assert.strictEqual(element('chunk-load-status').innerHTML,'');assert.deepStrictEqual(ids(),['2']);
''')

    def test_actual_batch_poll_fetches_and_renders_once_without_second_timer(self):
        self.run_js(r'''
let poll,reads=0,statusReads=0,drift=0;ctx.ensureEditorRenderSnapshot=async()=>[chunk(1)];ctx.showToast=()=>{};ctx.showConfirm=async()=>true;
ctx.cancelRender=()=>run('isRenderingAll=false');ctx.runDriftCheck=()=>drift++;ctx._startPolling=(key,fetch,options)=>{assert.strictEqual(key,'render_batch');poll={fetch,options};};ctx.API.post=async()=>({});
ctx.API.get=async url=>{if(url==='/api/status/audio'){statusReads++;return {running:reads===0};}assert.ok(url.startsWith('/api/chunks/status'));reads++;return [chunk(1,reads===1?'generating':'done')];};
run(source.slice(source.indexOf('function getBatchOutcome('),source.indexOf('function pollReviewBatch()')));
run(source.slice(source.indexOf('async function _runBatchRender('),source.indexOf('window.renderAll =')));
await ctx._runBatchRender('/fixture',false,{label:'fixture',describeStart:()=>''});
let data=await poll.fetch();if(poll.options.onTick){await poll.options.onTick(data);}assert.strictEqual(reads,1);assert.strictEqual(timers.size,0);assert.strictEqual(poll.options.doneCheck(data),false);assert.deepStrictEqual(ids(),['1']);
data=await poll.fetch();if(poll.options.onTick){await poll.options.onTick(data);}assert.strictEqual(poll.options.doneCheck(data),true);await poll.options.onDone(data);assert.strictEqual(reads,2);assert.strictEqual(statusReads,2);assert.strictEqual(timers.size,0);assert.strictEqual(drift,1);assert.strictEqual(run('cachedChunks[0].status'),'done');
ctx.API.get=async()=>{throw Error('offline');};await assert.rejects(poll.fetch(),/offline/);assert.deepStrictEqual(errors,[]);
''')

    def test_compact_deltas_keep_existing_rows_and_forced_refresh_gets_full_snapshot(self):
        self.run_js(r'''const urls=[];let reply={revision:'v1',full:true,total:2,changed_ids:[1,2],chunks:[chunk(1,'generating'),chunk(2)]};
ctx.API.get=async url=>{urls.push(url);return reply;};await ctx.loadChunks();const before=draws;
reply={revision:'v1',full:false,total:2,changed_ids:[],chunks:[]};await ctx.loadChunks();
assert.strictEqual(draws,before);assert.deepStrictEqual(updates,[]);assert.deepStrictEqual(ids(),['1','2']);assert.strictEqual(timers.size,1);
reply={revision:'v2',full:false,total:2,changed_ids:[1],chunks:[{...chunk(1,'done'),audio_path:'one.wav'}]};await ctx.loadChunks();
assert.strictEqual(draws,before);assert.deepStrictEqual(updates,[1]);assert.strictEqual(run('cachedChunks[0].audio_path'),'one.wav');assert.strictEqual(timers.size,0);
assert.strictEqual(urls[0],'/api/chunks/status');assert.strictEqual(urls[1],'/api/chunks/status?revision=v1');
reply={revision:'v3',full:true,total:2,changed_ids:[1,2],chunks:[{...chunk(1,'done'),text:'Edited text'},chunk(2)]};await ctx.loadChunks(true);
assert.strictEqual(urls.at(-1),'/api/chunks/status');assert.ok(draws>before);assert.match(body.innerHTML,/Edited text/);
reply={revision:'bad',full:false,total:2,changed_ids:[77],chunks:[chunk(77)]};await assert.rejects(ctx.ensureChunkRefresh(),/refresh required/);
assert.strictEqual(run('chunkSnapshotRevision'),null);assert.strictEqual(run('cachedChunks[0].text'),'Edited text');
''')

    def test_full_snapshot_preserves_uid_owned_drafts_focus_and_expansion(self):
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const document={body:{},activeElement:null,getElementById:id=>id==='chunks-table-body'?tbody:null,querySelectorAll:()=>[],createElement:()=>({})};document.activeElement=document.body;
function classes(){const values=new Set();return{contains:k=>values.has(k),add:k=>values.add(k),remove:k=>values.delete(k),toggle(k){if(values.has(k)){values.delete(k);return false;}values.add(k);return true;}};}
function control(value){return{value:String(value),options:[{value:'Narrator'}],appendChild(option){this.options.push(option);},style:{},scrollHeight:100,selectionStart:1,selectionEnd:4,selectionDirection:'forward',focus(){document.activeElement=this;},setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d;}};}
let rows=[],markup='',data;
const tbody={get children(){return rows;},querySelectorAll:()=>rows,querySelector:selector=>rows.find(row=>selector.includes('"'+row.dataset.id+'"'))};
Object.defineProperty(tbody,'innerHTML',{get:()=>markup,set(value){markup=value;document.activeElement=document.body;rows=[];
for(const m of value.matchAll(/<tr data-id="(\d+)"[\s\S]*?<\/tr>/g)){
 const html=m[0],fields={'.chunk-text':control(html.match(/chunk-text[^>]*>([\s\S]*?)<\/textarea>/)[1]),'.chunk-instruct':control(html.match(/chunk-instruct[^>]*>([\s\S]*?)<\/textarea>/)[1]),'.chunk-pause-after':control(html.match(/chunk-pause-after[^>]*value="([^"]*)"/)[1]),'select':control('Narrator')};
 const pause={classList:classes()},btn={innerHTML:'',closest:()=>row};
 const row={dataset:{id:m[1]},classList:classes(),fields,querySelector:selector=>selector==='.chunk-toggle-btn'?btn:fields[selector],querySelectorAll:selector=>selector==='.chunk-text, .chunk-instruct'?[fields['.chunk-text'],fields['.chunk-instruct']]:selector==='.chunk-pause-row'?[pause]:[],pause,btn};rows.push(row);
}}});
const c={window:null,document,currentBookFilename:'book-A',API:{get:async()=>data,post:async()=>({})},invalidateEditorIntegrity(){},refreshEditorIntegrity:async()=>{},Date,escapeHtml:String,buildSpeakerSelect:()=>'<select></select>',_driftBadge:()=>'',_driftKey:()=>'',isAudioPlaying:()=>false,updateChunkRow(){},setTimeout:()=>1,clearTimeout(){},applyDriftFilter(){},generateChunk(){}};c.window=c;vm.createContext(c);
function run(code){return vm.runInContext(code,c);}let a=source.indexOf('let isPlayingSequence =');run(source.slice(a,source.indexOf('function buildSpeakerSelect(',a)));
a=source.indexOf('async function refreshChunkSnapshot(');run(source.slice(a,source.indexOf('window.insertChunkAfter =',a)));
a=source.indexOf('const pendingChunkEdits =');run(source.slice(a,source.indexOf('async function flushChunkEdits()',a)));
const original=[{id:1,uid:'first',text:'First saved line',instruct:'',speaker:'Narrator',status:'pending'},{id:2,uid:'second',text:'Second saved line',instruct:'old style',pause_after:50,speaker:'Narrator',status:'generating'}];
function reply(chunks){data={revision:'r'+Math.random(),full:true,total:chunks.length,changed_ids:chunks.map(row=>row.id),chunks};}
(async()=>{
for(const change of ['none','uid','book']){
 c.currentBookFilename='book-A';reply(original);await c.refreshChunkSnapshot(true);
 const row=rows[1],field=row.fields['.chunk-text'];field.value='Unsaved second-row draft';field.focus();field.selectionStart=3;field.selectionEnd=12;
 row.fields['.chunk-instruct'].value='Unsaved delivery';row.fields['.chunk-pause-after'].value='900';row.fields.select.value='Custom speaker';c.toggleChunkExpand(row.btn);
 rows[0].fields['.chunk-text'].value='Submitted first row';await c.applyChunkEdits(1,{text:'Submitted first row'});assert.strictEqual(run('chunkSnapshotRevision'),null);
 if(change==='book'){c.currentBookFilename='book-B';}
 const updated=[{...original[0],text:'Server-normalized first row'},{...original[1],id:7,uid:change==='uid'?'replacement':'second',text:'Server second row'}];reply(updated);await c.refreshChunkSnapshot(false);
 assert.strictEqual(rows[0].fields['.chunk-text'].value,'Server-normalized first row');assert.strictEqual(run('cachedChunks')[1].text,'Server second row');
 const restored=rows[1];if(change==='none'){assert.strictEqual(restored.fields['.chunk-text'].value,'Unsaved second-row draft');assert.strictEqual(document.activeElement,restored.fields['.chunk-text']);assert.strictEqual(document.activeElement.selectionStart,3);assert.strictEqual(document.activeElement.selectionEnd,12);assert(restored.classList.contains('expanded'));assert.strictEqual(restored.fields['.chunk-instruct'].value,'Unsaved delivery');assert.strictEqual(restored.fields['.chunk-pause-after'].value,'900');assert.strictEqual(restored.fields.select.value,'Custom speaker');assert(restored.fields.select.options.some(option=>option.value==='Custom speaker'));}
 else{assert.strictEqual(restored.fields['.chunk-text'].value,'Server second row');assert(!restored.classList.contains('expanded'));assert.strictEqual(document.activeElement,document.body);}
}
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
