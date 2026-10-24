"""Binary voice/pause detection page and scoring for #1547."""
import json
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from experiments import listening_critic_detect as detect

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "alice_pd_listening.json"
LABELS = [
    ("clean_01", "clean", None, "scene_01"), ("clean_02", "clean", None, "scene_02"),
    ("clean_03", "clean", None, "scene_04"),
    ("voice_01", "defect", "voice", "scene_03"), ("voice_02", "defect", "voice", "scene_05"),
    ("voice_03", "defect", "voice", "scene_02"),
    ("pause_long_01", "defect", "pause_long", "scene_01"),
    ("pause_long_02", "defect", "pause_long", "scene_05"),
    ("pause_short_01", "defect", "pause_short", "scene_02"),
    ("pause_short_02", "defect", "pause_short", "scene_04"),
]


def write_wav(path, frames=160):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * frames)


class PageTests(unittest.TestCase):
    def test_page_has_twelve_trials_two_repeats_and_no_labels(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            labels = {}
            for item_id, expect, fault, scene in LABELS:
                write_wav(root / f"{item_id}.wav")
                labels[item_id] = {"expect": expect, "fault": fault, "scene": scene, "title": item_id}
            labels_path = root / "labels.json"
            labels_path.write_text(json.dumps(labels), encoding="utf-8")
            result = detect.build_detect_page(root, labels_path, FIXTURE,
                                              root / "page.html", root / "key.json", seed=1555)
            html = (root / "page.html").read_text(encoding="utf-8")
            key = json.loads((root / "key.json").read_text(encoding="utf-8"))
            self.assertEqual(12, result["trials"])
            self.assertEqual(12, len(key["trials"]))
            self.assertEqual(2, len(key["repeats"]["clean_01"]))
            self.assertEqual(2, len(key["repeats"]["voice_02"]))
            self.assertIn("listening-detect-", html)
            self.assertNotIn("pause_long", html)
            self.assertNotIn("voice_01", html)
            script = re.search(r"<script>(.*)</script>", html, re.DOTALL).group(1)
            if shutil.which("node"):
                path = root / "page.js"
                path.write_text(script, encoding="utf-8")
                parsed = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(0, parsed.returncode, parsed.stderr)


class AnalyzeTests(unittest.TestCase):
    def make(self, root):
        labels = {}
        for item_id, expect, fault, scene in LABELS:
            write_wav(root / f"{item_id}.wav")
            labels[item_id] = {"expect": expect, "fault": fault, "scene": scene, "title": item_id}
        labels_path = root / "labels.json"
        labels_path.write_text(json.dumps(labels), encoding="utf-8")
        detect.build_detect_page(root, labels_path, FIXTURE, root / "page.html", root / "key.json", seed=1555)
        key = json.loads((root / "key.json").read_text(encoding="utf-8"))
        labels_by_item = labels
        return key, labels_by_item, labels_path

    def perfect(self, key, labels):
        answers = {}
        for entry in key["trials"]:
            fault = labels[entry["item_id"]]["fault"]
            answers[entry["trial_id"]] = {
                "voice_changed": fault == "voice",
                "pause_issue": "long" if fault == "pause_long" else ("short" if fault == "pause_short" else "none"),
                "voice_line": "", "pause_line": "", "notes": "",
            }
        return {"version": 1, "seed": key["seed"], "responses": answers}

    def test_perfect_detection_and_repeat_consistency(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            key, labels, labels_path = self.make(root)
            payload = self.perfect(key, labels)
            path = root / "responses.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            summary = detect.analyze_detection(path, root / "key.json", labels_path)
            for kind in ("voice", "pause"):
                self.assertEqual(1.0, summary["counts"][kind]["precision"])
                self.assertEqual(1.0, summary["counts"][kind]["recall"])
            self.assertEqual(4, summary["pause_types"]["correct"])
            self.assertEqual(0, summary["pause_types"]["missed"])
            self.assertEqual({"voice": [False, False], "pause": ["none", "none"]}, summary["repeats"]["clean_01"])

    def test_miss_and_false_alarm_are_counted(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            key, labels, labels_path = self.make(root)
            payload = self.perfect(key, labels)
            voice_trial = next(entry["trial_id"] for entry in key["trials"]
                               if entry["item_id"] == "voice_01" and not entry["repeat"])
            clean_trial = next(entry["trial_id"] for entry in key["trials"]
                               if entry["item_id"] == "clean_01" and not entry["repeat"])
            payload["responses"][voice_trial]["voice_changed"] = False
            payload["responses"][clean_trial]["voice_changed"] = True
            path = root / "responses.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            summary = detect.analyze_detection(path, root / "key.json", labels_path)
            self.assertEqual(2, summary["counts"]["voice"]["tp"])
            self.assertEqual(1, summary["counts"]["voice"]["fn"])
            self.assertEqual(1, summary["counts"]["voice"]["fp"])

    def test_invalid_or_missing_answers_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            key, labels, labels_path = self.make(root)
            payload = self.perfect(key, labels)
            payload["responses"][key["trials"][0]["trial_id"]]["pause_issue"] = "maybe"
            path = root / "responses.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid pause_issue"):
                detect.analyze_detection(path, root / "key.json", labels_path)
            payload = self.perfect(key, labels)
            del payload["responses"][key["trials"][0]["trial_id"]]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                detect.analyze_detection(path, root / "key.json", labels_path)


if __name__ == "__main__":
    unittest.main()
