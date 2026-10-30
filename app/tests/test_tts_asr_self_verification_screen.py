import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import tts_asr_self_verification_screen as screen
from tests.test_tts_endpoint_content_screen import manifest_case, write_wav


class TTSASRSelfVerificationScreenTests(unittest.TestCase):
    def make_manifest(self, root, first=None, second=None):
        write_wav(root / "one.wav", [0] * 120)
        write_wav(root / "two.wav", [0] * 119 + [1])
        case = manifest_case()
        case["language"] = "en"
        if first is not None:
            case["attempts"][0]["primary_transcript"] = first
        if second is not None:
            case["attempts"][1]["primary_transcript"] = second
        path = root / "manifest.json"
        path.write_text(json.dumps({"version": 1, "cases": [case]}),
                        encoding="utf-8")
        return path

    def test_selects_lowest_wer_survivor_and_records_missing_codec_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.make_manifest(
                root,
                "It isn't step back with many unrelated extra words before nothing.",
                "It isn't step back before the cables snap.")
            report = screen.build_self_verification_report(manifest)
            case = report["cases"][0]
            self.assertEqual("observed_asr_policy_not_production_qualified",
                             report["status"])
            self.assertEqual(2, case["selected_attempt"])
            self.assertTrue(case["first_attempt_catastrophic"])
            self.assertTrue(case["recovered_by_second_attempt"])
            self.assertFalse(case["all_attempts_catastrophic"])
            self.assertIsNone(case["candidates"][0]["codec_token_count"])
            self.assertIn("token-count dropout rule", report["limitations"][0])
            self.assertEqual(1, report["counts"]["recovered_by_second_attempt"])
            self.assertEqual(1, report["counts"]["catastrophic_attempts"])
            self.assertEqual(0, report["counts"]["selected_attempt_1"])
            self.assertEqual(1, report["counts"]["selected_attempt_2"])

    def test_tie_is_deterministically_first_and_threshold_is_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exact = "It isn't step back before the cables snap."
            report = screen.build_self_verification_report(
                self.make_manifest(root, exact, exact))
            self.assertEqual(1, report["cases"][0]["selected_attempt"])
            candidates = report["cases"][0]["candidates"]
            self.assertEqual([0.0, 0.0], [row["word_error_rate"] for row in candidates])
            self.assertFalse(any(row["catastrophic_observed"] for row in candidates))

            # Four substitutions over eight reference words is exactly 0.5,
            # which the published rule does not classify as a failure.
            at_boundary = "one two three four before the cables snap"
            report = screen.build_self_verification_report(
                self.make_manifest(root, at_boundary, exact))
            first = report["cases"][0]["candidates"][0]
            self.assertEqual(0.5, first["word_error_rate"])
            self.assertFalse(first["content_failure_observed"])

    def test_dropout_rule_excludes_one_word_even_at_wer_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.make_manifest(root, "hello world", "hello")
            document = json.loads(manifest.read_text(encoding="utf-8"))
            case = document["cases"][0]
            case["input"]["text"] = "hello world"
            from tests.test_tts_endpoint_content_screen import digest
            case["input_sha256"] = digest(case["input"])
            manifest.write_text(json.dumps(document), encoding="utf-8")
            report = screen.build_self_verification_report(manifest)
            candidate = report["cases"][0]["candidates"][1]
            self.assertEqual(0.5, candidate["word_error_rate"])
            self.assertTrue(candidate["asr_dropout_observed"])
            self.assertFalse(candidate["content_failure_observed"])
            self.assertTrue(candidate["catastrophic_observed"])

    def test_eligible_first_candidate_survives_catastrophic_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exact = "It isn't step back before the cables snap."
            report = screen.build_self_verification_report(
                self.make_manifest(root, exact, "noise"))
            case = report["cases"][0]
            self.assertEqual(1, case["selected_attempt"])
            self.assertFalse(case["first_attempt_catastrophic"])
            self.assertFalse(case["all_attempts_catastrophic"])
            self.assertFalse(case["recovered_by_second_attempt"])

    def test_all_catastrophic_candidates_yield_no_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = screen.build_self_verification_report(
                self.make_manifest(root, "noise", "other noise"))
            case = report["cases"][0]
            self.assertIsNone(case["selected_attempt"])
            self.assertTrue(case["all_attempts_catastrophic"])
            self.assertEqual(1, report["counts"]["all_attempts_catastrophic"])

    def test_unsupported_references_are_refused_instead_of_misscored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.make_manifest(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            case = document["cases"][0]
            del case["language"]
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(screen.EndpointScreenError,
                                        "explicit English language assertion"):
                screen.build_self_verification_report(manifest)
            case["language"] = "es"
            case["input"]["text"] = "hola mundo"
            from tests.test_tts_endpoint_content_screen import digest
            case["input_sha256"] = digest(case["input"])
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(screen.EndpointScreenError,
                                        "explicit English language assertion"):
                screen.build_self_verification_report(manifest)
            case["language"] = "en"
            case["input"]["text"] = "彼は静かに答えた。"
            case["input_sha256"] = digest(case["input"])
            manifest.write_text(json.dumps(document, ensure_ascii=False),
                                encoding="utf-8")
            with self.assertRaisesRegex(screen.EndpointScreenError,
                                        "outside this English WER screen"):
                screen.build_self_verification_report(manifest)
            case["input"]["text"] = "Hello."
            case["input_sha256"] = digest(case["input"])
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(screen.EndpointScreenError,
                                        "too short for the dropout rule"):
                screen.build_self_verification_report(manifest)

    def test_mismatched_content_candidate_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.make_manifest(root)
            content = screen.build_content_report(manifest)
            content["cases"][0]["attempts"][1]["take_id"] = "replacement"
            with patch.object(screen, "build_content_report", return_value=content), \
                    self.assertRaisesRegex(screen.EndpointScreenError,
                                           "candidate identity changed"):
                screen.build_self_verification_report(manifest)

    def test_manifest_replacement_between_passes_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.make_manifest(root)
            original = screen._read_manifest

            def replace_before_second_read(path):
                document = json.loads(manifest.read_text(encoding="utf-8"))
                document["cases"][0]["attempts"][0]["primary_transcript"] = "changed"
                manifest.write_text(json.dumps(document), encoding="utf-8")
                return original(path)

            with patch.object(screen, "_read_manifest",
                              side_effect=replace_before_second_read), \
                    self.assertRaisesRegex(screen.EndpointScreenError,
                                           "manifest changed"):
                screen.build_self_verification_report(manifest)

    def test_cli_writes_once_without_raw_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.make_manifest(root)
            output = root / "report.json"
            command = [sys.executable, "-m",
                       "experiments.tts_asr_self_verification_screen",
                       "--manifest", str(manifest), "--out", str(output)]
            first = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(0, first.returncode, first.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("tts_asr_self_verification_screen.py",
                             report["provenance"]["script"])
            serialized = json.dumps(report)
            self.assertNotIn("long strange inserted passage", serialized)
            self.assertNotIn("It isn't step back before the cables snap.", serialized)
            def keys(value):
                if isinstance(value, dict):
                    return set(value).union(*(keys(item) for item in value.values()))
                if isinstance(value, list):
                    return set().union(*(keys(item) for item in value))
                return set()
            self.assertNotIn("primary_transcript", keys(report))
            before = output.read_bytes()
            second = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(3, second.returncode)
            self.assertEqual(before, output.read_bytes())

    def test_cli_refuses_unasserted_language_without_output_or_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.make_manifest(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            del document["cases"][0]["language"]
            manifest.write_text(json.dumps(document), encoding="utf-8")
            output = root / "report.json"
            result = subprocess.run(
                [sys.executable, "-m",
                 "experiments.tts_asr_self_verification_screen",
                 "--manifest", str(manifest), "--out", str(output)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                text=True, timeout=15)
            self.assertEqual(2, result.returncode)
            self.assertIn("refused input", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
