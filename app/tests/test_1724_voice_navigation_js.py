"""#1724 guide read-only Voices navigation + DS3006 tab-load boundary.

Real canonical activateTab/loadVoices/queue/collector JS against the real
FastAPI voices routes on an owned localhost socket. The guide's read-only
open must render cards for a clean unconfigured book without writing any
default config to disk; ordinary navigation keeps its default autosave.
No inference, no browser, no model loads.
"""
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.test_support import create_test_api_server

_HERE = Path(__file__).resolve().parent.parent
CORE = _HERE / 'static/js/app-core.js'
GUIDES = _HERE / 'static/js/app-guides.js'

_HARNESS = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const core=fs.readFileSync(process.argv[1],'utf8');
const base=process.argv[2],artifact=process.argv[3];
const helper=core.slice(core.indexOf('function createSerializedSaveQueue('),core.indexOf('// --- API Helpers ---'));
const api=core.slice(core.indexOf('const API = {'),core.indexOf('// --- Setup Tab ---'));
const saves=core.slice(core.indexOf('let _voiceStatusClearTimer ='),core.indexOf('// Auto-save on any change inside the voices list'));
const refresh=core.slice(core.indexOf('async function refreshVoiceMetadata()'),core.indexOf('let _voiceResourcesRefreshedAt'));
const literalStart=core.indexOf('function _isLiteralSourcePreservingChecked()');
const literalEnd=core.indexOf('\n        function ',literalStart+10);
const rosterSlice=core.slice(core.indexOf('const emptyVoiceRoster'),core.indexOf('function ensureVoiceRosterOptions('));
const metaSlice=core.slice(core.indexOf('function getTraitBadgeHtml('),core.indexOf('function getStateVoiceCardsMarkup('));
const cardsSlice=core.slice(core.indexOf('function getStateVoiceCardsMarkup('),core.indexOf('function getSavedAdapterOption('));
const collectSlice=core.slice(core.indexOf('function collectVoiceConfig('),core.indexOf('function onVoiceReadyChange('));
const voiceStateSlice=core.slice(core.indexOf('let _voiceResourcesRefreshedAt'),core.indexOf('function getVoiceSeedRepairMarkup('));
const loadVoicesSlice=core.slice(core.indexOf('async function loadVoices('),core.indexOf('window.selectVoiceVersion'));
const navSlice=core.slice(core.indexOf('function getTabLink('),core.indexOf('function restoreTab('));
function makeElement(id){return {id,children:[],checked:false,value:'',textContent:'',innerHTML:'',listeners:{},dataset:{},style:{},disabled:false,
 appendChild(node){this.children.push(node);return node;},
 replaceChildren(...nodes){this.children=[...nodes];},
 addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);},remove(){},
 classList:{toggle(){},add(){},remove(){},contains(){return false;}},
 querySelectorAll:()=>[],querySelector:()=>null,closest:()=>null,contains:()=>false,};}
const elements={};
for(const id of ['voice-save-status','voice-save-drafts','voices-list','narrator-strategy','voices-ready-count','voices-hide-ready','voices-state-filter','navbarNav']){elements[id]=makeElement(id);}
elements['voices-hide-ready'].checked=false;elements['voices-state-filter'].value='all';
const tabNames=['home','setup','script','voices','editor','audio','designer','casting','training','dataset-builder','preparer','voicelab','reports'];
const tabEls={};for(const name of tabNames){tabEls[name+'-tab']=makeElement(name+'-tab');}
const links=tabNames.map(name=>({dataset:{tab:name},classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){},addEventListener(){}}));
function parseCards(html){
 const chunks=html.split('<div class="card voice-card');
 const cards=[];
 for(let i=1;i<chunks.length;i++){
  const chunk=chunks[i];
  const voice=(chunk.match(/data-voice="([^"]+)"/)||[])[1];
  const version=(chunk.match(/data-version="([^"]*)"/)||[])[1]||'';
  const voiceSel=(chunk.match(/class="form-select voice-select"[^>]*>(.*?)<\/select>/s)||[])[1]||'';
  let voiceVal=(voiceSel.match(/value="([^"]*)"[^>]*selected/)||[])[1]||'';
  const aliasSel=(chunk.match(/alias-select[^>]*>(.*?)<\/select>/s)||[])[1]||'';
  let aliasVal=(aliasSel.match(/value="([^"]*)"[^>]*selected/)||[])[1]||'';
  const ready=/voice-ready[^>]*checked/.test(chunk);
  let styleVal=(chunk.match(/class="form-control character-style"[^>]*value="([^"]*)"/)||[])[1]||'';
  const card={dataset:{voice:voice,version:version},style:{display:''},classList:{toggle(){}},
   querySelectorAll:()=>[],
   querySelector:function(sel){
    if(sel==='.alias-select') return {value:aliasVal};
    if(sel==='.voice-type:checked') return {value:'custom'};
    if(sel==='.voice-select') return {value:voiceVal};
    if(sel==='.character-style') return {value:styleVal};
    if(sel==='.voice-ready') return {checked:ready};
    if(sel==='.voice-seed') return null;
    if(sel==='.voice-presentation') return null;
    return null;}};
  card._set=(sel,val)=>{ if(sel==='.alias-select') aliasVal=val; if(sel==='.voice-select') voiceVal=val; if(sel==='.character-style') styleVal=val; };
  cards.push(card);
 }
 return cards;
}
const document={getElementById:id=>elements[id]||tabEls[id]||null,
 querySelectorAll:selector=>selector==='.nav-link'?links:selector==='.tab-content'?Object.values(tabEls):selector==='.voice-card'?parseCards(elements['voices-list'].innerHTML):[],
 activeElement:null,body:{},createElement:tag=>makeElement('option-'+tag),
 createDocumentFragment:()=>({children:[],appendChild(node){this.children.push(node);return node;}})};
const context={currentTabName:'home',currentBookFilename:'book.txt',_bookSelectionEpoch:1,document,elements,
 fetch:(url,options)=>fetch(new URL(url,base),options),
 localStorage:{length:0,key:()=>null,setItem(){},getItem(){return null;},removeItem(){}},
 window:{addEventListener(){},location:{hash:'#home'},history:{pushState(){},replaceState(){}},_bookSelectionEpoch:1,
  localStorage:{length:0,key:()=>null,setItem(){},getItem(){return null;},removeItem(){}},
  _loraModelsCache:[],_cloneVoicesCache:[],_designedVoicesCache:[],_voicesNames:[],_voicesByName:{},_lineCounts:null,_voiceSuggestions:null},
 escapeHtml:String,showToast:(...args)=>{context.toasts.push(args);},
 rememberTab(){},
 showConfirm:async()=>true,showActionError:(...args)=>{context.actionErrors.push(args);},toasts:[],actionErrors:[],isEditorEditPending:()=>false,
 setTimeout:()=>1,clearTimeout(){},AbortController,console:{error(){},debug(){}},performance:{now:()=>Date.now()},AVAILABLE_VOICES:['Serena','Ryan'],
 getLibraryVoiceReference:()=>null,getTraitBadgeHtml:()=>'',getVoiceCandidateMarkup:()=>'',renderStyleTimeline:()=>'',getSavedAdapterOption:()=>'',disableStaleCardControls:s=>s,getLoraModelsById:()=>new Map(),loadCastLibrary:async()=>{},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},getVoicePanelSnapshot:()=>({bookToken:null,panels:[]}),restoreVoicePanels(){},getVoiceListFocusSnapshot:()=>null,restoreVoiceListFocus(){},getVoiceSeedRepairMarkup:()=>'',renderPersonaRecoveryTargets(){},applyTtsCapabilityGuidance(){},ensureVoiceRosterCache(){},renderVoiceDrafts(){},pollLmStudioStatus(){},loadChunks(){},loadFinalAudio(){},loadDesignedVoices(){},loadLoraDatasets(){},loadLoraModels(){},dsbLoadProjects(){},loadPreparerOutputs(){},loadVoicelabConfig(){},voicelabInspect(){},refreshVoicelabHealth(){},loadReports(){},loadCheckpoints(){},loadRunHistory(){},refreshBenchmarkStatus(){},bootstrap:{Collapse:{getOrCreateInstance:()=>({hide(){}})}}};
vm.createContext(context);
vm.runInContext(helper+api+saves+refresh+core.slice(literalStart,literalEnd)+rosterSlice+metaSlice+cardsSlice+collectSlice+voiceStateSlice+loadVoicesSlice+navSlice,context);
context.elements=elements;context.document=document;
context.API=vm.runInContext('API',context);
context.window._bookSelectionEpoch=1;
function persistedRaw(){return fs.readFileSync(artifact,'utf8');}
function persisted(){return JSON.parse(persistedRaw());}
async function untilSettled(){
 const deadline=Date.now()+15000;
 let pending=null;
 while(Date.now()<deadline&&!pending){pending=vm.runInContext('loadVoices.pending',context);if(!pending){await new Promise(r=>setTimeout(r,10));}}
 assert(pending,'activateTab must start an owned loadVoices load');
 await Promise.race([pending,new Promise((_,reject)=>setTimeout(()=>reject(new Error('owned loadVoices did not settle within its bounded deadline')),15000))]);
 assert(vm.runInContext('_voiceSaveSnapshot',context),'voices must finish loading');
 await new Promise(r=>setTimeout(r,20));
}
let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));
(async()=>{
'''

_TAIL = r'''
finished=true;console.log(JSON.stringify({actionErrors:context.actionErrors.map(e=>[e[0],e[1]&&e[1].message]),cardsHtml:context.elements['voices-list'].innerHTML.length}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class VoiceNavigationTests(unittest.TestCase):
    def _run(self, scenario, voice_config):
        from fastapi import FastAPI
        from routers import voices as voices_router_module

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            script = root / 'annotated_script.json'
            script.write_text(json.dumps([
                {'speaker': 'ALICE', 'text': 'Synthetic alpha line.'},
                {'speaker': 'BOB', 'text': 'Synthetic beta line.'}]))
            config_path = root / 'voice_config.json'
            config_path.write_text(json.dumps(voice_config))
            (root / 'state.json').write_text(json.dumps(
                {'active_book_id': 'book', 'book_generation': 'one'}))
            stack.enter_context(patch.object(
                voices_router_module, 'SCRIPT_PATH', str(script)))
            stack.enter_context(patch.object(
                voices_router_module, 'VOICE_CONFIG_PATH', str(config_path)))
            app = FastAPI()

            @app.get('/api/voice_design/list')
            async def _design():
                return []

            @app.get('/api/clone_voices/list')
            async def _clone():
                return []

            @app.get('/api/lora/models')
            async def _lora():
                return []

            @app.get('/api/voice_library')
            async def _library():
                return {'casts': [], 'current_characters': []}

            app.include_router(voices_router_module.router)
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            server = create_test_api_server(app)
            thread = threading.Thread(
                target=lambda: server.run(sockets=[sock]), daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started, 'owned HTTP server must start')
                code = _HARNESS + scenario + _TAIL
                result = subprocess.run(
                    ['node', '-e', code, str(CORE),
                     f'http://127.0.0.1:{port}', str(config_path)],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode,
                                 result.stdout + result.stderr)
                summary = json.loads(
                    result.stdout.strip().splitlines()[-1])
                return summary, config_path.read_bytes()
            finally:
                server.should_exit = True
                thread.join(5)
                sock.close()
                self.assertFalse(thread.is_alive(),
                                 'owned HTTP server must stop')

    def test_guide_read_only_voices_open_writes_no_defaults(self):
        scenario = r'''
vm.runInContext('activateTab("voices", true, {readOnlyVoices:true})',context);
await untilSettled();
assert(context.elements['voices-list'].innerHTML.includes('ALICE'),'cards must render for the clean book');
assert(context.elements['voices-list'].innerHTML.includes('BOB'),'cards must render for the clean book');
assert.strictEqual(persistedRaw(),JSON.stringify({}),'guide open must not write default configs to disk');
assert.strictEqual(context.actionErrors.length,0);
'''
        summary, raw = self._run(scenario, {})
        self.assertEqual(b'{}', raw)
        self.assertGreater(summary['cardsHtml'], 0)

    def test_ordinary_voices_navigation_still_autosaves_defaults(self):
        scenario = r'''
vm.runInContext('activateTab("voices", true, null)',context);
await untilSettled();
await vm.runInContext('flushVoiceSaves()',context);
const saved=persisted();
assert(saved.ALICE&&saved.BOB,'ordinary navigation must keep the existing default autosave');
'''
        summary, raw = self._run(scenario, {})
        saved = json.loads(raw)
        self.assertIn('ALICE', saved)
        self.assertIn('BOB', saved)

    def test_tab_load_rejections_visible_except_benign_supersession(self):
        # DS3006 at the real activateTab boundary with a stubbed loader:
        # benign supersession is silent, a real failure is shown.
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const navSlice=source.slice(source.indexOf('function getTabLink('),source.indexOf('function restoreTab('));
const links=['voices'].map(name=>({dataset:{tab:name},classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){},addEventListener(){}}));
const tabs={'voices-tab':{style:{display:'none'}}};
const errors=[];
let mode='superseded';
const ctx={window:{location:{hash:''},history:{pushState(){},replaceState(){}},addEventListener(){}},
 localStorage:{getItem:()=>null,setItem(){}},
 document:{querySelectorAll:s=>s==='.nav-link'?links:Object.values(tabs),getElementById:id=>id==='navbarNav'?{classList:{contains:()=>false}}:tabs[id]},
 showActionError:(action,error,recovery)=>errors.push([action,error&&error.message,recovery]),
 rememberTab(){},
 loadVoices:()=>mode==='superseded'?Promise.reject(Object.assign(new Error('A newer voice load superseded this request; existing edits were preserved.'),{voiceLoadObsolete:true})):mode==='refreshing'?Promise.reject(Object.assign(new Error('Active book changed while refreshing voices; existing edits were preserved.'),{voiceLoadObsolete:true})):mode==='server_mismatch'?Promise.reject(new Error('Active book changed while loading voices; voice edits were preserved.')):mode==='named'?Promise.reject(new Error("Speaker 'superseded' has a malformed voice entry (not an object); restore the saved voice configuration before continuing.")):mode==='http_prefix'?Promise.reject(Object.assign(new Error('Active book changed while loading voices; voice edits were preserved.'),{status:503})):mode==='marked_http'?Promise.reject(Object.assign(new Error('A newer voice load superseded this request; existing edits were preserved.'),{voiceLoadObsolete:true,status:503})):Promise.reject(new Error('snapshot exploded')),
 pollLmStudioStatus(){},loadChunks(){},loadFinalAudio(){}};
vm.createContext(ctx);vm.runInContext(navSlice,ctx);
let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));
(async()=>{
 ctx.activateTab('voices',false);
 await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
 assert.deepEqual(errors,[],'benign supersession must not surface as a failure');
 mode='refreshing';ctx.activateTab('voices',false);
 await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
 assert.deepEqual(errors,[],'benign refreshing-voices book change must stay silent');
 await assert.rejects(ctx.loadVoices(),/Active book changed while refreshing voices/,'await-callers must still receive the truthful rejection');
 mode='server_mismatch';ctx.activateTab('voices',false);
 await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
 assert.strictEqual(errors.length,1,'unmarked server book mismatch must stay visible even with obsolete text');
 mode='named';ctx.activateTab('voices',false);
 await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
 assert.strictEqual(errors.length,2,'genuine error containing superseded as a speaker name must stay visible');
 assert(errors[1][1].includes("'superseded'"));
 mode='http_prefix';ctx.activateTab('voices',false);
 await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
 assert.strictEqual(errors.length,3,'status-bearing API error starting with an obsolete prefix must stay visible');
 assert(errors[2][1].includes('Active book changed while loading voices'));
 mode='marked_http';ctx.activateTab('voices',false);
  await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
  assert.strictEqual(errors.length,4,'HTTP-status error must stay visible even with an accidental obsolete marker');
  mode='real';ctx.activateTab('voices',false);
  await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
  assert.strictEqual(errors.length,5,'real load failure must be visible');
 assert.strictEqual(errors[4][0],'Could not load voices');
 assert(errors[4][1].includes('snapshot exploded'));
 finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(CORE)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
