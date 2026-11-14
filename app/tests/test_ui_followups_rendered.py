"""Rendered #1938 controls and real browser audio decoding; no model calls."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import threading
import unittest
import wave

from tests.test_dropdown_layers import find_browser
from tests.test_table_themes import extract_function

STATIC = Path(__file__).resolve().parents[1] / 'static'


def get_rendered_cases(width):
    source = (STATIC / 'index.html').read_text(encoding='utf-8')
    source = re.sub(r'<script\b[^>]*>.*?</script>', '', source, flags=re.S | re.I)
    source = re.sub(r'<link\b[^>]*>', '', source, flags=re.I)
    css = (STATIC / 'vendor/bootstrap-5.3.0/bootstrap.min.css').read_text(encoding='utf-8')
    source = source.replace('<style>', '<style>' + css + '</style><style>', 1)
    core = (STATIC / 'js/app-core.js').read_text(encoding='utf-8')
    review = (STATIC / 'js/app-editor-review.js').read_text(encoding='utf-8')
    functions = re.search(r'const THEMES = \[[\s\S]*?\];', core).group(0)
    functions += '\n' + extract_function(core, 'applyTheme')
    functions += '\n' + review[review.index('let editorWaveformContext ='):review.index('function getEditorToolPanels(')]
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b''.join(struct.pack('<h', int(1000 * math.sin(i * math.tau * 440 / 8000))) for i in range(32000)))
    functions = 'const chunkSnapshotBookToken = "synthetic-book";\n' + functions
    functions += '\nconst SYNTHETIC_WAV = ' + json.dumps('data:audio/wav;base64,' + base64.b64encode(audio.getvalue()).decode()) + ';\n'
    functions += "function t(key,english,values={}) { return english.replace(/\\{(\\w+)\\}/g,(_,name)=>values[name]); }\n"
    functions += Path(__file__).with_name('ui_followups_fixture.js').read_text(encoding='utf-8')
    source = source.replace('</body>', '<script>' + functions + '</script></body>')
    # Browser media decoding runs outside Chromium's virtual clock. Wait for
    # the fixture's completion receipt in real time instead of dumping the DOM
    # while loadedmetadata/decodeAudioData can still be pending.
    functions = """
    window.uiFollowupChecks.then(async () => {
        await fetch('/results', {method: 'POST', body:
            document.getElementById('followup-results').textContent});
    });
    """
    source = source.replace('</body>', '<script>' + functions + '</script></body>')
    data = get_browser_result(source, width)
    if data.get('error'):
        raise RuntimeError(data['error'])
    if len(data['rows']) != 6:
        raise RuntimeError('All six themes must render')
    return data['rows']


def get_browser_result(source, width, timeout=30):
    completed = threading.Event()
    result = {}
    diagnostics = '''<script>
    window.addEventListener('error', event => {
        fetch('/results', {method: 'POST', body: JSON.stringify({error: event.message})});
    });
    </script>'''
    document = (source.replace('<head>', '<head>' + diagnostics, 1) if '<head>' in source else diagnostics + source).encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                # Chromium may drop an idle keep-alive connection on shutdown.
                pass

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def do_POST(self):
            if self.path == '/progress':
                result['stage'] = self.rfile.read(int(self.headers['Content-Length'])).decode('utf-8')
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path != '/results':
                self.send_error(404)
                return
            try:
                result['data'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            except (ValueError, KeyError) as error:
                result['data'] = {'error': str(error)}
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            completed.set()

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory(prefix='aa2-followups-') as directory:
                with tempfile.TemporaryFile() as errors:
                    browser = subprocess.Popen([find_browser(), '--headless=new', '--disable-gpu',
                        '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking',
                        '--no-first-run', '--no-default-browser-check',
                        '--user-data-dir=' + str(Path(directory) / 'profile'),
                        f'--window-size={width},1000', f'http://127.0.0.1:{server.server_port}/'],
                        stdout=subprocess.DEVNULL, stderr=errors)
                    try:
                        if not completed.wait(timeout):
                            errors.seek(0)
                            diagnostics = errors.read().decode('utf-8', errors='replace')[-2000:]
                            raise RuntimeError('Rendered follow-ups did not complete at ' + result.get('stage', 'page startup') + ': ' + diagnostics)
                        return result['data']
                    finally:
                        browser.terminate()
                        try:
                            browser.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            browser.kill()
                            browser.wait(timeout=10)
        finally:
            server.shutdown()
            worker.join(timeout=5)


class RenderedUiFollowupTests(unittest.TestCase):
    def test_browser_waits_for_async_fixture_completion(self):
        source = """<script>
        setTimeout(async () => {
            await fetch('/results', {method: 'POST', body: JSON.stringify({finished: true})});
        }, 750);
        </script>"""
        self.assertEqual({'finished': True}, get_browser_result(source, 500))

    def test_waveform_seek_decode_and_theme_states_at_desktop_width(self):
        for case in get_rendered_cases(1280):
            with self.subTest(theme=case['theme']):
                self.assertTrue(all(value for key, value in case.items() if key != 'theme'), case)

    def test_metrics_remain_available_on_compact_navigation(self):
        for case in get_rendered_cases(500):
            with self.subTest(theme=case['theme']):
                self.assertTrue(case['buildVisible'] and case['badges'], case)
