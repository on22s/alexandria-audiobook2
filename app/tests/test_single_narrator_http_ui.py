"""Real controller-to-route HTTP regression tests for #1724 single narrator.

Unlike tests.test_single_voice_js (which mocks API.get/post and persists to a
JS array), these tests execute the actual canonical API object, save queue,
refreshVoiceMetadata, loadVoices/createVoiceCard, collectVoiceConfig and the
ordinary save path from the real app JS against the actual FastAPI
voices.router routes on an owned localhost ephemeral socket. The server, its
thread, and the Node child are all owned and shut down per test.

Resource endpoints (/api/voice_design/list, /api/clone_voices/list,
/api/lora/models, /api/voice_library) serve deterministic empty fixture
lists so no private voice/model catalog is consulted. Voice
snapshot/preview/save are the real existing FastAPI routes. Synthetic text
only; no inference, no weights, no browser.

Source binding: AA2_1724_CORE_PATH / AA2_1724_SINGLE_VOICE_PATH may override
the app JS paths (defaults: current app JS). Run once against the frozen
pre-repair baseline (../temp/1724-rbac-old-check/app-core-old.js and
app-single-voice-old.js) to observe the behavioral failures, then against
the current UI files to observe green. The harness slices every function it
uses by name, so old sources load without a harness-level failure; the
scenarios only call functions present in both revisions.
"""
import json
import os
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
_DEFAULT_CORE = _HERE / 'static/js/app-core.js'
_DEFAULT_VOICE = _HERE / 'static/js/app-single-voice.js'
CORE = Path(os.environ.get('AA2_1724_CORE_PATH') or _DEFAULT_CORE)
VOICE = Path(os.environ.get('AA2_1724_SINGLE_VOICE_PATH') or _DEFAULT_VOICE)


class _LoseReplyAfterCommit:
    """ASGI wrapper: forward the first guarded save to the real app, let it
    run to completion (the route commits), then discard its reply and answer
    502 instead. This is a 502-after-commit unknown-response boundary: the
    client cannot tell whether the write landed. It never throws before the
    real route commits and never emulates the commit in JS."""

    def __init__(self, app):
        self.app = app
        self.save_requests = 0
        self._swallowed = False

    async def __call__(self, scope, receive, send):
        if (scope.get('type') == 'http' and scope.get('method') == 'POST'
                and scope.get('path') == '/api/voice_config/save'
                and not self._swallowed):
            self._swallowed = True
            self.save_requests += 1
            captured = []

            async def sink(message):
                captured.append(message)

            await self.app(scope, receive, sink)
            # Route has completed (committed). Replace its reply with a 502.
            body = json.dumps({'detail': 'Bad Gateway'}).encode()
            await send({'type': 'http.response.start', 'status': 502,
                        'headers': [(b'content-type', b'application/json'),
                                    (b'content-length', str(len(body)).encode())]})
            await send({'type': 'http.response.body', 'body': body})
            return
        if (scope.get('type') == 'http' and scope.get('method') == 'POST'
                and scope.get('path') == '/api/voice_config/save'):
            self.save_requests += 1
        await self.app(scope, receive, send)


_NODE_HARNESS = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const core=fs.readFileSync(process.argv[1],'utf8'),voice=fs.readFileSync(process.argv[2],'utf8');
const base=process.argv[3],artifact=process.argv[4];
const helper=core.slice(core.indexOf('function createSerializedSaveQueue('),core.indexOf('// --- API Helpers ---'));
const api=core.slice(core.indexOf('const API = {'),core.indexOf('// --- Setup Tab ---'));
const saves=core.slice(core.indexOf('let _voiceStatusClearTimer ='),core.indexOf('// Auto-save on any change inside the voices list'));
const refresh=core.slice(core.indexOf('async function refreshVoiceMetadata()'),core.indexOf('let _voiceResourcesRefreshedAt'));
const literalStart=core.indexOf('function _isLiteralSourcePreservingChecked()');
assert.notStrictEqual(literalStart,-1,'canonical literal accessor must exist');
const literalEnd=core.indexOf('\n        function ',literalStart+10);
const literalSlice=core.slice(literalStart,literalEnd);
const rosterSlice=core.slice(core.indexOf('const emptyVoiceRoster'),core.indexOf('function ensureVoiceRosterOptions('));
const metaSlice=core.slice(core.indexOf('function getTraitBadgeHtml('),core.indexOf('function getStateVoiceCardsMarkup('));
const cardsSlice=core.slice(core.indexOf('function getStateVoiceCardsMarkup('),core.indexOf('function getSavedAdapterOption('));
const collectSlice=core.slice(core.indexOf('function collectVoiceConfig('),core.indexOf('function onVoiceReadyChange('));
const voiceStateSlice=core.slice(core.indexOf('let _voiceResourcesRefreshedAt'),core.indexOf('function getVoiceSeedRepairMarkup('));
const loadVoicesSlice=core.slice(core.indexOf('async function loadVoices('),core.indexOf('window.selectVoiceVersion'));
function makeElement(id){return {id,children:[],checked:false,value:'',textContent:'',innerHTML:'',listeners:{},dataset:{},style:{},disabled:false,
 appendChild(node){this.children.push(node);return node;},
 replaceChildren(...nodes){this.children=[...nodes];
  if(this.value && !this.children.some(child=>child.value===this.value)){this.value=this.children[0]?.value ?? '';}},
 addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);},
 fire(type){for(const fn of (this.listeners[type] || [])){fn();}},remove(){},
 classList:{toggle(){},add(){},remove(){},contains(){return false;}},
 querySelectorAll:()=>[],querySelector:()=>null,closest:()=>null,contains:()=>false,};}
const elements={};
for(const id of ['source-preview-text','source-preview-meta','source-preview-status','source-preview-confirm','single-voice-source','script-literal-source-preserving','voice-save-status','voice-save-drafts','voices-list','narrator-strategy','voices-ready-count','voices-hide-ready','voices-state-filter']){
  elements[id]=makeElement(id);
}
elements['voices-hide-ready'].checked=false;elements['voices-state-filter'].value='all';
const document={getElementById:id=>elements[id]||null,querySelectorAll:()=>[],activeElement:null,body:{},
 createElement:tag=>makeElement('option-'+tag),createDocumentFragment:()=>({children:[],appendChild(node){this.children.push(node);return node;}})};
let confirmAnswer=true;
const context={currentBookFilename:'book.txt',_bookSelectionEpoch:1,document,elements,
 fetch:(url,options)=>fetch(new URL(url,base),options),
 window:{addEventListener(){},_bookSelectionEpoch:1,
  localStorage:{length:0,key:()=>null,setItem(){},getItem(){return null;},removeItem(){}},
  _loraModelsCache:[],_cloneVoicesCache:[],_designedVoicesCache:[],_voicesNames:[],_voicesByName:{},_lineCounts:null,_voiceSuggestions:null},
 escapeHtml:String,showToast:(...args)=>{context.toasts.push(args);},confirmMessages:[],
 showConfirm:async message=>{context.confirmMessages.push(message);return confirmAnswer;},
 showActionError:(...args)=>{context.actionErrors.push(args);},toasts:[],actionErrors:[],isEditorEditPending:()=>false,
 setTimeout:()=>1,clearTimeout(){},AbortController,console:{error(){},debug(){}},performance:{now:()=>Date.now()},AVAILABLE_VOICES:[],
 getLibraryVoiceReference:()=>null,getTraitBadgeHtml:()=>'',getVoiceCandidateMarkup:()=>'',renderStyleTimeline:()=>'',getSavedAdapterOption:()=>'',disableStaleCardControls:s=>s,getLoraModelsById:()=>new Map(),loadCastLibrary:async()=>{},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},saveVoicesDebounced:()=>{},renderVoiceSuggestions(){},getVoicePanelSnapshot:()=>({bookToken:null,panels:[]}),restoreVoicePanels(){},getVoiceListFocusSnapshot:()=>null,restoreVoiceListFocus(){},getVoiceSeedRepairMarkup:()=>'',renderPersonaRecoveryTargets(){},applyTtsCapabilityGuidance(){},
 ensureVoiceRosterCache(){},renderVoiceDrafts(){}};
vm.createContext(context);
vm.runInContext(helper+api+saves+refresh+literalSlice+rosterSlice+metaSlice+cardsSlice+collectSlice+voiceStateSlice+loadVoicesSlice+voice,context);
context.elements=elements;context.document=document;
context.API=vm.runInContext('API',context);
context.voiceSaveQueue=vm.runInContext('voiceSaveQueue',context);
context.window._bookSelectionEpoch=1;
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
function installCards(cards){ document.querySelectorAll=function(selector){return selector==='.voice-card'?cards:[];}; }
function persisted(){ return JSON.parse(fs.readFileSync(artifact,'utf8')); }
let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));
(async()=>{
'''

_NODE_TAIL = r'''
finished=true;console.log(JSON.stringify({actionErrors:context.actionErrors.map(e=>[e[0],e[1]&&e[1].message,e[2]]),toasts:context.toasts.length,dirty:context.voiceSaveQueue.isDirty()}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class SingleNarratorHttpUiTests(unittest.TestCase):
    def _run_scenario(self, scenario_js, voice_config, *, lose_reply=False):
        from fastapi import FastAPI
        from routers import voices as voices_router_module

        core_path, voice_path = str(CORE), str(VOICE)
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
            wrapper = _LoseReplyAfterCommit(app) if lose_reply else None
            asgi_app = wrapper if wrapper is not None else app

            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            server = create_test_api_server(asgi_app)
            thread = threading.Thread(
                target=lambda: server.run(sockets=[sock]), daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started, 'owned HTTP server must start')
                code = _NODE_HARNESS + scenario_js + _NODE_TAIL
                result = subprocess.run(
                    ['node', '-e', code, core_path, voice_path,
                     f'http://127.0.0.1:{port}', str(config_path)],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode,
                                 result.stdout + result.stderr)
                summary = json.loads(
                    result.stdout.strip().splitlines()[-1])
                persisted_config = json.loads(config_path.read_text())
                return summary, persisted_config, wrapper
            finally:
                server.should_exit = True
                thread.join(5)
                sock.close()
                self.assertFalse(thread.is_alive(),
                                 'owned HTTP server must stop')

    def test_bulk_apply_rerenders_and_later_edit_does_not_revert(self):
        scenario = r'''
await vm.runInContext('loadVoices(true)',context);
const initialHtml=context.elements['voices-list'].innerHTML;
const initialCards=parseCards(initialHtml);
assert.strictEqual(initialCards.length,2,'initial render must produce both cards');
assert.strictEqual(initialCards.find(function(c){return c.dataset.voice==='ALICE'}).querySelector('.voice-select').value,'Serena');
assert.strictEqual(initialCards.find(function(c){return c.dataset.voice==='BOB'}).querySelector('.voice-select').value,'Ryan');
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
await context.previewSingleVoiceBook();
let saved=persisted();
assert.strictEqual(saved.BOB.voice,'Serena','bulk apply must persist BOB=Serena over real HTTP');
assert.strictEqual(saved.BOB.ready,false,'bulk apply must clear BOB ready');
const rerenderedHtml=context.elements['voices-list'].innerHTML;
const rerendered=parseCards(rerenderedHtml);
assert.strictEqual(rerendered.find(function(c){return c.dataset.voice==='BOB'}).querySelector('.voice-select').value,'Serena','actual card HTML must now select Serena');
installCards(rerendered);
const aliceCard=rerendered.find(function(c){return c.dataset.voice==='ALICE'});
aliceCard._set('.character-style','edited synthetic style');
vm.runInContext('saveVoicesDebounced()',context);
await vm.runInContext('flushVoiceSaves()',context);
saved=persisted();
assert.strictEqual(saved.BOB.voice,'Serena','ordinary collector save must not revert BOB');
assert.strictEqual(saved.ALICE.character_style,'edited synthetic style','chosen edit must be saved');
assert.strictEqual(saved.ALICE.seed,'7','unrelated ALICE seed must be preserved');
assert.strictEqual(saved.BOB.seed,'3','unrelated BOB seed must be preserved');
assert.strictEqual(saved.BOB.ready,false);
'''
        summary, persisted_config, _ = self._run_scenario(scenario, {
            'ALICE': {'type': 'custom', 'voice': 'Serena', 'seed': '7',
                      'character_style': 'calm', 'description': 'Calm synthetic narrator.'},
            'BOB': {'type': 'custom', 'voice': 'Ryan', 'seed': '3',
                    'character_style': 'brave', 'description': 'Calm synthetic narrator.'},
        })
        self.assertEqual('Serena', persisted_config['BOB']['voice'])
        self.assertEqual('edited synthetic style',
                         persisted_config['ALICE']['character_style'])

    def test_legacy_alias_roundtrip_through_real_save(self):
        scenario = r'''
await vm.runInContext('loadVoices(true)',context);
let html=context.elements['voices-list'].innerHTML;
let cards=parseCards(html);
const bobCard=()=>cards.find(function(c){return c.dataset.voice==='BOB'});
assert.strictEqual(bobCard().querySelector('.alias-select').value,'ALICE','rendered card must show legacy alias selected');
installCards(cards);
vm.runInContext('saveVoicesDebounced()',context);
await vm.runInContext('flushVoiceSaves()',context);
let saved=persisted();
assert(saved.BOB.alias_of==='ALICE'||saved.BOB.alias==='ALICE','effective alias must survive an unrelated real save');
assert.strictEqual(saved.BOB.seed,'3','unrelated seed must survive');
cards=parseCards(context.elements['voices-list'].innerHTML);
installCards(cards);
bobCard()._set('.alias-select','');
vm.runInContext('saveVoicesDebounced()',context);
await vm.runInContext('flushVoiceSaves()',context);
saved=persisted();
assert(!saved.BOB.alias_of && !saved.BOB.alias,'selecting None must clear both alias keys on disk');
'''
        summary, persisted_config, _ = self._run_scenario(scenario, {
            'ALICE': {'type': 'custom', 'voice': 'Serena', 'seed': '7',
                      'description': 'Calm synthetic narrator.'},
            'BOB': {'type': 'custom', 'voice': 'Ryan', 'seed': '3',
                    'description': 'Calm synthetic narrator.', 'alias': 'ALICE'},
        })
        self.assertFalse(persisted_config['BOB'].get('alias_of'))
        self.assertFalse(persisted_config['BOB'].get('alias'))
        from tts import get_resolved_speaker
        self.assertEqual('BOB', get_resolved_speaker('BOB', persisted_config))

    def test_legacy_alias_resolves_to_target_after_real_save(self):
        # Kept separate so the TTS resolution of the *preserved* alias is
        # asserted on the on-disk state produced by the roundtrip above in
        # its preserved phase; this test re-runs that phase and resolves.
        scenario = r'''
await vm.runInContext('loadVoices(true)',context);
let cards=parseCards(context.elements['voices-list'].innerHTML);
assert.strictEqual(cards.find(function(c){return c.dataset.voice==='BOB'}).querySelector('.alias-select').value,'ALICE');
installCards(cards);
vm.runInContext('saveVoicesDebounced()',context);
await vm.runInContext('flushVoiceSaves()',context);
const saved=persisted();
assert(saved.BOB.alias_of==='ALICE'||saved.BOB.alias==='ALICE');
'''
        summary, persisted_config, _ = self._run_scenario(scenario, {
            'ALICE': {'type': 'custom', 'voice': 'Serena', 'seed': '7',
                      'description': 'Calm synthetic narrator.'},
            'BOB': {'type': 'custom', 'voice': 'Ryan', 'seed': '3',
                    'description': 'Calm synthetic narrator.', 'alias': 'ALICE'},
        })
        from tts import get_resolved_speaker
        self.assertEqual('ALICE', get_resolved_speaker('BOB', persisted_config))

    def test_lost_reply_after_commit_is_unknown_and_never_retried(self):
        scenario = r'''
await vm.runInContext('loadVoices(true)',context);
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
await context.previewSingleVoiceBook();
assert.strictEqual(context.actionErrors.length,1);
const failure=context.actionErrors[0];
assert(failure[1].message.includes('could not be confirmed'),'must report unknown outcome');
assert(failure[1].message.includes('Bad Gateway'),'must preserve the original detail');
assert(failure[2].includes('may have been saved'),'recovery copy must say may-have-saved');
assert(!failure[1].message.includes('Nothing was applied') && !failure[2].includes('Nothing was applied'),'must not claim nothing applied');
assert(context.voiceSaveQueue.isDirty(),'queue must remain dirty for canonical recovery');
const saved=persisted();
assert.strictEqual(saved.BOB.voice,'Serena','route must have really committed despite the lost reply');
assert.strictEqual(saved.BOB.ready,false);
const snap=await context.API.get('/api/voice_config/snapshot');
assert.strictEqual(snap.voices.find(function(v){return v.name==='BOB'}).config.voice,'Serena','direct snapshot read without any queue flush must verify the committed state');
assert(context.voiceSaveQueue.isDirty(),'direct snapshot read must not flush or retry the ambiguous write');
'''
        summary, persisted_config, wrapper = self._run_scenario(scenario, {
            'ALICE': {'type': 'custom', 'voice': 'Serena', 'seed': '7',
                      'description': 'Calm synthetic narrator.'},
            'BOB': {'type': 'custom', 'voice': 'Ryan', 'seed': '3',
                    'description': 'Calm synthetic narrator.'},
        }, lose_reply=True)
        self.assertIsNotNone(wrapper)
        self.assertEqual(1, wrapper.save_requests,
                         'exactly one save request must have been sent')
        self.assertEqual('Serena', persisted_config['BOB']['voice'])
        # Direct real snapshot read without any queue flush: proves the
        # committed state with no implicit retry.
        self.assertTrue(summary['dirty'])


if __name__ == '__main__':
    unittest.main()
