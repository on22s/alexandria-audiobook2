"""Actual UI URLs must fetch the raw local filename after one HTTP decode."""
import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qs

import requests
import socket
import sys
import time
from tests.test_designer_response_identity_js import SETUP, SOURCE

SCRIPT = r'''(async()=>{
const rows=[];
// The shared Voices-tab player lives in app-core.js (#1933); here only the URL
// it is asked to play matters, so a stand-in plays getUrl() once.
const withPlayer=s=>{s.context.window.toggleVoicePreviewPlayback=(button,getUrl)=>{const url=getUrl();if(url){new s.context.Audio(url).play();}};return s;};
for(const filename of ['plain.wav','legacy#voice.wav','legacy?voice.wav','legacy%voice.wav','legacy%2Fvoice.wav','普通 voice.wav']){
 const s=withPlayer(setup()),urls=[];s.context.Audio=class{constructor(src){this.src=src;urls.push(src);}play(){}};
 s.context.window.playDesignedVoice(filename);
 s.context.window.playCloneVoice({closest:()=>({querySelector:()=>({value:'clone_voices/'+filename})})});
 s.context.window._designedVoicesCache=[{id:'voice',filename}];
 const editing=s.context.window.openDesignedVoiceForEdit('voice');s.gets[0].resolve([]);await editing;
 urls.push(s.elements['design-preview-audio'].src);
 if(s.context.window._currentPreviewFile!==filename || s.context.window._editingDesignedVoiceId!=='voice'){throw new Error('stored filename/id changed');}
 rows.push(...urls.map((url,index)=>({url,raw:(index===1?'clone_voices/':'designed_voices/')+filename})));
 const count=urls.length;s.context.window.playCloneVoice({closest:()=>({querySelector:()=>({value:''})})});
 if(urls.length!==count){throw new Error('empty reference played');}
}
const s=withPlayer(setup()),urls=[];s.context.Audio=class{constructor(src){urls.push(src);}play(){}};
const raw='clone_voices/nested/普通 #?%2F.wav';
s.context.window.playCloneVoice({closest:()=>({querySelector:()=>({value:raw})})});
rows.push({url:urls[0],raw});
for(const row of rows){const parsed=new URL(row.url,'http://localhost');row.url=parsed.pathname+parsed.search+parsed.hash;}
console.log(JSON.stringify(rows));
})().catch(e=>{console.error(e);process.exit(1)});'''


class LocalAudioUrlTests(unittest.TestCase):
    def get_actual_ui_urls(self, native_files=False):
        script = SCRIPT.replace("legacy?voice.wav", "legacy&voice.wav").replace("#?%2F.wav", "#&%2F.wav") if native_files and os.name == "nt" else SCRIPT
        result = subprocess.run(['node', '-e', SETUP + script, str(SOURCE), str(SOURCE.with_name('app-core.js'))],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_actual_handlers_preserve_raw_paths_and_cache_query(self):
        rows = self.get_actual_ui_urls()
        self.assertEqual(19, len(rows))
        for row in rows:
            with self.subTest(path=row['raw']):
                url = urlsplit(row['url'])
                self.assertEqual('', url.fragment)
                self.assertEqual('/' + row['raw'], unquote(url.path))
                self.assertEqual(['t'], list(parse_qs(url.query)))
                self.assertTrue(parse_qs(url.query)['t'][0].isdigit())
        plain = rows[0]['url']
        self.assertTrue(plain.startswith('/designed_voices/plain.wav?t='), plain)

    def test_actual_ui_urls_fetch_distinct_static_file_bytes(self):
        rows = self.get_actual_ui_urls(native_files=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix in ('designed_voices', 'clone_voices'):
                (root / prefix).mkdir()
            expected = {}
            for row in rows:
                path = root / row['raw']
                path.parent.mkdir(parents=True, exist_ok=True)
                expected[row['raw']] = b'unique file: ' + row['raw'].encode()
                path.write_bytes(expected[row['raw']])
            # A decoy proves a literal %2F must not resolve to a directory slash.
            decoy = root / 'designed_voices/legacy/voice.wav'
            decoy.parent.mkdir()
            decoy.write_bytes(b'wrong decoded separator target')
            server_source = """import sys
from pathlib import Path
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
import uvicorn, socket
from contextlib import asynccontextmanager
root = Path(sys.argv[1])
@asynccontextmanager
async def lifespan(app):
    (root / 'http-ready').write_text('ready')
    yield
app = FastAPI(lifespan=lifespan)
for prefix in ('designed_voices', 'clone_voices'):
    app.mount('/' + prefix, StaticFiles(directory=root / prefix))
listener = socket.socket(fileno=int(sys.argv[2])) if sys.argv[3] == 'fd' else socket.fromshare(bytes.fromhex(sys.argv[2]))
uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='error')).run(sockets=[listener])
"""
            with socket.socket() as listener, tempfile.TemporaryFile(mode='w+') as log, requests.Session() as http:
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                base = f'http://127.0.0.1:{listener.getsockname()[1]}'
                fd = listener.fileno()
                if os.name == 'nt':
                    # Share the already-bound socket with the new Windows PID;
                    # the child waits for stdin before constructing its socket.
                    native_source = server_source.replace("listener = socket.socket", "sys.argv[2] = sys.stdin.readline().strip()\nlistener = socket.socket")
                    server = subprocess.Popen([sys.executable, '-c', native_source, str(root), '', 'share'],
                                              stdin=subprocess.PIPE, stdout=log, stderr=log, text=True)
                    server.stdin.write(listener.share(server.pid).hex() + '\n')
                    server.stdin.close()
                else:
                    server = subprocess.Popen([sys.executable, '-c', server_source, str(root), str(fd), 'fd'],
                                              pass_fds=(fd,), stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if server.poll() is not None:
                            log.seek(0)
                            self.fail('isolated HTTP server exited: ' + log.read())
                        if (root / 'http-ready').exists():
                            break
                        time.sleep(.02)
                    else:
                        log.seek(0)
                        self.fail('isolated HTTP server did not become ready: ' + log.read())
                    for row in rows:
                        with self.subTest(path=row['raw']):
                            response = http.get(base + row['url'], timeout=3)
                            self.assertEqual(200, response.status_code)
                            self.assertEqual(expected[row['raw']], response.content)
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
            for raw, data in expected.items():
                self.assertEqual(data, (root / raw).read_bytes())
