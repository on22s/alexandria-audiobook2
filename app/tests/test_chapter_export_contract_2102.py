"""Issue #2102: a chapter audio decode failure must refuse with a tuple, not a TypeError.

The route unpacks ``success, msg = project_manager.export_chapters(...)``. Every
producer on that path must keep the ``(bool, str)`` contract, including the two
preparation failures that used to return a bare ``None``.
"""
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from project import CHAPTER_EXPORT_DIR, ProjectManager
from routers import editor as editor_module
# Plain helpers only; importing a TestCase subclass would double-register it.
from tests.test_chapter_export import _project


def _skip_decode_of(relative_path):
    """A loader that drops one admitted row the way a corrupt clip does.

    Accepts *args/**kwargs so it survives new keyword arguments on the real
    method (test hygiene: fakes must not pin the current signature).
    """
    real = ProjectManager._load_chunks_with_audio

    def fake(self, *args, **kwargs):
        rows = kwargs.get('chunks')
        if rows and any(row.get('audio_path') == relative_path for row in rows):
            return [], 1
        return real(self, *args, **kwargs)

    return fake


class ChapterExportContract2102Tests(unittest.TestCase):
    def _post_export(self, tmp, pm, *, changed_only=False, patches=()):
        """Drive the real /api/export_chapters route with a controlled state."""
        import core
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry['running'] = False
        with ExitStack() as stack:
            stack.enter_context(patch.object(core, 'process_state', state))
            stack.enter_context(patch.object(editor_module, 'process_state', state))
            stack.enter_context(patch.object(core, '_gpu_leases', {}))
            stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
            stack.enter_context(patch.object(editor_module, 'project_manager', pm))
            stack.enter_context(patch.object(editor_module, 'DATA_DIR', tmp))
            stack.enter_context(patch.object(editor_module, 'SCRIPT_PATH', pm.script_path))
            stack.enter_context(patch.object(editor_module,
                                             'validate_export_readiness', return_value=None))
            for context in patches:
                stack.enter_context(context)
            app = FastAPI()
            app.include_router(editor_module.router)
            with TestClient(app) as client:
                response = client.post('/api/export_chapters', json={
                    'format': 'wav', 'changed_only': changed_only})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('started', response.json()['status'], response.text)
        return state['chapter_export']

    def test_full_export_corrupt_chapter_is_a_clean_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            target = chunks[3]['audio_path']  # first line of chapter 2
            with patch.object(pm, 'load_chunks', return_value=chunks), \
                    patch.object(ProjectManager, '_load_chunks_with_audio',
                                 _skip_decode_of(target)):
                result = self._post_export(tmp, pm)
            self.assertEqual('failed', result['result']['status'], result)
            message = result['result']['message']
            self.assertNotIn('NoneType', message)
            self.assertIn('Chapter 2', message)
            self.assertIn('missing or corrupt audio', message)
            self.assertIn('line 4', message)
            self.assertTrue(any('Export failed: Chapter 2' in line
                                for line in result['logs']), result['logs'])
            self.assertNotIn(tmp, message)
            self.assertFalse((Path(tmp) / CHAPTER_EXPORT_DIR / 'manifest.json').exists())

    def test_changed_only_corrupt_chapter_is_a_clean_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, 'load_chunks', return_value=chunks):
                self.assertTrue(pm.export_chapters(fmt='wav')[0])
                out = Path(tmp) / CHAPTER_EXPORT_DIR
                self.assertTrue((out / 'manifest.json').exists())
                target = chunks[3]['audio_path']
                # Mark that row changed so changed-only actually re-stages it
                # instead of reusing both chapters from the manifest.
                source = Path(tmp, target)
                stat = source.stat()
                os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
                with patch.object(ProjectManager, '_load_chunks_with_audio',
                                  _skip_decode_of(target)), \
                        patch('chapter_timing_cache.ensure_chapter_timing_facts',
                              return_value=None):
                    result = self._post_export(tmp, pm, changed_only=True)
            self.assertEqual('failed', result['result']['status'], result)
            message = result['result']['message']
            self.assertNotIn('NoneType', message)
            self.assertIn('Chapter 2', message)
            self.assertIn('missing or corrupt audio', message)
            self.assertIn('line 4', message)
            self.assertNotIn(tmp, message)

    def test_normal_full_export_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, 'load_chunks', return_value=chunks):
                result = self._post_export(tmp, pm)
            self.assertEqual('done', result['result']['status'], result)
            out = Path(tmp) / CHAPTER_EXPORT_DIR
            rows = json.loads((out / 'manifest.json').read_text())['chapters']
            self.assertEqual(2, len(rows))
            for row in rows:
                self.assertGreater((out / row['file']).stat().st_size, 0)

    def _stage(self, pm, chunks, plans, *, previous=None, chapters=None,
               changed_only=False, get_pairs=None, durations=None, cancel_check=None):
        return pm._stage_chapter_export_plans(
            plans, chunks, {'format': 'wav', 'export_quality': {}}, (500, 200),
            previous or {}, chapters, changed_only,
            get_pairs or (lambda first, last: ([], 0)), durations,
            None, cancel_check, '', [])

    def _assert_contract(self, result):
        self.assertIsInstance(result, tuple, result)
        self.assertEqual(2, len(result), result)
        self.assertIsInstance(result[0], bool, result)
        self.assertIsInstance(result[1], str, result)

    def test_durations_none_has_its_own_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            chunks = [{'index': 0, 'speaker': 'N', 'text': 'x', 'audio_path': 'voicelines/c0.wav'}]
            plans = [{'index': 0, 'number': 1, 'title': 'Only', 'file': '01 - Only.wav',
                      'chunks': [0, 0], 'fingerprint': 'fake'}]
            with patch('project.get_chapter_selection', return_value=[99]):
                result = self._stage(pm, chunks, plans, chapters=[0])
            self._assert_contract(result)
            self.assertFalse(result[0])
            self.assertIn('Chapter 1', result[1])
            self.assertIn('no retained export and no computed duration', result[1])
            self.assertNotIn(tmp, result[1])

    def test_every_reachable_staging_failure_keeps_the_tuple_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            chunks = [{'index': 0, 'speaker': 'N', 'text': 'x', 'audio_path': 'voicelines/c0.wav'}]
            plans = [{'index': 0, 'number': 1, 'title': 'One', 'file': '01 - One.wav',
                      'chunks': [0, 0], 'fingerprint': 'fake'}]

            with self.subTest('skipped audio'):
                result = self._stage(pm, chunks, plans, chapters=[0])
                self._assert_contract(result)
                self.assertFalse(result[0])
                self.assertIn('missing or corrupt audio', result[1])

            with self.subTest('cancelled'):
                result = self._stage(pm, chunks, plans, chapters=[0],
                                     cancel_check=lambda: True)
                self._assert_contract(result)
                self.assertEqual((False, 'Export cancelled'), result)

            with self.subTest('invalid selection'):
                with patch('project.get_chapter_selection',
                           side_effect=ValueError('bad chapter number')):
                    result = self._stage(pm, chunks, plans, chapters=[0])
                self._assert_contract(result)
                self.assertEqual((False, 'bad chapter number'), result)

            with self.subTest('invalid retained output'):
                old = {'index': 0, 'file': '01 - One.wav', 'duration_ms': 1,
                       'start_ms': 0, 'end_ms': 1, 'output_sha256': '0' * 64,
                       'fingerprint': 'fake'}
                # Select a different chapter so chapter 0 is unselected and
                # reaches the retained-output check.
                with patch('project.get_chapter_selection', return_value=[99]):
                    result = self._stage(pm, chunks, plans, previous={0: old}, chapters=[0])
                self._assert_contract(result)
                self.assertFalse(result[0])
                self.assertIn('invalid or unverified retained output', result[1])


if __name__ == '__main__':
    unittest.main()
