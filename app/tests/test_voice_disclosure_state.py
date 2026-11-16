"""Native clicks exercise the real voice-card renderer and async actions."""
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.test_dropdown_layers import find_browser
from tests.test_table_themes import extract_function

STATIC = Path(__file__).resolve().parents[1] / 'static'
CORE = Path(os.environ.get('VOICE_DISCLOSURE_SOURCE', STATIC / 'js/app-core.js'))


def run_disclosure_cases():
    source = CORE.read_text(encoding='utf-8')
    def section(start, end):
        return source[source.index(start):source.index(end, source.index(start))]
    page = (STATIC / 'index.html').read_text(encoding='utf-8')
    page = re.sub(r'<script\b[^>]*>.*?</script>', '', page, flags=re.S | re.I)
    page = page.replace('/static/', STATIC.as_uri() + '/')
    script = (STATIC / 'js/app-i18n.js').read_text(encoding='utf-8')
    script += section('// Voice roster dropdowns:', '// End voice roster dropdowns.')
    script += extract_function(source, 'escapeHtml')
    script += section('const THEMES =', 'function createSerializedSaveQueue(')
    script += extract_function(source, 'getInlineStringArgument')
    script += section('function getVoiceCandidateMarkup(', 'function getLibraryVoiceReference(')
    script += extract_function(source, 'getTraitBadgeHtml')
    script += section('function getSavedAdapterOption(', 'function showVoiceTypeOptions(')
    script += section('function getVoiceCardMetadata(', '// Suggest members')
    script += section('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =')
    script += section('async function flushVoiceSaves(', 'async function discardVoiceEditsAndReload()')
    script += section('window.setVoiceApproval =', 'window.editPersonaVoiceAudit =')
    script += section('window.selectVoiceCandidate =', '// --- Auto-suggest best LoRA')
    script += Path(__file__).with_name('voice_disclosure_fixture.js').read_text(encoding='utf-8')
    page = page.replace('</body>', '<script>' + script + '</script></body>')
    with tempfile.TemporaryDirectory(prefix='aa2-voice-disclosure-') as directory:
        fixture = Path(directory) / 'fixture.html'
        fixture.write_text(page, encoding='utf-8')
        result = subprocess.run([
            find_browser(), '--headless=new', '--disable-gpu', '--no-sandbox',
            '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
            '--user-data-dir=' + str(Path(directory) / 'profile'),
            '--window-size=390,844', '--virtual-time-budget=6000', '--dump-dom', fixture.as_uri(),
        ], capture_output=True, encoding='utf-8', errors='replace', timeout=45)
    match = re.search(r'<pre id="voice-disclosure-results">(.*?)</pre>', result.stdout, re.S)
    if not match:
        raise AssertionError('Browser fixture did not finish: ' + result.stderr[-2000:])
    data = json.loads(html.unescape(match.group(1)))
    if data.get('error'):
        raise AssertionError(data['error'])
    return data


class VoiceDisclosureStateTests(unittest.TestCase):
    def test_native_actions_defaults_bulk_and_persistence(self):
        self.assertEqual(run_disclosure_cases(), {
            'actions': True, 'defaults': True, 'bulk': True,
            'persistence': True, 'detached': True, 'themes': True,
        })
