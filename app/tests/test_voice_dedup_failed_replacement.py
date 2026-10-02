"""Failed current extraction cannot erase a previously published dataset."""
import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import unittest
from unittest.mock import Mock
import zipfile

from tests import test_voice_dedup_source_cache as fixture


class DedupFailedReplacementTests(unittest.TestCase):
    setUp = fixture.VoiceDedupSourceCacheTests.setUp
    write_zip = fixture.VoiceDedupSourceCacheTests.write_zip
    run_dedup = fixture.VoiceDedupSourceCacheTests.run_dedup

    def test_failure_preserves_previous_zip_and_existing_cache_bytes(self):
        for failure in ('unreadable_audio', 'no_wavs', 'unreadable_zip', 'empty_folder'):
            with self.subTest(failure=failure):
                self.write_zip(0.25)
                self.run_dedup()
                old_outputs = {p: p.read_bytes() for p in (self.zips / '_deduped').glob('*.zip')}
                old_cache = self.cache.read_bytes()
                if failure == 'unreadable_zip':
                    self.zip.write_bytes(b'not a ZIP archive')
                elif failure == 'empty_folder':
                    self.zip.unlink()
                else:
                    with zipfile.ZipFile(self.zip, 'w') as archive:
                        archive.writestr('train/bad.wav' if failure == 'unreadable_audio' else 'README.txt',
                                         b'not decodable audio')
                if os.environ.get('DEDUP_FAILURE_SOURCE'):
                    source = Path(os.environ['DEDUP_FAILURE_SOURCE'])
                    tree = ast.parse(source.read_text())
                    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                                and node.name == 'run_dedup')
                    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), self.ns)
                self.ns['tqdm'].write = Mock()
                error = None
                with redirect_stdout(io.StringIO()):
                    try:
                        self.ns['run_dedup'](self.ns['fixture_model'], 'cpu', self.zips, self.output)
                    except RuntimeError as exc:
                        error = exc
                self.assertEqual(old_outputs, {p: p.read_bytes() for p in (self.zips / '_deduped').glob('*.zip')})
                self.assertEqual(old_cache, self.cache.read_bytes())
                self.assertIsInstance(error, RuntimeError)
                self.assertRegex(str(error), 'Dedup incomplete.*preserving prior outputs')
                self.assertEqual([], list((self.zips / '_deduped').glob('*.tmp')))
