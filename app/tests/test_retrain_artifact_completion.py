"""Bound retrain artifacts/resume and actual CPU fixture producer; no training."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from experiments import generation, library_voice_fidelity as fidelity
from tests.test_retrain_completion import make_measured_files, measured, write_wav

REPO = Path(__file__).resolve().parents[2]


class RetrainArtifactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        app = self.root / 'app'
        (app / 'experiments').mkdir(parents=True)
        for rel in ('experiments/retrain_honest.py', 'train_lora.py', 'tts.py', 'voice_reference.py',
                    'experiments/generation.py', 'experiments/library_voice_fidelity.py'):
            shutil.copyfile(REPO / 'app' / rel, app / rel)
        (app / 'experiments/voice_compare_view.py').write_text('# No view calls in fixture.\n')
        (app / 'config.json').write_text('{}')
        spec = importlib.util.spec_from_file_location('fixture_retrain', app / 'experiments/retrain_honest.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.addCleanup(lambda: sys.path.remove(str(app)) if str(app) in sys.path else None)
        self.addCleanup(lambda: sys.path.remove(str(app / 'experiments')) if str(app / 'experiments') in sys.path else None)
        self.runtime = self.root / 'ab_test_runtime'
        (self.runtime / 'experiments').mkdir(parents=True)
        (self.runtime / 'logs').mkdir()
        self.args = types.SimpleNamespace(models=str(self.root / 'lora_models'), zips=str(self.root / 'zips'),
            work=str(self.runtime / 'reference_rank1_all21'), out=str(self.runtime / 'experiments/result.json'),
            epochs=6, lora_r=64, lora_alpha=128, seed=1234, reference_rank=1,
            eval_lines=8, use_medoid=False, medoid_clips=14, adapters=['good'], controls=[])
        Path(self.args.models).mkdir()
        Path(self.args.zips).mkdir()
        self.rows = [make_measured_files(self.module, self.root, self.args, measured())]
        self.document = self.make_document()

    def make_document(self, rows=None):
        rows = copy.deepcopy(rows if rows is not None else self.rows)
        document = dict(settings=self.module.get_retrain_settings(self.args),
            **{key:getattr(self.args, key) for key in ('epochs', 'lora_r', 'seed', 'reference_rank', 'eval_lines')},
            requested_adapters=list(self.args.adapters), requested_controls=list(self.args.controls),
            **self.module.get_retrain_completion(rows, self.args.adapters, self.args.controls, 8), results=rows)
        Path(self.args.out).write_text(json.dumps(document))
        return document

    def validate(self):
        return self.module.get_completed_retrain_result(self.args.out, self.args)

    def resume(self):
        return self.module.load_resumed_results(self.args.out, True, 1234, 1, 8, self.args)

    def test_complete_positive_and_low_score_results_are_reusable_and_read_only(self):
        for score in (0.8, -0.1):
            with self.subTest(score=score):
                row = copy.deepcopy(self.rows[0])
                row.update(new_ecapa_heldout=score, ecapa_scores=[score] * 8)
                document = self.make_document([row])
                before = Path(self.args.out).read_bytes()
                self.assertEqual(document, self.validate())
                self.assertEqual([row], self.resume())
                self.assertEqual(before, Path(self.args.out).read_bytes())

    def test_vectors_counts_roles_settings_and_completion_cannot_be_forged(self):
        changes = [lambda d:d.update(complete=True, completed=0),
            lambda d:d['settings'].update(reference_rank=True),
            lambda d:d['settings'].update(use_medoid=0),
            lambda d:d['results'][0].update(role='control'), lambda d:d['results'][0]['ecapa_scores'].pop(),
            lambda d:d['results'][0].update(new_ecapa_heldout=0.9),
            lambda d:d['results'][0]['duration_ratios'].__setitem__(0, float('nan')),
            lambda d:d['results'][0]['measurement']['settings'].update(lora_alpha=64),
            lambda d:d.update(seed=99), lambda d:d.update(requested_adapters=['other']),
            lambda d:d['results'][0]['measurement'].update(schema_version=True)]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                document = copy.deepcopy(self.document)
                change(document)
                Path(self.args.out).write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    self.validate()

    def test_current_source_weights_data_reference_or_generated_bytes_must_match(self):
        base = Path(self.args.work) / 'good'
        paths = [base/'adapter/adapter_model.safetensors', base/'adapter/adapter_config.json',
            base/'adapter/training_meta.json', base/'adapter/ref_sample.wav', base/'data/val/clip_0.wav',
            base/'data/metadata.jsonl', base/'val/clip_0.wav', base/'gen_0.wav',
            self.root/'zips/gooddataset.zip', self.root/'lora_models/good/training_meta.json',
            self.root/'app/config.json', self.root/'app/train_lora.py']
        for path in paths:
            with self.subTest(path=str(path)):
                old = path.read_bytes()
                self.make_document()
                path.write_bytes(old + b' ')
                with self.assertRaises(ValueError):
                    self.validate()
                self.assertEqual([], self.resume())
                path.write_bytes(old)

    def test_pilot_rows_survive_roster_expansion_but_partial_artifact_is_not_complete(self):
        second = make_measured_files(self.module, self.root, self.args, measured('second'))
        self.args.adapters = ['good', 'second']
        self.make_document([self.rows[0], second])
        self.args.adapters.append('third')
        self.assertEqual([self.rows[0], second], self.resume())
        with self.assertRaises(ValueError):
            self.validate()

    def test_training_setting_changes_drop_cached_rows(self):
        self.args.epochs = 7
        self.assertEqual([], self.resume())
        with self.assertRaises(ValueError):
            self.validate()

    def test_truncated_resume_starts_fresh_and_missing_weights_cannot_be_reused(self):
        Path(self.args.out).write_text('{"results":')
        self.assertEqual([], self.resume())
        self.make_document()
        (Path(self.args.work)/'good/adapter/adapter_model.safetensors').unlink()
        self.assertEqual([], self.resume())
        with self.assertRaises(OSError):
            self.validate()

    def run_producer(self, failure=False, changed=False):
        Path(self.args.out).unlink()
        for path in (Path(self.args.work)/'good').glob('gen_*.wav'):
            path.unlink()
        calls = []
        actual_run = subprocess.run
        def training(command, **kwargs):
            if command[0] != str(self.root/'app/env/bin/python'):
                return actual_run(command, **kwargs)
            calls.append(command)
            (Path(self.args.work)/'good/adapter/adapter_model.safetensors').write_bytes(b'new fixture trained bytes')
            return types.SimpleNamespace(returncode=0)
        renders = []
        def render(engine, text, instruct, speaker, config, ref, wav):
            renders.append(wav)
            if failure and len(renders) == 1:
                raise generation.GenerationFailed('fixture render failed')
            write_wav(Path(wav))
            if changed:
                (self.root/'zips/gooddataset.zip').write_bytes((self.root/'zips/gooddataset.zip').read_bytes()+b' ')
        fake = types.ModuleType('tts')
        fake.TTSEngine = lambda config: object()
        args = ['retrain', '--adapters', 'good', '--models', self.args.models, '--zips', self.args.zips,
                '--work', self.args.work, '--out', self.args.out, '--reference-rank', '1']
        code = 0
        with patch.object(sys, 'argv', args), patch.dict(sys.modules, {'tts':fake, 'library_voice_fidelity':fidelity}), \
                patch.object(self.module.subprocess, 'run', side_effect=training), \
                patch.object(generation, 'render', side_effect=render), \
                patch.object(fidelity, 'ecapa_pairs', return_value=([0.8]*8, None)), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                self.module.main()
            except SystemExit as result:
                code = result.code
        return code, calls, renders

    def test_actual_producer_publishes_bound_full_vectors_and_reusable_artifacts(self):
        code, calls, renders = self.run_producer()
        self.assertEqual(0, code)
        self.assertEqual(1, len(calls))
        self.assertEqual(8, len(renders))
        document = self.validate()
        self.assertEqual([0.8]*8, document['results'][0]['ecapa_scores'])
        self.assertEqual([1.0]*8, document['results'][0]['duration_ratios'])
        self.assertEqual(document['results'], self.resume())

    def test_actual_producer_records_failed_render_without_claiming_completion(self):
        code, _, _ = self.run_producer(failure=True)
        self.assertEqual(3, code)
        document = json.loads(Path(self.args.out).read_text())
        self.assertFalse(document['complete'])
        self.assertIn('not every requested', document['results'][0]['error'])
        with self.assertRaises(ValueError):
            self.validate()

    def test_source_changes_during_measurement_remain_failed_not_reusable(self):
        code, _, _ = self.run_producer(changed=True)
        self.assertEqual(3, code)
        document = json.loads(Path(self.args.out).read_text())
        self.assertFalse(document['complete'])
        self.assertIn('inputs changed', document['results'][0]['error'])
        self.assertEqual([], self.resume())


class RetrainChainTests(unittest.TestCase):
    setUp = RetrainArtifactTests.setUp
    make_document = RetrainArtifactTests.make_document

    def run_boundary(self, phase, kind, *, worker_rc=0, generated_valid=True):
        actual = (REPO / 'run_chains/remaining_gpu_research.sh').read_text()
        pilots = re.search(r'pilot_adapters=\((.*?)\)', actual, re.S).group(1).split()
        full = re.search(r'contaminated_adapters=\((.*?)\)', actual, re.S).group(1).split()
        self.assertEqual(5, len(pilots))
        self.assertEqual(21, len(full))
        self.args.adapters = pilots if phase == 'pilot' else full
        self.args.use_medoid = True
        self.args.out = str(self.runtime/'experiments'/f'reference_rank1_{"pilot" if phase == "pilot" else "all21"}.json')
        self.rows = [make_measured_files(self.module, self.root, self.args, measured(name)) for name in self.args.adapters]
        good = self.make_document()
        if phase == 'full':
            out, adapters = self.args.out, self.args.adapters
            self.args.out = str(self.runtime/'experiments/reference_rank1_pilot.json')
            self.args.adapters = pilots
            self.make_document([row for row in self.rows if row['adapter'] in pilots])
            self.args.out, self.args.adapters = out, adapters
        cached = json.dumps(good).encode()
        if kind == 'partial':
            cached = b'{"complete":true,"results":[]}'
        elif kind == 'malformed':
            cached = b'{"results":'
        elif kind == 'stale':
            weights = Path(self.args.work)/self.args.adapters[0]/'adapter/adapter_model.safetensors'
            weights.write_bytes(weights.read_bytes()+b' changed')
        repaired = copy.deepcopy(self.rows)
        for row in repaired:
            row['measurement']['inputs'] = self.module.get_retrain_input_hashes(self.args, row['adapter'])
        document = self.make_document(repaired)
        payload = self.root/'generated.json'
        payload.write_text(json.dumps(document) if generated_valid else '{}')
        if kind == 'missing':
            Path(self.args.out).unlink()
        else:
            Path(self.args.out).write_bytes(cached)
        path = Path(os.environ.get('ALEXANDRIA_TEST_RESEARCH_SOURCE', str(REPO/'run_chains/remaining_gpu_research.sh')))
        source = path.read_text()
        if phase == 'pilot':
            start = source.index('retrain_args=') if 'retrain_args=' in source else source.index('adapter_out=')
            block = source[start:source.index('\n# Finish Goal 2.7', start)]
        else:
            prefix = source[source.index('retrain_args='):source.index('pilot_adapters=')] if 'retrain_args=' in source else ''
            array_start = source.index('contaminated_adapters=(')
            array_end = source.index(')\n', array_start)+2
            start = source.index('all_adapters_out=')
            block = prefix + source[array_start:array_end] + source[start:source.index('\ngate_failures=', start)]
        script = ('set -uo pipefail\nrepo=$1; runtime=$2; python=$3\n'
                  'adapter_out="$runtime/experiments/reference_rank1_pilot.json"\n'
                  'stage() { echo DISPATCH:$1; cp "$FIXTURE_PAYLOAD" "$FIXTURE_TARGET"; return "$FIXTURE_RC"; }\n'
                  + block + '\necho NEXT_STAGE\n')
        return subprocess.run(['bash', '-c', script, 'fixture', str(self.root), str(self.runtime), sys.executable],
            capture_output=True, text=True, timeout=15,
            env=dict(os.environ, PYTHONPATH=str(REPO/'app'), ALEXANDRIA_VOICE_ZIPS=self.args.zips,
                     FIXTURE_PAYLOAD=str(payload), FIXTURE_TARGET=self.args.out, FIXTURE_RC=str(worker_rc)))

    def test_pilot_and_full_complete_results_skip_gpu_dispatch(self):
        for phase in ('pilot', 'full'):
            with self.subTest(phase=phase):
                result = self.run_boundary(phase, 'complete')
                self.assertEqual(0, result.returncode, result.stdout+result.stderr)
                self.assertNotIn('DISPATCH', result.stdout)
                self.assertIn('NEXT_STAGE', result.stdout)

    def test_partial_malformed_stale_and_missing_pilot_dispatch_and_validate(self):
        for kind in ('partial', 'malformed', 'stale', 'missing'):
            with self.subTest(kind=kind):
                result = self.run_boundary('pilot', kind)
                self.assertEqual(0, result.returncode, result.stdout+result.stderr)
                self.assertIn('DISPATCH:reference_rank1_pilot', result.stdout)
                self.assertIn('NEXT_STAGE', result.stdout)

    def test_pilot_and_full_failure_or_incomplete_success_blocks_next_stage(self):
        for phase in ('pilot', 'full'):
            for worker_rc, valid in ((7, True), (0, False)):
                with self.subTest(phase=phase, worker_rc=worker_rc):
                    result = self.run_boundary(phase, 'missing', worker_rc=worker_rc, generated_valid=valid)
                    self.assertEqual(1, result.returncode, result.stdout+result.stderr)
                    self.assertIn('DISPATCH', result.stdout)
                    self.assertNotIn('NEXT_STAGE', result.stdout)

    def test_full_incomplete_result_dispatches_and_preserves_pilot_seed(self):
        result = self.run_boundary('full', 'missing')
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertIn('DISPATCH:reference_rank1_all21', result.stdout)
        self.assertIn('NEXT_STAGE', result.stdout)
        pilot = json.loads((self.runtime/'experiments/reference_rank1_pilot.json').read_text())
        self.assertEqual(5, len(pilot['results']))
