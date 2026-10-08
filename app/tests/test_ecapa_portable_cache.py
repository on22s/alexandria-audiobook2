"""Windows checkout placeholders must never become speaker-model YAML."""
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments import _ecapa_batch as worker
import voice_reference


class EcapaPortableCacheTests(unittest.TestCase):
    def test_worker_copies_yaml_without_using_checkout_placeholder_or_symlinks(self):
        from hyperpyyaml import load_hyperpyyaml
        from speechbrain.utils.fetching import LocalStrategy, fetch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / 'ab_test_runtime/ecapa/hyperparams.yaml'
            legacy.parent.mkdir(parents=True)
            placeholder = str(root / 'unavailable-cache/hyperparams.yaml')
            legacy.write_text(placeholder)
            with self.assertRaisesRegex(AttributeError, 'keys'):
                load_hyperpyyaml(io.StringIO(placeholder))
            source = root / 'provider'
            source.mkdir()
            (source / 'hyperparams.yaml').write_text('model: ecapa\n')
            calls = []

            class Encoder:
                @classmethod
                def from_hparams(cls, **kwargs):
                    calls.append(kwargs)
                    path = fetch('hyperparams.yaml', source=str(source),
                                 savedir=kwargs['savedir'],
                                 local_strategy=kwargs['local_strategy'])
                    with open(path) as handle:
                        self.assertEqual(load_hyperpyyaml(handle), {'model': 'ecapa'})
                    return cls()

            speaker = types.ModuleType('speechbrain.inference.speaker')
            speaker.EncoderClassifier = Encoder
            output = io.StringIO()
            with patch.object(worker, 'REPO', str(root)), \
                    patch.dict(sys.modules, {'speechbrain.inference.speaker': speaker}), \
                    patch.object(sys, 'stdin', io.StringIO('[]')), \
                    patch.object(sys, 'stdout', output), \
                    patch.object(Path, 'symlink_to', side_effect=OSError('no symlink privileges')):
                self.assertEqual(worker.main(), 0)
            copied = root / 'cache/ecapa/hyperparams.yaml'
            self.assertEqual(copied.read_text(), 'model: ecapa\n')
            self.assertFalse(copied.is_symlink())
            self.assertEqual(legacy.read_text(), placeholder)
            self.assertEqual(calls[0]['local_strategy'], LocalStrategy.COPY)
            self.assertEqual(calls[0]['run_opts'], {'device': 'cpu'})
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1]), [])

    def test_reference_cache_tracks_the_actual_worker_model_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'python'; python.write_bytes(b'interpreter')
            script = root / 'worker.py'; script.write_bytes(b'worker')
            clip = root / 'audio.wav'; clip.write_bytes(b'clip')
            with patch.object(worker, 'REPO', str(root)), \
                    patch.object(voice_reference, 'REPO', str(root)):
                assets = Path(worker.get_ecapa_model_dir())
                assets.mkdir(parents=True)
                model = assets / 'embedding_model.ckpt'; model.write_bytes(b'first')
                key = voice_reference._get_reference_score_key(
                    [[str(clip), str(clip)]], str(python), str(script))
                model.write_bytes(b'second')
                changed = voice_reference._get_reference_score_key(
                    [[str(clip), str(clip)]], str(python), str(script))
            self.assertNotEqual(key, changed)
