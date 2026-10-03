"""User-requested seed repair preserves explicit choices, snapshots and original bytes."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voices
from utils import character_voice_seed


class VoiceSeedRepairTests(unittest.TestCase):
    def fixture(self, root, stack):
        raw = {'ALICE': {'type': 'custom', 'voice': 'Aiden', 'seed': '-1', 'audit': {'keep': True}},
               'BOB': {'type': 'clone', 'seed': '12', 'ref_audio': 'unchanged.wav'},
               'CAROL': {'type': 'design', 'seed': ''}, 'DAVE': {'type': 'lora'},
               'EVE': {'type': 'custom', 'seed': '0'}}
        path = root / 'voice_config.json'
        path.write_text(json.dumps(raw, indent=3) + '\n')
        script = root / 'annotated_script.json'
        script.write_text(json.dumps([{'speaker': name, 'text': name} for name in raw]))
        (root / 'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
        stack.enter_context(patch.object(voices, 'VOICE_CONFIG_PATH', str(path)))
        stack.enter_context(patch.object(voices, 'SCRIPT_PATH', str(script)))
        app = FastAPI(); app.include_router(voices.router)
        return stack.enter_context(TestClient(app)), path, raw

    def payload(self, snapshot):
        return {key: snapshot[key] for key in ('revision', 'book_token')}

    def test_native_preview_apply_preserves_explicit_seeds_metadata_and_original_backup(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); client, path, raw = self.fixture(root, stack)
            before = path.read_bytes()
            snapshot = client.get('/api/voice_config/snapshot').json()
            expected = [{'name': name, 'seed': str(character_voice_seed(name))}
                        for name in ('ALICE', 'CAROL', 'DAVE')]
            self.assertEqual(expected, snapshot.get('seed_changes'))
            self.assertEqual(before, path.read_bytes())
            result = client.post('/api/voice_config/seed_unseeded', json=self.payload(snapshot))
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual(expected, result.json()['changes'])
            backup = root / result.json()['backup']
            self.assertTrue(backup.exists(), 'Original-file backup must exist before success')
            self.assertEqual(before, backup.read_bytes())
            saved = json.loads(path.read_bytes())
            for change in expected:
                raw[change['name']]['seed'] = change['seed']
            self.assertEqual(raw, saved)
            fresh = client.get('/api/voice_config/snapshot').json()
            self.assertEqual([], fresh['seed_changes'])
            unchanged = path.read_bytes()
            repeated = client.post('/api/voice_config/seed_unseeded', json=self.payload(fresh))
            self.assertEqual(200, repeated.status_code, repeated.text)
            self.assertIsNone(repeated.json()['backup'])
            self.assertEqual(unchanged, path.read_bytes())
            self.assertEqual(1, len(list(root.glob('voice_config.json.bak-*'))))

    def test_stale_voice_or_book_refuses_without_backup_or_write(self):
        for changed in ('voice', 'book'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp); client, path, raw = self.fixture(root, stack)
                snapshot = client.get('/api/voice_config/snapshot').json()
                if changed == 'voice':
                    raw['ALICE']['seed'] = '7'; path.write_text(json.dumps(raw))
                else:
                    (root / 'state.json').write_text('{"active_book_id":"other","book_generation":"two"}')
                before = path.read_bytes()
                result = client.post('/api/voice_config/seed_unseeded', json=self.payload(snapshot))
                self.assertEqual(409, result.status_code, result.text)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual([], list(root.glob('voice_config.json.bak-*')))

    def test_backup_failure_preserves_config_and_refuses_success(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); client, path, raw = self.fixture(root, stack)
            snapshot = client.get('/api/voice_config/snapshot').json(); before = path.read_bytes()
            with patch('voice_config_store.shutil.copy2', side_effect=OSError('fixture backup failed')):
                with self.assertRaisesRegex(OSError, 'fixture backup failed'):
                    client.post('/api/voice_config/seed_unseeded', json=self.payload(snapshot))
            self.assertEqual(before, path.read_bytes())


class VoiceSeedRepairUiTests(unittest.TestCase):
    def test_native_preview_one_click_and_changed_displayed_snapshot_refusal(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            fixture = VoiceSeedRepairTests()
            client, path, raw = fixture.fixture(root, stack)
            raw['<ALICE>&'] = raw.pop('ALICE')
            path.write_text(json.dumps(raw))
            (root / 'annotated_script.json').write_text(json.dumps([{'speaker': name, 'text': name} for name in raw]))
            native = client.get('/api/voice_config/snapshot').json()
        source = Path(__file__).parent.parent / 'static/js/app-core.js'
        harness = r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const native=JSON.parse(process.argv[2]),source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('let _voiceSeedRepairPending ='),end=source.indexOf('async function loadVoices(',start);
assert(start>=0 && end>start,'Seed UI handlers must be installed');
const posts=[],events=[],toasts=[];let dirty=false,failFlush=false;
const ctx={window:null,_voiceSaveSnapshot:native,_voiceCardsRevision:native.revision,_voiceCardsBookToken:native.book_token,
voiceSaveQueue:{isDirty:()=>dirty},escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
flushVoiceSaves:async()=>{events.push('flush');if(failFlush){throw Error('pending edit failed');}},
loadVoices:async full=>{assert.strictEqual(full,false);events.push('load');},showToast:message=>toasts.push(message),
API:{post:async(url,data)=>{events.push('post');posts.push({url,data});return{changes:native.seed_changes,backup:'voice_config.json.bak-fixture'};}}};
ctx.window=ctx;vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);
const markup=ctx.getVoiceSeedRepairMarkup(native);
assert(markup.includes('&lt;ALICE&gt;&amp;'));assert(!markup.includes('<ALICE>'));
for(const change of native.seed_changes){assert(markup.includes(change.seed));}
assert(markup.includes('original-file backup'));assert(markup.includes('Existing rendered audio is kept'));
assert(markup.includes('fast-batch'));assert(markup.includes('onclick="applyStableVoiceSeeds()"'));
assert.strictEqual(ctx.getVoiceSeedRepairMarkup({...native,seed_changes:[]}), '');
let finished=false;process.on('beforeExit',()=>assert(finished,'All seed UI assertions must finish'));
(async()=>{
const container={innerHTML:''};ctx.document={getElementById:id=>id==='voices-list'?container:null};
ctx.performance={now:()=>0};ctx._voiceResourcesRefreshedAt=-Infinity;ctx._voiceRecoveryDrafts=[];
ctx.refreshVoiceMetadata=async()=>native.voices;ctx.refreshVoicesScope=()=>{};ctx.updateNarratorPreviewFields=()=>{};
ctx.loadCastLibrary=async()=>{};ctx.API.get=async()=>[];ctx.createVoiceCard=()=>'<article>fixture card</article>';
ctx.renderReadyCount=()=>{};ctx.onToggleHideReady=()=>{};ctx.window._voicesByName={};
vm.runInContext(source.slice(source.indexOf('async function loadVoices('),source.indexOf('window.selectVoiceVersion =')),ctx);
await ctx.loadVoices();assert(container.innerHTML.includes('onclick="applyStableVoiceSeeds()"'),'Real voice loading must display the action');
assert(container.innerHTML.includes('&lt;ALICE&gt;&amp;'));assert(container.innerHTML.includes('fixture card'));
ctx.loadVoices=async full=>{assert.strictEqual(full,false);events.push('load');};events.length=0;
assert.strictEqual(posts.length,0,'Showing the preview must not write');
await ctx.applyStableVoiceSeeds();assert.strictEqual(posts.length,1);
assert.strictEqual(posts[0].url,'/api/voice_config/seed_unseeded');
assert.strictEqual(posts[0].data.revision,native.revision);assert.strictEqual(posts[0].data.book_token,native.book_token);
assert.deepStrictEqual(events,['flush','post','load']);assert(toasts.at(-1).includes('Backup: voice_config.json.bak-fixture'));
for(const changed of ['dirty','revision','book']){
 dirty=changed==='dirty';ctx._voiceCardsRevision=changed==='revision'?'0'.repeat(64):native.revision;
 ctx._voiceCardsBookToken=changed==='book'?'0'.repeat(64):native.book_token;
 await ctx.applyStableVoiceSeeds();assert.strictEqual(posts.length,1,'Changed displayed preview cannot apply');
 assert(toasts.at(-1).includes('reload voices'));
}
dirty=false;ctx._voiceCardsRevision=native.revision;ctx._voiceCardsBookToken=native.book_token;
failFlush=true;await ctx.applyStableVoiceSeeds();assert.strictEqual(posts.length,1);assert(toasts.at(-1).includes('pending edit failed'));
finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', harness, str(source), json.dumps(native)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
