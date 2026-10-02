"""Native ZIP/PCM reads preserve features while parsing once per extraction loop."""
import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import pickle
import shutil
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf
from tests import test_voice_analysis_phase_completion as phase_fixture


class VoiceAnalysisZipReuseTests(unittest.TestCase):
    setUp = phase_fixture.VoiceAnalysisPhaseCompletionTests.setUp
    write_zip = phase_fixture.VoiceAnalysisPhaseCompletionTests.write_zip
    run_dedup = phase_fixture.VoiceAnalysisPhaseCompletionTests.run_dedup
    configure = phase_fixture.VoiceAnalysisPhaseCompletionTests.configure
    analyze_phase = phase_fixture.VoiceAnalysisPhaseCompletionTests.analyze_phase

    def prepare(self):
        self.configure()
        if os.environ.get('ZIP_REUSE_SOURCE'):
            source = Path(os.environ['ZIP_REUSE_SOURCE'])
            names = {'run_dedup', 'run_analyze', 'load_wav_from_zip'}
            nodes = [node for node in ast.parse(source.read_text()).body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), self.ns)
        self.values = {}
        with zipfile.ZipFile(self.zip, 'w') as archive:
            for index in range(7):
                stream = io.BytesIO()
                sf.write(stream, np.full(160, index / 10.), 16000, format='WAV', subtype='FLOAT')
                name = f'train/{index}.wav'
                archive.writestr(name, stream.getvalue())
                self.values[name] = index / 10.
        self.ns['tqdm'].write = lambda *_args: None

    def track_reads(self, operation):
        original = zipfile.ZipFile
        opened = []
        peak = 0
        def open_archive(*args, **kwargs):
            nonlocal peak
            archive = original(*args, **kwargs)
            opened.append(archive)
            peak = max(peak, sum(item.fp is not None for item in opened))
            return archive
        with patch.object(zipfile, 'ZipFile', open_archive):
            operation()
        self.assertTrue(all(archive.fp is None for archive in opened))
        self.assertEqual(1, peak)
        return len(opened)

    def test_dedup_opens_source_twice_instead_of_once_per_clip_and_preserves_pcm(self):
        self.prepare()
        count = self.track_reads(self.run_dedup)
        self.assertEqual(2, count)
        with self.cache.open('rb') as handle:
            cache = pickle.load(handle)
        embeddings, names = next(iter(cache.values()))
        self.assertEqual(7, len(embeddings))
        for embedding, name in zip(embeddings, names):
            self.assertAlmostEqual(self.values[name], embedding[0])
            self.assertEqual(16000, embedding[1])
        self.assertEqual(self.zip.read_bytes(), next((self.zips/'_deduped').glob('*.zip')).read_bytes())

    def test_analyze_opens_each_zip_twice_and_persists_matching_clip_provenance(self):
        self.prepare()
        self.run_dedup()
        self.ns['extract_embedding'].reset_mock()
        count = self.track_reads(self.analyze_phase)
        self.assertEqual(2, count)
        self.assertEqual(7, self.ns['extract_embedding'].call_count)
        with (self.analyze/'embeddings_cache.pkl').open('rb') as handle:
            cache = pickle.load(handle)
        key = next(iter(cache['embeddings']))
        names = cache['wav_names'][key]
        self.assertEqual(sorted(self.values), [name for _path, name in names])
        for embedding, (_path, name) in zip(cache['embeddings'][key], names):
            self.assertAlmostEqual(self.values[name], embedding[0])

    def test_extraction_failure_closes_borrowed_archive_and_preserves_prior_output(self):
        self.prepare()
        self.run_dedup()
        old = next((self.zips/'_deduped').glob('*.zip')).read_bytes()
        self.write_zip(-0.25)
        self.ns['extract_embedding'].side_effect = RuntimeError('controlled extraction failure')
        def fail():
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'Dedup incomplete'):
                self.ns['run_dedup'](self.ns['fixture_model'], 'cpu', self.zips, self.output)
        self.assertEqual(2, self.track_reads(fail))
        self.assertEqual(old, next((self.zips/'_deduped').glob('*.zip')).read_bytes())

    def test_normalized_multi_zip_group_processes_every_clip_with_one_open_archive(self):
        self.prepare()
        self.run_dedup()
        source = next((self.zips/'_deduped').glob('*.zip'))
        first = source.parent/'same-key.zip'
        second = source.parent/'same_key.zip'
        source.rename(first)
        shutil.copyfile(first, second)
        self.assertEqual(4, self.track_reads(self.analyze_phase))
        with (self.analyze/'embeddings_cache.pkl').open('rb') as handle:
            cache = pickle.load(handle)
        self.assertEqual(14, len(cache['embeddings']['same_key']))
        self.assertEqual({str(first), str(second)}, {path for path, _name in cache['wav_names']['same_key']})
