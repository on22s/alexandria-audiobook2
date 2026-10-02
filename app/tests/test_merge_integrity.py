import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from tests.test_chapter_export import _project


class MergeIntegrityAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project, rows = _project(self.temp.name)
        self.rows = [dict(rows[0], text='alpha beta')]
        self.project.save_chunks(self.rows)
        self.rows = self.project.load_chunks()
        self.source = self.root / 'source.txt'
        self.source.write_text('alpha beta')
        self.script = self.root / 'annotated_script.json'
        self.script.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        self.state = {'active_book_id': 'fixture', 'book_generation': 'generation-a',
                      'input_file_path': str(self.source)}
        self.write_state()
        self.scheduled = []
        self.tasks = {'audio': {'cancel': False, 'running': False, 'logs': []}}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in {'DATA_DIR': self.temp.name, 'SCRIPT_PATH': str(self.script),
                            'project_manager': self.project, 'process_state': self.tasks}.items():
            stack.enter_context(patch.object(editor, name, value))
        stack.enter_context(patch.object(editor, 'schedule_claimed_background_task',
                                        side_effect=lambda *args: self.scheduled.append(args)))
        app = FastAPI()
        app.include_router(editor.router)
        self.client = stack.enter_context(TestClient(app))

    def write_state(self):
        (self.root / 'state.json').write_text(json.dumps(self.state))

    def change_rows(self, text):
        self.project.save_chunks([dict(self.rows[0], text=text)])

    def test_known_word_replacement_refuses_merge_with_locations(self):
        self.change_rows('alpha gamma')
        self.script.write_text(json.dumps([{'text': 'alpha gamma'}]))
        response = self.client.post('/api/merge', json={})
        self.assertEqual(409, response.status_code, response.text)
        detail = response.json()['detail']
        self.assertEqual('differences', detail['status'])
        self.assertEqual(('beta', 'gamma', 0),
                         (detail['hunks'][0]['source_words'], detail['hunks'][0]['script_words'],
                          detail['hunks'][0]['entry_index']))
        self.assertTrue(detail['snapshot'])
        self.assertEqual([], self.scheduled)

    def test_clean_chunks_schedule_as_before_without_body(self):
        response = self.client.post('/api/merge')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('audio', self.scheduled[0][1])

    def test_clean_annotation_does_not_hide_editor_change(self):
        self.change_rows('alpha gamma')
        response = self.client.get('/api/editor/integrity')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('differences', response.json()['status'])
        self.assertEqual('gamma', response.json()['hunks'][0]['script_words'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

    def test_saved_script_json_is_unavailable_not_verified_source(self):
        self.state['script_generation_input_file'] = str(self.source)
        self.state['script_generation_options'] = {'strip_front_matter': False}
        self.state['input_file_path'] = str(self.script)
        self.write_state()
        result = self.client.post('/api/merge', json={})
        self.assertEqual(409, result.status_code, result.text)
        self.assertEqual('unavailable', result.json()['detail']['status'])
        self.assertEqual([], self.scheduled)

    def test_explicit_confirmation_is_bound_to_source_book_and_chunks(self):
        self.change_rows('alpha gamma')
        snapshot = self.client.post('/api/merge', json={}).json()['detail']['snapshot']
        for kind in ('source', 'book', 'chunks'):
            with self.subTest(kind=kind):
                self.source.write_text('alpha beta' if kind != 'source' else 'alpha delta')
                self.state['book_generation'] = 'generation-b' if kind == 'book' else 'generation-a'
                self.write_state()
                self.change_rows('alpha epsilon' if kind == 'chunks' else 'alpha gamma')
                refusal = self.client.post('/api/merge', json={'integrity_confirmation': snapshot})
                self.assertEqual(409, refusal.status_code, refusal.text)
                self.assertEqual([], self.scheduled)
        self.source.write_text('alpha beta'); self.state['book_generation'] = 'generation-a'
        self.write_state(); self.change_rows('alpha gamma')
        snapshot = self.client.post('/api/merge', json={}).json()['detail']['snapshot']
        self.assertEqual(200, self.client.post('/api/merge', json={'integrity_confirmation': snapshot}).status_code)
        self.assertEqual(1, len(self.scheduled))

    def test_worker_rechecks_and_exports_checked_rows(self):
        self.assertEqual(200, self.client.post('/api/merge', json={}).status_code)
        self.change_rows('alpha gamma')
        with patch.object(self.project, 'merge_audio') as export:
            self.scheduled[0][2]()
            export.assert_not_called()
        self.assertTrue(any('changed' in msg.lower() for msg in self.tasks['audio']['logs']))
        self.scheduled.clear(); self.change_rows('alpha beta')
        self.assertEqual(200, self.client.post('/api/merge', json={}).status_code)
        with patch.object(self.project, 'merge_audio', return_value=(True, 'fixture.mp3')) as export:
            self.scheduled[0][2]()
            self.assertEqual('alpha beta', export.call_args.kwargs['chunks'][0]['text'])


class SourceIntegritySnapshotTests(unittest.TestCase):
    def test_real_owned_http_admission_does_not_deadlock_and_releases_failed_registration(self):
        import subprocess
        import sys
        command=[sys.executable,'-c',
                 'import ci_env,unittest; '
                 's=unittest.defaultTestLoader.loadTestsFromName("tests.test_task_claim_ownership.TaskClaimOwnershipTests.test_actual_editor_and_script_http_registration_failures_release_only_their_claim"); '
                 'r=unittest.TextTestRunner().run(s); raise SystemExit(not r.wasSuccessful())']
        try:
            result=subprocess.run(command,cwd=Path(__file__).resolve().parent.parent,
                                  capture_output=True,text=True,timeout=10)
        except subprocess.TimeoutExpired:
            self.fail('Native owned HTTP task admission deadlocked or exceeded its 10-second bound')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_native_preparation_accepts_publisher_cleanup_and_rejects_wrong_settings(self):
        from merge_integrity import get_source_integrity
        from tests.test_publisher_matter import StripPublisherMatterTest
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.txt'
            source.write_text(StripPublisherMatterTest.FRONT+StripPublisherMatterTest.STORY)
            state={'input_file_path':str(source),'script_generation_input_file':str(source),
                   'script_generation_options':{'strip_front_matter':False}}
            rows=[{'text':StripPublisherMatterTest.STORY}]
            self.assertEqual('verified',get_source_integrity(state,b'fixture',rows)['status'])
            changed=[{'text':StripPublisherMatterTest.STORY.replace('snow','rain',1)}]
            rejected=get_source_integrity(state,b'fixture',changed)
            self.assertEqual('differences',rejected['status'])
            self.assertEqual(('snow','rain'),(rejected['hunks'][0]['source_words'],rejected['hunks'][0]['script_words']))
            state['script_generation_options']['strip_front_matter']='false'
            self.assertEqual('unavailable',get_source_integrity(state,b'fixture',rows)['status'])
            state['script_generation_input_file']='other-book.txt'
            self.assertEqual('unavailable',get_source_integrity(state,b'fixture',rows)['status'])

    def test_cache_is_bounded_copy_safe_and_invalidates_source_book_annotation_and_rows(self):
        import merge_integrity
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.txt';source.write_text('alpha beta')
            state={'input_file_path':str(source),'book_generation':'one'};rows=[{'text':'alpha beta'}]
            merge_integrity._get_cached_diff.cache_clear()
            with patch.object(merge_integrity,'word_diff',wraps=merge_integrity.word_diff) as compare:
                first=merge_integrity.get_source_integrity(state,b'annotation',rows)
                first['hunks'].append({'kind':'fake'})
                second=merge_integrity.get_source_integrity(state,b'annotation',rows)
                self.assertEqual([],second['hunks']);self.assertEqual(1,compare.call_count)
                previous=second['snapshot']
                for kind in ('source','book','annotation','rows'):
                    if kind=='source':source.write_text('alpha gamma')
                    if kind=='book':state['book_generation']='two'
                    if kind=='rows':rows=[{'text':'beta alpha'}]
                    current=merge_integrity.get_source_integrity(state,b'new annotation' if kind in ('annotation','rows') else b'annotation',rows)
                    self.assertNotEqual(previous,current['snapshot']);previous=current['snapshot']
                self.assertEqual(5,compare.call_count)
                self.assertEqual(1,merge_integrity._get_cached_diff.cache_info().currsize)


if __name__ == '__main__':
    unittest.main()
