"""Bounded occurrence editor keeps exact source, takes and task admission."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
import pronunciation
from book_state_transaction import JOURNAL
from project import ProjectManager
from routers import pronunciation_panel as panel
from task_ownership import acquire_task_lease, TaskOwnershipBusy
from tests.test_support import use_private_cpu_fixture_lock


class OccurrencePanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name); self.pm = ProjectManager(directory.name)
        self.text = '𠀀𠀁𠀂𠀃 Ann\r\nAnn'
        rows = [dict(id=i, uid=uid, text=self.text, speaker='A', status='done',
                     audio_path=f'voicelines/prior-{uid}.wav', audio_revision='old-' + uid)
                for i, uid in enumerate(('a', 'b'))]
        self.pm.save_chunks(rows)
        for row in rows: sf.write(self.root / row['audio_path'], np.full(2400, .1), 24000)
        (self.root/'annotated_script.json').write_text(json.dumps([{'text': self.text, 'speaker': 'A'}]))
        (self.root/'voice_config.json').write_text(json.dumps({'A': {'type': 'custom', 'voice': 'Ryan'}}))
        (self.root/'config.json').write_text(json.dumps({'tts': {'mode': 'local', 'language': 'English'}}))
        self.lexicon = self.root/'pronunciation.json'
        self.lexicon.write_text(json.dumps({'names': {'Ann': 'Shared'}}))
        states = copy.deepcopy(core.process_state)
        for state in states.values(): state['running'] = False
        for obj, name, value in ((pronunciation, 'DEFAULT_PATH', str(self.lexicon)),
                                 (panel, 'project_manager', self.pm), (panel, 'SCRIPT_PATH', str(self.pm.script_path)),
                                 (core, 'DATA_DIR', str(self.root)), (core, 'process_state', states),
                                 (panel, 'process_state', states), (core, '_task_claims', {}), (core, '_gpu_leases', {})):
            active = patch.object(obj, name, value); active.start(); self.addCleanup(active.stop)
        api = FastAPI(); api.include_router(panel.router)
        self.client = TestClient(api); self.addCleanup(self.client.close)
        self.request = {'expected_book_token': panel.get_snapshot_digest(panel.get_editor_book_identity(str(self.root))),
                        'index': 0, 'expected_uid': 'a'}

    def post(self, suffix='', **fields):
        return self.client.post('/api/pronunciation/occurrences'+suffix, json={**self.request, **fields})

    def snapshot(self):
        result = self.post(); self.assertEqual(200, result.status_code, result.text); return result.json()

    def candidate(self, number=0, query='Ann'):
        snapshot = self.snapshot()
        result = self.post('/candidates', revision=snapshot['revision'], query=query)
        self.assertEqual(200, result.status_code, result.text)
        anchor = result.json()['candidates'][number]['anchor']
        return {**anchor, 'id': 'rule-a', 'state': 'approved', 'replacement': 'Ah-n'}, snapshot['revision']

    def save(self, rules, revision=None):
        return self.post('/save', revision=revision or self.snapshot()['revision'], rules=rules)

    def artifacts(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*')
                if path.is_file() and not path.name.endswith('.lock')
                and '.task_ownership' not in path.parts}

    def test_server_candidates_use_exact_codepoints_and_preview_changes_no_source_or_take(self):
        before = self.artifacts()
        with patch.object(self.pm, 'get_engine', side_effect=AssertionError('preview must not initialize a model')):
            rule, revision = self.candidate()
            self.assertEqual(5, rule['start']); self.assertEqual('unicode_codepoint', rule['offset_unit'])
            other, _ = self.candidate(1); self.assertEqual(10, other['start'])
            response = self.post('/preview', revision=revision, rules=[rule])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('𠀀𠀁𠀂𠀃 Ah-n\r\nShared.', response.json()['preview']['text'])
        self.assertTrue(response.json()['audio_will_be_detached'])
        self.assertEqual('configured', response.json()['context_source'])
        self.assertEqual(before, self.artifacts())

    def test_save_detaches_only_effective_changed_uid_and_notes_drafts_bypasses_preserve_take(self):
        rule, revision = self.candidate(); source = (self.root/'annotated_script.json').read_bytes()
        old = self.pm.get_saved_chunks(); audio = (self.root/old[0]['audio_path']).read_bytes()
        result = self.save([rule], revision); self.assertEqual(200, result.status_code, result.text)
        saved = self.pm.get_saved_chunks(); self.assertEqual('pending', saved[0]['status'])
        self.assertIsNone(saved[0]['audio_path']); self.assertEqual(old[1], saved[1])
        self.assertEqual(audio, (self.root/old[0]['audio_path']).read_bytes())
        self.assertEqual(source, (self.root/'annotated_script.json').read_bytes())
        self.pm._update_chunk_fields(0, status='done', audio_path=old[0]['audio_path'], audio_revision='rendered')
        rules = copy.deepcopy(self.snapshot()['rules']); rules[0].update(id='renamed-id', note='Reviewed again')
        rules.append({**rule, 'id': 'draft', 'state': 'draft', 'replacement': 'Unapproved'})
        before = self.pm.get_saved_chunks()[0]
        result = self.save(rules); self.assertEqual(200, result.status_code, result.text)
        self.assertFalse(result.json()['audio_detached'])
        after = self.pm.get_saved_chunks()[0]
        self.assertEqual((before['status'], before['audio_path'], before['audio_revision']),
                         (after['status'], after['audio_path'], after['audio_revision']))
        # A bypassed additional span must not invalidate the current take.
        bypass, _ = self.candidate(1); bypass.update(id='bypass', restrictions={'speaker': 'Other'})
        result = self.save([*rules, bypass]); self.assertEqual(200, result.status_code, result.text)
        self.assertFalse(result.json()['audio_detached'])

    def test_stale_document_book_uid_and_exact_text_refuse_without_overwriting(self):
        rule, revision = self.candidate()
        first = self.save([rule], revision); self.assertEqual(200, first.status_code, first.text)
        before = self.artifacts()
        self.assertEqual(409, self.save([], revision).status_code)
        self.assertEqual(409, self.post(expected_uid='other').status_code)
        self.assertEqual(409, self.post(expected_book_token='f'*64).status_code)
        self.assertEqual(before, self.artifacts())
        self.pm.update_chunk(0, {'text': self.text.replace('Ann', 'Anne', 1)})
        current = self.snapshot()['revision']; before = self.artifacts()
        result = self.save([rule], current); self.assertEqual(409, result.status_code, result.text)
        self.assertIn('choose fresh matches', result.json()['detail']); self.assertEqual(before, self.artifacts())
        self.assertEqual(200, self.save([], current).status_code)

    def test_overlap_duplicate_boolean_offsets_and_unknown_fields_are_refused(self):
        rule, revision = self.candidate(); before = self.artifacts()
        for rules in ([rule, {**rule, 'id': 'overlap'}], [rule, rule],
                      [{**rule, 'start': False}], [{**rule, 'unknown': 'data'}],
                      [{**rule, 'restrictions': {'engine_mode': 'unsupported'}}]):
            result = self.save(rules, revision)
            self.assertEqual(422, result.status_code, result.text)
            self.assertEqual(before, self.artifacts())

    def test_unsupported_phonetic_hint_bypasses_or_applies_explicit_fallback(self):
        rule, revision = self.candidate(); rule.pop('replacement'); rule['phonetic_hint'] = 'æn'
        result = self.post('/preview', revision=revision, rules=[rule])
        self.assertEqual(200, result.status_code, result.text)
        self.assertFalse(result.json()['audio_will_be_detached'])
        self.assertEqual('unsupported_phonetic_hint', result.json()['preview']['decisions'][0]['reason'])
        rule['fallback'] = 'Ah-n'
        result = self.post('/preview', revision=revision, rules=[rule])
        self.assertTrue(result.json()['audio_will_be_detached'])
        self.assertEqual('applied_phonetic_fallback', result.json()['preview']['decisions'][0]['reason'])

    def test_harmless_reordering_keeps_anchor_on_uid_and_rejects_old_row_position(self):
        rule, revision = self.candidate(); self.assertEqual(200, self.save([rule], revision).status_code)
        rows = self.pm.get_saved_chunks(); rows.reverse()
        for index, row in enumerate(rows): row['id'] = index
        self.pm.save_chunks(rows)
        self.assertEqual(409, self.post().status_code)
        self.request['index'] = 1
        snapshot = self.snapshot(); self.assertEqual('a', snapshot['uid'])
        self.assertEqual('rule-a', snapshot['rules'][0]['id'])
        result = self.post('/preview', revision=snapshot['revision'], rules=snapshot['rules'])
        self.assertEqual(200, result.status_code, result.text)
        self.assertTrue(result.json()['preview']['decisions'][0]['applied'])

    def test_read_only_missing_corrupt_legacy_and_pending_book_state_never_repair(self):
        path = Path(self.pm.chunks_path); original = path.read_bytes()
        variants = (None, b'{broken', json.dumps([{'id': 0, 'text': 'Ann'}]).encode(),
                    json.dumps([{'id': 0, 'uid': 'a', 'text': 'Ann'}, {'id': 0, 'uid': 'b', 'text': 'Ann'}]).encode())
        for value in variants:
            if value is None: path.unlink(missing_ok=True)
            else: path.write_bytes(value)
            before = self.artifacts()
            with patch.object(self.pm, 'get_engine', side_effect=AssertionError('must not load')):
                response = self.post()
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, self.artifacts()); self.assertFalse(list(self.root.glob('chunks.json.corrupt*')))
        path.write_bytes(original)
        (self.root/JOURNAL).write_bytes(b'{unfinished-switch')
        before = self.artifacts(); self.assertEqual(409, self.post().status_code)
        self.assertEqual(before, self.artifacts())

    def test_read_only_status_journal_uses_committed_prefix_without_repairing_torn_tail(self):
        self.pm._active_journal_identity = {'active_book_id': None, 'book_generation': None}
        self.pm._update_chunk_fields_by_uid('a', status='done')
        journal = Path(str(self.pm.chunks_path)+'.status.jsonl')
        with journal.open('ab') as output: output.write(b'{torn')
        before = self.artifacts()
        self.assertEqual(200, self.post().status_code)
        self.assertEqual(before, self.artifacts())
        self.assertFalse(list(self.root.glob('*.torn-*')))

    @use_private_cpu_fixture_lock()
    def test_claimed_unstarted_work_refuses_mutation_even_when_row_status_is_done(self):
        rule, revision = self.candidate(); before = self.artifacts()
        owner = core.reserve_background_task('audio', cpu_only=True)
        try:
            self.assertEqual('done', self.pm.get_saved_chunks()[0]['status'])
            response = self.save([rule], revision); self.assertEqual(409, response.status_code, response.text)
            self.assertIn('Wait', response.json()['detail'])
            self.assertEqual(200, self.post().status_code)
            self.assertEqual(before, self.artifacts())
        finally: core.release_gpu_task_claim('audio', owner)
        self.assertEqual(200, self.save([rule], revision).status_code)

    def test_foreign_cpu_ownership_refuses_mutation_and_releases_cleanly(self):
        rule, revision = self.candidate()
        code = "from task_ownership import acquire_task_lease;import sys;lease=acquire_task_lease(sys.argv[1],'audio',(),cpu_only=True);print('ready',flush=True);sys.stdin.readline();lease.close()"
        child = subprocess.Popen([sys.executable, '-c', code, str(self.root)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual('ready', child.stdout.readline().strip())
            result = self.save([rule], revision); self.assertEqual(409, result.status_code, result.text)
            self.assertEqual(200, self.post().status_code)
            output, errors = child.communicate('\n', timeout=5); self.assertEqual(0, child.returncode, errors)
        finally:
            if child.poll() is None: child.kill(); child.communicate(timeout=5)
        self.assertEqual(200, self.save([rule], revision).status_code)

    def test_mutation_holds_admission_through_write_and_write_failure_retains_prior_state(self):
        rule, revision = self.candidate(); entered = threading.Event(); release = threading.Event()
        original = self.pm._save_chunks_locked
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5): raise RuntimeError('test did not release mutation')
            return original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.pm, '_save_chunks_locked', side_effect=blocked):
            future = pool.submit(self.save, [rule], revision)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(TaskOwnershipBusy): acquire_task_lease(str(self.root), 'audio', (), cpu_only=True)
            finally: release.set()
            self.assertEqual(200, future.result(5).status_code)
        before = self.artifacts(); revision = self.snapshot()['revision']
        with patch.object(self.pm, '_save_chunks_locked', side_effect=OSError('injected write failure')):
            result = self.save([], revision)
        self.assertEqual(409, result.status_code, result.text); self.assertEqual(before, self.artifacts())
        lease = acquire_task_lease(str(self.root), 'audio', (), cpu_only=True); lease.close()
