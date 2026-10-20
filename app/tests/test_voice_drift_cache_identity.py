"""Run the real drift CLI with valid cached PCM and changed experiment inputs."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from experiments import generation
from experiments import voice_drift as runner
from tests.test_lora_generation_cache import _write
import tts


class VoiceDriftCacheIdentityTests(unittest.TestCase):
    def prepare(self, root):
        app = root / 'app'; (app / 'experiments').mkdir(parents=True)
        (app / 'config.json').write_text(json.dumps({'tts': {'mode': 'local', 'language': 'English'}}))
        (app / 'experiments/voice_compare_view.py').write_text(
            "def pitch_stats(path): return {'f0_median': 100., 'f0_spread': 2.}\n"
            "def voice_quality(path): return {'jitter_local': .01, 'hnr_db': 20.}\n"
            "def vocal_tract_length(path): return 1.\n")
        adapter = root / 'lora_models/voice'; _write(adapter)
        source = root / 'source.txt'
        source.write_text(' '.join(f'This is sufficiently long source sentence number {word} for a stable fixture.'
                                   for word in ('one', 'two', 'three')))
        out = root / 'result.json'; work = root / 'work'
        return app, adapter, source, out, work

    def run_cli(self, root, paths, rendered, seed=1, on_score=None):
        app, adapter, source, out, work = paths
        args = [runner.__file__, '--adapter', 'voice', '--source', str(source), '--lines', '3',
                '--anchor-lines', '1', '--seed', str(seed), '--work', str(work), '--out', str(out)]
        def render(engine, text, instruct, speaker, config, entry, path):
            rendered.append((text, copy.deepcopy(entry)))
            sf.write(path, np.full(2400, .1, dtype=np.float32), 24000)
            return path
        def score(pairs):
            if on_score:
                on_score()
            return [.75 + .01 * i for i in range(len(pairs))], None
        with patch.object(runner, 'REPO', str(root)), patch.object(runner, 'APP', str(app)), \
                patch.object(sys, 'argv', args), patch.object(tts, 'TTSEngine', return_value=SimpleNamespace()), \
                patch.object(tts, '_resolve_asset_path', side_effect=lambda path: str(root / path)), \
                patch.object(generation, 'render', side_effect=render), \
                patch.object(runner, 'ecapa', side_effect=score), \
                contextlib.redirect_stdout(io.StringIO()):
            runner.main()

    def test_seed_source_adapter_and_settings_changes_refuse_old_pcm_before_scoring(self):
        for change in ('seed', 'source', 'adapter', 'settings', 'legacy'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); paths = self.prepare(root); rendered = []
                self.run_cli(root, paths, rendered)
                self.assertEqual(3, len(rendered))
                self.run_cli(root, paths, rendered)
                self.assertEqual(3, len(rendered), 'Unchanged valid PCM remains reusable')
                app, adapter, source, out, work = paths
                seed = 1
                if change == 'seed':
                    seed = 99
                elif change == 'source':
                    source.write_text(source.read_text().replace('stable', 'different'))
                elif change == 'adapter':
                    _write(adapter, 2)
                elif change == 'settings':
                    (app / 'config.json').write_text(json.dumps({'tts': {'mode': 'local', 'language': 'French'}}))
                else:
                    (work / 'voice/input_identity.json').unlink(missing_ok=True)
                previous = {str(p.relative_to(work)): p.read_bytes() for p in work.rglob('*') if p.is_file()}
                published = out.read_bytes()
                with self.assertRaisesRegex(SystemExit, 'identity|fresh.*work'):
                    self.run_cli(root, paths, rendered, seed)
                self.assertEqual(3, len(rendered))
                self.assertEqual(published, out.read_bytes())
                self.assertEqual(previous, {str(p.relative_to(work)): p.read_bytes()
                                           for p in work.rglob('*') if p.is_file()})

    def test_changed_or_unreceipted_wav_regenerates_only_that_line(self):
        for change in ('pcm', 'receipt'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); paths = self.prepare(root); rendered = []
                self.run_cli(root, paths, rendered)
                work = paths[-1] / 'voice'; target = work / 'line_00001.wav'
                accepted = {p.name: p.read_bytes() for p in work.glob('line_*.wav')}
                if change == 'pcm':
                    sf.write(target, np.full(2400, .25, dtype=np.float32), 24000)
                else:
                    receipt = work / 'input_identity.json'
                    data = json.loads(receipt.read_text()); data['outputs'].pop(target.name)
                    receipt.write_text(json.dumps(data))
                self.run_cli(root, paths, rendered)
                self.assertEqual(4, len(rendered))
                self.assertEqual(rendered[1][0], rendered[-1][0])
                self.assertEqual(accepted, {p.name: p.read_bytes() for p in work.glob('line_*.wav')})
                self.assertTrue(all(row[1]['adapter_generation_sha256'] for row in rendered))

    def test_adapter_change_during_scoring_refuses_publication_of_cached_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = self.prepare(root); rendered = []
            self.run_cli(root, paths, rendered)
            published = paths[3].read_bytes()
            with self.assertRaisesRegex(SystemExit, 'generation identity'):
                self.run_cli(root, paths, rendered, on_score=lambda: _write(paths[1], 2))
            self.assertEqual(3, len(rendered))
            self.assertEqual(published, paths[3].read_bytes())
