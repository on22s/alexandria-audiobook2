"""External TTS: a pool of Gradio endpoints, locked clients per endpoint worker, concurrent batches."""
import os
import glob
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tts as tts_module


class _FakeClient:
    """Records calls; behaviour per URL is set by the test."""
    instances = []

    def __init__(self, url):
        self.url = url
        self.calls = []
        self.behaviour = _FakeClient.behaviour_for.get(url, "ok")
        _FakeClient.instances.append(self)

    behaviour_for = {}

    def predict(self, *args, **kwargs):
        self.calls.append(kwargs.get("text") or (args[2] if len(args) > 2 else None))
        if self.behaviour == "raise":
            raise RuntimeError("server exploded")
        if self.behaviour == "hang":
            time.sleep(5)
        out = os.path.join(_FakeClient.tmp, f"gen_{threading.get_ident()}_{len(self.calls)}.wav")
        with wave.open(out, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(24000)
            f.writeframes(b"\0\0" * 1200)
        return (out,)


class _FakeGradio:
    Client = _FakeClient
    handle_file = staticmethod(lambda p: p)


def _engine(urls, workers=2, timeout=300):
    cfg = {"tts": {"mode": "external", "url": "http://one:7860", "external_urls": urls,
                   "parallel_workers": workers, "external_timeout_seconds": timeout}}
    return tts_module.TTSEngine(cfg)


class ExternalPoolTests(unittest.TestCase):
    def test_unreadable_gpu_model_config_limits_batch_to_one(self):
        engine = tts_module.TTSEngine.__new__(tts_module.TTSEngine)
        engine._max_new_tokens = 2048
        fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
        with patch.dict(sys.modules, {"torch": fake_torch}):
            self.assertEqual(1, engine._estimate_max_batch_size(SimpleNamespace()))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _FakeClient.tmp = self.tmp.name
        _FakeClient.instances = []
        _FakeClient.behaviour_for = {}
        self._mod = patch.dict(sys.modules, {"gradio_client": _FakeGradio})
        self._mod.start()
        self.voice_config = {"NARR": {"type": "custom", "voice": "Ryan", "seed": 1},
                             "HOLO": {"type": "clone", "ref_audio": os.path.join(self.tmp.name, "ref.wav"),
                                      "ref_text": "hi", "seed": 1}}
        Path(self.voice_config["HOLO"]["ref_audio"]).write_bytes(b"RIFF")

    def tearDown(self):
        self._mod.stop()
        self.tmp.cleanup()

    def _chunks(self, n, speaker="NARR"):
        return [{"index": i, "text": f"line {i}", "instruct": "", "speaker": speaker} for i in range(n)]

    def test_bad_external_audio_is_rejected_and_prior_output_is_preserved(self):
        good = Path(self.tmp.name, "good.wav")
        with wave.open(str(good), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\0\0" * 1200)
        broken = Path(self.tmp.name, "broken.wav")
        output = Path(self.tmp.name, "output.wav")
        for payload in (b"not audio", good.read_bytes()[:100]):
            broken.write_bytes(payload)
            for kind, speaker in (("custom", "NARR"), ("clone", "HOLO")):
                with self.subTest(kind=kind, payload_size=len(payload)):
                    output.write_bytes(good.read_bytes())
                    engine = _engine([])
                    with patch.object(_FakeClient, "predict", return_value=(str(broken),)):
                        if kind == "custom":
                            success = engine.generate_custom_voice("Hi", "", speaker, self.voice_config, str(output))
                        else:
                            success = engine.generate_clone_voice("Hi", speaker, self.voice_config, str(output))
                    self.assertFalse(success)
                    self.assertEqual(good.read_bytes(), output.read_bytes())
                    self.assertEqual([], list(Path(self.tmp.name).glob("output.wav.*.tmp")))

    def test_valid_external_output_is_copied_and_fully_decodable(self):
        from audio_validation import validate_generated_audio
        engine = _engine([])
        for kind, speaker in (("custom", "NARR"), ("clone", "HOLO")):
            with self.subTest(kind=kind):
                output = str(Path(self.tmp.name, kind + ".wav"))
                if kind == "custom":
                    success = engine.generate_custom_voice("Hi", "", speaker, self.voice_config, output)
                else:
                    success = engine.generate_clone_voice("Hi", speaker, self.voice_config, output)
                self.assertTrue(success)
                self.assertEqual(output, validate_generated_audio(output))

    def test_empty_pool_is_the_single_url_with_clients_per_worker(self):
        engine = _engine([])
        self.assertEqual(["http://one:7860"], engine.external_endpoints())
        for i in range(3):
            self.assertTrue(engine.generate_custom_voice(f"t{i}.", "", "NARR", self.voice_config,
                                                        os.path.join(self.tmp.name, f"o{i}.wav")))
        self.assertEqual(2, len(_FakeClient.instances))
        self.assertEqual(["t0.", "t1.", "t2."], sorted(call for client in _FakeClient.instances for call in client.calls))

    def test_round_robin_across_the_pool_and_endpoint_workers(self):
        urls = ["http://a:7860", "http://b:7860", "http://c:7860"]
        engine = _engine(urls)
        for i in range(6):
            engine.generate_custom_voice(f"t{i}.", "", "NARR", self.voice_config,
                                         os.path.join(self.tmp.name, f"o{i}.wav"))
        by_url = {}
        for client in _FakeClient.instances:
            by_url.setdefault(client.url, []).extend(client.calls)
        self.assertEqual(set(urls), set(by_url))
        self.assertEqual({"http://a:7860": ["t0.", "t3."], "http://b:7860": ["t1.", "t4."],
                          "http://c:7860": ["t2.", "t5."]}, by_url)

    def test_batch_completes_every_chunk_across_the_pool(self):
        engine = _engine(["http://a:7860", "http://b:7860"], workers=2)
        result = engine.generate_batch(self._chunks(10), self.voice_config, self.tmp.name)
        self.assertEqual(list(range(10)), sorted(result["completed"]))
        self.assertEqual([], result["failed"])
        for i in range(10):
            self.assertTrue(os.path.exists(os.path.join(self.tmp.name, f"temp_batch_{i}.wav")))
        self.assertEqual(4, len(_FakeClient.instances))
        self.assertEqual(10, sum(len(c.calls) for c in _FakeClient.instances))

    def test_clone_chunks_go_through_the_pool_too(self):
        engine = _engine(["http://a:7860", "http://b:7860"])
        result = engine.generate_batch(self._chunks(4, speaker="HOLO"), self.voice_config, self.tmp.name)
        self.assertEqual([0, 1, 2, 3], sorted(result["completed"]))
        self.assertEqual(4, len(_FakeClient.instances))

    def test_one_broken_endpoint_fails_its_chunks_and_the_rest_complete(self):
        _FakeClient.behaviour_for = {"http://b:7860": "raise"}
        engine = _engine(["http://a:7860", "http://b:7860"])
        result = engine.generate_batch(self._chunks(4), self.voice_config, self.tmp.name)
        # round-robin: b gets chunks 1 and 3; a raise inside the external call is
        # caught there and reported as a generation failure
        self.assertEqual([0, 2], sorted(result["completed"]))
        self.assertEqual([1, 3], sorted(i for i, _ in result["failed"]))

    def test_a_hung_endpoint_is_reported_as_a_timeout_not_waited_for(self):
        _FakeClient.behaviour_for = {"http://b:7860": "hang"}
        engine = _engine(["http://a:7860", "http://b:7860"], timeout=1)
        started = time.time()
        result = engine.generate_batch(self._chunks(2), self.voice_config, self.tmp.name)
        self.assertLess(time.time() - started, 4.0)
        self.assertEqual([0], result["completed"])
        self.assertEqual(1, len(result["failed"]))
        self.assertIn("timed out after 1s", result["failed"][0][1])

    def test_timed_out_call_never_publishes_its_late_audio(self):
        _FakeClient.behaviour_for = {"http://one:7860": "hang"}
        engine = _engine(["http://one:7860"], timeout=1)
        result = engine.generate_batch(self._chunks(1), self.voice_config, self.tmp.name)
        self.assertEqual([], result["completed"])
        self.assertEqual([0], [index for index, _ in result["failed"]])

        # The client writes only after its delayed request returns. The batch
        # has already declared failure, so that late result must stay private.
        time.sleep(5.2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "temp_batch_0.wav")))

    def test_single_custom_and_clone_timeout_preserve_prior_audio(self):
        _FakeClient.behaviour_for = {"http://one:7860": "hang"}
        engine = _engine(["http://one:7860"], timeout=1)
        custom = os.path.join(self.tmp.name, "custom.wav")
        clone = os.path.join(self.tmp.name, "clone.wav")
        for path in (custom, clone):
            Path(path).write_bytes(b"previous")
        started = time.time()
        self.assertFalse(engine.generate_custom_voice(
            "hello", "", "NARR", self.voice_config, custom))
        self.assertFalse(engine.generate_clone_voice(
            "hello", "HOLO", self.voice_config, clone))
        self.assertLess(time.time() - started, 3.5)
        self.assertEqual(b"previous", Path(custom).read_bytes())
        self.assertEqual(b"previous", Path(clone).read_bytes())
        time.sleep(5.2)
        self.assertEqual([], glob.glob(os.path.join(self.tmp.name, "*.pending.*")))


if __name__ == "__main__":
    unittest.main()
