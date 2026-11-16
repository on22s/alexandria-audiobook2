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


def run_storage_case(case):
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8'), values = new Map(), warnings = [];
let clock = 0;
const storage = {get length() {return values.size;}, key: index => [...values.keys()][index],
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key)};
const context = {localStorage: storage, console: {warn: (...args) => warnings.push(args)}, Date: {now: () => ++clock}};
context.window = context;
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function getVoiceSectionStorageKey('), source.indexOf('function createVoiceCard(')), context);
''' + case
    result = subprocess.run(['node', '-e', script, str(CORE)], capture_output=True,
                            text=True, encoding='utf-8', timeout=15)
    if result.returncode:
        raise AssertionError(result.stderr)


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
    def test_caps_recent_books_and_sections_and_migrates_legacy_keys(self):
        run_storage_case(r'''
values.set('alex-voice-section-collapsed-old-A', 'true');
values.set('alex-voice-section-collapsed-old-B', 'false');
values.set('unrelated-preference', 'keep');
for (let index = 0; index < 25; index++) {context.saveVoiceSectionStates('book-' + index, {'synthetic': false});}
const bookKey = context.getVoiceSectionStorageKey;
assert.strictEqual([...values.keys()].filter(key => key.startsWith('alex-voice-sections:')).length, 20);
assert.strictEqual(context._voiceSectionStates.size, 20);
assert(!values.has(bookKey('book-0')) && values.has(bookKey('book-24')));
assert(!values.has('alex-voice-section-collapsed-old-A') && !values.has('alex-voice-section-collapsed-old-B'));
assert.strictEqual(values.get('unrelated-preference'), 'keep');
context.saveVoiceSectionStates('book-5', context.ensureVoiceSectionState('book-5').sections);
context.saveVoiceSectionStates('book-25', {});
assert(values.has(bookKey('book-5')) && !values.has(bookKey('book-6')), 'retention uses the most recent access');
const sections = Object.fromEntries(Array.from({length: 1005}, (_, index) => [context.getVoiceSectionId('voice-' + index), false]));
context.saveVoiceSectionStates('book-25', sections);
assert.strictEqual(Object.keys(JSON.parse(values.get(bookKey('book-25'))).sections).length, 1000);
delete context._voiceSectionStates;
assert.strictEqual(Object.keys(context.ensureVoiceSectionState('book-25').sections).length, 1000);
assert(context.isVoiceSectionOpen('book-25', 'voice-0'));
assert(!context.isVoiceSectionOpen('book-25', 'voice-1004'));
context.pruneVoiceSectionStates('book-25', [{name: 'voice-1004'}]);
assert.strictEqual(Object.keys(JSON.parse(values.get(bookKey('book-25'))).sections).length, 1);
context.pruneVoiceSectionStates('book-25', []);
assert.deepStrictEqual(JSON.parse(values.get(bookKey('book-25'))).sections, {});
assert.strictEqual(warnings.length, 0);
''')

    def test_failed_writes_warn_without_losing_book_scoped_memory(self):
        run_storage_case(r'''
const section = context.getVoiceSectionId('Alice');
storage.setItem = () => {throw Error('quota exceeded');};
context.saveVoiceSectionStates('book-A', {[section]: false});
assert(!context.isVoiceSectionOpen('book-A', 'Alice'));
context.ensureVoiceSectionState('book-B');
assert(context.isVoiceSectionOpen('book-B', 'Alice'));
assert(warnings.some(([message]) => message === 'Could not persist voice section state.'));
Object.defineProperty(context, 'localStorage', {get() {throw Error('storage blocked');}});
context.saveVoiceSectionStates('book-B', {[section]: false});
assert(!context.isVoiceSectionOpen('book-B', 'Alice'));
assert(!context.isVoiceSectionOpen('book-A', 'Alice'));
''')

    def test_native_actions_defaults_bulk_and_persistence(self):
        self.assertEqual(run_disclosure_cases(), {
            'actions': True, 'defaults': True, 'bulk': True,
            'books': True, 'pruning': True,
            'persistence': True, 'detached': True, 'themes': True,
        })
