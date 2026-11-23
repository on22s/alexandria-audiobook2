"""Forward-compatible workspace snapshot validation across enum additions."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from book_workspace import get_book_workspace
from project import ProjectManager
from routers import scripts_library
from routers.voices import get_voice_rows
from tests.test_book_workspace_js import SETUP, STATIC
from tests.test_ui_help_js import DOM_FIXTURE


class WorkspaceSchemaForwardCompatJsTests(unittest.TestCase):
    def run_js(self, script, payload=None):
        args = ['node', '-e', DOM_FIXTURE + SETUP + script,
                str(STATIC / 'app-workspace.js'), str(STATIC / 'app-core.js'),
                str(STATIC / 'app-workbench.js')]
        if payload is not None:
            args.append(json.dumps(payload))
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_backend_declared_enum_additions_still_admit_continue(self):
        self.run_js(r'''
(function(){
  const extended = () => ({
    recoveryStatus: ['candidate','not_reported','migration_required'],
    recoveryEligibility: ['not_rechecked','rechecking'],
    reviewStatus: ['not_prepared','unavailable','observed','partial'],
    exportStatus: ['unavailable','observed','pending'],
    exportInputs: ['not_rechecked'],
    audioStatus: ['not_started','files_present','needs_attention','unavailable','rendering'],
    outputStatus: ['present_unverified','historical_source_scope','not_found','unavailable','verified'],
    nextTab: ['script','voices','editor','audio','result']
  });
  assert(context.isWorkspaceSnapshotValid(snapshot()) === true, 'baseline snapshot must be valid');
  const cases = [
    ['next.tab', d => {d.next.tab = 'result';}],
    ['audio.status', d => {d.audio.status = 'rendering';}],
    ['outputs[].status', d => {d.outputs[0].status = 'verified';}],
    ['audio.review.status', d => {d.audio.review = {status: 'partial'};}],
    ['export_review.status', d => {d.export_review = {status: 'pending'};}],
    ['script.recovery.status', d => {d.script.recovery = {status: 'migration_required', eligibility: 'rechecking'};}]
  ];
  for (const [name, change] of cases) {
    const data = snapshot(); data.enums = extended(); change(data);
    assert(context.isWorkspaceSnapshotValid(data) === true, 'rejected declared enum: ' + name);
  }
  const forged = snapshot(); forged.enums = extended(); forged.next.tab = 'training';
  assert(context.isWorkspaceSnapshotValid(forged) === false, 'accepted undeclared tab');
})();
''')

    def test_older_and_newer_payloads_are_both_accepted_with_safe_fallback(self):
        self.run_js(r'''
(function(){
  const older = snapshot();
  assert(!('enums' in older), 'older producer response must be enum-block free');
  assert(context.isWorkspaceSnapshotValid(older) === true, 'older payload rejected');
  older.next.tab = 'training';
  assert(context.isWorkspaceSnapshotValid(older) === false, 'fallback accepted unsafe tab');
  const newer = snapshot(); newer.enums = {nextTab: ['script','voices','editor','audio']};
  assert(context.isWorkspaceSnapshotValid(newer) === true, 'newer payload rejected');
  const partial = snapshot(); partial.enums = {nextTab: ['script','voices','editor','audio','result']};
  partial.next.tab = 'result';
  assert(context.isWorkspaceSnapshotValid(partial) === true, 'partial enum block rejected new tab');
  partial.audio.status = 'rendering';
  assert(context.isWorkspaceSnapshotValid(partial) === false, 'missing keys must fall back to exact lists');
})();
''')

    def test_malformed_enum_blocks_fall_back_without_admitting_unsafe_values(self):
        self.run_js(r'''
(function(){
  const malformed = [null, [], 'script', 7, {nextTab: 'script'}, {nextTab: []},
    {nextTab: [123]}, {nextTab: ['']}, {nextTab: [null, 'result']}];
  for (const enums of malformed) {
    const data = snapshot(); data.enums = enums; data.next.tab = 'result';
    assert(context.isWorkspaceSnapshotValid(data) === false, 'malformed enum block admitted a new tab');
    data.next.tab = 'training';
    assert(context.isWorkspaceSnapshotValid(data) === false, 'malformed enum block admitted unsafe routing');
    data.next.tab = 'script';
    assert(context.isWorkspaceSnapshotValid(data) === true, 'malformed enum block broke baseline');
  }
})();
''')

    def test_real_producer_payload_is_accepted_by_browser_validator(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'state.json').write_text(json.dumps(
                {'active_book_id': 'Book A', 'book_generation': 'one'}))
            (directory / 'annotated_script.json').write_text(json.dumps(
                [{'type': 'dialogue', 'speaker': 'ALICE', 'text': 'Synthetic line.'}]))
            (directory / 'voice_config.json').write_text(json.dumps(
                {'ALICE': {'type': 'custom', 'description': 'Synthetic voice'}}))
            from book_workspace import WORKSPACE_ENUMS
            producer = ProjectManager(root)
            data = get_book_workspace(root, {}, get_voice_rows, producer.get_chunk_audio_path)
            self.assertEqual(data['enums'], WORKSPACE_ENUMS)
            self.assertIn(data['next']['tab'], data['enums']['nextTab'])
            self.assertIn(data['audio']['status'], data['enums']['audioStatus'])
            application = FastAPI()
            application.include_router(scripts_library.router)
            with patch.object(scripts_library, 'DATA_DIR', root), patch('core.project_manager', producer):
                with TestClient(application) as client:
                    response = client.get('/api/workspace')
            self.assertEqual(200, response.status_code)
            data = response.json()
            self.assertEqual(data['enums'], WORKSPACE_ENUMS)
        self.run_js(r'''
(function(){
  const data = JSON.parse(process.argv[4]);
  assert(context.isWorkspaceSnapshotValid(data) === true, 'real producer payload rejected');
  const extended = JSON.parse(JSON.stringify(data));
  extended.enums.nextTab.push('result'); extended.next.tab = 'result';
  assert(context.isWorkspaceSnapshotValid(extended) === true, 'declared newer tab rejected');
  extended.next.tab = 'training';
  assert(context.isWorkspaceSnapshotValid(extended) === false, 'undeclared unsafe tab accepted');
})();
''', payload=data)

    def test_structural_and_count_checks_still_reject_malformed_snapshots(self):
        self.run_js(r'''
(function(){
  const cases = [
    d => {d.book_token = 'short';},
    d => {d.script.entries = -1;},
    d => {d.cast.pending = d.cast.total + 1;},
    d => {d.audio.total = '0';},
    d => {d.audio.present = d.audio.total + 1;},
    d => {d.outputs = 'not-an-array';},
    d => {d.outputs[0] = null;},
    d => {d.next = null;}
  ];
  for (const change of cases) {
    const data = snapshot(); change(data);
    assert(context.isWorkspaceSnapshotValid(data) === false, 'accepted malformed snapshot');
  }
})();
''')


if __name__ == '__main__':
    unittest.main()
