import unittest

from experiments.lora_serving_eval import (get_book_paths, get_eval_arms,
                                           get_eval_metadata, ServingResponseCapture)
from experiments.distill_eval import (get_book_paths as get_distill_book_paths,
                                      get_model_loader_name,
                                      get_model_load_kwargs,
                                      load_peft_adapter_or_raise)


class LoraServingEvalModeTests(unittest.TestCase):

    def test_http_capture_preserves_request_and_attribution_decision(self):
        import base64, json, tempfile
        from unittest.mock import patch
        import httpx
        from openai import DefaultHttpxClient, OpenAI
        from generate_script import LLMGenParams
        from three_pass_generate import attribute_batch

        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        for speaker in ("ELENA", ""):
            with self.subTest(speaker=speaker), tempfile.TemporaryDirectory() as tmp:
                requests, decisions = [], []
                raw = "  " + json.dumps([{"n": 0, "head": "Tell me.",
                                          "speaker": speaker}]) + "\n"
                body = json.dumps({"id": "synthetic", "object": "chat.completion",
                    "created": 0, "model": "stub", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": raw},
                    "finish_reason": "stop"}]}).encode()
                def serve(request):
                    requests.append(request.content)
                    return httpx.Response(200, content=body)
                capture = ServingResponseCapture()
                for enabled in (False, True):
                    hooks = {"request": [capture.observe_request],
                             "response": [capture.observe_response]} if enabled else {}
                    with OpenAI(base_url="http://localhost/v1", api_key="synthetic-secret",
                        http_client=DefaultHttpxClient(transport=httpx.MockTransport(serve),
                                                      event_hooks=hooks)) as client:
                        with patch("generate_script.get_response_log_path", return_value=tmp + "/responses.log"):
                            try:
                                out = attribute_batch(client, "stub", frozen,
                                    LLMGenParams(max_tokens=512, context_length=4096),
                                    roster=["ELENA"], max_retries=0,
                                    attempt_observer=capture.observe_validation if enabled else None)
                                decisions.append(out)
                            except Exception as exc:
                                decisions.append(type(exc).__name__)
                self.assertEqual(requests[0], requests[1])
                self.assertEqual(decisions[0], decisions[1])
                diagnostic = capture.finalize_window(0)[0]
                self.assertEqual(raw, diagnostic["raw_response"])
                self.assertEqual(body, base64.b64decode(diagnostic["raw_response_body_base64"]))
                self.assertEqual("accepted" if speaker == "ELENA" else "quality_rejected",
                                 diagnostic["validation"]["outcome"])
                self.assertNotIn("synthetic-secret", json.dumps(diagnostic))
                self.assertEqual(64, len(diagnostic["prompt_sha256"]))

    def test_http_capture_retains_sdk_retry_reply(self):
        import httpx
        from openai import DefaultHttpxClient, OpenAI
        capture = ServingResponseCapture()
        replies = [httpx.Response(500, content=b'{"error":{"message":"synthetic"}}'),
                   httpx.Response(200, json={"id": "stub", "object": "chat.completion",
                       "created": 0, "model": "stub", "choices": []})]
        with OpenAI(base_url="http://localhost/v1", api_key="local", max_retries=1,
                    http_client=DefaultHttpxClient(transport=httpx.MockTransport(
                        lambda request: replies.pop(0)), event_hooks={
                            "request": [capture.observe_request],
                            "response": [capture.observe_response]})) as client:
            client.chat.completions.create(model="stub", messages=[])
        self.assertEqual([500, 200], [a["http_status"] for a in capture.attempts])
        self.assertTrue(all(a.get("raw_response_body_base64") for a in capture.attempts))
    def test_normal_mode_toggles_both_adapter_scales(self):
        self.assertEqual((("base", 0.0), ("lora", 1.0)), get_eval_arms())

    def test_base_only_mode_never_requests_adapter_state(self):
        self.assertEqual((("base", None),), get_eval_arms(base_only=True))

    def test_base_only_metadata_claims_only_the_arm_actually_run(self):
        decoding, notes = get_eval_metadata(base_only=True)
        self.assertEqual(["base"], decoding["arms"])
        self.assertNotIn("base_quant", decoding)
        self.assertNotIn("lora", decoding)
        self.assertIn("no adapter was loaded", notes)
        self.assertIn("does not observe base quantisation", notes)

    def test_metadata_records_nondefault_request_controls(self):
        decoding, _ = get_eval_metadata(
            base_only=True, batch=5, reasoning_effort="low", max_tokens=6000,
            structured_output="off")
        self.assertEqual(5, decoding["batch"])
        self.assertEqual("low", decoding["reasoning_effort"])
        self.assertEqual(6000, decoding["max_tokens"])
        self.assertEqual("off", decoding["structured_output"])

    def test_default_budget_is_the_products_not_a_smaller_one(self):
        """2000 was half the product's room; Muse overflowed 70% of windows."""
        from generate_script import LLMGenParams
        decoding, _ = get_eval_metadata()
        self.assertEqual(LLMGenParams().max_tokens, decoding["max_tokens"])
        self.assertGreaterEqual(decoding["max_tokens"], 4096)

    def test_hard_max_tokens_equal_to_budget_stops_escalation(self):
        """A runaway at IQ2 escalated 4096 -> 6144 -> 9216 at ~4 min a try
        (tnr-4, 2026-09-28). With the ceiling at the budget it must stay put,
        while the product default still escalates."""
        from generate_script import LLMGenParams, get_next_retry_max_tokens
        self.assertEqual(4096, get_next_retry_max_tokens(4096, "incomplete_output", 4096))
        self.assertGreater(get_next_retry_max_tokens(
            4096, "incomplete_output", LLMGenParams().hard_max_tokens), 4096)

    def test_hard_max_tokens_below_budget_is_refused(self):
        import os, subprocess, sys
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        run = subprocess.run(
            [sys.executable, os.path.join(app, "experiments", "lora_serving_eval.py"),
             "--max-tokens", "4096", "--hard-max-tokens", "2048",
             "--base_url", "http://127.0.0.1:9/v1"],
            cwd=app, capture_output=True, text=True, timeout=60)
        self.assertEqual(2, run.returncode, run.stderr[-500:])
        self.assertIn("--hard-max-tokens must be at least --max-tokens", run.stderr)

    def test_paired_metadata_matches_the_two_executed_arms(self):
        decoding, notes = get_eval_metadata()
        self.assertEqual(["base", "lora"], decoding["arms"])
        self.assertNotIn("base_quant", decoding)
        self.assertNotIn("lora", decoding)
        self.assertIn("differ only by adapter scale", notes)
        self.assertIn("does not observe base quantisation", notes)

    def test_corrected_data_directories_override_legacy_layout(self):
        self.assertEqual(
            ("/inputs/index18.txt",
             "/checkpoints/index18__three_pass.json.threepass_checkpoint.json"),
            get_book_paths("index18", "/inputs", "/checkpoints"),
        )

    def test_distill_eval_uses_the_same_corrected_layout(self):
        self.assertEqual(
            ("/inputs/index18.txt",
             "/checkpoints/index18__three_pass.json.threepass_checkpoint.json"),
            get_distill_book_paths("index18", "/inputs", "/checkpoints"),
        )

    def test_nf4_loader_policy_preserves_bf16_compute(self):
        class Torch:
            bfloat16 = "bf16"

        marker = object()
        self.assertEqual(
            {"torch_dtype": "bf16", "device_map": "auto",
             "trust_remote_code": True, "quantization_config": marker},
            get_model_load_kwargs(Torch, marker),
        )

    def test_qwen35_moe_uses_training_compatible_wrapper(self):
        self.assertEqual(
            "AutoModelForImageTextToText",
            get_model_loader_name(["Qwen3_5MoeForConditionalGeneration"]),
        )
        self.assertEqual(
            "AutoModelForCausalLM",
            get_model_loader_name(["Qwen3ForCausalLM"]),
        )

    def test_missing_adapter_keys_fail_instead_of_running_inert(self):
        import warnings

        class Peft:
            @staticmethod
            def from_pretrained(base, adapter):
                warnings.warn("Found missing adapter keys while loading")
                return "plausible but inert"

        with self.assertRaisesRegex(RuntimeError, "did not activate"):
            load_peft_adapter_or_raise(Peft, object(), "/adapter")

    def test_clean_adapter_load_is_returned(self):
        class Peft:
            @staticmethod
            def from_pretrained(base, adapter):
                return (base, adapter)

        base = object()
        self.assertEqual(
            (base, "/adapter"),
            load_peft_adapter_or_raise(Peft, base, "/adapter"),
        )


if __name__ == "__main__":
    unittest.main()
