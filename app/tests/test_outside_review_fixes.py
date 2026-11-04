"""Regressions for defects an outside review found and the code confirmed (2026-11-04)."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import alexandria_alignment  # noqa: E402
import alexandria_compare  # noqa: E402
import find_nicknames  # noqa: E402
import generate_script  # noqa: E402
import speaker_identity  # noqa: E402
from routers import voices  # noqa: E402
from tests.test_chunk_quality import _entry  # noqa: E402
import windows_export_staging_directory as windows_staging  # noqa: E402

TOKEN = 'a' * 64


class SpeakerMergeTests(unittest.TestCase):
    def test_unknown_is_protected_on_both_sides_of_a_merge(self):
        for variant, canonical in (('UNKNOWN', 'ALICE'), (' unknown ', 'ALICE'), ('ALICE', 'UNKNOWN')):
            with self.subTest(variant=variant):
                self.assertFalse(speaker_identity.is_speaker_merge_allowed(variant, canonical))
        self.assertTrue(speaker_identity.is_speaker_merge_allowed('ALLY', 'ALICE'))


class VoiceBookFenceTests(unittest.TestCase):
    def setUp(self):
        api = FastAPI()
        api.include_router(voices.router)
        self.client = TestClient(api)
        self.tokens = []

        def mutate(speaker, mutator, book_token=None):
            self.tokens.append(book_token)
            return {}, 'revision'

        for patcher in (patch.object(voices, '_mutate_book_voice_entry', mutate),
                        patch.object(voices, '_require_script_speaker', lambda speaker: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_version_selection_and_style_points_pass_the_book_token_to_the_write(self):
        self.client.post('/api/voices/ALICE/versions/old/select', json={'book_token': TOKEN}).raise_for_status()
        self.client.post('/api/voices/ALICE/style_timeline',
                         json={'from_index': 1, 'character_style': 'older', 'book_token': TOKEN}).raise_for_status()
        self.client.delete(f'/api/voices/ALICE/style_timeline/1?book_token={TOKEN}').raise_for_status()
        self.assertEqual([TOKEN] * 3, self.tokens)

    def test_clients_without_a_token_keep_working_and_a_malformed_token_is_refused(self):
        self.client.post('/api/voices/ALICE/versions/old/select').raise_for_status()
        self.client.post('/api/voices/ALICE/style_timeline',
                         json={'from_index': 1, 'character_style': 'older'}).raise_for_status()
        self.assertEqual([None, None], self.tokens)
        self.assertEqual(422, self.client.post('/api/voices/ALICE/versions/old/select',
                                               json={'book_token': 'stale'}).status_code)


class GenerationCheckpointPrefixTests(unittest.TestCase):
    def test_a_bad_quality_record_keeps_the_validated_prefix(self):
        params = generate_script.LLMGenParams('system', '{chunk}')
        for name, changed in (('missing', {'quality': None}), ('not accepted', {'quality': {'passed': False}})):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                output = str(Path(tmp, 'book.json'))
                fingerprint = generate_script.get_generation_fingerprint(
                    'one two', ['one', 'two'], 'model', 'http://local', params, 6000)
                good = {'chunk_number': 1, 'source_sha256': fingerprint['chunk_sha256'][0],
                        'entries': [_entry('one')], 'quality': {'passed': True}}
                bad = {'chunk_number': 2, 'source_sha256': fingerprint['chunk_sha256'][1],
                       'entries': [_entry('two')], **changed}
                generate_script.save_generation_checkpoint(output, fingerprint, [good, bad])
                self.assertEqual([good], generate_script.load_generation_checkpoint(output, fingerprint))


class NicknameRegistryTests(unittest.TestCase):
    def run_main(self, aliases_text):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, 'script.json')
            script.write_text(json.dumps([{'speaker': 'ALICE', 'text': 'Hello.'}]), encoding='utf-8')
            aliases = Path(tmp, 'aliases.json')
            aliases.write_text(aliases_text, encoding='utf-8')
            stdout = io.StringIO()
            argv = ['find_nicknames.py', '--input', str(script), '--aliases-file', str(aliases)]
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(stdout), \
                    self.assertRaises(SystemExit) as caught:
                find_nicknames.main()
            return caught.exception.code, stdout.getvalue()

    def test_invalid_registry_reports_the_handled_error_without_append(self):
        for text in ('[1, 2]', '{broken'):
            with self.subTest(text=text):
                code, output = self.run_main(text)
                self.assertEqual(1, code)
                self.assertIn('invalid alias registry', output)


class DurabilityTests(unittest.TestCase):
    def test_corrected_jsonl_is_on_disk_before_it_replaces_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'corrected.jsonl')
            output.write_text('old\n', encoding='utf-8')
            real, seen = os.fsync, []

            def fsync(descriptor):
                seen.append(output.read_text(encoding='utf-8'))
                real(descriptor)

            with patch.object(alexandria_compare.os, 'fsync', fsync):
                alexandria_compare.write_jsonl_atomic([{'text': 'new'}], output)
            self.assertEqual(['old\n'], seen)
            self.assertEqual('{"text": "new"}\n', output.read_text(encoding='utf-8'))

    def test_windows_recovery_guard_is_synced_like_its_posix_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = windows_staging.WindowsStageDirectory.__new__(windows_staging.WindowsStageDirectory)
            stage.path = Path(tmp)
            real, calls = os.fsync, []

            def fsync(descriptor):
                calls.append(descriptor)
                real(descriptor)

            with patch.object(windows_staging.WindowsStageDirectory, 'check', lambda self: None), \
                    patch.object(windows_staging.os, 'fsync', fsync):
                stage.create_recovery_guard('.export-staging-recovery')
            self.assertEqual(1, len(calls))
            self.assertEqual(b'1', Path(tmp, '.export-staging-recovery').read_bytes())


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class VoiceCardJsTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(['node', '-e', script, str(ROOT / 'app/static/js/app-core.js')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_saved_adapter_outside_the_list_stays_selected(self):
        self.run_node(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');
const context={escapeHtml:v=>String(v).replace(/</g,'&lt;').replace(/"/g,'&quot;')};vm.createContext(context);
vm.runInContext(s.slice(s.indexOf('function getSavedAdapterOption('),s.indexOf('function showVoiceTypeOptions(')),context);
const option=context.getSavedAdapterOption('gone"<b>',[{id:'kept'}]);
assert(option.includes('selected'));assert(option.includes('value="gone&quot;&lt;b>"'));assert(!option.includes('"<b>'));
assert.strictEqual(context.getSavedAdapterOption('kept',[{id:'kept'}]),'');
assert.strictEqual(context.getSavedAdapterOption('',[]),'');
assert.strictEqual(context.getSavedAdapterOption(undefined,[]),'');
''')

    def test_showing_an_applied_suggestion_does_not_queue_a_second_save(self):
        self.run_node(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');
let saves=0;
const opts={style:{}};
const body={querySelector:selector=>selector.startsWith('.voice-type')?radio:opts};
const radio={value:'lora',checked:false,closest:()=>body};
const card={querySelector:selector=>selector==='.card-body'?body:null};
const context={saveVoicesDebounced(){saves++;},CSS:{escape:v=>v},updateSuggestionToolbar(){},document:{querySelector:()=>card},
  Option:function(){}};
context.window=context;context._voiceSuggestions={};vm.createContext(context);
vm.runInContext(s.slice(s.indexOf('function showVoiceTypeOptions('),s.indexOf('async function refreshVoiceMetadata(')),context);
vm.runInContext(s.slice(s.indexOf('function applySuggestionToCard('),s.indexOf('function getSuggestionApplySnapshot(')),context);
assert.strictEqual(context.applySuggestionToCard('ALICE',{type:'lora',adapter_id:'a',adapter_name:'A',character_style:''}),true);
assert.strictEqual(radio.checked,true);assert.strictEqual(saves,0);
context.toggleVoiceType(radio);assert.strictEqual(saves,1,'a real type change still saves');
for(const name of ['async function applyVoiceSuggestion(','async function applyAllVoiceSuggestions(']){
  const start=s.indexOf(name);const body=s.slice(start,s.indexOf('catch (e)',start));
  assert(body.indexOf('await loadVoices();')>body.indexOf('reconcile()'),'the card and revision refresh after the server apply');
}
''')


class PreparerAlignmentTests(unittest.TestCase):
    def test_emphasis_covers_every_word_of_a_name_the_asr_split(self):
        merged = alexandria_alignment.merge_annotations_with_source('*TrullSengar* said hi', ['Trull', 'Sengar', 'said', 'hi'])
        self.assertIn('*Trull Sengar*', merged.replace('* *', ' '))

    def test_buffered_batches_track_the_speaker_and_fresh_runs_clear_old_rejects(self):
        source = (ROOT / 'alexandria_preparer_rocm_compatible.py').read_text(encoding='utf-8')
        buffered = source[source.index('# Batch not full yet'):]
        buffered = buffered[:buffered.index('continue  # Skip the per-chunk code below')]
        self.assertIn('prev_raw_speaker = chunk_word_data[0].get("speaker", "UNKNOWN")', buffered)
        self.assertIn("name in ('metadata.jsonl', 'rejected_chunks.jsonl')", source)


if __name__ == '__main__':
    unittest.main()
