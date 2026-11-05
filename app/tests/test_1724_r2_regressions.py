"""#1724 R2 coherent-correctness regressions (authored for Crouton R2 repairs).

Covers: CR2014 reserved speaker names through collector + HTTP save,
CR2004 source-preview identity (selected upload authoritative),
CR2005/2007 overlapping loadVoices fencing, mass-save applying guard,
CR2008 guide Home->tab navigation, CR2017 modal label escaping,
CR2002/2001/2006/2009/2010/2013/2016 backend seams, and pin safety.
Synthetic data only; no browser, no inference.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.voices as voices
from routers.voices import (
    _direct_source_asset, build_single_voice_proposal, get_voice_rows,
    _apply_voice_save, _ensure_voice_snapshot,
)

CORE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SINGLE = Path(__file__).resolve().parent.parent / 'static/js/app-single-voice.js'
GUIDES = Path(__file__).resolve().parent.parent / 'static/js/app-guides.js'

PIN = 'a' * 64


def _script(*names):
    return [{"speaker": n, "text": "Synthetic line."} for n in names]


class ReservedNameCollectorHttpTests(unittest.TestCase):
    """CR2014: __proto__/constructor/toString/hasOwnProperty/Unicode names
    survive collector -> JSON -> real HTTP guarded save as own entries."""

    def _collect(self, names):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const names=JSON.parse(process.argv[2]);
const stateHelpers=source.slice(source.indexOf('function getVoiceCardMetadata('),source.indexOf('function createVoiceCard('));
const indexStart=source.indexOf('function getLoraModelsById(');
const indexHelper=indexStart>=0?source.slice(indexStart,source.indexOf('async function suggestVoices(',indexStart)):'';
const code=stateHelpers+indexHelper+source.slice(source.indexOf('function collectVoiceConfig()'),source.indexOf('function onVoiceReadyChange('));
const cards=names.map(name=>{
  const fields={'.voice-presentation':{value:''},'.alias-select':{value:''},'.voice-type:checked':{value:'custom'},'.voice-select':{value:'Ryan'},'.character-style':{value:'Warm'},'.voice-ready':{checked:false},'.voice-seed':{value:'-1'}};
  return {dataset:{voice:name},querySelector:s=>fields[s]||null};
});
const ctx={window:{_voicesByName:{}},document:{querySelectorAll:()=>cards},getLoraModelsById:()=>new Map()};
vm.createContext(ctx);vm.runInContext(code,ctx);
const collected=ctx.collectVoiceConfig();
const serialized=JSON.stringify(collected);
const parsed=JSON.parse(serialized);
// Reference expectation via Object.fromEntries (safe own-property
// creation), never a second unsafe object literal.
const expected=Object.fromEntries(names.map(n=>[n,{type:'custom',voice:'Ryan',seed:'-1'}]));
for(const n of names){
  assert(Object.prototype.hasOwnProperty.call(parsed,n),'own key missing for '+JSON.stringify(n));
  assert.strictEqual(parsed[n].voice,'Ryan','voice lost for '+JSON.stringify(n));
  assert.strictEqual(parsed[n].type,'custom');
}
assert.strictEqual(Object.keys(parsed).length,names.length,'key count mismatch');
assert.strictEqual({}.__proto__,Object.prototype,'global prototype untouched');
console.log(serialized);
'''
        result = subprocess.run(['node', '-e', script, str(CORE), json.dumps(names)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_reserved_names_roundtrip_http(self):
        names = ['__proto__', 'constructor', 'toString', 'hasOwnProperty', 'Ñoño-日本']
        payload = self._collect(names)
        self.assertEqual(set(names), set(payload.keys()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'voice_config.json'
            source = root / 'annotated_script.json'
            path.write_text(json.dumps({n: {'type': 'custom', 'voice': 'Ryan'} for n in names}))
            source.write_text(json.dumps(_script(*names)))
            app = FastAPI(); app.include_router(voices.router)
            with patch.object(voices, 'VOICE_CONFIG_PATH', str(path)), \
                 patch.object(voices, 'SCRIPT_PATH', str(source)), \
                 TestClient(app) as client:
                snap = client.get('/api/voice_config/snapshot')
                self.assertEqual(200, snap.status_code, snap.text)
                snap = snap.json()
                resp = client.post('/api/voice_config/save', json={
                    'voices': payload, 'book_token': snap['book_token'],
                    'revision': snap['revision']})
                self.assertEqual(200, resp.status_code, resp.text)
            saved = json.loads(path.read_text())
            self.assertEqual(set(names), set(saved.keys()))
            for n in names:
                self.assertEqual('Ryan', saved[n]['voice'])

    def test_generated_map_roundtrip_invariant(self):
        # Deterministic generated names (property-style, no new dependency):
        # every generated name must roundtrip collector+JSON as own key.
        import random
        rng = random.Random(1724)
        alphabet = 'ab__protoconstructorÑ日 '
        names = []
        for i in range(30):
            n = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 12))) or f'speaker-{i}'
            if n not in names:
                names.append(n)
        payload = self._collect(names)
        self.assertEqual(set(names), set(payload.keys()))


class AdmissionRouteTests(unittest.TestCase):
    def test_invalid_journal_maps_to_409_no_mutation(self):
        # CR2037: ensure_book_state recovery failure (invalid journal)
        # must be honest 409, not 500, and must not mutate files.
        from contextlib import ExitStack
        from routers import script as script_router
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'book.txt'; source.write_text('Synthetic source.', encoding='utf-8')
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one", "input_file_path": str(source)}))
            (root / '.active_book_transaction.json').write_text(json.dumps({"version": 99}))
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
            with ExitStack() as stack:
                stack.enter_context(patch.object(script_router, 'DATA_DIR', str(root)))
                app = FastAPI(); app.include_router(script_router.router)
                with stack.enter_context(TestClient(app, raise_server_exceptions=False)) as client:
                    resp = client.get('/api/single-narrator/source-preview')
                    self.assertEqual(409, resp.status_code, resp.text)
                    self.assertIn('could not be verified', resp.json()['detail'])
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
            self.assertEqual(before, after, 'admission refusal must not mutate')


class BackendSeamTests(unittest.TestCase):
    def test_adapter_id_whitespace_normalized_and_dotdot_refused(self):
        asset = _direct_source_asset('ALICE', {
            'type': 'lora', 'adapter_id': '  my-adapter  ',
            'adapter_generation_sha256': PIN})
        self.assertEqual('my-adapter', asset['adapter_id'])
        self.assertEqual('lora_models/my-adapter', asset['adapter_path'])
        self.assertEqual(PIN, asset['adapter_generation_sha256'])
        for bad in (' .. ', ' . '):
            with self.assertRaises(Exception) as cm:
                _direct_source_asset('ALICE', {'type': 'lora', 'adapter_id': bad})
            self.assertEqual(422, cm.exception.status_code)

    def test_nonstring_alias_refused(self):
        with self.assertRaises(Exception) as cm:
            _direct_source_asset('ALICE', {'type': 'custom', 'voice': 'Ryan', 'alias_of': 123})
        self.assertEqual(422, cm.exception.status_code)
        self.assertIn('malformed alias', cm.exception.detail)

    def test_legacy_alias_suppresses_persona_states(self):
        from tests.test_state_personas import state_script
        script = state_script()
        from speaker_traits import get_persona_state_targets
        targets = get_persona_state_targets(script)
        self.assertTrue(targets.get('ARTHUR'), 'fixture must produce state targets')
        rows = get_voice_rows(script, {'ARTHUR': {'type': 'custom', 'voice': 'Ryan', 'alias': 'BOB'}})
        arthur = next(r for r in rows if r['name'] == 'ARTHUR')
        self.assertNotIn('persona_states', arthur)
        rows_direct = get_voice_rows(script, {'ARTHUR': {'type': 'custom', 'voice': 'Ryan'}})
        arthur_direct = next(r for r in rows_direct if r['name'] == 'ARTHUR')
        self.assertIn('persona_states', arthur_direct)

    def test_get_voice_rows_domain_error_not_http(self):
        with self.assertRaises(ValueError):
            get_voice_rows(_script('ALICE'), {'ALICE': None})
        with self.assertRaises(ValueError):
            get_voice_rows(_script('ALICE'), None)
        # Absent stays unconfigured; inactive malformed preserved (no raise)
        rows = get_voice_rows(_script('ALICE'), {'GHOST': None})
        self.assertEqual('ALICE', rows[0]['name'])

    def test_collection_and_workspace_unavailable_not_500(self):
        # CR2009: malformed active entry -> ValueError seam caught by
        # compound callers as unavailable, valid books preserved.
        from collection_overview import _voices_stage_from_entry, get_collection_overview
        from project import ProjectManager
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); library = root / 'scripts'; library.mkdir()
            entries = [{'type': 'dialogue', 'speaker': 'ALICE', 'text': 'Synthetic line.'}]
            def save(name, voices_map):
                (library / f'{name}.json').write_text(json.dumps(entries))
                (library / f'{name}.meta.json').write_text(json.dumps({'book_id': name}))
                (library / f'{name}.voice_config.json').write_text(json.dumps(voices_map))
            save('Bad', {'ALICE': None})
            save('Good', {'ALICE': {'type': 'custom', 'description': 'Synthetic voice'}})
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            rows = get_collection_overview(str(library), ['Bad', 'Good'], get_voice_rows)
            by_name = {r.get('name') or r.get('book_id') or r.get('title'): r for r in rows}
            # Rows are per-book; find by any identifier field
            self.assertEqual(2, len(rows))
            statuses = []
            for r in rows:
                stage = (r.get('voices') or r.get('stages', {}).get('voices') or {})
                statuses.append(stage)
            # At least one row is unavailable and one is not, valid preserved
            self.assertTrue(any(s.get('status') == 'unavailable' for s in statuses), statuses)
            self.assertTrue(any(s.get('status') in ('ready', 'needs_decisions') for s in statuses), statuses)
            after = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before, after, 'compound reads must not write')
        # Workspace compound reader: ValueError (mapped to 409 by its HTTP
        # facade), never an uncaught HTTPException/500
        from book_workspace import get_book_workspace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'annotated_script.json').write_text(json.dumps(entries))
            (root / 'voice_config.json').write_text(json.dumps({'ALICE': None}))
            (root / 'state.json').write_text('{"active_book_id":"b","book_generation":"one"}')
            mgr = ProjectManager(str(root))
            with self.assertRaises(ValueError):
                get_book_workspace(str(root), {}, get_voice_rows, mgr.get_chunk_audio_path)
        entry = {'exists': True, 'sha': 'x', 'raw': json.dumps({'ALICE': None}).encode()}
        stage = _voices_stage_from_entry(entry, _script('ALICE'), get_voice_rows)
        self.assertEqual('unavailable', stage['status'])

    def test_proposal_amplification_bound_patched_budget(self):
        import project_scope_voices
        script = _script('ALICE', 'BOB', 'CAROL')
        cfg = {'ALICE': {'type': 'custom', 'voice': 'Ryan'}}
        with patch.object(project_scope_voices, 'MAX_SCOPE_VOICE_MAPPING_BYTES', 10):
            with self.assertRaises(Exception) as cm:
                build_single_voice_proposal(cfg, script, 'ALICE')
            self.assertEqual(422, cm.exception.status_code)

    def test_proposal_amplification_exact_boundary(self):
        # Assert against ACTUAL serialized proposal, not a copied formula:
        # admitted proposal's serialized voices map must fit its budget;
        # a budget one byte smaller than that actual size must refuse.
        import project_scope_voices, json as _json
        script = _script('ALICE', 'BOB')
        cfg = {'ALICE': {'type': 'custom', 'voice': 'Ryan'}}
        with patch.object(project_scope_voices, 'MAX_SCOPE_VOICE_MAPPING_BYTES', 10**9):
            proposal = build_single_voice_proposal(cfg, script, 'ALICE')
        actual = len(_json.dumps(proposal['voices'], ensure_ascii=False, allow_nan=False).encode('utf-8'))
        self.assertGreater(actual, 0)
        # Admit with a budget that fits the actual serialized mapping;
        # assert the actual mapping really fits (not a copied formula).
        with patch.object(project_scope_voices, 'MAX_SCOPE_VOICE_MAPPING_BYTES', actual + 1000):
            admitted = build_single_voice_proposal(cfg, script, 'ALICE')
            self.assertEqual(2, admitted['affected_speakers'])
            admitted_bytes = len(_json.dumps(admitted['voices'], ensure_ascii=False, allow_nan=False).encode('utf-8'))
            self.assertLessEqual(admitted_bytes, actual + 1000)
        # Oversize: budget below the actual serialized size must refuse.
        with patch.object(project_scope_voices, 'MAX_SCOPE_VOICE_MAPPING_BYTES', actual - 1):
            with self.assertRaises(Exception) as cm:
                build_single_voice_proposal(cfg, script, 'ALICE')
            self.assertEqual(422, cm.exception.status_code)

    def test_pin_explicit_wins_on_same_id_path_move(self):
        # CR2000/2003 adjudication + parent policy: explicit P must win
        # even when catalog path moves for same ID; runtime Q/P refuses.
        # Real behavior: JS collector -> HTTP guarded save -> disk -> TTS gate.
        # Collector: lora card, metadata pin P, catalog path moved.
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const pin=process.argv[2];
const stateHelpers=source.slice(source.indexOf('function getVoiceCardMetadata('),source.indexOf('function createVoiceCard('));
const indexStart=source.indexOf('function getLoraModelsById(');
const indexHelper=indexStart>=0?source.slice(indexStart,source.indexOf('async function suggestVoices(',indexStart)):'';
const code=stateHelpers+indexHelper+source.slice(source.indexOf('function collectVoiceConfig()'),source.indexOf('function onVoiceReadyChange('));
const fields={'.voice-presentation':{value:''},'.alias-select':{value:''},'.voice-type:checked':{value:'lora'},'.lora-adapter-select':{value:'m1'},'.lora-character-style':{value:'Warm'},'.voice-ready':{checked:false},'.voice-seed':{value:'-1'}};
const card={dataset:{voice:'ALICE'},querySelector:s=>fields[s]||null};
const ctx={window:{_voicesByName:{ALICE:{config:{type:'lora',adapter_id:'m1',adapter_path:'lora_models/m1',adapter_generation_sha256:pin}}},_loraModelsCache:[{id:'m1',adapter_path:'lora_models/moved'}]},document:{querySelectorAll:()=>[card]}};
vm.createContext(ctx);vm.runInContext(code,ctx);
const collected=ctx.collectVoiceConfig();
assert.strictEqual(collected.ALICE.adapter_path,'lora_models/moved','catalog move must be reflected');
assert.strictEqual(collected.ALICE.adapter_generation_sha256,pin,'explicit P must be retained, not cleared');
console.log(JSON.stringify(collected));
'''
        result = subprocess.run(['node', '-e', code, str(CORE), PIN], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'voice_config.json'; script_path = root / 'annotated_script.json'
            path.write_text(json.dumps({'ALICE': {'type': 'lora', 'adapter_id': 'm1',
                                                  'adapter_path': 'lora_models/m1',
                                                  'adapter_generation_sha256': PIN}}))
            script_path.write_text(json.dumps(_script('ALICE')))
            (root / 'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
            app = FastAPI(); app.include_router(voices.router)
            with patch.object(voices, 'VOICE_CONFIG_PATH', str(path)), \
                 patch.object(voices, 'SCRIPT_PATH', str(script_path)), \
                 TestClient(app) as client:
                snap = client.get('/api/voice_config/snapshot').json()
                resp = client.post('/api/voice_config/save', json={
                    'voices': payload, 'book_token': snap['book_token'], 'revision': snap['revision']})
                self.assertEqual(200, resp.status_code, resp.text)
            saved = json.loads(path.read_text())
            self.assertEqual('lora_models/moved', saved['ALICE']['adapter_path'])
            self.assertEqual(PIN, saved['ALICE']['adapter_generation_sha256'],
                             'explicit P retained after same-ID path move')
        # Runtime P/Q refusal (existing mismatch gate, exercised for real)
        import contextlib, threading
        from tts import TTSEngine
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'adapter_model.safetensors').write_bytes(b'synthetic')
            @contextlib.contextmanager
            def fake_snapshot(adapter_path):
                yield ('synthetic-snapshot', 'b' * 64)
            engine = object.__new__(TTSEngine)
            engine._lora_prompt_cache = {}; engine._lora_adapter_path = None
            engine._lora_generation_sha256 = None; engine._model_lock = threading.Lock()
            with patch('tts.ensure_adapter_generation_snapshot', fake_snapshot), \
                 patch.object(TTSEngine, '_init_local_lora',
                            side_effect=AssertionError('load must not start on mismatch')):
                with self.assertRaisesRegex(ValueError, 'Adapter generation changed'):
                    engine._ensure_local_lora_generation(str(root), PIN)

    def test_snapshot_nan_is_400_not_500(self):
        # CR2013: nonfinite in config makes revision digest raise ValueError.
        bad_config = {'ALICE': {'type': 'custom', 'voice': 'Ryan', 'seed': float('nan')}}
        fake_snapshot = {'script_bytes': json.dumps(_script('ALICE')).encode(),
                         'voices': bad_config, 'book_id': 'b'}
        with patch.object(voices, 'get_book_snapshot', return_value=fake_snapshot), \
             patch.object(voices, 'ensure_book_state', lambda *a, **k: _nullctx()), \
             patch.object(voices, 'file_lock', lambda *a, **k: _nullctx()), \
             patch('routers.voices.get_book_operation_token', return_value='b' * 64), \
             patch('routers.voices.get_book_snapshot_token', return_value='b' * 64):
            with self.assertRaises(Exception) as cm:
                _ensure_voice_snapshot()
            self.assertEqual(400, cm.exception.status_code)


class _nullctx:
    def __enter__(self): return None
    def __exit__(self, *a): return False


class GuideAndEscapeJsTests(unittest.TestCase):
    def run_node(self, code, *args):
        result = subprocess.run(['node', '-e', code, *args], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_guide_home_to_tab_container(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(process.argv[1],'utf8');
const els={};
function el(id){return els[id]||(els[id]={id,style:{display:'none'},hidden:false,textContent:'',disabled:false,children:[],
  replaceChildren(){},appendChild(){},append(){},addEventListener(){},
  matches:()=>false,setAttribute(){},focus(){},scrollIntoView(){},querySelector(){return null;},querySelectorAll:()=>[],closest:()=>null});}
const ctx={document:{getElementById:el,createElement:()=>el('x-'+Math.random())},currentTabName:'home',showToast(){},isEditorEditPending:()=>false,activateTab(name){ctx.currentTabName=name;
  for(const id of ['script-tab','voices-tab','editor-tab']){els[id].style.display=(id===name+'-tab')?'block':'none';}},openConfigField(){},console};
els['script-tab']=el('script-tab');els['voices-tab']=el('voices-tab');els['editor-tab']=el('editor-tab');
els['script-tab'].style.display='none';els['voices-tab'].style.display='none';els['editor-tab'].style.display='none';
els['task-guide-feedback']=el('task-guide-feedback');
vm.createContext(ctx);vm.runInContext(src,ctx);
// Simulate guide state at voices step targeting voices-tab (tab container)
vm.runInContext("activeTaskGuide={steps:[['t','voices','voices-tab','i','e','c']]};taskGuideStep=0;",ctx);
ctx.openTaskGuideTarget();
assert.strictEqual(ctx.currentTabName,'voices','Home->voices-tab must navigate');
assert.notStrictEqual(els['task-guide-feedback'].textContent.includes('hidden'),true,'must not report hidden for tab container');
// Batch-mode hidden descendant: script-single-area display none after activation
els['script-single-area']=el('script-single-area');els['script-single-area'].style.display='none';
vm.runInContext("activeTaskGuide={steps:[['t','script','script-single-area','i','e','c']]};taskGuideStep=0;",ctx);
ctx.openTaskGuideTarget();
assert(els['task-guide-feedback'].textContent.includes('hidden'),'batch hidden descendant must give feedback');
'''

        self.run_node(code, str(GUIDES))

    def test_escape_label_executed(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(process.argv[1],'utf8');
const a=src.indexOf('function singleVoiceEscapeLabel');
const ctx={console};vm.createContext(ctx);
vm.runInContext(src.slice(a,src.indexOf('async function previewSingleVoiceBook',a)),ctx);
const out=ctx.singleVoiceEscapeLabel('A\nB\tC\rD\u0001E');
assert.strictEqual(out,'A\\nB\\tC\\rD\\u0001E');
assert.strictEqual(ctx.singleVoiceEscapeLabel('plain'),'plain');
'''
        self.run_node(code, str(SINGLE))


class SourceIdentityJsTests(unittest.TestCase):
    def test_selected_upload_authoritative_over_stale_snapshot(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'),voice=fs.readFileSync(process.argv[2],'utf8');
const ctx={currentBookFilename:'book.txt',_bookSelectionEpoch:1,document:{getElementById:()=>null},console,
  _voiceSaveSnapshot:{book_identity_token:'a'.repeat(64)},window:{_bookSelectionEpoch:1,_selectedUploadIdentity:null}};
ctx.window._selectedUploadIdentity={token:'b'.repeat(64)};
vm.createContext(ctx);
// Extract only identity helpers from single-voice (avoid full file DOM deps)
const start=voice.indexOf('function singleVoiceBookEpochCurrent');
const end=voice.indexOf('function singleVoiceRefreshOpts');
vm.runInContext(voice.slice(start,end),ctx);
vm.runInContext('let _singleVoiceGeneration=0;',ctx);
// Capture identity AFTER upload selection B (snapshot still A)
ctx.window._bookSelectionEpoch=1;ctx.currentBookFilename='book.txt';
const identity=ctx.singleVoiceCaptureIdentity();
assert.strictEqual(identity.identityToken,'b'.repeat(64));
assert.strictEqual(ctx.singleVoiceIdentityCurrent(identity),true,'stale A snapshot must not veto B selection');
// Saved-script fallback (no selection) still fenced by snapshot
ctx.window._selectedUploadIdentity=null;
const id2=ctx.singleVoiceCaptureIdentity();
assert.strictEqual(id2.identityToken,'a'.repeat(64));
assert.strictEqual(ctx.singleVoiceIdentityCurrent(id2),true);
// Missing all context: capture yields null token (load refuses)
ctx._voiceSaveSnapshot=null;
const id3=ctx.singleVoiceCaptureIdentity();
assert.strictEqual(id3.identityToken,null);
'''
        result = subprocess.run(['node', '-e', code, str(CORE), str(SINGLE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class ControllerHarnessTests(unittest.TestCase):
    """Full controller proofs reusing the existing test_single_voice_js harness."""

    def _harness(self):
        from tests.test_single_voice_js import SingleVoiceJsTests
        return SingleVoiceJsTests()

    def test_source_preview_B_before_voices(self):
        # Selection B authoritative; stale snapshot A must not discard B.
        # Harness preview returns book_token 'b'*64; set selection to that,
        # snapshot identity to 'a'*64, then full loadSingleVoiceSourcePreview.
        out = self._harness().run_node(r'''
context.window._selectedUploadIdentity={token:'b'.repeat(64)};
context.window._bookSelectionEpoch=1;
vm.runInContext("_voiceSaveSnapshot.book_identity_token='a'.repeat(64)",context);
await context.loadSingleVoiceSourcePreview();
assert(context.elements['source-preview-text'].textContent.includes('Synthetic'),'B text must render');
assert.strictEqual(context.actionErrors.length,0,'no error for valid B preview');
''')
        self.assertIsNotNone(out)

    def test_catalog_failure_warns_after_saved(self):
        # CR2024+CR2078: post-save loadVoices(false) re-renders (revision
        # changed), failed catalogs are reported via failedResources and
        # visibly warned; save itself succeeded (writes==1, no error).
        # Harness loadVoices is real; its resource fetches go through
        # API.get which returns [] for catalogs - to force a failure we
        # stub the vm loadVoices? No: instead assert the report path by
        # checking toasts after a normal apply (no failed resources = no
        # warning), and separately verify apply code consumes report.
        # Actual failure report proof: patch API.get in harness to fail
        # lora models during post-save load.
        out = self._harness().run_node(r'''
context.elements['single-voice-source'].value='ALICE';
context.console.debug=()=>{};
// Force lora catalog failure on the post-save loadVoices(false) call:
// first snapshot load succeeds (already done by harness setup), now fail.
const origGet=context.API.get;
let calls=0;
context.API.get=async(url,opts)=>{calls++;if(url==='/api/lora/models'){throw new Error('catalog down');}return origGet(url,opts);};
await context.previewSingleVoiceBook();
assert.strictEqual(context.actionErrors.length,0,'save succeeded, catalog failure must not become a save error');
assert(context.toasts.some(t=>String(t[0]).includes('could not refresh') && String(t[0]).includes('LoRA models') && !String(t[0]).includes('/api/')),'warning must use plain catalog labels, no raw endpoints');
assert(context.toasts.some(t=>String(t[0]).includes('applied book-wide')),'success toast must still show');
console.log(JSON.stringify({toasts:context.toasts.map(t=>t[0])}));
''')
        self.assertTrue(out['writes'])

    def test_pending_apply_guard_during_await(self):
        # During bulk flush (saveHook), ordinary saveVoicesDebounced must be
        # fenced, original disabled states preserved, stale owner release safe.
        out = self._harness().run_node(r'''
// voices-list controls with mixed original disabled states
const controls=[{disabled:false},{disabled:true}];
context.elements['voices-list'].querySelectorAll=()=>controls;
// Track writes during apply via saveHook
let duringApplying=false, duringDisabled=null;
saveHook=()=>{
  duringApplying=!!context.window._singleVoiceApplying;
  duringDisabled=controls.map(c=>c.disabled);
  // Ordinary edit attempt during pending save must not enqueue a second write
  const before=context.voiceSaveQueue.getRevision();
  context.saveVoicesDebounced();
  assert.strictEqual(context.voiceSaveQueue.getRevision(),before,'ordinary edit during bulk must not enqueue');
};
context.elements['single-voice-source'].value='ALICE';
await context.previewSingleVoiceBook();
assert.strictEqual(duringApplying,true,'guard must be active DURING flush await');
assert.deepStrictEqual(duringDisabled,[true,true],'all owned controls disabled during apply');
assert.deepStrictEqual(controls.map(c=>c.disabled),[false,true],'original disabled states must be restored, not blanket-enabled');
assert.strictEqual(context.window._singleVoiceApplying,false,'owner must release after apply');
console.log(JSON.stringify({duringApplying,duringDisabled}));
''')
        self.assertTrue(out['writes'])


class WaitingLoadTests(unittest.TestCase):
    def _run(self, code):
        result = subprocess.run(['node', '-e', code, str(CORE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('FINISHED-OK', result.stdout, 'pending Promise must fail, never count as green')

    def test_waiting_obsolete_does_not_supersede_newer(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let finished=false;process.on('beforeExit',()=>assert(finished,'pending Promise must fail, never count as green'));
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const fields={},draws=[];let saveGate=deferred();const snapGates=[];
const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'old cards',style:{}});
const ctx={currentBookFilename:'bookA.txt',ensureVoiceRosterCache:()=>{},performance:{now:()=>0},document:{getElementById:el},console:{debug(){}},
 API:{get:url=>{if(url==='/api/voice_config/snapshot'){const g=deferred();snapGates.push(g);return g.promise;}return Promise.resolve([]);}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>0,isDirty:()=>false},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){},
 createVoiceCard:voice=>{draws.push(voice.name);return `<card>${voice.name}</card>`;}};
ctx.window={_bookSelectionEpoch:1,_selectedUploadIdentity:null,_castLibraryRequest:0};
ctx._designedVoicesCache=[];ctx._cloneVoicesCache=[];ctx._loraModelsCache=[];
vm.createContext(ctx);
const load=(a,b)=>{const first=source.indexOf(a);const second=source.indexOf(b,first);assert(first>=0&&second>first,'extraction boundary must be valid for '+a);vm.runInContext(source.slice(first,second),ctx);};
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()');
vm.runInContext('let _voiceSaveSnapshot=null;let _voiceRecoveryDrafts=[];',ctx);
ctx.loadCastLibrary=async()=>{};
const mkSnap=name=>({revision:'0'.repeat(64),book_token:'b'.repeat(64),book_identity_token:'b'.repeat(64),voices:[{name,config:{type:'custom',voice:'Ryan'}}],seed_changes:[]});
(async()=>{
  saveGate.resolve();
  // A(true) pending
  const pA=ctx.loadVoices(true);pA.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,1,'A snapshot pending');
  // Select B epoch2, B(false) waits on A
  ctx.currentBookFilename='bookB.txt';ctx.window._bookSelectionEpoch=2;
  const genBeforeB=ctx.loadVoices.generation;
  const pB=ctx.loadVoices(false);pB.catch(()=>{});await turn();
  // Select C epoch3, C(true) forced newer
  ctx.currentBookFilename='bookC.txt';ctx.window._bookSelectionEpoch=3;
  const pC=ctx.loadVoices(true);pC.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,2,'C snapshot pending');
  const genAfterC=ctx.loadVoices.generation;
  // Finish A -> waiting B resumes; must NOT increment generation
  snapGates[0].resolve(mkSnap('A'));
  await turn();await turn();
  assert.strictEqual(ctx.loadVoices.generation,genAfterC,'obsolete waiter must not increment generation');
  await assert.rejects(pB,/superseded|changed/);
  // Finish C -> C must succeed and render C cards
  snapGates[1].resolve(mkSnap('C'));
  await pC;
  assert.strictEqual(el('voices-list').innerHTML,'<card>C</card>','newer C must render, not be superseded by B');
  await pA.catch(()=>{});
  finished=true;console.log('FINISHED-OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self._run(code)

    def test_waiting_legitimate_when_no_newer(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let finished=false;process.on('beforeExit',()=>assert(finished,'pending Promise must fail, never count as green'));
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const fields={},draws=[];let saveGate=deferred();const snapGates=[];
const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'old cards',style:{}});
const ctx={currentBookFilename:'bookA.txt',ensureVoiceRosterCache:()=>{},performance:{now:()=>0},document:{getElementById:el},console:{debug(){}},
 API:{get:url=>{if(url==='/api/voice_config/snapshot'){const g=deferred();snapGates.push(g);return g.promise;}return Promise.resolve([]);}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>0,isDirty:()=>false},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){},
 createVoiceCard:voice=>{draws.push(voice.name);return `<card>${voice.name}</card>`;}};
ctx.window={_bookSelectionEpoch:1,_selectedUploadIdentity:null,_castLibraryRequest:0};
ctx._designedVoicesCache=[];ctx._cloneVoicesCache=[];ctx._loraModelsCache=[];
vm.createContext(ctx);
const load=(a,b)=>{const first=source.indexOf(a);const second=source.indexOf(b,first);assert(first>=0&&second>first,'extraction boundary must be valid for '+a);vm.runInContext(source.slice(first,second),ctx);};
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()');
vm.runInContext('let _voiceSaveSnapshot=null;let _voiceRecoveryDrafts=[];',ctx);
ctx.loadCastLibrary=async()=>{};
const mkSnap=name=>({revision:'0'.repeat(64),book_token:'b'.repeat(64),book_identity_token:'b'.repeat(64),voices:[{name,config:{type:'custom',voice:'Ryan'}}],seed_changes:[]});
(async()=>{
  saveGate.resolve();
  const pA=ctx.loadVoices(true);pA.catch(()=>{});await turn();await turn();
  ctx.currentBookFilename='bookB.txt';ctx.window._bookSelectionEpoch=2;
  const pB=ctx.loadVoices(false);pB.catch(()=>{});await turn();
  snapGates[0].resolve(mkSnap('A'));
  await turn();await turn();await turn();
  assert.strictEqual(snapGates.length,2,'B must claim fresh load after A settles');
  snapGates[1].resolve(mkSnap('B'));
  await pB;
  assert.strictEqual(el('voices-list').innerHTML,'<card>B</card>');
  await pA.catch(()=>{});
  finished=true;console.log('FINISHED-OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self._run(code)


class WaitingLoadRecoveryTests(unittest.TestCase):
    def _run(self, code):
        result = subprocess.run(['node', '-e', code, str(CORE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('FINISHED-OK', result.stdout, 'pending Promise must fail, never count as green')

    def test_failed_newer_load_does_not_supersede_waiter(self):
        # DS3082: A pending, forced B starts (generation bump) and FAILS
        # rendering nothing; waiter C for its own identity must claim a
        # fresh load and render, not abort as superseded.
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let finished=false;process.on('beforeExit',()=>assert(finished,'pending Promise must fail, never count as green'));
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const fields={};let saveGate=deferred();const snapGates=[];
const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'old cards',style:{}});
const ctx={currentBookFilename:'bookA.txt',ensureVoiceRosterCache:()=>{},performance:{now:()=>0},document:{getElementById:el},console:{debug(){}},
 API:{get:url=>{if(url==='/api/voice_config/snapshot'){const g=deferred();snapGates.push(g);return g.promise;}return Promise.resolve([]);}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>0,isDirty:()=>false},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){},
 createVoiceCard:voice=>`<card>${voice.name}</card>`};
ctx.window={_bookSelectionEpoch:1,_selectedUploadIdentity:null,_castLibraryRequest:0};
ctx._designedVoicesCache=[];ctx._cloneVoicesCache=[];ctx._loraModelsCache=[];
vm.createContext(ctx);
const load=(a,b)=>{const first=source.indexOf(a);const second=source.indexOf(b,first);assert(first>=0&&second>first,'extraction boundary must be valid for '+a);vm.runInContext(source.slice(first,second),ctx);};
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()');
vm.runInContext('let _voiceSaveSnapshot=null;let _voiceRecoveryDrafts=[];',ctx);
ctx.loadCastLibrary=async()=>{};
const mkSnap=name=>({revision:'0'.repeat(64),book_token:'b'.repeat(64),book_identity_token:'b'.repeat(64),voices:[{name,config:{type:'custom',voice:'Ryan'}}],seed_changes:[]});
(async()=>{
  saveGate.resolve();
  const pA=ctx.loadVoices(true);pA.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,1,'A snapshot pending');
  // C waiter for a different identity while A is pending
  ctx.currentBookFilename='bookC.txt';ctx.window._bookSelectionEpoch=2;
  const pC=ctx.loadVoices(false);pC.catch(()=>{});await turn();
  // Forced B (newest) starts, then fails with a transient error
  ctx.currentBookFilename='bookC.txt';ctx.window._bookSelectionEpoch=2;
  const pB=ctx.loadVoices(true);pB.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,2,'B snapshot pending');
  snapGates[1].reject(new Error('transient 503'));
  await pB.catch(()=>{});
  // A settles (fenced, rejects); waiter C must claim a fresh load
  snapGates[0].resolve(mkSnap('A'));
  await turn();await turn();await turn();
  assert.strictEqual(snapGates.length,3,'waiter must claim a fresh load after the newer load failed');
  snapGates[2].resolve(mkSnap('C'));
  await pC;
  assert.strictEqual(el('voices-list').innerHTML,'<card>C</card>','recovered waiter must render current cards');
  finished=true;console.log('FINISHED-OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self._run(code)


class WaitingLoadCoalesceTests(unittest.TestCase):
    def _run(self, code):
        result = subprocess.run(['node', '-e', code, str(CORE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('FINISHED-OK', result.stdout, 'pending Promise must fail, never count as green')

    def test_same_book_waiter_coalesces_on_newer_load(self):
        # DS3045: waiter W (sel:<token>) waits on P; forced F for the
        # same logical book (ctx key carrying the same token) starts.
        # W must coalesce onto F's result, not report superseded.
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let finished=false;process.on('beforeExit',()=>assert(finished,'pending Promise must fail, never count as green'));
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const fields={};let saveGate=deferred();const snapGates=[];
const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'old cards',style:{}});
const TOKEN='t'.repeat(64);
const ctx={currentBookFilename:'book.txt',ensureVoiceRosterCache:()=>{},performance:{now:()=>0},document:{getElementById:el},console:{debug(){}},
 API:{get:url=>{if(url==='/api/voice_config/snapshot'){const g=deferred();snapGates.push(g);return g.promise;}return Promise.resolve([]);}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>0,isDirty:()=>false},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){},
 createVoiceCard:voice=>`<card>${voice.name}</card>`};
const selection={filename:'book.txt',token:TOKEN,request:{}};
ctx.window={_bookSelectionEpoch:1,_selectedUploadIdentity:selection,_castLibraryRequest:0};
ctx._designedVoicesCache=[];ctx._cloneVoicesCache=[];ctx._loraModelsCache=[];
vm.createContext(ctx);
const load=(a,b)=>{const first=source.indexOf(a);const second=source.indexOf(b,first);assert(first>=0&&second>first,'extraction boundary must be valid for '+a);vm.runInContext(source.slice(first,second),ctx);};
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()');
vm.runInContext('let _voiceSaveSnapshot=null;let _voiceRecoveryDrafts=[];',ctx);
ctx.loadCastLibrary=async()=>{};
const mkSnap=name=>({revision:'0'.repeat(64),book_token:'b'.repeat(64),book_identity_token:TOKEN,voices:[{name,config:{type:'custom',voice:'Ryan'}}],seed_changes:[]});
(async()=>{
  saveGate.resolve();
  const pP=ctx.loadVoices(true);pP.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,1,'P snapshot pending');
  const pW=ctx.loadVoices(false,{expectedSelection:selection});pW.catch(()=>{});await turn();
  const pF=ctx.loadVoices(true);pF.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,2,'F snapshot pending');
  snapGates[0].resolve(mkSnap('P'));
  await turn();await turn();await turn();
  assert.strictEqual(snapGates.length,2,'compatible waiter must not issue a third snapshot');
  snapGates[1].resolve(mkSnap('B'));
  const wResult=await pW;
  assert(wResult&&Array.isArray(wResult.refreshedResources),'waiter must settle with the newer load result');
  await pF;
  assert.strictEqual(el('voices-list').innerHTML,'<card>B</card>');
  finished=true;console.log('FINISHED-OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self._run(code)


class WaitingLoadRestoredTests(unittest.TestCase):
    def _run(self, code):
        result = subprocess.run(['node', '-e', code, str(CORE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('FINISHED-OK', result.stdout, 'pending Promise must fail, never count as green')

    def test_restored_tab_waiter_coalesces_on_newer_same_book_load(self):
        # DS3045 restored-tab case (parent probe): prior snapshot A,
        # no local selection object, waiter expectedIdentityToken A,
        # newer forced unscoped load for the SAME book succeeds.
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let finished=false;process.on('beforeExit',()=>assert(finished,'pending Promise must fail, never count as green'));
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const fields={};let saveGate=deferred();const snapGates=[];
const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'old cards',style:{}});
const TOKEN='a'.repeat(64);
const ctx={currentBookFilename:'book.txt',ensureVoiceRosterCache:()=>{},performance:{now:()=>0},document:{getElementById:el},console:{debug(){}},
 API:{get:url=>{if(url==='/api/voice_config/snapshot'){const g=deferred();snapGates.push(g);return g.promise;}return Promise.resolve([]);}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>0,isDirty:()=>false},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){},
 createVoiceCard:voice=>`<card>${voice.name}</card>`};
ctx.window={_bookSelectionEpoch:1,_selectedUploadIdentity:null,_castLibraryRequest:0};
ctx._designedVoicesCache=[];ctx._cloneVoicesCache=[];ctx._loraModelsCache=[];
vm.createContext(ctx);
const load=(a,b)=>{const first=source.indexOf(a);const second=source.indexOf(b,first);assert(first>=0&&second>first,'extraction boundary must be valid for '+a);vm.runInContext(source.slice(first,second),ctx);};
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()');
vm.runInContext('let _voiceSaveSnapshot={book_identity_token:"'+('a'.repeat(64))+'"};let _voiceRecoveryDrafts=[];',ctx);
ctx.loadCastLibrary=async()=>{};
const mkSnap=name=>({revision:'0'.repeat(64),book_token:'b'.repeat(64),book_identity_token:TOKEN,voices:[{name,config:{type:'custom',voice:'Ryan'}}],seed_changes:[]});
(async()=>{
  saveGate.resolve();
  const pP=ctx.loadVoices(true);pP.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,1,'P snapshot pending');
  const pW=ctx.loadVoices(false,{expectedIdentityToken:TOKEN});pW.catch(()=>{});await turn();
  const pF=ctx.loadVoices(true);pF.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,2,'F snapshot pending');
  snapGates[0].resolve(mkSnap('P'));
  await turn();await turn();await turn();
  assert.strictEqual(snapGates.length,2,'restored same-book waiter must not issue a third snapshot');
  snapGates[1].resolve(mkSnap('B'));
  const wResult=await pW;
  assert(wResult&&Array.isArray(wResult.refreshedResources),'waiter must settle with the newer same-book result');
  await pF;
  assert.strictEqual(el('voices-list').innerHTML,'<card>B</card>');
  finished=true;console.log('FINISHED-OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self._run(code)


class WaitingLoadUnavailableControlTests(unittest.TestCase):
    def _run(self, code):
        result = subprocess.run(['node', '-e', code, str(CORE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('FINISHED-OK', result.stdout, 'pending Promise must fail, never count as green')

    def test_unavailable_selection_never_falls_back_to_old_snapshot(self):
        # Selected-but-unavailable (token null) + old snapshot A: the
        # unscoped key must use empty/unavailable authority, NOT the old
        # snapshot token, so an A-token waiter must not coalesce onto it.
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let finished=false;process.on('beforeExit',()=>assert(finished,'pending Promise must fail, never count as green'));
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const fields={};let saveGate=deferred();const snapGates=[];
const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'old cards',style:{}});
const TOKEN='a'.repeat(64);
const ctx={currentBookFilename:'book.txt',ensureVoiceRosterCache:()=>{},performance:{now:()=>0},document:{getElementById:el},console:{debug(){}},
 API:{get:url=>{if(url==='/api/voice_config/snapshot'){const g=deferred();snapGates.push(g);return g.promise;}return Promise.resolve([]);}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>0,isDirty:()=>false},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){},
 createVoiceCard:voice=>`<card>${voice.name}</card>`};
ctx.window={_bookSelectionEpoch:1,_selectedUploadIdentity:{filename:'book.txt',token:null,unavailable:true,request:{}},_castLibraryRequest:0};
ctx._designedVoicesCache=[];ctx._cloneVoicesCache=[];ctx._loraModelsCache=[];
vm.createContext(ctx);
const load=(a,b)=>{const first=source.indexOf(a);const second=source.indexOf(b,first);assert(first>=0&&second>first,'extraction boundary must be valid for '+a);vm.runInContext(source.slice(first,second),ctx);};
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()');
vm.runInContext('let _voiceSaveSnapshot={book_identity_token:"'+('a'.repeat(64))+'"};let _voiceRecoveryDrafts=[];',ctx);
ctx.loadCastLibrary=async()=>{};
const mkSnap=name=>({revision:'0'.repeat(64),book_token:'b'.repeat(64),book_identity_token:TOKEN,voices:[{name,config:{type:'custom',voice:'Ryan'}}],seed_changes:[]});
(async()=>{
  saveGate.resolve();
  const pU=ctx.loadVoices(false);pU.catch(()=>{});await turn();await turn();
  assert.strictEqual(snapGates.length,1,'unavailable load starts its own snapshot');
  const unavailableKey=ctx.loadVoices.pendingIdentity;
  assert(!String(unavailableKey).includes(TOKEN),'unavailable key must not embed the old snapshot token');
  const pW=ctx.loadVoices(false,{expectedIdentityToken:TOKEN});pW.catch(()=>{});await turn();
  snapGates[0].resolve(mkSnap('U'));
  // The unavailable load's own refresh refuses the A snapshot
  // (fail-closed); it publishes nothing, and the A-token waiter claims
  // its own fresh load instead of coalescing onto unavailable authority.
  await assert.rejects(pU,/Active book changed/);
  await turn();await turn();
  assert.strictEqual(snapGates.length,2,'A-token waiter must claim a fresh load, not reuse unavailable authority');
  snapGates[1].resolve(mkSnap('W'));await pW;
  finished=true;console.log('FINISHED-OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self._run(code)


class LoadFencingJsTests(unittest.TestCase):
    def test_old_last_never_publishes(self):
        # Separate gate arrays per load (not one shared Map): old load gates
        # 0..3, new load gates 4..7. Finish NEW first, then OLD last; both
        # must actually complete and assert final cards AND caches stay new.
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let finished=false;process.on('beforeExit',()=>assert(finished,'pending Promise must fail, never count as green'));
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const fields={},gates=[],draws=[];let saveGate=deferred();
const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'old',style:{}});
const ctx={currentBookFilename:'book.txt',ensureVoiceRosterCache:()=>{},performance:{now:()=>0},document:{getElementById:el},console:{debug(){}},
 API:{get:url=>{const gate=deferred();gate.url=url;gates.push(gate);return gate.promise;}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>0,isDirty:()=>false},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){},
 createVoiceCard:voice=>{draws.push(voice.name);return `<card>${voice.name}</card>`;}};
ctx.window={_bookSelectionEpoch:1,_selectedUploadIdentity:null,_castLibraryRequest:0};
ctx._designedVoicesCache=[];ctx._cloneVoicesCache=[];ctx._loraModelsCache=[];
vm.createContext(ctx);
const load=(a,b)=>{const first=source.indexOf(a);const second=source.indexOf(b,first);assert(first>=0&&second>first,'extraction boundary must be valid for '+a);vm.runInContext(source.slice(first,second),ctx);};
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()');
vm.runInContext('let _voiceSaveSnapshot=null;let _voiceRecoveryDrafts=[];',ctx);
ctx.loadCastLibrary=async()=>{};
ctx.window._bookSelectionEpoch=1;
saveGate.resolve();
const mkSnap=name=>({revision:'0'.repeat(64),book_token:'b'.repeat(64),book_identity_token:'b'.repeat(64),voices:[{name,config:{type:'custom',voice:'Ryan'}}],seed_changes:[]});
(async()=>{
  const oldLoad=ctx.loadVoices(true);oldLoad.catch(()=>{});await turn();await turn();
  assert.strictEqual(gates.length,4,'old load must have 4 pending gates (snapshot+3 resources)');
  const oldGates=gates.slice(0,4);
  const newLoad=ctx.loadVoices(true);newLoad.catch(()=>{});await turn();await turn();
  assert.strictEqual(gates.length,8,'new load must have its own 4 gates');
  const newGates=gates.slice(4,8);
  // Finish NEW first with new-* resource ids
  for(const gate of newGates){gate.resolve(gate.url==='/api/voice_config/snapshot'?mkSnap('Newer'):[{id:'new-'+gate.url}]);}
  await newLoad;
  assert.strictEqual(el('voices-list').innerHTML,'<card>Newer</card>');
  assert.strictEqual(ctx.window._loraModelsCache[0].id,'new-/api/lora/models','new cache must publish');
  // Finish OLD last with old-* resource ids; must not overwrite cards or caches
  for(const gate of oldGates){gate.resolve(gate.url==='/api/voice_config/snapshot'?mkSnap('Older'):[{id:'old-'+gate.url}]);}
  await oldLoad.catch(()=>{});
  assert.strictEqual(el('voices-list').innerHTML,'<card>Newer</card>','older load must not overwrite newer cards');
  assert.strictEqual(ctx.window._loraModelsCache[0].id,'new-/api/lora/models','older load must not overwrite newer caches');
  finished=true;console.log('FINISHED-OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(CORE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('FINISHED-OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
