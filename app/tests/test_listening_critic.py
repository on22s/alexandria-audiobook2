"""Known-answer scoring and parsing for the #1547 listening critic."""
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from experiments import listening_critic as critic
from experiments import listening_critic_known_answers as known


def write_wav(path, frames=160):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * frames)


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    def __init__(self, replies):
        self.chat = SimpleNamespace(completions=FakeCompletions(replies))


def reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


class ParseTests(unittest.TestCase):
    def test_empty_findings_and_bare_list(self):
        self.assertEqual([], critic.parse_findings('{"findings": []}'))
        self.assertEqual([{"type": "content", "severity": "high", "evidence": "x"}],
                         critic.parse_findings('[{"type": "content", "severity": "high", "evidence": "x"}]'))

    def test_fenced_reply_is_accepted(self):
        text = '```json\n{"findings": [{"type": "content", "severity": "low", "evidence": "cut"}]}\n```'
        self.assertEqual("cut", critic.parse_findings(text)[0]["evidence"])

    def test_malformed_and_unknown_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "not JSON"):
            critic.parse_findings("no json here")
        with self.assertRaisesRegex(ValueError, "Unknown finding type"):
            critic.parse_findings('{"findings": [{"type": "pacing", "severity": "low", "evidence": "x"}]}')
        with self.assertRaisesRegex(ValueError, "Unknown finding severity"):
            critic.parse_findings('{"findings": [{"type": "content", "severity": "huge", "evidence": "x"}]}')
        with self.assertRaisesRegex(ValueError, "no content"):
            critic.parse_findings("")

    def test_voice_finding_may_carry_its_own_aspect(self):
        findings = critic.parse_review_findings(
            '{"findings": [{"type": "voice", "aspect": "consistency", "severity": "medium", "evidence": "x"}]}')
        self.assertTrue(critic.has_review_finding(findings, "voice"))

    def test_style_finding_still_requires_a_known_aspect(self):
        with self.assertRaisesRegex(ValueError, "needs aspect"):
            critic.parse_review_findings('{"findings": [{"type": "style", "severity": "low", "evidence": "x"}]}')

    def test_content_prediction_ignores_other_types(self):
        self.assertTrue(critic.has_content_finding([{"type": "content", "severity": "low", "evidence": "x"}]))
        self.assertFalse(critic.has_content_finding([{"type": "style", "severity": "low", "evidence": "x"}]))
        self.assertFalse(critic.has_content_finding([]))


class PromptTests(unittest.TestCase):
    def test_prompt_carries_expected_text_and_no_finding_path(self):
        prompt = critic.build_content_prompt('Alice waited patiently')
        self.assertIn("Alice waited patiently", prompt)
        self.assertIn('"findings": []', prompt)
        self.assertIn("Never invent a finding", prompt)

    def test_critique_line_sends_audio_once_and_parses(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "clip.wav"
            write_wav(path)
            client = FakeClient([reply('{"findings": [{"type": "content", "severity": "high", "evidence": "cut"}]}')])
            findings, usage, raw = critic.critique_line(client, "m", path, "Alice waited patiently")
            self.assertEqual("content", findings[0]["type"])
            self.assertEqual(10, usage.prompt_tokens)
            content = client.chat.completions.calls[0]["messages"][0]["content"]
            self.assertEqual(["text", "input_audio"], [part["type"] for part in content])
            self.assertIn("Alice waited patiently", content[0]["text"])

    def test_malformed_critic_reply_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "clip.wav"
            write_wav(path)
            client = FakeClient([reply("I cannot do that")])
            with self.assertRaisesRegex(ValueError, "not JSON"):
                critic.critique_line(client, "m", path, "Alice waited patiently")


class ResolveTests(unittest.TestCase):
    def test_group_mapping_and_defect_directory(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for directory in ("alice/wav", "numbers/wav", "w1435_U/wav", "verify/wav", "v1434/derived"):
                (root / directory).mkdir(parents=True)
                write_wav(root / directory / "clip.wav")
            cases = [("alice", "control", "alice/wav"), ("numbers", "control", "numbers/wav"),
                     ("w1435_U", "control", "w1435_U/wav"), ("w1435_W", "control", "w1435_U/wav"),
                     ("v", "control", "verify/wav"), ("r", "control", "verify/wav"),
                     ("alice", "mid_repeat", "v1434/derived")]
            for group, fault, expected in cases:
                row = {"group": group, "fault": fault, "wav": "clip.wav"}
                self.assertEqual(root / expected / "clip.wav", known.resolve_audio(row, root))

    def test_unknown_group_and_missing_audio_fail(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "Unknown #1434 group"):
                known.resolve_audio({"group": "nope", "fault": "control", "wav": "x.wav"}, root)
            with self.assertRaises(FileNotFoundError):
                known.resolve_audio({"group": "alice", "fault": "control", "wav": "missing.wav"}, root)


class LoadTests(unittest.TestCase):
    def test_each_artifact_row_keeps_its_own_asr_flag(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "alice/wav").mkdir(parents=True)
            (root / "v1434/derived").mkdir(parents=True)
            write_wav(root / "alice/wav" / "a.wav")
            write_wav(root / "v1434/derived" / "a.wav")
            rows = [
                {"case_id": "a__control", "group": "alice", "fault": "control", "expect": "clean",
                 "text": "Line.", "wav": "a.wav", "status": "no_content_candidate"},
                {"case_id": "a__control", "group": "alice", "fault": "control", "expect": "clean",
                 "text": "Line.", "wav": "a.wav", "status": "review"},
                {"case_id": "a__cut", "group": "alice", "fault": "head_cut", "expect": "defect",
                 "text": "Line.", "wav": "a.wav", "status": "review"},
            ]
            path = root / "results.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            cases = known.load_cases(path, root)
            self.assertEqual(3, len(cases))
            self.assertFalse(cases[0]["asr_flagged"])
            self.assertTrue(cases[1]["asr_flagged"])
            self.assertEqual("head_cut", cases[2]["fault"])

    def test_license_must_be_declared(self):
        known.ensure_license(known.LICENSE)
        with self.assertRaisesRegex(ValueError, "Refusing unspecified material"):
            known.ensure_license("public-domain")


class SummaryTests(unittest.TestCase):
    def case(self, expect, predicted, group="alice", fault="control", error=None):
        return {"expect": expect, "predicted": predicted, "group": group,
                "fault": fault, "error": error}

    def test_precision_recall_and_errors_are_separate(self):
        cases = [self.case("defect", True, fault="head_cut"),
                 self.case("defect", False, fault="mid_repeat"),
                 self.case("clean", True),
                 self.case("clean", False),
                 self.case("defect", True, error="timeout")]
        summary = known.summarize(cases)
        self.assertEqual((1, 1, 1, 1), (summary["tp"], summary["fn"], summary["fp"], summary["tn"]))
        self.assertEqual(1, summary["errors"])
        self.assertEqual(0.5, summary["precision"])
        self.assertEqual(0.5, summary["recall"])
        self.assertEqual(1.0, summary["per_fault"]["head_cut"]["recall"])
        self.assertEqual(0.0, summary["per_fault"]["mid_repeat"]["recall"])
        self.assertEqual(4, summary["per_group"]["alice"]["n"])

    def test_asr_baseline_uses_the_artifact_verdict(self):
        cases = [{"expect": "defect", "asr_flagged": True},
                 {"expect": "defect", "asr_flagged": False},
                 {"expect": "clean", "asr_flagged": False},
                 {"expect": "clean", "asr_flagged": True}]
        baseline = known.asr_baseline(cases)
        self.assertEqual((1, 1, 1, 1), (baseline["tp"], baseline["fn"], baseline["fp"], baseline["tn"]))


if __name__ == "__main__":
    unittest.main()
