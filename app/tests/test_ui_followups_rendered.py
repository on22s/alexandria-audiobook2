"""Rendered #1938 controls and real browser audio decoding; no model calls."""
import base64
import html
import io
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import tempfile
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
    with tempfile.TemporaryDirectory(prefix='aa2-followups-') as directory:
        fixture = Path(directory) / 'fixture.html'
        fixture.write_text(source, encoding='utf-8')
        result = subprocess.run([find_browser(), '--headless=new', '--disable-gpu', '--no-sandbox',
            '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
            '--user-data-dir=' + str(Path(directory) / 'profile'), f'--window-size={width},1000',
            '--virtual-time-budget=6000', '--dump-dom', fixture.as_uri()],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    match = re.search(r'<pre id="followup-results">(.*?)</pre>', result.stdout, re.S)
    if not match:
        raise RuntimeError('Rendered follow-ups did not finish: ' + result.stderr[-1000:])
    data = json.loads(html.unescape(match.group(1)))
    if data.get('error'):
        raise RuntimeError(data['error'])
    if len(data['rows']) != 6:
        raise RuntimeError('All six themes must render')
    return data['rows']


class RenderedUiFollowupTests(unittest.TestCase):
    def test_waveform_seek_decode_and_theme_states_at_desktop_width(self):
        for case in get_rendered_cases(1280):
            with self.subTest(theme=case['theme']):
                self.assertTrue(all(value for key, value in case.items() if key != 'theme'), case)

    def test_metrics_remain_available_on_compact_navigation(self):
        for case in get_rendered_cases(500):
            with self.subTest(theme=case['theme']):
                self.assertTrue(case['buildVisible'] and case['badges'], case)
