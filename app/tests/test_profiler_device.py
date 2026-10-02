import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from routers import voicelab
from tests.test_voicelab_pipeline_scripts import voice_profiler


class ProfilerDeviceTests(unittest.TestCase):
    def test_preflight_and_stage_share_normalized_device_command(self):
        for device, expected in ((None, 'auto'), ('auto', 'auto'), ('cpu', 'cpu'),
                                 ('cuda', 'cuda'), ('rocm', 'cuda'), ('hip', 'cuda'), ('mps', 'mps')):
            with self.subTest(device=device), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                model = root / 'model.gguf'
                model.write_bytes(b'fixture')
                cfg = {'zips_dir': tmp, 'rocm_python': sys.executable,
                       'profiler_model': str(model), 'epub_dirs': []}
                request = voicelab.VoiceLabRequest(stages=['profile'], device=device)
                original = request.model_dump()
                probe = {'torch': 'fixture', 'gpu': 'fixture', 'deps': {'llama_cpp': True}}
                with patch.object(voicelab, 'DATA_DIR', tmp), \
                     patch.object(voicelab, '_probe_voicelab_interpreter', return_value=probe), \
                     patch.object(voicelab, '_run_profiler_preflight', return_value={}) as check, \
                     patch.object(voicelab.shutil, 'disk_usage', return_value=SimpleNamespace(free=30 * 1024 ** 3)):
                    report = voicelab._build_voicelab_preflight(request, cfg)
                    stage = voicelab._voicelab_build_commands(request, cfg, tmp)[0][1]
                self.assertTrue(report['ready'], report)
                self.assertEqual(stage, check.call_args.args[0])
                self.assertEqual(expected, stage[stage.index('--device') + 1])
                self.assertEqual(original, request.model_dump())
                default = voicelab._build_profiler_command(sys.executable, str(model), [])
                self.assertEqual('auto', default[default.index('--device') + 1])

    def test_actual_cli_consumes_device_and_publishes_profile(self):
        for device, layers in ((None, -1), ('auto', -1), ('cpu', 0), ('cuda', -1),
                               ('rocm', -1), ('hip', -1), ('mps', -1), ('cuda:2', -1)):
            with self.subTest(device=device):
                self.run_cli(device, layers)

    def test_check_dry_run_and_no_work_do_not_initialize_model(self):
        for mode in ('--check', '--dry_run', 'no-work'):
            with self.subTest(mode=mode):
                self.run_cli('cpu', None, mode)

    def test_invalid_device_rejected_before_model_or_artifact_changes(self):
        self.run_cli('invalid-device', None, 'invalid')

    def run_cli(self, device, layers, mode=None):
        features = {'mean_f0': 120., 'std_f0': 10., 'mean_rms': .04,
                    'speaking_rate': 3., 'mean_centroid': 2000., 'mean_rolloff': 3000.,
                    'smoothness': .4, 'flatness': .03, 'duration': 5.}
        entry = {'id': 'new', 'dataset_id': 'narrator_new_voice_book',
                 'zip_source': 'fixture.zip', 'custom': {'keep': 7}}
        if mode == 'no-work':
            entry.update(voice_profile='Already profiled.', voice_features=copy.deepcopy(features))
        provider = Mock()
        provider.create_chat_completion.return_value = {'choices': [{'message': {'content': 'Warm baritone.'}}]}
        constructor = Mock(return_value=provider)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, model, output = [root / name for name in ('manifest.json', 'model.gguf', 'profiles.csv')]
            manifest.write_text(json.dumps([entry]), encoding='utf-8')
            original = manifest.read_bytes()
            model.write_bytes(b'not a real model')
            argv = ['voice_profiler.py', '--manifest', str(manifest), '--model', str(model), '--output_csv', str(output)]
            if device is not None:
                argv += ['--device', device]
            if mode in ('--check', '--dry_run'):
                argv += [mode]
            with patch.object(sys, 'argv', argv), \
                 patch.dict(sys.modules, {'llama_cpp': SimpleNamespace(Llama=constructor)}), \
                 patch.object(voice_profiler, 'get_ref_wav', return_value=b'fixture'), \
                 patch.object(voice_profiler, 'analyze_ref_wav', return_value=features), \
                 patch.object(voice_profiler, 'find_epub', return_value=None), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if mode == 'invalid':
                    with self.assertRaises(SystemExit) as raised:
                        voice_profiler.main()
                    self.assertEqual(2, raised.exception.code)
                else:
                    self.assertEqual(0, voice_profiler.main())
            if layers is None:
                constructor.assert_not_called()
                provider.create_chat_completion.assert_not_called()
                self.assertEqual(original, manifest.read_bytes())
                self.assertEqual(mode == 'no-work', output.exists())
            else:
                constructor.assert_called_once()
                self.assertEqual(layers, constructor.call_args.kwargs['n_gpu_layers'])
                provider.create_chat_completion.assert_called_once()
                saved = json.loads(manifest.read_text())[0]
                self.assertEqual(entry['custom'], saved['custom'])
                self.assertEqual('Warm baritone.', saved['voice_profile'])
                self.assertEqual('measured', saved['voice_features']['pitch_status'])
                with output.open(newline='') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(['new'], [row['id'] for row in rows])
                self.assertEqual(saved['voice_profile'], rows[0]['voice_profile'])
