import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from tests import test_voice_library_transactions as transaction_tests

routes = transaction_tests.routes


class CastLabelWarningTests(unittest.TestCase):
    def test_secondary_timeout_preserves_committed_single_assignment(self):
        with transaction_tests.VoiceLibraryTransactionTests().fixture() as (root, api), TestClient(api) as client:
            with patch.object(routes, '_remember_applied_labels', side_effect=TimeoutError('busy')):
                response = client.post('/api/voice_library/apply', json={
                    'cast': 'series', 'mapping': {'Hero': 'hero'}})
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual('applied', body['status'])
            self.assertEqual(['Hero'], body['applied'])
            self.assertEqual(1, body['count'])
            self.assertEqual([routes.get_cast_label_persistence_warning('series')], body['warnings'])
            self.assertEqual('old', json.loads(Path(routes.VOICE_CONFIG_PATH).read_text())['Hero']['voice'])

    def test_bulk_warns_only_committed_books_and_preserves_errors_and_empty_books(self):
        with transaction_tests.VoiceLibraryTransactionTests().fixture() as (root, api), TestClient(api) as client:
            (root / 'scripts/two.json').write_text(json.dumps([{'speaker': 'Other', 'text': 'Line.'}]))
            empty_before = (root / 'scripts/two.voice_config.json').read_bytes()
            with patch.object(routes, '_remember_applied_labels', side_effect=TimeoutError('busy')):
                response = client.post('/api/voice_library/apply_bulk', json={
                    'cast': 'series', 'mapping': {'Hero': 'hero'},
                    'script_names': ['one', 'two', '../bad']})
            self.assertEqual(200, response.status_code)
            body = response.json()
            warning = routes.get_cast_label_persistence_warning('series')
            self.assertEqual([warning], body['warnings'])
            self.assertEqual([warning], body['results'][0]['warnings'])
            self.assertEqual(1, body['results'][0]['count'])
            self.assertNotIn('warnings', body['results'][1])
            self.assertEqual(0, body['results'][1]['count'])
            self.assertNotIn('warnings', body['results'][2])
            self.assertEqual('Invalid script name', body['results'][2]['error'])
            self.assertEqual(empty_before, (root / 'scripts/two.voice_config.json').read_bytes())
            self.assertEqual('old', json.loads((root / 'scripts/one.voice_config.json').read_text())['Hero']['voice'])

    def test_successful_label_recording_has_no_warning(self):
        with transaction_tests.VoiceLibraryTransactionTests().fixture() as (_root, api), TestClient(api) as client:
            for bulk in (False, True):
                payload = {'cast': 'series', 'mapping': {'Hero': 'hero'}}
                if bulk:
                    payload['script_names'] = ['one', 'two']
                response = client.post('/api/voice_library/apply_bulk' if bulk else '/api/voice_library/apply', json=payload)
                self.assertEqual(200, response.status_code)
                self.assertEqual([], response.json()['warnings'])

    def test_actual_ui_handlers_show_escaped_warnings_and_success_counts(self):
        source = Path(__file__).parent.parent / 'static/js/app-core.js'
        script = r'''
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const guard = source.slice(source.indexOf('function isCastApplyContextCurrent('), source.indexOf('// Shared by submitCastApply/submitCastApplyBulk'));
const single = source.slice(source.indexOf('function getCastApplyWarningsHtml('), source.indexOf('// --- Apply a cast to multiple saved books'));
const bulk = source.slice(source.indexOf('async function submitCastApplyBulk('), source.indexOf('// Identity anchors that take over'));
const panel = {innerHTML:''}; const statuses = []; let refreshes = 0;
let response;
const context = {window:{_selectedCast:'series'},currentBookFilename:'fixture-book',_voiceSaveSnapshot:{book_token:'fixture-token'},document:{getElementById:()=>panel},
    _collectCastApplyMapping:()=>({Hero:'hero'}),
    setCastStatus:value=>statuses.push(value), loadVoices:async()=>{refreshes++;},
    escapeHtml:value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'),
    API:{post:async()=>response}};
context.window._castApplyContext={cast:'series',book:'fixture-book',bookToken:'fixture-token'};
vm.runInNewContext(guard + single + bulk, context);
(async()=>{
    response = {count:1,warnings:['<script>labels busy</script>']};
    await context.submitCastApply();
    assert(statuses[0].includes('Applied 1 voice'));
    assert(statuses[0].includes('&lt;script&gt;labels busy&lt;/script&gt;'));
    assert(!statuses[0].includes('<script>')); assert.strictEqual(refreshes,1);
    response = {results:[{name:'one',count:1,warnings:['<b>labels busy</b>']},
        {name:'bad',count:0,error:'Invalid script name'}]};
    await context.submitCastApplyBulk(['one','bad']);
    assert(panel.innerHTML.includes('1 voice applied in total'));
    assert(panel.innerHTML.includes('&lt;b&gt;labels busy&lt;/b&gt;'));
    assert(!panel.innerHTML.includes('<b>')); assert(panel.innerHTML.includes('Invalid script name'));
    response = {count:2,warnings:[]}; await context.submitCastApply();
    assert(statuses.at(-1).includes('Applied 2 voices'));
    assert(!statuses.at(-1).includes('alert-warning')); assert.strictEqual(refreshes,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
