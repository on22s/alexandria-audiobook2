import math
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alexandria_preparer_rocm_compatible as preparer


class WordSegmentValidationTests(unittest.TestCase):
    def test_clean_segments_are_copied_without_mutating_input(self):
        source = [
            {"word": " hello ", "start": 0.0, "end": 0.4,
             "confidence": 0.9},
            {"word": "world", "start": 0.35, "end": 0.8},
        ]
        validated = preparer.get_validated_word_segments(source, 1.0)

        self.assertEqual("hello", validated[0]["word"])
        self.assertEqual(0.9, validated[0]["confidence"])
        self.assertEqual(" hello ", source[0]["word"])
        self.assertIsNot(source[0], validated[0])

    def test_empty_words_are_safely_discarded(self):
        validated = preparer.get_validated_word_segments([
            {"word": " ", "start": 0.0, "end": 0.1},
            {"word": "kept", "start": 0.1, "end": 0.4},
        ], 1.0)
        self.assertEqual(["kept"], [word["word"] for word in validated])

    def test_tiny_audio_boundary_excursions_are_clamped(self):
        source = [
            {"word": "first", "start": -0.01, "end": 0.2},
            {"word": "last", "start": 0.8, "end": 1.01},
        ]
        validated = preparer.get_validated_word_segments(source, 1.0)
        self.assertEqual(0.0, validated[0]["start"])
        self.assertEqual(1.0, validated[1]["end"])

    def test_zero_duration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-positive duration"):
            preparer.get_validated_word_segments(
                [{"word": "bad", "start": 0.5, "end": 0.5}], 1.0)

    def test_non_finite_timestamp_is_rejected(self):
        for bad in (math.nan, math.inf):
            with self.subTest(timestamp=bad), \
                    self.assertRaisesRegex(ValueError, "non-finite"):
                preparer.get_validated_word_segments(
                    [{"word": "bad", "start": 0.0, "end": bad}], 1.0)

    def test_materially_out_of_range_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the audio bounds"):
            preparer.get_validated_word_segments(
                [{"word": "bad", "start": 0.8, "end": 1.2}], 1.0)

    def test_backward_timeline_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "starts before"):
            preparer.get_validated_word_segments([
                {"word": "first", "start": 0.4, "end": 0.8},
                {"word": "second", "start": 0.3, "end": 0.9},
            ], 1.0)

    def test_invalid_primary_result_uses_existing_fallback(self):
        audio = np.zeros(16000, dtype=np.float32)
        invalid = [{"word": "bad", "start": 0.5, "end": 0.5}]
        valid = [{"word": "good", "start": 0.1, "end": 0.4}]

        with patch.object(preparer, "TRANSFORMERS_WHISPER_AVAILABLE", True), \
                patch.object(preparer, "WHISPER_CPP_AVAILABLE", True), \
                patch.object(preparer, "WHISPERX_AVAILABLE", True), \
                patch.object(preparer, "transcribe_with_wav2vec2",
                             return_value=(invalid, "en")) as primary, \
                patch.object(preparer, "transcribe_with_whisper_cpp",
                             return_value=(valid, "en")) as fallback, \
                patch.object(preparer, "transcribe_with_whisperx_cpu") as final:
            words, language = preparer.choose_and_transcribe(
                audio, "cuda", "en")

        self.assertEqual(valid, words)
        self.assertEqual("en", language)
        primary.assert_called_once()
        fallback.assert_called_once()
        final.assert_not_called()


class WhisperCppTests(unittest.TestCase):
    def test_zero_duration_words_are_coalesced_without_losing_text(self):
        converted = preparer.get_coalesced_whisper_cpp_segments([
            {"text": "Bring", "offsets": {"from": 100, "to": 100},
             "tokens": [{"p": 0.7}]},
            {"text": "it", "offsets": {"from": 100, "to": 400},
             "tokens": [{"p": 0.9}]},
            {"text": "home", "offsets": {"from": 400, "to": 700},
             "tokens": [{"p": 0.8}]},
            {"text": "now", "offsets": {"from": 700, "to": 700},
             "tokens": [{"p": 0.6}]},
        ])

        self.assertEqual(["Bring it", "home now"],
                         [segment["word"] for segment in converted])
        self.assertEqual((0.1, 0.4),
                         (converted[0]["start"], converted[0]["end"]))

    def test_adapter_parses_json_written_by_cli(self):
        payload = {
            "result": {"language": "en"},
            "transcription": [{
                "text": "hello",
                "offsets": {"from": 0, "to": 500},
                "tokens": [{"p": 0.75}],
            }],
        }

        def fake_run(command, **_kwargs):
            output_prefix = command[command.index("--output-file") + 1]
            Path(output_prefix + ".json").write_text(json.dumps(payload))
            return type("Result", (), {
                "returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(preparer, "WHISPER_CPP_AVAILABLE", True), \
                patch.object(preparer, "WHISPER_CPP_BIN", "/bin/whisper"), \
                patch.object(preparer, "WHISPER_CPP_MODEL", "/models/small"), \
                patch.object(preparer.subprocess, "run",
                             side_effect=fake_run) as run:
            words, language = preparer.transcribe_with_whisper_cpp(
                np.zeros(16000, dtype=np.float32))

        self.assertEqual("en", language)
        self.assertEqual(
            [{"word": "hello", "start": 0.0, "end": 0.5}],
            words)
        command = run.call_args.args[0]
        self.assertIn("--output-json-full", command)
        self.assertIn("--split-on-word", command)

    def test_adapter_reports_cli_failure(self):
        failed = type("Result", (), {
            "returncode": 9, "stdout": "", "stderr": "GPU failed"})()
        with patch.object(preparer, "WHISPER_CPP_AVAILABLE", True), \
                patch.object(preparer, "WHISPER_CPP_BIN", "/bin/whisper"), \
                patch.object(preparer, "WHISPER_CPP_MODEL", "/models/small"), \
                patch.object(preparer.subprocess, "run",
                             return_value=failed), \
                self.assertRaisesRegex(RuntimeError, "GPU failed"):
            preparer.transcribe_with_whisper_cpp(
                np.zeros(16000, dtype=np.float32))

    def test_english_only_model_rejects_other_languages(self):
        with patch.object(preparer, "WHISPER_CPP_AVAILABLE", True), \
                self.assertRaisesRegex(ValueError, "only supports English"):
            preparer.transcribe_with_whisper_cpp(
                np.zeros(16000, dtype=np.float32), "fr")


if __name__ == "__main__":
    unittest.main()
