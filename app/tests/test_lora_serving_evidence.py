"""Known-answer serving identities through the real evaluator and consumers."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import HTTPException
import evaluation_reviews as reviews
import lora_evidence as evidence
from routers import lora
from tests import test_lora_evaluation as fixture
from tests.test_support import SyntheticEvaluationEngine, write_test_serving_adapter

evaluation = fixture.evaluation


class PcmEngine(SyntheticEvaluationEngine):
    def __init__(self, config=None, callback=None):
        super().__init__(config)
        self.callback = callback
        self.calls = []

    def generate_voice(self, output_path, voice_config, **kwargs):
        voice = voice_config['_evaluation_']
        snapshot = Path(voice['adapter_path'])
        self.calls.append({'snapshot': str(snapshot), 'seed': voice.get('seed'),
                           'alpha': json.loads((snapshot / 'adapter_config.json').read_text())['lora_alpha'],
                           'generation': evidence.get_adapter_generation_sha256(snapshot)})
        times = np.arange(8000, dtype=np.float32) / 16000
        sf.write(output_path, .1 * np.sin(2 * np.pi * 180 * times), 16000)
        if self.callback:
            self.callback(self, snapshot, Path(output_path))
        return True


class ServingEvidenceTests(unittest.TestCase):
    def evaluate(self, adapter, engine=None):
        adapter = Path(adapter)
        production = adapter if adapter.name == 'voice' else adapter.parents[1]
        with patch.object(evaluation, 'apply_evaluation_seed'), \
             patch.object(evaluation, 'get_speaker_similarity', return_value=.9):
            return evaluation.evaluate_adapter({'id': 'voice'}, str(production.parent),
                engine or PcmEngine(), object(), 'cpu',
                adapter_dir_override=None if adapter == production else str(adapter))

    def make_pair(self, root):
        adapter = root / 'voice'
        candidate = adapter / 'candidates' / 'chosen'
        write_test_serving_adapter(adapter)
        write_test_serving_adapter(candidate, 2)
        prod, cand = self.evaluate(adapter), self.evaluate(candidate)
        (adapter / 'evaluation.json').write_text(json.dumps(prod))
        (candidate / 'evaluation.json').write_text(json.dumps(cand))
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps([{'id': 'voice', 'evaluation': {'recommended_candidate': 'chosen'},
                                         'evaluation_candidates': [{'id': 'chosen'}]}]))
        return adapter, candidate, manifest, prod, cand

    def run_main(self, root, engine_type=PcmEngine, *, writer=None):
        classifier = SimpleNamespace(from_hparams=lambda **kwargs: SimpleNamespace(eval=lambda: None))
        modules = {'speechbrain': ModuleType('speechbrain'),
                   'speechbrain.inference': ModuleType('speechbrain.inference'),
                   'speechbrain.inference.speaker': SimpleNamespace(EncoderClassifier=classifier)}
        argv = ['evaluate_lora.py', '--manifest', str(root / 'manifest.json'),
                '--models-dir', str(root), '--config', str(root / 'config.json'), '--device', 'cpu']
        with patch.dict(sys.modules, modules), patch.object(sys, 'argv', argv), \
             patch.object(evaluation, 'resolve_device', return_value='cpu'), \
             patch.object(evaluation, 'TTSEngine', engine_type), \
             patch.object(evaluation, 'apply_evaluation_seed'), \
             patch.object(evaluation, 'get_speaker_similarity', return_value=.9), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.ExitStack() as stack:
            if writer:
                stack.enter_context(patch.object(evaluation, 'atomic_json_write', side_effect=writer))
            return evaluation.main()

    def test_shared_generation_digest_preserves_the_original_four_file_contract(self):
        from adapter_checkpoint_transaction import get_adapter_generation_sha256
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / 'voice'
            write_test_serving_adapter(adapter)
            rows = [(name, evidence.get_file_sha256(adapter / name)) for name in (
                'adapter_config.json', 'adapter_model.safetensors', 'ref_sample.wav', 'training_meta.json')]
            expected = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
            self.assertEqual(expected, get_adapter_generation_sha256(adapter))
            self.assertEqual(expected, evidence.get_adapter_generation_sha256(adapter))
            (adapter / 'notes.txt').write_text('Auxiliary content is not a serving input.')
            self.assertEqual(expected, evidence.get_adapter_generation_sha256(adapter))

    def test_real_engine_defaults_and_explicit_settings_match_the_pure_reader(self):
        from tts import TTSEngine
        for config in ({}, {'tts': {'mode': 'local', 'language': 'Japanese', 'device': 'cpu',
                                   'max_new_tokens': 1024, 'compile_codec': True}}):
            with self.subTest(config=config):
                engine = TTSEngine(config)
                self.assertEqual(evidence.get_tts_model_settings(config), engine.get_model_settings())
                self.assertIsNone(engine._local_lora_model)

    def test_shared_evidence_layer_remains_importable_without_site_dependencies(self):
        environment = os.environ.copy()
        environment.update(PYTHONPATH=str(Path(evidence.__file__).parent), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-S', '-c',
            'import json,lora_evidence;print(json.dumps(lora_evidence.get_tts_model_settings({})))'],
            env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(evidence.get_tts_model_settings({}), json.loads(result.stdout))

    def test_each_model_setting_and_legacy_missing_fields_refuse_cache_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / 'voice';write_test_serving_adapter(adapter)
            result = self.evaluate(adapter)
            for name, value in (('language', 'Japanese'), ('max_new_tokens', 1024),
                                ('device', 'cpu'), ('compile_codec', True), ('mode', 'local')):
                with self.subTest(name=name):
                    requested = {**result['synthesis_settings'], name: value}
                    self.assertFalse(evaluation.is_complete_evaluation(result, str(adapter), requested))
            for field in ('synthesis_settings', 'adapter_generation_sha256'):
                with self.subTest(field=field):
                    missing = copy.deepcopy(result)
                    (missing if field == 'synthesis_settings' else missing['evidence']).pop(field)
                    self.assertFalse(evaluation.is_complete_evaluation(missing, str(adapter)))
            legacy = copy.deepcopy(result);legacy['version'] = 2
            self.assertFalse(evaluation.is_complete_evaluation(legacy, str(adapter)))

    def test_config_and_metadata_only_changes_refuse_completed_native_evidence(self):
        for filename, field, value in (('adapter_config.json', 'lora_alpha', 8),
                                       ('training_meta.json', 'ref_sample_text', 'Other synthetic reference.')):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                adapter = Path(tmp) / 'voice';write_test_serving_adapter(adapter)
                result = self.evaluate(adapter)
                self.assertTrue(evaluation.is_complete_evaluation(result, str(adapter)))
                weights = (adapter / 'adapter_model.safetensors').read_bytes()
                reference = (adapter / 'ref_sample.wav').read_bytes()
                data = json.loads((adapter / filename).read_text());data[field] = value
                (adapter / filename).write_text(json.dumps(data))
                self.assertFalse(evaluation.is_complete_evaluation(result, str(adapter)))
                self.assertEqual(weights, (adapter / 'adapter_model.safetensors').read_bytes())
                self.assertEqual(reference, (adapter / 'ref_sample.wav').read_bytes())

    def test_cache_reuse_tracks_config_and_metadata_native_versions_even_with_restored_mtime(self):
        for filename, old, new in (('adapter_config.json', '"lora_alpha": 4', '"lora_alpha": 8'),
                                   ('training_meta.json', 'Known synthetic reference.', 'Other synthetic reference.')):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                adapter = Path(tmp) / 'voice';write_test_serving_adapter(adapter)
                snapshot = (evidence.get_adapter_generation_sha256(adapter),
                            evidence.get_adapter_generation_file_identity(adapter),
                            evaluation.get_checkpoint_sha256(str(adapter)))
                candidates = str(adapter / 'candidates')
                with patch.object(evaluation, 'get_adapter_generation_sha256', wraps=evidence.get_adapter_generation_sha256) as hashing:
                    evaluation.partition_unique_candidates(str(adapter), candidates, production_snapshot=snapshot)
                    hashing.assert_not_called()
                path = adapter / filename;version = path.stat();text = path.read_text()
                self.assertIn(old, text);path.write_text(text.replace(old, new))
                self.assertEqual(version.st_size, path.stat().st_size)
                os.utime(path, ns=(version.st_atime_ns, version.st_mtime_ns))
                with patch.object(evaluation, 'get_adapter_generation_sha256', wraps=evidence.get_adapter_generation_sha256) as hashing:
                    evaluation.partition_unique_candidates(str(adapter), candidates, production_snapshot=snapshot)
                    hashing.assert_called_once_with(str(adapter))
                self.assertNotEqual(snapshot[0], evidence.get_adapter_generation_sha256(adapter))

    def test_same_weights_distinct_serving_bundles_are_unique_and_exact_copies_deduplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / 'voice';write_test_serving_adapter(adapter)
            for name in ('exact', 'config', 'metadata', 'reference'):
                target = adapter / 'candidates' / name;target.mkdir(parents=True)
                for filename in evidence.GENERATION_FILES:
                    shutil.copy2(adapter / filename, target / filename)
                if name == 'reference':
                    sf.write(target / 'ref_sample.wav', np.full(8000, .2), 16000)
                elif name in ('config', 'metadata'):
                    filename = 'adapter_config.json' if name == 'config' else 'training_meta.json'
                    data = json.loads((target / filename).read_text())
                    data['lora_alpha' if name == 'config' else 'ref_sample_text'] = 8 if name == 'config' else 'Different known reference.'
                    (target / filename).write_text(json.dumps(data))
            weight_hash, unique, duplicates, skipped = evaluation.partition_unique_candidates(str(adapter), str(adapter / 'candidates'))
            self.assertEqual([], skipped)
            self.assertEqual(['config', 'metadata', 'reference'], [item[0] for item in unique])
            self.assertTrue(all(len(item) == 3 and item[2] == weight_hash for item in unique))
            self.assertEqual([('exact', 'production')], [(item['id'], item['duplicate_of']) for item in duplicates])
            self.assertEqual(weight_hash, duplicates[0]['sha256'])

    def test_source_replacement_between_probes_keeps_one_snapshot_and_refuses_newer_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / 'voice';write_test_serving_adapter(adapter)
            cache = adapter / 'evaluation.json';cache.write_bytes(b'{"retained":true}')
            def changed(engine, snapshot, output):
                if len(engine.calls) == 1:
                    data = json.loads((adapter / 'adapter_config.json').read_text());data['lora_alpha'] = 8
                    (adapter / 'adapter_config.json').write_text(json.dumps(data))
            engine = PcmEngine(callback=changed)
            with self.assertRaisesRegex(ValueError, 'serving generation'):
                self.evaluate(adapter, engine)
            self.assertEqual([4, 4], [call['alpha'] for call in engine.calls])
            self.assertEqual(1, len({call['snapshot'] for call in engine.calls}))
            self.assertEqual([evaluation.EVALUATION_SEED, evaluation.EVALUATION_SEED + 1], [call['seed'] for call in engine.calls])
            self.assertEqual(b'{"retained":true}', cache.read_bytes())

    def test_changed_snapshot_or_engine_settings_stop_before_second_probe(self):
        for changed in ('snapshot', 'settings'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                adapter = Path(tmp) / 'voice';write_test_serving_adapter(adapter)
                def mutate(engine, snapshot, output):
                    if changed == 'settings':
                        engine._settings['language'] = 'Japanese'
                    else:
                        data = json.loads((snapshot / 'adapter_config.json').read_text());data['lora_alpha'] = 8
                        (snapshot / 'adapter_config.json').write_text(json.dumps(data))
                engine = PcmEngine(callback=mutate)
                with self.assertRaisesRegex(ValueError, 'changed'):
                    self.evaluate(adapter, engine)
                self.assertEqual(1, len(engine.calls))
                self.assertFalse((adapter / 'evaluation.json').exists())

    def test_actual_main_reuses_unchanged_settings_and_rescores_language_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);adapter = root / 'voice';write_test_serving_adapter(adapter)
            (root / 'manifest.json').write_text('[{"id":"voice"}]')
            config = root / 'config.json';config.write_text('{}')
            engines = []
            def create(settings):
                engine = PcmEngine(settings);engines.append(engine);return engine
            self.assertEqual(0, self.run_main(root, create))
            before = json.loads((adapter / 'evaluation.json').read_text())
            self.assertEqual(2, len(engines[-1].calls))
            self.assertEqual(0, self.run_main(root, create))
            self.assertEqual(0, len(engines[-1].calls))
            config.write_text('{"tts":{"language":"Japanese"}}')
            self.assertEqual(0, self.run_main(root, create))
            self.assertEqual(2, len(engines[-1].calls))
            after = json.loads((adapter / 'evaluation.json').read_text())
            self.assertNotEqual(before['evidence']['evaluation_spec_sha256'], after['evidence']['evaluation_spec_sha256'])
            self.assertEqual(before['evidence']['adapter_generation_sha256'], after['evidence']['adapter_generation_sha256'])

    def test_actual_main_reruns_legacy_evidence_instead_of_claiming_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);adapter = root / 'voice';write_test_serving_adapter(adapter)
            legacy = self.evaluate(adapter);legacy['version'] = 2
            legacy.pop('synthesis_settings', None);legacy['evidence'].pop('adapter_generation_sha256', None)
            (adapter / 'evaluation.json').write_text(json.dumps(legacy))
            (root / 'manifest.json').write_text('[{"id":"voice"}]');(root / 'config.json').write_text('{}')
            engines = []
            def create(settings):
                engine = PcmEngine(settings);engines.append(engine);return engine
            self.assertEqual(0, self.run_main(root, create))
            self.assertEqual(2, len(engines[0].calls))
            replacement = json.loads((adapter / 'evaluation.json').read_text())
            self.assertEqual(evidence.EVALUATION_EVIDENCE_VERSION, replacement['version'])
            self.assertTrue(evaluation.is_complete_evaluation(replacement, str(adapter)))

    def test_stale_candidate_before_cleanup_fails_without_removing_captured_or_new_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);adapter = root / 'voice';candidate = adapter / 'candidates' / 'chosen'
            write_test_serving_adapter(adapter);write_test_serving_adapter(candidate, 2)
            (root / 'manifest.json').write_text('[{"id":"voice"}]');(root / 'config.json').write_text('{}')
            native_write = evaluation.atomic_json_write
            def changed(value, path):
                native_write(value, path)
                if Path(path) == candidate / 'evaluation.json':
                    data = json.loads((candidate / 'adapter_config.json').read_text());data['lora_alpha'] = 8
                    (candidate / 'adapter_config.json').write_text(json.dumps(data))
                    added = adapter / 'candidates' / 'added_later';added.mkdir();(added / 'marker').write_text('new')
            with patch.object(evaluation, 'cleanup_candidates', wraps=evaluation.cleanup_candidates) as cleanup:
                self.assertEqual(1, self.run_main(root, writer=changed))
                cleanup.assert_not_called()
            self.assertTrue(candidate.is_dir())
            self.assertEqual('new', (adapter / 'candidates/added_later/marker').read_text())
            self.assertFalse((adapter / 'evaluation.json').exists())
            self.assertEqual('failed', json.loads((root / 'manifest.json').read_text())[0]['evaluation']['status'])

    def test_runtime_comparison_rejects_settings_and_spec_mismatch_without_constructing_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);adapter, candidate, manifest, prod, cand = self.make_pair(root)
            config = root / 'config.json';config.write_text('{}')
            with patch.object(lora, 'CONFIG_PATH', str(config)), patch('tts.TTSEngine.__init__', side_effect=AssertionError('no engine')) as init:
                self.assertEqual(2, len(lora._load_candidate_comparison_full('voice', str(root), str(manifest))[0]['probe_pairs']))
                config.write_text('{"tts":{"language":"Japanese"}}')
                with self.assertRaisesRegex(HTTPException, 'synthesis settings'):
                    lora._load_candidate_comparison_full('voice', str(root), str(manifest))
                config.write_text('{}')
                cand['thresholds'] = {**cand['thresholds'], 'speaker_similarity_min': .8}
                cand['evidence']['evaluation_spec_sha256'] = evidence.get_evaluation_spec_sha256(
                    evaluation.PROBES, evaluation.EVALUATION_SEED, cand['thresholds'], cand['synthesis_settings'])
                (candidate / 'evaluation.json').write_text(json.dumps(cand))
                with self.assertRaisesRegex(HTTPException, 'specifications'):
                    lora._load_candidate_comparison_full('voice', str(root), str(manifest))
                init.assert_not_called()

    def test_rerun_config_only_changes_review_fingerprint_and_refuses_prior_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);adapter, candidate, manifest, prod, cand = self.make_pair(root)
            before = reviews.evidence_fingerprint(prod, cand)
            data = json.loads((candidate / 'adapter_config.json').read_text());data['lora_alpha'] = 8
            (candidate / 'adapter_config.json').write_text(json.dumps(data))
            rerun = self.evaluate(candidate)
            after = reviews.evidence_fingerprint(prod, rerun)
            self.assertEqual(before['candidate_checkpoint_sha256'], after['candidate_checkpoint_sha256'])
            self.assertEqual(before['candidate_spec_sha256'], after['candidate_spec_sha256'])
            self.assertNotEqual(before.get('candidate_generation_sha256'), after.get('candidate_generation_sha256'))
            self.assertNotEqual(before, after)
            session = reviews.create_session(str(root / 'reviews'), 'voice', 'chosen', before,
                [{'id':'probe','text':'Known line','seed':1}],
                {'production':{'probe':'old-production.wav'},'candidate':{'probe':'old-candidate.wav'}})
            session_path = Path(reviews._session_path(str(root / 'reviews'), session['session_id']))
            session_bytes = session_path.read_bytes()
            with self.assertRaises(reviews.ReviewError):
                reviews.submit(str(root / 'reviews'), 'voice', session['session_id'], 'tie', after)
            self.assertEqual(session_bytes, session_path.read_bytes())
            self.assertEqual([], reviews.list_reviews(str(root / 'reviews'), 'voice'))


if __name__ == '__main__':
    unittest.main()
