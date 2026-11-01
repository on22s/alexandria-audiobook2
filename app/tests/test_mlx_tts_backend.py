"""MLX backend (#1442) with a stand-in worker: protocol, ownership, cancellation, dispatch.

The fake worker speaks the real protocol from plain Python, so these run on any
CI host. Native MLX behaviour is qualified separately on Apple Silicon.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

import mlx_tts_backend
from mlx_tts_backend import (DEFAULT_MLX_MODELS, MLXGenerationCancelled, MLXWorkerClient,
                             get_mlx_backend_status, get_mlx_settings)
import tts

FAKE_WORKER = textwrap.dedent('''
    import json, math, os, struct, sys, time, wave
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("op") == "status":
            print(json.dumps({"id": request["id"], "ok": True, "pid": os.getpid()}), flush=True)
            continue
        text = request.get("text", "")
        if "DIE" in text:
            os._exit(3)
        if "FAIL" in text:
            print(json.dumps({"id": request["id"], "ok": False, "type": "ValueError", "error": "bad input"}), flush=True)
            continue
        if "SLOW" in text:
            time.sleep(60)
        with open(request["output"] + ".request.json", "w") as record:
            json.dump(request, record)
        partial = request["output"] + ".partial"
        with wave.open(partial, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
            out.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(i / 8))) for i in range(36000)))
        os.replace(partial, request["output"])
        print(json.dumps({"id": request["id"], "ok": True, "sample_rate": 24000, "samples": 36000,
                          "pid": os.getpid()}), flush=True)
''')


class MLXTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worker = self.root / 'fake_worker.py'
        self.worker.write_text(FAKE_WORKER)
        self.client = MLXWorkerClient(sys.executable, worker=self.worker)

    def tearDown(self):
        self.client.ensure_stopped_if_idle()
        self.tmp.cleanup()

    def request(self, text, **extra):
        output = self.root / f'out-{time.monotonic_ns()}.wav'
        return output, dict({'op': 'generate', 'text': text, 'output': str(output)}, **extra)


class MLXWorkerClientTests(MLXTestBase):
    def test_round_trip_reuses_one_worker_and_publishes_whole_files(self):
        first, payload = self.request('Hello there.')
        reply = self.client.request(payload)
        second, payload = self.request('Again.')
        again = self.client.request(payload)
        self.assertEqual(24000, reply['sample_rate'])
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(reply['pid'], again['pid'])

    def test_cancel_mid_request_stops_the_worker_and_publishes_nothing(self):
        output, payload = self.request('SLOW line')
        process_holder = {}
        started = time.monotonic()

        def cancel():
            process_holder.setdefault('process', self.client._process)
            return time.monotonic() - started > 0.5
        with self.assertRaises(MLXGenerationCancelled):
            self.client.request(payload, cancel_check=cancel)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIsNone(self.client._process)
        self.assertIsNotNone(process_holder['process'].poll())
        self.assertFalse(output.exists() or Path(f'{output}.partial').exists())

    def test_error_reply_raises_and_keeps_the_worker(self):
        _, payload = self.request('FAIL here')
        with self.assertRaisesRegex(RuntimeError, 'bad input'):
            self.client.request(payload)
        process = self.client._process
        output, payload = self.request('Fine now.')
        self.client.request(payload)
        self.assertIs(process, self.client._process)
        self.assertTrue(output.exists())

    def test_worker_death_is_reported_and_the_next_request_restarts(self):
        _, payload = self.request('DIE now')
        with self.assertRaisesRegex(RuntimeError, 'exited unexpectedly'):
            self.client.request(payload)
        output, payload = self.request('Back again.')
        self.client.request(payload)
        self.assertTrue(output.exists())

    def test_idle_stop_refuses_while_a_request_runs(self):
        _, payload = self.request('SLOW line')
        stop = threading.Event()
        thread = threading.Thread(target=lambda: self.assertRaises(
            MLXGenerationCancelled, self.client.request, payload, cancel_check=stop.is_set))
        thread.start()
        deadline = time.monotonic() + 10
        while self.client._process is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.client.ensure_stopped_if_idle())
        stop.set()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.client.ensure_stopped_if_idle())
        self.assertIsNone(self.client._process)

    def test_missing_interpreter_fails_before_starting_anything(self):
        client = MLXWorkerClient(str(self.root / 'no' / 'python'), worker=self.worker)
        _, payload = self.request('Hello.')
        with self.assertRaisesRegex(FileNotFoundError, 'MLX environment missing'):
            client.request(payload)
        self.assertIsNone(client._process)


class MLXStatusTests(unittest.TestCase):
    def test_settings_default_and_ignore_unknown_or_blank_models(self):
        settings = get_mlx_settings({'mlx_models': {'custom': '/m/custom', 'lora': '/m/lora', 'clone': ' '}})
        self.assertEqual('/m/custom', settings['models']['custom'])
        self.assertEqual(DEFAULT_MLX_MODELS['clone'], settings['models']['clone'])
        self.assertNotIn('lora', settings['models'])
        self.assertTrue(settings['python'].endswith(os.path.join('mlx_env', 'bin', 'python')))

    def test_status_names_each_missing_piece(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {'python': os.path.join(tmp, 'missing-python'),
                        'models': dict(DEFAULT_MLX_MODELS, design=os.path.join(tmp, 'missing-model'))}
            with patch.object(mlx_tts_backend, 'is_mlx_platform', return_value=False):
                status = get_mlx_backend_status(settings)
        self.assertFalse(status['ready'])
        text = ' '.join(status['problems'])
        self.assertIn('Apple Silicon', text)
        self.assertIn('mlx_env', text)
        self.assertIn('design MLX model folder', text)

    def test_status_ready_with_interpreter_and_hub_ids(self):
        with patch.object(mlx_tts_backend, 'is_mlx_platform', return_value=True):
            status = get_mlx_backend_status({'python': sys.executable, 'models': dict(DEFAULT_MLX_MODELS)})
        self.assertEqual({'ready': True, 'problems': []}, {k: status[k] for k in ('ready', 'problems')})


class MLXEngineDispatchTests(MLXTestBase):
    def engine(self, **tts_config):
        engine = tts.TTSEngine({'tts': dict({'mode': 'mlx', 'language': 'English', 'max_new_tokens': 1024,
                                             'mlx_python': sys.executable}, **tts_config)})
        engine._mlx_client = self.client
        return engine

    def recorded(self, output):
        return json.loads(Path(f'{output}.pending').parent.joinpath(
            next(p.name for p in Path(output).parent.iterdir() if p.name.endswith('.request.json'))).read_text())

    def ready(self):
        return patch.object(mlx_tts_backend, 'is_mlx_platform', return_value=True)

    def test_custom_voice_publishes_and_sends_speaker_seed_language_cap(self):
        output = self.root / 'custom' / 'line.wav'
        output.parent.mkdir()
        with self.ready(), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.engine().generate_custom_voice(
                'A short synthetic line.', '', 'S', {'S': {'voice': 'Ryan', 'seed': 7}}, str(output)))
        sent = self.recorded(output)
        self.assertEqual(('custom', 'Ryan', 7, 'English', 1024),
                         (sent['family'], sent['speaker'], sent['seed'], sent['language'], sent['max_tokens']))
        self.assertEqual(DEFAULT_MLX_MODELS['custom'], sent['model'])
        self.assertTrue(output.exists())
        self.assertEqual(['line.wav'], sorted(p.name for p in output.parent.iterdir() if p.suffix == '.wav'))

    def test_clone_voice_sends_the_resolved_reference(self):
        reference = self.root / 'ref.wav'
        reference.write_bytes(b'RIFF')
        output = self.root / 'clone' / 'line.wav'
        output.parent.mkdir()
        voice = {'C': {'type': 'clone', 'ref_audio': str(reference), 'ref_text': 'Reference words.', 'seed': 3}}
        with self.ready(), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.engine().generate_clone_voice('A cloned line.', 'C', voice, str(output)))
        sent = self.recorded(output)
        self.assertEqual(('clone', str(reference), 'Reference words.', 3),
                         (sent['family'], sent['ref_audio'], sent['ref_text'], sent['seed']))

    def test_voice_design_returns_path_and_rate(self):
        output = self.root / 'design' / 'preview.wav'
        with self.ready(), contextlib.redirect_stdout(io.StringIO()):
            path, rate = self.engine().generate_voice_design('A calm narrator.', 'Designed words.', seed=5,
                                                             preview_path=str(output))
        self.assertEqual((str(output), 24000), (path, rate))
        sent = self.recorded(output)
        self.assertEqual(('design', 'A calm narrator.', 5), (sent['family'], sent['instruct'], sent['seed']))

    def test_lora_and_ensemble_are_refused_in_mlx_mode(self):
        engine = self.engine()
        for category in ('lora', 'ensemble'):
            with self.subTest(category=category), self.assertRaises(tts.UnsupportedVoiceBackendError):
                engine.get_voice_backend(category)

    def test_missing_environment_fails_with_an_actionable_message(self):
        engine = tts.TTSEngine({'tts': {'mode': 'mlx', 'mlx_python': str(self.root / 'none' / 'python')}})
        output = self.root / 'line.wav'
        with self.ready(), contextlib.redirect_stdout(io.StringIO()):
            result = engine.generate_voice_result('A line.', '', 'S', {'S': {'voice': 'Ryan'}}, str(output))
        self.assertFalse(result.success)
        self.assertIn('mlx_env', str(result.failure))
        self.assertFalse(output.exists())
        self.assertIsNone(engine._mlx_client)

    def test_batch_renders_each_line_through_the_worker(self):
        out_dir = self.root / 'batch'
        out_dir.mkdir()
        chunks = [{'index': 0, 'text': 'First line.', 'speaker': 'S'},
                  {'index': 1, 'text': 'FAIL line.', 'speaker': 'S'},
                  {'index': 2, 'text': 'Third line.', 'speaker': 'S'}]
        with self.ready(), contextlib.redirect_stdout(io.StringIO()):
            results = self.engine()._mlx_batch(chunks, {'S': {'voice': 'Ryan', 'seed': 1}}, str(out_dir), 'custom')
        self.assertEqual([0, 2], results['completed'])
        self.assertEqual([1], [index for index, _ in results['failed']])

    def test_idle_release_stops_the_worker(self):
        engine = self.engine()
        output = self.root / 'line.wav'
        with self.ready(), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(engine.generate_custom_voice('A line.', '', 'S', {'S': {'voice': 'Ryan'}}, str(output)))
        process = self.client._process
        self.assertIsNotNone(process)
        self.assertTrue(engine.ensure_idle_local_models_released())
        self.assertIsNone(self.client._process)
        self.assertIsNotNone(process.poll())


if __name__ == '__main__':
    unittest.main()
