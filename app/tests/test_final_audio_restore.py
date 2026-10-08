"""Existing audiobook downloads survive page reload; missing exports stay hidden."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor


class FinalAudioRestoreTests(unittest.TestCase):
    def test_head_checks_the_real_download_without_transferring_audio(self):
        app = FastAPI()
        app.include_router(editor.router)
        with tempfile.TemporaryDirectory() as tmp, patch.object(editor, 'AUDIOBOOK_PATH', str(Path(tmp) / 'book.mp3')):
            with TestClient(app) as client:
                self.assertEqual(404, client.head('/api/audiobook').status_code)
                Path(editor.AUDIOBOOK_PATH).write_bytes(b'existing audiobook bytes')
                response = client.head('/api/audiobook')
                self.assertEqual(200, response.status_code)
                self.assertEqual(b'', response.content)
                self.assertEqual(str(len(b'existing audiobook bytes')), response.headers['content-length'])
                self.assertEqual(b'existing audiobook bytes', client.get('/api/audiobook').content)
                Path(editor.AUDIOBOOK_PATH).unlink()
                self.assertEqual(404, client.head('/api/audiobook').status_code)

    def test_native_result_tab_restores_download_and_rejects_stale_missing_or_failed_checks(self):
        source = Path(__file__).parent.parent / 'static/js/app-core.js'
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),pending=[],elements={};
const el=id=>elements[id]||(elements[id]={style:{},textContent:'',src:'',href:'',pause(){this.paused=true;},removeAttribute(name){delete this[name];},classList:{contains:()=>false}});
const link={dataset:{tab:'audio'},classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){}};
const ctx={window:{location:{hash:'#audio'},innerWidth:1200},rememberTab(){},getTabLink:()=>link,document:{getElementById:el,querySelectorAll:selector=>selector==='.nav-link'?[link]:[el('audio-tab')]},console:{error(){}},fetch:(url,options)=>{assert.strictEqual(url,'/api/audiobook');assert.strictEqual(options.method,'HEAD');assert.strictEqual(options.cache,'no-store');return new Promise((resolve,reject)=>pending.push({resolve,reject}));}};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('let finalAudioRequest ='),source.indexOf('async function pollLogs(')),ctx);
vm.runInContext(source.slice(source.indexOf('function activateTab('),source.indexOf('function restoreTab(')),ctx);
const turn=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
ctx.activateTab('audio',false);assert.strictEqual(pending.length,1);
pending.shift().resolve({ok:true,status:200});await turn();
assert.strictEqual(el('audio-player-container').style.display,'block');assert.strictEqual(el('audio-empty-state').style.display,'none');assert.match(el('main-audio').src,/^\/api\/audiobook\?t=\d+$/);assert.strictEqual(el('download-link').href,el('main-audio').src);
const old=ctx.loadFinalAudio(),newer=ctx.loadFinalAudio();
pending[1].resolve({ok:false,status:404});await newer;
assert.strictEqual(el('audio-player-container').style.display,'none');assert.strictEqual(el('audio-empty-state').style.display,'');assert.strictEqual(el('main-audio').paused,true);assert.strictEqual(el('download-link').href,undefined);
pending[0].resolve({ok:true,status:200});await old;pending.length=0;
assert.strictEqual(el('audio-player-container').style.display,'none','stale availability must not resurrect another book');
for(const failure of ['http','network']){const request=ctx.loadFinalAudio();const call=pending.shift();if(failure==='http'){call.resolve({ok:false,status:500});}else{call.reject(Error('offline'));}await request;assert.match(el('audio-empty-state').textContent,/Could not check/);}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
