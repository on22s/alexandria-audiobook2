import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from routers import voices


def pick(name, ranking=None):
    return {'name': name, 'ranked_adapter_ids': ['voice'] if ranking is None else ranking,
            'character_style': 'Measured delivery', 'reason': 'Dialogue evidence',
            'character_gender': 'unknown', 'age_group': 'unknown',
            'trait_confidence': 'unknown', 'trait_evidence': ''}


class VoiceSuggestionProvenanceTests(unittest.TestCase):
    def run_suggestions(self, responses, names=('A', 'B', 'C', 'D'), script_rows=None):
        requests = []
        iterator = iter(responses)

        def create(**kwargs):
            requests.append(kwargs)
            response = next(iterator)
            if isinstance(response, Exception):
                raise response
            raw = response if isinstance(response, str) else json.dumps(response)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=raw), finish_reason='stop')])

        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'script.json'), Path(root, 'voices.json')
            script.write_text(json.dumps(script_rows if script_rows is not None else
                                         [{'speaker': name, 'text': f'Line for {name}.'} for name in names]))
            config.write_text('{}')
            before = (script.read_bytes(), config.read_bytes())
            candidates = [{'adapter_id': 'voice', 'name': 'Voice', 'type': 'lora',
                           'gender': 'unknown', 'age_group': 'unknown', 'description': ''}]
            with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices, 'VOICE_CONFIG_PATH', str(config)), \
                 patch.object(voices, '_build_lora_candidates', side_effect=lambda: copy.deepcopy(candidates)), \
                 patch.object(voices, '_load_voice_library', return_value={'casts': {}, 'shared': {}, 'favorites': []}), \
                 patch.object(voices, 'get_active_book_id', return_value='book'), \
                 patch.object(voices, '_script_line_counts', return_value={name: 1 for name in names}), \
                 patch.object(voices, '_make_llm_client', return_value=(SimpleNamespace(
                     chat=SimpleNamespace(completions=SimpleNamespace(create=create))), 'model')), \
                 patch.object(voices, 'load_app_config', return_value={}), \
                 patch.object(voices, 'get_active_llm_config', return_value={}), \
                 patch.object(voices, 'get_current_status', return_value={}):
                result = voices._suggest_voices_impl(voices.SuggestVoicesRequest())
            self.assertEqual(before, (script.read_bytes(), config.read_bytes()))
        return result, requests

    def test_omitted_and_invalid_rankings_are_disclosed_per_character(self):
        result, calls = self.run_suggestions([
            {'characters': [pick('A')]},
            {'characters': [pick('C', ['unknown']), pick('D', {'voice': True})]}])
        self.assertEqual(2, len(calls))
        self.assertEqual('mixed', result['method'])
        self.assertEqual(['B', 'C', 'D'], result['heuristic_characters'])
        self.assertEqual('llm', result['suggestions']['A']['method'])
        for name in ('B', 'C', 'D'):
            self.assertEqual('heuristic', result['suggestions'][name]['method'])
            self.assertIn(name, result['llm_warning'])
            self.assertEqual('voice', result['suggestions'][name]['adapter_id'])

    def test_later_unparseable_batch_keeps_valid_prior_llm_choices_and_warns(self):
        result, _ = self.run_suggestions([{'characters': [pick('A'), pick('B')]}, 'not JSON'])
        self.assertEqual('mixed', result['method'])
        self.assertEqual(['C', 'D'], result['heuristic_characters'])
        self.assertTrue(result['llm_warning'])
        self.assertEqual('Measured delivery', result['suggestions']['A']['character_style'])
        self.assertEqual('heuristic', result['suggestions']['C']['method'])

    def test_wholly_heuristic_and_complete_llm_results_are_distinguished(self):
        result, _ = self.run_suggestions([RuntimeError('fixture provider failure')])
        self.assertEqual('heuristic', result['method'])
        self.assertEqual(['A', 'B', 'C', 'D'], result['heuristic_characters'])
        self.assertIn('fixture provider failure', result['llm_warning'])
        result, _ = self.run_suggestions([{'characters': [pick('A'), pick('B')]},
                                          {'characters': [pick('C'), pick('D')]}])
        self.assertEqual('llm', result['method'])
        self.assertEqual([], result['heuristic_characters'])
        self.assertIsNone(result['llm_warning'])
        self.assertTrue(all(row['method'] == 'llm' for row in result['suggestions'].values()))


class VoiceSuggestionProvenanceUiTests(unittest.TestCase):
    def test_actual_handler_displays_mixed_status_and_escapes_the_warning(self):
        import subprocess
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('async function suggestVoices(');
const end = source.indexOf('window.suggestMoreVoices =', start);
assert(start >= 0 && end > start);
let finished = false;
process.on('beforeExit', () => assert(finished, 'handler assertions must finish'));
(async () => {
for (const [method, label] of [['mixed', 'LLM + heuristic'], ['llm', 'LLM'], ['heuristic', 'heuristic']]) {
    const nodes = new Map();
    const node = id => { if (!nodes.has(id)) { nodes.set(id, {style:{}, innerHTML:'', disabled:false}); } return nodes.get(id); };
    const result = {method, suggestions:{A:{adapter_id:'voice', ranked_adapter_ids:['voice']}}, llm_warning:'Fallback for <B>'};
    const context = {window:null,currentBookFilename:'fixture-book',currentIsRemote:false,failoverIsRemote:false, document:{getElementById:node, querySelectorAll:()=>[]},
        API:{get:async()=>[], post:async path=>path==='/api/suggest_voices'?result:{}},
        refreshVoiceMetadata:async()=>{}, getVoiceCandidateMarkup:()=>'', renderVoiceSuggestions:()=>{},
        voicesScopeIsNew:()=>true, keepCurrentVoicesIfAsked:async()=>true, console,
        escapeHtml:value=>value.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')};
    context.window=context;vm.createContext(context);
    for(const [a,b] of [['async function confirmIfRemote(', '// navigator.clipboard'],['const taskStartButtons =','// --- API Helpers ---'],['function getLoraModelsById(', 'async function suggestVoices(']]){
        const first=source.indexOf(a);vm.runInContext(source.slice(first,source.indexOf(b,first)),context);
    }
    vm.runInContext(source.slice(start,end),context);
    await context.suggestVoices(['A']);
    assert(node('suggest-status').innerHTML.includes(`(${label})`));
    assert(node('suggest-status').innerHTML.includes('Fallback for &lt;B&gt;'));
    assert(!node('suggest-status').innerHTML.includes('<B>'));
    assert.strictEqual(node('btn-suggest-voices').disabled, false);
}
finished = true;
})().catch(error => { console.error(error); process.exitCode=1; });
"""
        result = subprocess.run(['node', '-e', script, str(source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
