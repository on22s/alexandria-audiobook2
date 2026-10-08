"""Designer create/update identity survives cache replacement and name collisions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voice_design
from tests.test_designer_response_identity_js import SETUP, SOURCE as DEFAULT_SOURCE

SOURCE = Path(os.environ.get('DESIGN_IDENTITY_SOURCE', DEFAULT_SOURCE))


class DesignerResourceIdentityJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(DEFAULT_SOURCE.with_name('app-core.js'))], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_resource_update_id_survives_missing_cache_and_persona_name_collision_creates(self):
        self.run_js(r'''
const s=setup();s.context.CSS={escape:value=>value};s.context.document.querySelector=selector=>selector.startsWith('.voice-card')?null:{click(){}};
s.context.window._designedVoicesCache=[{id:'resource',name:'Saved voice',filename:'saved.wav'}];const opening=s.context.window.openDesignedVoiceForEdit('resource');s.gets[0].resolve([]);await opening;
s.context.window._designedVoicesCache=[];s.context.window._currentPreviewFile='edited.wav';s.context.window._designerPreviewInputs={file:'edited.wav',description:s.elements['design-description'].value.trim(),sample_text:s.elements['design-sample-text'].value.trim()};const saving=s.context.window.saveDesignedVoice();assert.strictEqual(s.posts[0].args[1].voice_id,'resource','refreshing cache must not turn update into create');s.posts[0].reject(Error('resource was deleted'));await saving;assert.strictEqual(s.context.window._editingDesignedVoiceId,'resource');assert.strictEqual(s.elements['design-voice-name'].value,'Saved voice');
s.context.document.querySelector=selector=>selector.startsWith('.voice-card')?null:{click(){}};await s.select('resource');assert.strictEqual(s.context.window._editingDesignedVoiceId,null);assert.strictEqual(s.elements['design-source-name'].value,'resource');
s.context.window._designedVoicesCache=[{id:'resource',name:'Unrelated resource'}];s.context.window._currentPreviewFile='persona.wav';s.context.window._designerPreviewInputs={file:'persona.wav',description:s.elements['design-description'].value.trim(),sample_text:s.elements['design-sample-text'].value.trim()};const create=s.context.window.saveDesignedVoice();assert.strictEqual(s.posts[1].args[1].voice_id,null,'character name must not name a resource to update');s.posts[1].resolve({});await create;
''')

    def test_actual_packets_update_one_native_artifact_then_create_without_overwriting_collision(self):
        packets = json.loads(self.run_js(r'''
const s=setup();s.context.CSS={escape:value=>value};s.context.document.querySelector=selector=>selector.startsWith('.voice-card')?null:{click(){}};s.context.window._designedVoicesCache=[{id:'resource',name:'Existing',filename:'saved.wav'}];
const opening=s.context.window.openDesignedVoiceForEdit('resource');s.gets[0].resolve([]);await opening;s.context.window._designedVoicesCache=[];s.context.window._currentPreviewFile='edited.wav';s.context.window._designerPreviewInputs={file:'edited.wav',description:s.elements['design-description'].value.trim(),sample_text:s.elements['design-sample-text'].value.trim()};s.elements['design-voice-name'].value='Updated resource';const update=s.context.window.saveDesignedVoice();const a=s.posts[0].args[1];s.posts[0].resolve({});await update;
s.context.document.querySelector=selector=>selector.startsWith('.voice-card')?null:{click(){}};await s.select('resource');s.context.window._designedVoicesCache=[{id:'resource',name:'Updated resource',filename:'saved.wav'}];s.context.window._currentPreviewFile='persona.wav';s.context.window._designerPreviewInputs={file:'persona.wav',description:s.elements['design-description'].value.trim(),sample_text:s.elements['design-sample-text'].value.trim()};const create=s.context.window.saveDesignedVoice();const b=s.posts[1].args[1];s.posts[1].resolve({});await create;console.log(JSON.stringify([a,b]));
'''))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'previews').mkdir()
            (root / 'previews/edited.wav').write_bytes(b'updated preview fixture')
            (root / 'previews/persona.wav').write_bytes(b'new persona preview fixture')
            (root / 'saved.wav').write_bytes(b'original voice fixture')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps([{'id':'resource','name':'Existing','filename':'saved.wav'}]))
            app = FastAPI()
            app.include_router(voice_design.router)
            with patch.object(voice_design, 'DESIGNED_VOICES_DIR', str(root)), patch.object(voice_design, 'DESIGNED_VOICES_MANIFEST', str(manifest)), TestClient(app) as client:
                update = client.post('/api/voice_design/save', json=packets[0])
                self.assertEqual(200, update.status_code, update.text)
                self.assertEqual('updated', update.json()['status'])
                self.assertEqual(1, len(json.loads(manifest.read_text())))
                self.assertEqual(b'updated preview fixture', (root / 'saved.wav').read_bytes())
                created = client.post('/api/voice_design/save', json=packets[1])
                self.assertEqual(200, created.status_code, created.text)
                rows = json.loads(manifest.read_text())
                self.assertEqual(2, len(rows))
                self.assertNotEqual('resource', created.json()['voice_id'])
                self.assertEqual(b'updated preview fixture', (root / 'saved.wav').read_bytes())
                new_row = next(row for row in rows if row['id'] == created.json()['voice_id'])
                self.assertEqual(b'new persona preview fixture', (root / new_row['filename']).read_bytes())
                manifest_before = manifest.read_bytes()
                missing = client.post('/api/voice_design/save', json={**packets[0], 'voice_id':'deleted-resource'})
                self.assertEqual(404, missing.status_code)
                self.assertEqual(manifest_before, manifest.read_bytes())
