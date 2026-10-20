"""Native CLI reports must survive valid one-item and empty-stratum inputs."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import pitch_separation, roster_order_bias

APP = Path(__file__).resolve().parent.parent


class DiagnosticBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def cli(self, script, *args):
        return subprocess.run([sys.executable, str(APP / "experiments" / script),
                               *map(str, args)], cwd=self.root,
                              capture_output=True, text=True, timeout=30)

    def test_single_adapter_selection_preserves_even_range_controls(self):
        manifest = self.save("manifest.json", [
            {"id": name, "voice_features": {"mean_f0": value}}
            for name, value in (("high", 300), ("low", 100), ("mid", 200))])
        picked, pool = pitch_separation.spanning_adapters(manifest, 1)
        self.assertEqual([("low", 100)], picked)
        self.assertEqual([("low", 100), ("mid", 200), ("high", 300)], pool)
        self.assertEqual([pool[0], pool[-1]],
                         pitch_separation.spanning_adapters(manifest, 2)[0])
        self.assertEqual(pool, pitch_separation.spanning_adapters(manifest, 9)[0])

    def test_invalid_counts_and_empty_pitch_pools_refuse_before_inference(self):
        manifest = self.save("manifest.json", [
            {"id": "low", "voice_features": {"mean_f0": 100}}])
        for count in (0, -1):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "positive"):
                    pitch_separation.spanning_adapters(manifest, count)
                result = self.cli("pitch_separation.py", "--manifest", manifest,
                                  "--adapters", count)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("positive", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        empty = self.save("empty.json", [])
        with self.assertRaisesRegex(ValueError, "no adapters"):
            pitch_separation.spanning_adapters(empty, 1)
        result = self.cli("pitch_separation.py", "--manifest", empty)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("no adapters", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_single_adapter_cli_publishes_native_pcm_provider_results(self):
        import numpy as np
        import soundfile as sf
        manifest = self.save("manifest.json", [
            {"id": "low", "voice_features": {"mean_f0": 100}},
            {"id": "high", "voice_features": {"mean_f0": 300}}])
        lines = [{"speaker": "NARRATOR", "text": ("An unchanged synthetic line. " * 4) + str(i)}
                 for i in range(3)]
        script = self.save("chunks.json", lines)
        voices = self.save("voices.json", {"NARRATOR": {"type": "clone", "ref_text": "keep"}})
        config = self.save("config.json", {})
        out = self.root / "pitch.json"
        audio = self.root / "audio"
        calls = []

        def render(engine, text, instruct, speaker, vc, entry, wav):
            calls.append((text, dict(entry)))
            sf.write(wav, np.zeros(1600), 16000, subtype="PCM_16")

        def pitch(wav):
            samples, rate = sf.read(wav)
            self.assertEqual(16000, rate)
            self.assertEqual(1600, len(samples))
            return 100 + 5 * (len(calls) - 1)

        with patch("tts.TTSEngine"), patch("experiments.generation.render", side_effect=render), \
             patch.object(pitch_separation, "median_pitch", side_effect=pitch), \
             patch("experiments.provenance.provenance", return_value={"fixture": "CPU provider"}), \
             patch("sys.argv", ["pitch", "--manifest", str(manifest), "--adapters", "1",
                                "--lines", "3", "--script", str(script), "--voice-config", str(voices),
                                "--config", str(config), "--out-dir", str(audio), "--out", str(out)]), \
             contextlib.redirect_stdout(io.StringIO()):
            pitch_separation.main()
        self.assertEqual([line["text"] for line in lines], [text for text, entry in calls])
        self.assertTrue(all(entry["adapter_id"] == "low" and entry["seed"] == "1234"
                            and entry["ref_text"] == "keep" for text, entry in calls))
        self.assertEqual({"NARRATOR": {"type": "clone", "ref_text": "keep"}}, json.loads(voices.read_text()))
        result = json.loads(out.read_text())
        self.assertEqual(2, result["pool_size"])
        self.assertEqual(1, len(result["measured"]))
        self.assertEqual(3, result["measured"][0]["n"])
        self.assertEqual([100, 105, 110], result["measured"][0]["values"])
        self.assertEqual(10, result["typical_within_voice_spread"])

    def test_selectivity_cli_saves_all_correct_all_failed_and_mixed_strata(self):
        for name, rows, absent in (
                ("correct", [(True, False)], "where plain FAILS"),
                ("failed", [(False, True)], "where plain WORKS"),
                ("mixed", [(True, False), (False, True)], None)):
            with self.subTest(name=name):
                source = self.save(name + ".json", {"status": "complete", "results": [
                    {"plain_recovers_word": plain, "respelled_recovers_word": respelled}
                    for plain, respelled in rows]})
                out = self.root / (name + "-report.json")
                result = self.cli("respelling_selectivity.py", source, "--out", out)
                self.assertEqual(0, result.returncode, result.stderr)
                report = json.loads(out.read_text())["comparisons"][0]
                self.assertEqual(len(rows), report["paired_terms"])
                if absent:
                    line = next(line for line in result.stdout.splitlines() if absent in line)
                    self.assertIn("n=0", line)
                    self.assertIn("n/a", line)
                    key, metric = (("where_plain_fails", "rescue_pct") if name == "correct"
                                   else ("where_plain_already_works", "break_pct"))
                    self.assertIsNone(report[key][metric])
                else:
                    self.assertEqual(100.0, report["where_plain_fails"]["rescue_pct"])
                    self.assertEqual(100.0, report["where_plain_already_works"]["break_pct"])
                    self.assertNotIn("n/a", result.stdout)

    def test_selectivity_without_pairs_still_refuses_without_publishing(self):
        source = self.save("empty.json", {"results": [{"term": "no measurement"}]})
        out = self.root / "report.json"
        result = self.cli("respelling_selectivity.py", source, "--out", out)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no paired rows", result.stderr)
        self.assertFalse(out.exists())

    def roster_main(self, rows, fixtures):
        artifact = self.save("rows.json", {"rows": rows})
        fixtures_dir = self.root / "fixtures"
        fixtures_dir.mkdir(exist_ok=True)
        for name, doc in fixtures.items():
            (fixtures_dir / (name + ".json")).write_text(json.dumps(doc), encoding="utf-8")
        out = self.root / "roster.json"
        result = self.cli("roster_order_bias.py", "--artifact", artifact,
                          "--fixtures", fixtures_dir, "--out", out)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(out.read_text()), result.stdout

    def test_perfect_roster_cli_uses_real_gold_and_reports_no_discordances(self):
        path = next((APP / "fixtures").glob("attribution_gold_pdnc_*emma*.json"))
        fixture = json.loads(path.read_text())
        entry = next(e for e in fixture["entries"]
                     if roster_order_bias.position(e["expected_speaker"],
                         [line.split(" [also")[0] for line in
                          roster_order_bias.roster_lines(fixture)],
                         roster_order_bias.alias_groups(fixture)) is not None)
        doc, output = self.roster_main([{
            "id": path.stem + ":" + entry["id"], "expected": entry["expected_speaker"],
            "predicted": entry["expected_speaker"], "correct": True}], {path.stem: fixture})
        self.assertEqual(1, doc["rows_positioned"])
        self.assertIsNone(doc["paired_on_wrong_rows"]["share_earlier"])
        self.assertIsNone(doc["paired_on_wrong_rows"]["mean_signed_shift"])
        self.assertIn("n/a", output)
        self.assertIn("no directional discordances", output)
        self.assertEqual("complete", doc["status"])

    def test_roster_ties_preserve_zero_shift_and_directional_controls(self):
        stem = "attribution_gold_pdnc_fixture"
        fixture = {"roster": ["ALICE", "BOB"], "aliases": [],
                   "entries": [{"id": "q1", "expected_speaker": "ALICE", "line": "hello"}]}
        row = {"id": stem + ":q1", "expected": "ALICE", "predicted": "ALICE", "correct": False}
        tied, output = self.roster_main([row], {stem: fixture})
        self.assertEqual(1, tied["paired_on_wrong_rows"]["same_position"])
        self.assertEqual(0.0, tied["paired_on_wrong_rows"]["mean_signed_shift"])
        self.assertIn("n/a", output)
        self.assertIn("+0.0000", output)
        directional, output = self.roster_main([{**row, "predicted": "BOB"}], {stem: fixture})
        self.assertEqual(0.0, directional["paired_on_wrong_rows"]["share_earlier"])
        self.assertEqual(1.0, directional["paired_on_wrong_rows"]["mean_signed_shift"])
        self.assertIn("0.0%", output)
        self.assertIn("+1.0000", output)


if __name__ == "__main__":
    unittest.main()
