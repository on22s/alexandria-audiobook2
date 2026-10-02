"""Dedup cache must follow actual source ZIP bytes, not its human-readable name."""
import ast
from contextlib import redirect_stdout
import io
import pickle
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

import numpy as np
import soundfile as sf
from lora_evidence import get_file_sha256
from utils import atomic_json_write
from voicelab_settings import get_deduped_zip_name
from tests.test_voice_analysis_cache import load_analysis_functions, SOURCE


class VoiceDedupSourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.zips = self.root / 'zips'
        self.narrator = self.zips / 'narrator'
        self.narrator.mkdir(parents=True)
        self.output = self.root / 'dedup'
        self.output.mkdir()
        self.zip = self.narrator / 'book.zip'
        self.cache = self.output / 'embeddings_cache.pkl'
        self.ns = load_analysis_functions()
        self.ns.update(sf=sf, shutil=shutil, DEDUP_SAMPLES=150, DEDUP_THRESHOLD=0.45,
            _EMBEDDING_MODEL_ID='fixture-ecapa', get_file_sha256=get_file_sha256,
            atomic_json_write=atomic_json_write, get_deduped_zip_name=get_deduped_zip_name)
        tree = ast.parse(SOURCE.read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('run_dedup', 'load_wav_from_zip')]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)
        self.ns['extract_embedding'] = Mock(side_effect=lambda wav, sr, model, device: np.array([float(wav[0]), sr]))
        self.write_zip(0.25)

    def write_zip(self, value, filename='train/one.wav'):
        stream = io.BytesIO()
        sf.write(stream, np.full(160, value), 16000, format='WAV', subtype='PCM_16')
        with zipfile.ZipFile(self.zip, 'w') as archive:
            archive.writestr(filename, stream.getvalue())

    def run_dedup(self):
        with redirect_stdout(io.StringIO()):
            self.ns['run_dedup'](self.ns['fixture_model'], 'cpu', self.zips, self.output)
        with self.cache.open('rb') as handle:
            return pickle.load(handle)

    def test_same_named_replacement_extracts_new_audio_and_clip_names(self):
        first = self.run_dedup()
        self.assertEqual(1, self.ns['extract_embedding'].call_count)
        self.write_zip(-0.25, 'train/replaced.wav')
        second = self.run_dedup()
        self.assertEqual(2, self.ns['extract_embedding'].call_count)
        new_entries = [value for key, value in second.items() if key not in first]
        self.assertEqual(1, len(new_entries))
        self.assertEqual(['train/replaced.wav'], new_entries[0][1])
        self.assertAlmostEqual(-0.25, new_entries[0][0][0, 0])
        self.assertEqual(self.zip.read_bytes(), next((self.zips / '_deduped').glob('*.zip')).read_bytes())

    def test_unchanged_zip_reuses_extraction_and_preserves_cached_artifact(self):
        first = self.run_dedup()
        original = self.cache.read_bytes()
        self.run_dedup()
        self.assertEqual(1, self.ns['extract_embedding'].call_count)
        self.assertEqual(original, self.cache.read_bytes())
        self.assertEqual(1, len(first))

    def test_legacy_name_only_cache_is_not_content_evidence(self):
        legacy = {'narrator/book::150::fixture-ecapa': (np.array([[99.0, 99.0]]), ['old.wav'])}
        self.cache.write_bytes(pickle.dumps(legacy))
        current = self.run_dedup()
        self.assertEqual(1, self.ns['extract_embedding'].call_count)
        self.assertEqual(2, len(current))
        self.assertIn(next(iter(legacy)), current)

    def test_source_change_during_extraction_refuses_checkpoint_publication(self):
        self.run_dedup()
        before = self.cache.read_bytes()
        self.write_zip(0.5)
        def changed(wav, sr, model, device):
            self.write_zip(-0.5)
            return np.array([float(wav[0]), sr])
        self.ns['extract_embedding'].side_effect = changed
        with self.assertRaisesRegex(RuntimeError, 'ZIP changed during extraction'):
            self.run_dedup()
        self.assertEqual(before, self.cache.read_bytes())
