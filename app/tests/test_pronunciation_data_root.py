"""ALEXANDRIA_DATA_DIR owns the shared lexicon path (#2181).

Two isolated data roots must not read, write, prepare speech from, or bind a
repair digest to the install root's lexicon. An unconfigured runtime keeps the
legacy install-root default so existing installs and tests are unaffected.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pronunciation
from routers import editor, pronunciation_panel as panel


class PronunciationDataRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.a = self.base / 'root_a'
        self.b = self.base / 'root_b'
        for root, name in ((self.a, 'Alpha'), (self.b, 'Beta')):
            root.mkdir()
            (root / 'pronunciation.json').write_text(json.dumps({'names': {'Rom': name}}))

    def _env(self, root):
        return patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(root)})

    def test_explicit_path_and_no_override_default(self):
        self.assertEqual('/tmp/explicit.json', pronunciation.get_pronunciation_path('/tmp/explicit.json'))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ALEXANDRIA_DATA_DIR', None)
            self.assertEqual(pronunciation.DEFAULT_PATH, pronunciation.get_pronunciation_path())
            self.assertEqual(os.path.join(pronunciation.REPO, 'pronunciation.json'),
                             pronunciation.DEFAULT_PATH)

    def test_two_roots_resolve_separate_paths_and_reads(self):
        with self._env(self.a):
            path_a = pronunciation.get_pronunciation_path()
            spoken_a = pronunciation.apply_pronunciation('Rom')[0]
        with self._env(self.b):
            path_b = pronunciation.get_pronunciation_path()
            spoken_b = pronunciation.apply_pronunciation('Rom')[0]
        self.assertEqual(str(self.a / 'pronunciation.json'), path_a)
        self.assertEqual(str(self.b / 'pronunciation.json'), path_b)
        self.assertNotEqual(path_a, path_b)
        self.assertEqual(('Alpha', 'Beta'), (spoken_a, spoken_b))

    def test_panel_writes_stay_in_their_root_and_leave_install_untouched(self):
        install = Path(pronunciation.DEFAULT_PATH)
        original = install.read_bytes() if install.exists() else None
        for root, after in ((self.a, 'Alpha2'), (self.b, 'Beta2')):
            with self._env(root):
                expected = pronunciation.get_pronunciation_path()
                with patch.object(panel, 'file_lock', wraps=panel.file_lock) as lock:
                    _document, current = panel.get_pronunciation_document()
                    saved = panel.save_pronunciation_document(
                        panel.PronunciationSave(names={'Rom': after}, revision=current['revision']))
                lock.assert_called_once_with(expected)
                self.assertEqual(after, saved['names']['Rom'])
        self.assertEqual({'names': {'Rom': 'Alpha2'}}, json.loads((self.a / 'pronunciation.json').read_text()))
        self.assertEqual({'names': {'Rom': 'Beta2'}}, json.loads((self.b / 'pronunciation.json').read_text()))
        self.assertEqual(('Alpha2', 'Beta2'), (
            pronunciation.apply_pronunciation('Rom', str(self.a / 'pronunciation.json'))[0],
            pronunciation.apply_pronunciation('Rom', str(self.b / 'pronunciation.json'))[0]))
        self.assertEqual(original, install.read_bytes() if install.exists() else None)

    def test_synthesis_default_and_repair_digest_follow_the_root(self):
        with self._env(self.a):
            speech_a = pronunciation.ensure_speech_pronunciation_result('Rom')
            digest_a = editor.get_audio_repair_revision([])
        with self._env(self.b):
            speech_b = pronunciation.ensure_speech_pronunciation_result('Rom')
            digest_b = editor.get_audio_repair_revision([])
        self.assertEqual(('Alpha', 'Beta'), (speech_a['text'], speech_b['text']))
        self.assertEqual(('Alpha', 'Beta'),
                         (speech_a['substitutions'][0]['spoken'], speech_b['substitutions'][0]['spoken']))
        self.assertNotEqual(digest_a, digest_b)


if __name__ == '__main__':
    unittest.main()
