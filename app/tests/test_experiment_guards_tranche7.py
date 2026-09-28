"""Goal 6.6, seventh tranche: guards in app/experiments/ that no test had shown failing.

Each guard gets a case it must REJECT, named for the attack it stops, and a case it must
ACCEPT, so a guard that refuses everything fails too.

- The adapter-scale toggle exists in five copies (lora_serving_eval, pdnc_eval,
  lora_scale_sweep, chinese_attribution, scale_vs_register). A llama-server that ignores
  POST /lora-adapters would make the "base" arm the adapter at scale 1 - the mistake that
  cost a whole chain on 2026-09-19. Each copy is driven against a stand-in server that
  honours the request, ignores it, or omits the scale from its readback.
- ljspeech_prepare.split_by_book must hold out whole books (the 2.7 failure mode is a
  held-out set sharing a source with training) and refuse a book id it does not know.
- attribution_hybrid.index_rows must refuse duplicate and missing ids.
- blinded_listening._resolve_source must refuse a path outside the repository and a
  missing file.
- library_voice_fidelity_resume_20260831.is_valid_audio must call a truncated WAV invalid.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, os.path.join(REPO, "app"))

import experiments.attribution_hybrid as hybrid  # noqa: E402
import experiments.blinded_listening as blinded  # noqa: E402
import experiments.chinese_attribution as chinese  # noqa: E402
import experiments.ljspeech_prepare as ljs  # noqa: E402
import experiments.library_voice_fidelity_resume_20260831 as fidelity  # noqa: E402
import experiments.lora_scale_sweep as sweep  # noqa: E402
import experiments.lora_serving_eval as lse  # noqa: E402
import experiments.pdnc_eval as pdnc  # noqa: E402
import experiments.scale_vs_register as register  # noqa: E402

SCALE_SETTERS = {
    "lora_serving_eval.set_adapter_scale": lse.set_adapter_scale,
    "pdnc_eval.set_scale": pdnc.set_scale,
    "lora_scale_sweep.set_scale": sweep.set_scale,
    "chinese_attribution.set_scale": chinese.set_scale,
    "scale_vs_register.set_scale": register.set_scale,
}


class FakeLoraServer:
    """A /lora-adapters endpoint. mode: 'honest' | 'ignores' | 'no_scale_field'."""

    def __init__(self, mode):
        state = {"scale": 1.0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if mode != "ignores":
                    state["scale"] = float(body[0]["scale"])
                self._reply([])

            def do_GET(self):
                entry = {"id": 0, "path": "adapter.gguf"}
                if mode != "no_scale_field":
                    entry["scale"] = state["scale"]
                self._reply([entry])

            def _reply(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class AdapterScaleToggleTests(unittest.TestCase):
    def _each(self, mode):
        server = FakeLoraServer(mode)
        self.addCleanup(server.close)
        return server.url

    def test_honest_server_accepted_by_every_copy(self):
        url = self._each("honest")
        for name, setter in SCALE_SETTERS.items():
            with self.subTest(name):
                self.assertEqual(setter(url, 0.0), 0.0)
                self.assertEqual(setter(url, 1.0), 1.0)

    def test_server_that_ignores_the_toggle_is_refused_by_every_copy(self):
        # Base arm silently served at adapter scale 1: every copy must refuse to go on.
        url = self._each("ignores")
        for name, setter in SCALE_SETTERS.items():
            with self.subTest(name), self.assertRaisesRegex(RuntimeError, "did not take"):
                setter(url, 0.0)

    def test_readback_without_a_scale_is_refused_by_every_copy(self):
        # A server that does not report the scale cannot confirm the toggle.
        url = self._each("no_scale_field")
        for name, setter in SCALE_SETTERS.items():
            with self.subTest(name), self.assertRaisesRegex(RuntimeError, "did not take"):
                setter(url, 0.0)


class HoldoutSplitTests(unittest.TestCase):
    ROWS = ([{"book": "a", "normalized": "x" * 50} for _ in range(60)]
            + [{"book": "b", "normalized": "y" * 50} for _ in range(45)]
            + [{"book": "c", "normalized": "z" * 50} for _ in range(300)])

    def test_no_book_is_on_both_sides(self):
        train, test, held, _ = ljs.split_by_book(self.ROWS, [], 10, 100)
        self.assertEqual(held, ["a", "b"])
        self.assertTrue(test)
        self.assertEqual(set(), {r["book"] for r in train} & {r["book"] for r in test})

    def test_unknown_book_id_is_refused(self):
        with self.assertRaises(SystemExit):
            ljs.split_by_book(self.ROWS, ["no_such_book"], 10, 100)


class PredictionIndexTests(unittest.TestCase):
    def test_duplicate_id_is_refused(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            hybrid.index_rows([{"id": "l1"}, {"id": "l1"}])

    def test_missing_id_is_refused(self):
        with self.assertRaisesRegex(ValueError, "needs an id"):
            hybrid.index_rows([{"id": "l1"}, {"speaker": "EMMA"}])

    def test_distinct_ids_are_indexed(self):
        self.assertEqual(set(hybrid.index_rows([{"id": "l1"}, {"id": "l2"}])), {"l1", "l2"})


def _write_wav(path, frames=1600):
    import numpy as np
    import soundfile as sf
    sf.write(path, np.zeros(frames, dtype="float32"), 16000)


class ListeningSourceTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._td.name)
        self._repo = blinded.REPO
        blinded.REPO = self.root

    def tearDown(self):
        blinded.REPO = self._repo
        self._td.cleanup()

    def test_path_outside_the_repository_is_refused(self):
        outside = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        _write_wav(outside.name)
        with self.assertRaisesRegex(blinded.ListeningPackageError, "escapes repository"):
            blinded._resolve_source(outside.name)

    def test_dotdot_escape_is_refused(self):
        with self.assertRaisesRegex(blinded.ListeningPackageError, "escapes repository"):
            blinded._resolve_source("../../etc/passwd")

    def test_missing_file_is_refused(self):
        with self.assertRaisesRegex(blinded.ListeningPackageError, "does not exist"):
            blinded._resolve_source("missing.wav")

    def test_real_wav_inside_the_repository_is_accepted(self):
        _write_wav(os.path.join(self.root, "ok.wav"))
        self.assertEqual(blinded._resolve_source("ok.wav"), os.path.join(self.root, "ok.wav"))


class GeneratedWavValidityTests(unittest.TestCase):
    def test_truncated_wav_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            good = os.path.join(td, "good.wav")
            _write_wav(good)
            data = open(good, "rb").read()
            cut = os.path.join(td, "cut.wav")
            open(cut, "wb").write(data[:30])    # header torn mid-way
            self.assertFalse(fidelity.is_valid_audio(cut))
            self.assertFalse(fidelity.is_valid_audio(os.path.join(td, "absent.wav")))
            self.assertTrue(fidelity.is_valid_audio(good))


if __name__ == "__main__":
    unittest.main()
