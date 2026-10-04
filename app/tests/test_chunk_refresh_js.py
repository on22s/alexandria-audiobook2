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
run(source.slice(start,source.indexOf('window.toggleChunkExpand',start)));
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
