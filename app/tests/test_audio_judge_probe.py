"""Gate A probe logic for the #1547 listening critic.

Fixtures are synthetic and public-domain only. The gate is tested on cases
whose answer is known: a perfect transcript, a real miss, and a silent
control that the model must not turn into speech.
"""
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from experiments import audio_judge_probe as probe


def write_wav(path, frames=1600, sample=0):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", sample) * frames)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.audio = []
        for index in range(3):
            path = Path(self.root.name) / f"{index}.wav"
            write_wav(path, sample=index)
            self.audio.append(str(path))

    def write_manifest(self, **overrides):
        manifest = {"license": "public-domain",
                    "items": [{"audio": path, "text": f"line {index}"}
                              for index, path in enumerate(self.audio)]}
        manifest.update(overrides)
        path = Path(self.root.name) / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_public_domain_manifest_loads(self):
        loaded = probe.load_manifest(self.write_manifest())
        self.assertEqual(3, len(loaded["items"]))

    def test_non_public_domain_manifest_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Refusing non-public-domain"):
            probe.load_manifest(self.write_manifest(license="copyrighted"))

    def test_manifest_needs_three_items(self):
        path = self.write_manifest(items=[{"audio": self.audio[0], "text": "one"}])
        with self.assertRaisesRegex(ValueError, "at least three"):
            probe.load_manifest(path)

    def test_missing_audio_is_refused(self):
        path = self.write_manifest(items=[{"audio": "/missing.wav", "text": "one"}] * 3)
        with self.assertRaisesRegex(ValueError, "audio not found"):
            probe.load_manifest(path)


class RecallTests(unittest.TestCase):
    def test_normalize_words_drops_punctuation(self):
        self.assertEqual(["alice", "waited", "patiently"],
                         probe.normalize_words("Alice waited, patiently!"))

    def test_word_recall_matches_known_transcript(self):
        self.assertEqual(1.0, probe.word_recall("Alice waited patiently", "alice waited patiently"))
        self.assertEqual(0.0, probe.word_recall("Alice waited patiently", "the rabbit ran"))

    def test_word_recall_counts_misses_not_just_any_overlap(self):
        # The failure the old kana scorer missed: words present but the
        # intended phrase was mispronounced.
        self.assertAlmostEqual(1 / 3, probe.word_recall("Alice waited patiently", "alice ran away"))

    def test_reordered_words_do_not_pass_as_a_transcript(self):
        self.assertAlmostEqual(1 / 3, probe.word_recall("Alice waited patiently", "patiently waited alice"))

    def test_extra_words_around_the_line_still_pass(self):
        self.assertEqual(1.0, probe.word_recall("Alice waited patiently", "and then alice waited patiently again"))

    def test_empty_expected_text_is_rejected(self):
        with self.assertRaises(ValueError):
            probe.word_recall("", "anything")


class GateTests(unittest.TestCase):
    def result(self, recall, control="", error=None):
        return {"recall": recall, "control_transcript": control, "error": error}

    def test_perfect_items_pass_with_silent_control(self):
        passed, reasons = probe.evaluate_gate([self.result(1.0), self.result(0.9), self.result(0.8)])
        self.assertTrue(passed, reasons)

    def test_majority_pass_is_enough(self):
        passed, reasons = probe.evaluate_gate([self.result(1.0), self.result(0.8), self.result(0.2)])
        self.assertTrue(passed, reasons)

    def test_most_items_missing_audio_content_fails(self):
        passed, reasons = probe.evaluate_gate([self.result(0.2), self.result(0.1), self.result(0.0)])
        self.assertFalse(passed)
        self.assertTrue(any("recall passed" in reason for reason in reasons))

    def test_control_that_invents_speech_fails(self):
        passed, reasons = probe.evaluate_gate(
            [self.result(1.0, control="hello there friend")] * 3)
        self.assertFalse(passed)
        self.assertTrue(any("silent control" in reason for reason in reasons))

    def test_request_error_fails_loudly(self):
        passed, reasons = probe.evaluate_gate(
            [self.result(1.0), self.result(1.0), self.result(1.0, error="timeout")])
        self.assertFalse(passed)
        self.assertTrue(any("timeout" in reason for reason in reasons))


class ConfigTests(unittest.TestCase):
    def test_remote_profile_is_the_judge(self):
        config = {"llm_remote": {"base_url": "https://openrouter.ai/api/v1",
                                 "api_key": "env:OPENROUTER_API_KEY",
                                 "model_name": "google/gemini-3.7-flash"}}
        self.assertEqual("google/gemini-3.7-flash",
                         probe.get_judge_config(config)["model_name"])

    def test_missing_remote_profile_is_refused(self):
        with self.assertRaisesRegex(ValueError, "remote LLM profile"):
            probe.get_judge_config({"llm_remote": None})

    def test_manual_transport_is_refused(self):
        config = {"llm_remote": {"base_url": "https://openrouter.ai/api/v1",
                                 "api_key": "x", "model_name": "m",
                                 "transport": "manual"}}
        with self.assertRaisesRegex(ValueError, "manual transport"):
            probe.get_judge_config(config)

    def test_untrusted_endpoint_is_refused(self):
        config = {"llm_remote": {"base_url": "https://example.test/v1",
                                 "api_key": "x", "model_name": "m"}}
        with self.assertRaisesRegex(ValueError, "not local"):
            probe.get_judge_config(config)



class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def response(transcript):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"transcript": transcript})))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
    )


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.audio = Path(self.root.name) / "clip.wav"
        write_wav(self.audio)

    def test_request_sends_only_instruction_and_audio(self):
        client = FakeClient([response("hello")])
        transcript, _, style = probe.request_transcript(client, "m", self.audio, "input_audio")
        self.assertEqual("hello", transcript)
        self.assertEqual("input_audio", style)
        content = client.chat.completions.calls[0]["messages"][0]["content"]
        self.assertEqual(["text", "input_audio"], [part["type"] for part in content])
        self.assertEqual(probe.INSTRUCTION, content[0]["text"])

    def test_both_style_falls_back_once_and_records_the_working_shape(self):
        client = FakeClient([RuntimeError("unsupported content type"), response("hello")])
        transcript, _, style = probe.request_transcript(client, "m", self.audio, "both")
        self.assertEqual("hello", transcript)
        self.assertEqual("audio_url", style)
        self.assertEqual(2, len(client.chat.completions.calls))

    def test_both_style_raises_when_both_shapes_fail(self):
        client = FakeClient([RuntimeError("first"), RuntimeError("second")])
        with self.assertRaisesRegex(RuntimeError, "second"):
            probe.request_transcript(client, "m", self.audio, "both")


class ContentTests(unittest.TestCase):
    def test_both_supported_audio_shapes(self):
        self.assertEqual({"type": "input_audio",
                          "input_audio": {"data": "AAA=", "format": "wav"}},
                         probe.build_audio_content("input_audio", "AAA=", "wav"))
        self.assertEqual({"type": "audio_url",
                          "audio_url": {"url": "data:audio/wav;base64,AAA="}},
                         probe.build_audio_content("audio_url", "AAA=", "wav"))

    def test_encode_audio_round_trips_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "clip.wav"
            write_wav(path, sample=7)
            data, fmt = probe.encode_audio(path)
            self.assertEqual("wav", fmt)
            import base64
            self.assertEqual(path.read_bytes(), base64.b64decode(data))

    def test_empty_audio_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "empty.wav"
            path.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "Empty audio"):
                probe.encode_audio(path)


class CostTests(unittest.TestCase):
    def test_reported_cost_wins(self):
        self.assertEqual(0.25, probe.get_response_cost({"cost": 0.25}, (1.0, 2.0)))
        self.assertEqual(0.5, probe.get_response_cost(SimpleNamespace(model_extra={"cost": 0.5}), None))

    def test_estimated_cost_uses_prompt_and_completion_tokens(self):
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=100)
        self.assertAlmostEqual(0.001 + 0.0002, probe.get_response_cost(usage, (0.000001, 0.000002)))

    def test_unknown_cost_is_none(self):
        self.assertIsNone(probe.get_response_cost(SimpleNamespace(prompt_tokens=10, completion_tokens=1), None))

    def test_spend_cap_refuses_the_next_call(self):
        probe.ensure_spend_capacity(4.9, 5.0, 0.05)
        with self.assertRaisesRegex(RuntimeError, "Spend cap"):
            probe.ensure_spend_capacity(4.9, 5.0, 0.2)

    def test_unknown_cost_does_not_block(self):
        probe.ensure_spend_capacity(4.99, 5.0, None)


class TranscriptTests(unittest.TestCase):
    def test_json_transcript_and_raw_text(self):
        self.assertEqual("hello", probe.parse_transcript('{"transcript": "hello"}'))
        self.assertEqual("hello", probe.parse_transcript("hello"))


if __name__ == "__main__":
    unittest.main()
