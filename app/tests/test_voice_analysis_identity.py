"""Validate the cache-identity instrument before connecting scientific callers."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from voice_analysis_cache import get_voice_analysis_cache_identity, get_voice_analysis_model_state_sha256, get_voice_analysis_model_files


class VoiceAnalysisIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.zip, self.model, self.stage = [self.root / name for name in ('voice.zip', 'embedding.ckpt', 'stage.py')]
        for file, value in ((self.zip, b'old audio'), (self.model, b'old weights'), (self.stage, b'old code')):
            file.write_bytes(value)
        self.arguments = dict(zip_paths=[self.zip], model_id='fixture-model',
            model_state_sha256=hashlib.sha256(b'loaded fixture state').hexdigest(),
            model_files={'embedding': self.model}, stage_files={'feature': self.stage},
            dependency_versions={'decoder': '1'}, sample_count=150, seed=42)

    def identity(self, **changes):
        return get_voice_analysis_cache_identity(**{**self.arguments, **changes})

    def test_same_name_size_and_timestamp_replacement_changes_identity(self):
        first = self.identity()
        stamp = self.zip.stat().st_mtime_ns
        self.zip.write_bytes(b'new audio')
        os.utime(self.zip, ns=(stamp, stamp))
        second = self.identity()
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(hashlib.sha256(b'new audio').hexdigest(), second['document']['sources'][0]['sha256'])

    def test_same_model_id_with_different_weights_and_changed_code_invalidate(self):
        first = self.identity()
        self.model.write_bytes(b'new weights')
        second = self.identity()
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.stage.write_bytes(b'new code')
        self.assertNotEqual(second['sha256'], self.identity()['sha256'])

    def test_sampling_versions_and_model_id_are_part_of_identity(self):
        first = self.identity()['sha256']
        for changes in ({'seed': 43}, {'seed': -1}, {'sample_count': 0}, {'sample_count': 200},
                        {'model_id': 'other'}, {'dependency_versions': {'decoder': '2'}}):
            with self.subTest(changes=changes):
                self.assertNotEqual(first, self.identity(**changes)['sha256'])

    def test_unchanged_bytes_and_set_order_reuse_identity(self):
        extra = self.root / 'other.zip'
        extra.write_bytes(b'other audio')
        first = self.identity(zip_paths=[self.zip, extra])
        os.utime(self.zip, None)
        self.assertEqual(first, self.identity(zip_paths=[extra, self.zip, extra]))

    def test_metadata_is_independent_and_does_not_mutate_inputs(self):
        before = copy.deepcopy(self.arguments)
        result = self.identity()
        result['document']['dependency_versions']['decoder'] = 'changed'
        self.assertEqual(before, self.arguments)
        self.assertEqual('1', self.identity()['document']['dependency_versions']['decoder'])

    def test_missing_identity_and_bad_sampling_refuse(self):
        for changes in ({'model_state_sha256': None}, {'model_state_sha256': 'unknown'},
                        {'model_id': ''}, {'model_files': {}}, {'stage_files': {}},
                        {'zip_paths': []}, {'dependency_versions': {}},
                        {'sample_count': True}, {'sample_count': -1}, {'seed': True},
                        {'dependency_versions': {'decoder': None}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.identity(**changes)
        self.model.unlink()
        with self.assertRaises(FileNotFoundError):
            self.identity()


class LoadedModelStateIdentityTests(unittest.TestCase):
    def test_real_cpu_model_weights_and_configuration_change_identity(self):
        import torch
        model = torch.nn.Sequential(torch.nn.Linear(2, 2), torch.nn.ReLU())
        original = get_voice_analysis_model_state_sha256(model)
        self.assertEqual(original, get_voice_analysis_model_state_sha256(model))
        before = {key: value.clone() for key, value in model.state_dict().items()}
        with torch.no_grad():
            model[0].weight[0, 0] += 1
        changed = get_voice_analysis_model_state_sha256(model)
        self.assertNotEqual(original, changed)
        model.load_state_dict(before)
        self.assertEqual(original, get_voice_analysis_model_state_sha256(model))
        model[1].inplace = True
        self.assertNotEqual(original, get_voice_analysis_model_state_sha256(model))
        self.assertTrue(model[0].weight.requires_grad)

    def test_scalar_and_bfloat16_state_can_be_fingerprinted_without_mutation(self):
        import torch
        model = torch.nn.Module()
        model.register_buffer('count', torch.tensor(2))
        model.register_parameter('weights', torch.nn.Parameter(torch.tensor([1, 2], dtype=torch.bfloat16)))
        before = model.weights.clone()
        first = get_voice_analysis_model_state_sha256(model)
        self.assertEqual(first, get_voice_analysis_model_state_sha256(model))
        self.assertTrue(torch.equal(before, model.weights))
        self.assertEqual(torch.bfloat16, model.weights.dtype)

    def test_empty_or_sparse_state_refuses_identity(self):
        import torch
        with self.assertRaisesRegex(ValueError, 'state is required'):
            get_voice_analysis_model_state_sha256(torch.nn.Module())
        model = torch.nn.Module()
        model.register_buffer('sparse', torch.sparse_coo_tensor([[0]], [1.0], (2,)))
        with self.assertRaisesRegex(ValueError, 'dense tensors'):
            get_voice_analysis_model_state_sha256(model)


class LoadedModelBindingTests(unittest.TestCase):
    def test_actual_loader_binds_fingerprint_to_returned_cpu_model(self):
        import ast
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock, patch
        import torch
        from tests.test_voice_analysis_cache import SOURCE
        source = ast.parse(SOURCE.read_text())
        node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'load_model')
        model = torch.nn.Linear(2, 2)
        constructor = Mock(return_value=model)
        modules = {name: ModuleType(name) for name in ('speechbrain', 'speechbrain.inference', 'speechbrain.inference.speaker')}
        modules['speechbrain.inference.speaker'].EncoderClassifier = SimpleNamespace(from_hparams=constructor)
        namespace = {'_EMBEDDING_MODEL_ID': 'fixture-model',
                     'get_voice_analysis_model_state_sha256': get_voice_analysis_model_state_sha256,
                     'get_voice_analysis_model_files': get_voice_analysis_model_files,
                     'get_voice_analysis_dependency_versions': lambda: {'fixture': '1'}}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, modules):
            root = Path(tmp)
            (root / 'hyperparams.yaml').write_text('fixture hyperparameters')
            (root / 'embedding.ckpt').write_bytes(b'fixture parameters')
            model.hparams = SimpleNamespace(pretrainer=SimpleNamespace(
                loadables={'embedding': model}, is_loadable=lambda name: True,
                is_local=[], collect_in=root))
            loaded = namespace['load_model'](root, 'cpu')
            self.assertEqual(root / 'embedding.ckpt', loaded._alexandria_model_files['parameter:embedding'])
            constructor.assert_called_once_with(source='fixture-model', savedir=str(root), run_opts={'device': 'cpu'})
        self.assertIs(loaded, model)
        self.assertFalse(loaded.training)
        self.assertEqual('cpu', loaded.weight.device.type)
        self.assertEqual(get_voice_analysis_model_state_sha256(model), loaded._alexandria_state_sha256)
        self.assertEqual({'fixture': '1'}, loaded._alexandria_dependency_versions)


class CollectedModelArtifactTests(unittest.TestCase):
    def test_active_local_and_collected_paths_match_pretrainer_contract(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            savedir = root / 'savedir'
            collected = root / 'collected'
            savedir.mkdir()
            collected.mkdir()
            (savedir / 'hyperparams.yaml').write_text('model configuration')
            external = root / 'actual-local.ckpt'
            external.write_bytes(b'external collected artifact')
            (collected / 'normalizer.ckpt').write_bytes(b'normalizer artifact')
            pretrainer = SimpleNamespace(loadables={'embedding': object(), 'normalizer': object(), 'disabled': object()},
                is_loadable=lambda name: name != 'disabled', is_local=['embedding'],
                paths={'embedding': str(external), 'normalizer': 'not used'}, collect_in=collected)
            model = SimpleNamespace(hparams=SimpleNamespace(pretrainer=pretrainer))
            result = get_voice_analysis_model_files(model, savedir)
            self.assertEqual({'hyperparams': savedir / 'hyperparams.yaml',
                'parameter:embedding': external,
                'parameter:normalizer': collected / 'normalizer.ckpt'}, result)
            self.assertEqual('not used', pretrainer.paths['normalizer'])
            self.assertEqual(['embedding'], pretrainer.is_local)

    def test_missing_parameter_config_and_uncollected_state_refuse(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pretrainer = SimpleNamespace(loadables={'embedding': object()}, is_loadable=lambda name: True,
                                         is_local=[], collect_in=root)
            model = SimpleNamespace(hparams=SimpleNamespace(pretrainer=pretrainer))
            with self.assertRaises(FileNotFoundError):
                get_voice_analysis_model_files(model, root)
            (root / 'hyperparams.yaml').write_text('configuration')
            with self.assertRaises(FileNotFoundError):
                get_voice_analysis_model_files(model, root)
            pretrainer.collect_in = None
            with self.assertRaisesRegex(ValueError, 'no collected artifact'):
                get_voice_analysis_model_files(model, root)
            pretrainer.loadables = {}
            with self.assertRaisesRegex(ValueError, 'no collected parameter'):
                get_voice_analysis_model_files(model, root)

    def test_dependency_versions_are_explicit_and_missing_distribution_fails(self):
        from importlib.metadata import PackageNotFoundError
        from unittest.mock import patch
        from voice_analysis_cache import get_voice_analysis_dependency_versions
        with patch('importlib.metadata.version', side_effect=lambda name: 'fixture-' + name):
            versions = get_voice_analysis_dependency_versions()
        self.assertEqual('fixture-speechbrain', versions['speechbrain'])
        self.assertEqual('fixture-librosa', versions['librosa'])
        self.assertTrue(versions['libsndfile'])
        self.assertTrue(versions['python'])
        with patch('importlib.metadata.version', side_effect=PackageNotFoundError('missing')):
            with self.assertRaises(PackageNotFoundError):
                get_voice_analysis_dependency_versions()
