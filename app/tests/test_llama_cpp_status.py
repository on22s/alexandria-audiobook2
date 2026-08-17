"""Status reporting for the runtime this project actually runs.

`get_lmstudio_status` asks `lms ps`, which is the right instrument for LM
Studio and the wrong one for llama.cpp. Against the live server it reported

    {"available": true, "loaded": false, "context_length": null,
     "parallel": null, "optimized": false}

for a model answering in 0.2 seconds - reading "LM Studio has never heard of
this" as "the model is not loaded". Two routers show that to the user, so the
Setup tab has been wrong on every load since the project moved to llama.cpp.
"""
import io
import json
import unittest
from unittest.mock import patch

import lmstudio_settings as ls


PROPS = {
    "model_alias": "qwen3-14b",
    "model_path": "/models/Qwen3-14B-Q4_K_M.gguf",
    "model_ftype": "Q4_K - Medium",
    "build_info": "b10448-176f4c2",
    "total_slots": 1,
    "default_generation_settings": {"n_ctx": 32768,
                                    "params": {"reasoning_format": "none"}},
}


def fake_props(payload):
    """Patch urlopen to serve `payload` from /props, or raise if payload None."""
    def opener(url, timeout=None):
        if payload is None:
            raise OSError("connection refused")
        body = json.dumps(payload).encode()
        return io.BytesIO(body).__class__(body) if False else _Ctx(body)
    return opener


class _Ctx(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LlamaCppStatusTests(unittest.TestCase):
    def _status(self, payload, model="qwen3-14b"):
        with patch("urllib.request.urlopen", fake_props(payload)):
            return ls.get_llama_cpp_status("http://127.0.0.1:8090/v1", model)

    def test_a_loaded_model_is_reported_as_loaded(self):
        status = self._status(PROPS)
        self.assertTrue(status["loaded"])
        self.assertEqual(32768, status["context_length"])
        self.assertEqual(1, status["parallel"])
        self.assertEqual("llama.cpp", status["runtime"])

    def test_optimized_is_none_not_false(self):
        """llama.cpp is configured at launch and has no reload settings.

        Reporting False would say "your model is misconfigured"; None says the
        question does not apply to this runtime.
        """
        self.assertIsNone(self._status(PROPS)["optimized"])

    def test_a_non_llama_endpoint_returns_None_so_lm_studio_still_works(self):
        # None, not a status - the LM Studio path must be reachable unchanged.
        self.assertIsNone(self._status(None))

    def test_an_endpoint_answering_something_else_is_not_claimed(self):
        self.assertIsNone(self._status({"hello": "world"}))

    def test_a_server_running_the_wrong_model_is_not_loaded(self):
        # Scoring a run against a model nobody meant to use is the failure
        # this guards; a mismatched alias must never report loaded.
        status = self._status(PROPS, model="some-other-model")
        self.assertFalse(status["loaded"])
        self.assertEqual("qwen3-14b", status["server_alias"])

    def test_a_bare_model_name_matches_a_namespaced_config_value(self):
        # config.json may say "qwen/qwen3-14b" where the server aliases it
        # "qwen3-14b"; that is the same model, not a mismatch.
        self.assertTrue(self._status(PROPS, model="qwen/qwen3-14b")["loaded"])

    def test_reasoning_format_is_surfaced(self):
        # This is what distinguishes a server that can do structured passes
        # from one that will burn its token budget thinking.
        self.assertEqual("none", self._status(PROPS)["reasoning_format"])


class DispatchTests(unittest.TestCase):
    """get_current_status must ask the endpoint what it is first."""

    def test_llama_cpp_is_preferred_over_asking_lms(self):
        with patch.object(ls, "get_llama_cpp_status", return_value={"loaded": True}), \
             patch.object(ls, "get_lmstudio_status") as lms:
            out = ls.get_current_status("local", "http://127.0.0.1:8090/v1", "m")
        self.assertEqual({"loaded": True}, out)
        lms.assert_not_called()

    def test_lm_studio_is_still_asked_when_the_endpoint_is_not_llama_cpp(self):
        sentinel = {"available": True, "loaded": True, "from": "lms"}
        with patch.object(ls, "get_llama_cpp_status", return_value=None), \
             patch.object(ls, "get_lmstudio_status", return_value=sentinel) as lms:
            out = ls.get_current_status("local", "http://127.0.0.1:1234/v1", "m")
        self.assertEqual(sentinel, out)
        lms.assert_called_once()

    def test_a_remote_endpoint_is_untouched_by_this_change(self):
        sentinel = {"remote": True}
        with patch.object(ls, "is_remote_llm", return_value=True), \
             patch.object(ls, "get_remote_lmstudio_status", return_value=sentinel), \
             patch.object(ls, "get_llama_cpp_status") as native:
            out = ls.get_current_status("remote", "https://x/v1", "m", "alias")
        self.assertEqual(sentinel, out)
        native.assert_not_called()


class SingleReaderTests(unittest.TestCase):
    def test_the_experiment_provenance_uses_the_shared_reader(self):
        """Rule 15: one definition of "what is the server running".

        pdnc_narrator_prior parsed /props itself. Two copies would drift, and
        the app's status route needed the same answer.
        """
        from experiments import pdnc_narrator_prior
        # No assertIn("get_llama_cpp_status") here: it is decorative, and
        # demonstrably so. Stubbing pdnc's call out entirely left that
        # assertion passing - the name still appears in the import line -
        # while the two behavioural tests below caught it immediately.
        source = open(pdnc_narrator_prior.__file__, encoding="utf-8").read()
        self.assertNotIn('"/props"', source,
                         "the /props URL should exist in one place only")

    def test_provenance_still_refuses_an_unverified_server(self):
        from experiments import pdnc_narrator_prior as p
        with patch.object(ls, "get_llama_cpp_status", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "no llama.cpp"):
                p.get_llama_server_environment("http://x/v1", "qwen3-14b")

    def test_provenance_refuses_a_mismatched_model(self):
        from experiments import pdnc_narrator_prior as p
        wrong = {"loaded": False, "server_alias": "other", "context_length": 32768,
                 "parallel": 1}
        with patch.object(ls, "get_llama_cpp_status", return_value=wrong):
            with self.assertRaisesRegex(RuntimeError, "!="):
                p.get_llama_server_environment("http://x/v1", "qwen3-14b")


if __name__ == "__main__":
    unittest.main()
