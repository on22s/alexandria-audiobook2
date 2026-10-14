"""Known PCM pairs must remain disjoint and describe the audio actually scored."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from experiments import ljspeech_score as scorer


def write_audio(root, name, seconds, value, rate=8000):
    path = root / name
    sf.write(path, np.full(int(seconds * rate), value, dtype=np.float32),
             rate, subtype="PCM_16")
    return path


def rows_for(root, seconds):
    rows = []
    for index, length in enumerate(seconds):
        human = write_audio(root, f"human{index}.wav", length, (index + 1) / 8)
        clone = write_audio(root, f"clone{index}.wav", length, -0.125)
        rows.append({"id": str(index), "book": "SyntheticNarrator",
                     "human_wav": human.name, "clone_wav": clone.name,
                     "human_seconds": scorer.duration(human)})
    return rows


class AnchorMaterialTests(unittest.TestCase):
    def run_score(self, root, rows, limit=0, arms=None):
        source, output = root / "generated.json", root / "score.json"
        source.write_text(json.dumps({"arms": ["clone"] if arms is None else arms, "rows": rows}))
        source_bytes = source.read_bytes()
        calls, pairs = [], []

        def acoustic(name):
            def record(a, b):
                calls.append((name, a, b))
                return 0.0  # synthetic callback; no acoustic-quality measurement
            return record

        def ecapa(inputs):
            pairs.extend(inputs)
            return [0.25] * len(inputs), None  # no model/subprocess

        argv = ["score", "--generated", str(source), "--out", str(output),
                "--limit", str(limit)]
        log = io.StringIO()
        with (patch.object(scorer, "REPO", str(root)),
              patch.object(scorer.sys, "argv", argv),
              patch.object(scorer, "f0_contour_correlation", side_effect=acoustic("f0")),
              patch.object(scorer, "mel_cepstral_distortion", side_effect=acoustic("mcd")),
              patch.object(scorer, "ecapa_scores", side_effect=ecapa),
              contextlib.redirect_stdout(log)):
            scorer.main()
        self.assertEqual(source_bytes, source.read_bytes())
        return json.loads(output.read_text()), pairs, calls, log.getvalue()

    def build(self, root, rows, start=1, comparison=None):
        return scorer.build_anchor_side(rows, start, lambda path: str(root / path),
                                        7.0, str(root / "cache"),
                                        exclude=rows[0] if comparison is None else comparison,
                                        input_paths=[str(root / row[key]) for row in rows
                                                     for key in ("human_wav", "clone_wav")])

    def test_two_short_selected_clips_cannot_supply_a_disjoint_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 4, 4])
            document, pairs, calls, log = self.run_score(root, rows, limit=2)
            self.assertEqual(2, len(document["rows"]))
            self.assertEqual(2, len(pairs))  # generated pairs remain evaluated
            self.assertEqual(4, len(calls))
            for row in document["rows"]:
                self.assertNotIn("human_vs_human", row)
                self.assertIn("disjoint", row["human_vs_human_error"])
            self.assertNotIn("human_vs_human", document["summary"])
            self.assertEqual(2, document["summary"]["clone"]["n"])
            self.assertIn("unavailable", log.lower())
            self.assertFalse((root / "ab_test_runtime/anchor_cache/score").exists())

    def test_sufficient_material_scores_exact_disjoint_pcm_and_measured_ratios(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 4, 4])
            originals = {path: path.read_bytes() for path in root.glob("*.wav")}
            before = copy.deepcopy(rows)
            document, pairs, calls, _ = self.run_score(root, rows)
            self.assertEqual(before, rows)
            self.assertEqual(6, len(pairs))
            self.assertEqual(12, len(calls))
            for index, row in enumerate(document["rows"]):
                target = root / rows[index]["human_wav"]
                anchor = pairs[2 * index + 1][1]
                self.assertEqual(str(target), pairs[2 * index + 1][0])
                target_pcm, _ = sf.read(target, dtype="float32")
                anchor_pcm, rate = sf.read(anchor, dtype="float32")
                expected = np.concatenate([sf.read(root / rows[(index + step) % 3]["human_wav"],
                                                   dtype="float32")[0] for step in (1, 2)])
                np.testing.assert_array_equal(expected, anchor_pcm)
                self.assertFalse(np.any(anchor_pcm == target_pcm[0]))
                self.assertEqual(8000, rate)
                self.assertEqual(8.0, row["human_vs_human"]["anchor_seconds"])
                self.assertEqual(scorer.duration(anchor) / scorer.duration(target),
                                 row["human_vs_human"]["dur_ratio"])
                self.assertNotIn("human_vs_human_error", row)
            self.assertEqual(3, document["summary"]["human_vs_human"]["n"])
            self.assertEqual(2.0, document["summary"]["human_vs_human"]["dur_ratio"])
            self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_single_long_disjoint_clip_returns_original_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 8])
            self.assertEqual(str(root / "human1.wav"), self.build(root, rows))
            self.assertFalse((root / "cache").exists())

    def test_all_target_id_aliases_are_excluded_even_at_different_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 4, 4])
            rows[1]["id"] = rows[0]["id"]
            self.assertIsNone(self.build(root, rows))

    def test_target_source_aliases_are_excluded_even_with_different_ids(self):
        for kind in ("same path", "normalized path", "copy", "symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = rows_for(root, [4, 4, 4])
                target, alias = root / "human0.wav", root / "alias.wav"
                if kind == "copy":
                    shutil.copyfile(target, alias)
                elif kind == "symlink":
                    alias.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, alias)
                rows[1]["human_wav"] = ("./human0.wav" if kind == "normalized path"
                                        else "human0.wav" if kind == "same path" else alias.name)
                self.assertIsNone(self.build(root, rows))
                self.assertFalse((root / "cache").exists())

    def test_main_excludes_copied_source_and_id_aliases_before_metric_calls(self):
        for kind in ("source copy", "row id"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = rows_for(root, [4, 4, 4])
                if kind == "source copy":
                    shutil.copyfile(root / rows[0]["human_wav"], root / rows[1]["human_wav"])
                else:
                    rows[1]["id"] = rows[0]["id"]
                document, pairs, calls, _ = self.run_score(root, rows)
                self.assertNotIn("human_vs_human", document["rows"][0])
                self.assertIn("human_vs_human_error", document["rows"][0])
                self.assertIn("disjoint", document["rows"][0]["human_vs_human_error"])
                self.assertEqual(str(root / rows[0]["clone_wav"]), pairs[0][1])
                self.assertFalse(any(Path(b).name.startswith("anchor_00001") for _, b in pairs))
                self.assertFalse(any(Path(b).name.startswith("anchor_00001") for _, _, b in calls))

    def test_short_missing_and_mixed_rate_inputs_do_not_bypass_floor_or_resample(self):
        for kind in ("short", "missing", "mixed rate"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = rows_for(root, [4, 3, 3] if kind == "short" else [4, 4, 4])
                if kind == "missing":
                    (root / rows[1]["human_wav"]).unlink()
                elif kind == "mixed rate":
                    write_audio(root, rows[2]["human_wav"], 4, 0.375, rate=16000)
                self.assertIsNone(self.build(root, rows))
                self.assertFalse((root / "cache").exists())

    def test_wraparound_excludes_target_and_keeps_other_component_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 4, 4])
            path = self.build(root, rows, start=2)
            actual, _ = sf.read(path, dtype="float32")
            expected = np.concatenate([sf.read(root / rows[index]["human_wav"], dtype="float32")[0]
                                       for index in (2, 1)])
            np.testing.assert_array_equal(expected, actual)

    def test_unavailable_material_does_not_reuse_a_stale_cached_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 4])
            cache = root / "cache"
            cache.mkdir()
            stale = write_audio(cache, "anchor_00001.wav", 8, 0.125)
            before = stale.read_bytes()
            self.assertIsNone(self.build(root, rows))
            self.assertEqual(before, stale.read_bytes())

    def test_actual_target_duration_and_unrounded_anchor_drive_recorded_ratios(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 7.125])
            rows[0]["human_seconds"] = 99.0
            document, pairs, _, _ = self.run_score(root, rows)
            row = document["rows"][0]
            self.assertEqual(4.0, row["human_seconds"])
            self.assertEqual(1.0, row["clone"]["dur_ratio"])
            self.assertEqual(str(root / "human1.wav"), pairs[1][1])
            self.assertEqual(round(7.125, 2), row["human_vs_human"]["anchor_seconds"])
            self.assertEqual(7.125 / 4.0, row["human_vs_human"]["dur_ratio"])

    def test_cache_destination_cannot_replace_or_alias_any_input_source(self):
        for kind in ("same path", "symlink", "hardlink", "other source"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = rows_for(root, [4, 4, 4])
                cache = root / "cache"
                cache.mkdir()
                destination = cache / "anchor_00001.wav"
                source = root / rows[2 if kind == "other source" else 0]["human_wav"]
                if kind == "same path":
                    shutil.move(source, destination)
                    rows[0]["human_wav"] = str(destination.relative_to(root))
                elif kind == "hardlink":
                    os.link(source, destination)
                else:
                    destination.symlink_to(source)
                originals = {root / row["human_wav"]: (root / row["human_wav"]).read_bytes() for row in rows}
                self.assertIsNone(self.build(root, rows))
                self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_main_refuses_cache_alias_before_submitting_a_self_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = rows_for(root, [4, 4, 4])
            cache = root / "ab_test_runtime/anchor_cache/score"
            cache.mkdir(parents=True)
            target = root / rows[0]["human_wav"]
            (cache / "anchor_00001.wav").symlink_to(target)
            before = target.read_bytes()
            document, pairs, calls, _ = self.run_score(root, rows)
            self.assertEqual(before, target.read_bytes())
            self.assertEqual(4.0, scorer.duration(target))
            self.assertNotIn("human_vs_human", document["rows"][0])
            self.assertIn("disjoint", document["rows"][0]["human_vs_human_error"])
            self.assertEqual(2, document["summary"]["human_vs_human"]["n"])
            self.assertEqual(5, len(pairs))
            self.assertEqual(10, len(calls))
            self.assertFalse(any(Path(b).name == "anchor_00001.wav" for _, b in pairs))

    def test_main_preserves_generated_inputs_when_cache_aliases_an_admitted_arm(self):
        for kind in ("clone target", "clone other row", "lora target"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = rows_for(root, [4, 4, 4])
                if kind == "lora target":
                    for row in rows:
                        row["lora_wav"] = row.pop("clone_wav")
                    arm = "lora"
                else:
                    arm = "clone"
                target = root / rows[2 if kind == "clone other row" else 0][f"{arm}_wav"]
                cache = root / "ab_test_runtime/anchor_cache/score"
                cache.mkdir(parents=True)
                (cache / "anchor_00001.wav").symlink_to(target)
                before = {root / row[key]: (root / row[key]).read_bytes() for row in rows
                          for key in ("human_wav", f"{arm}_wav")}
                document, pairs, calls, _ = self.run_score(root, rows, arms=[arm])
                for path, data in before.items():
                    self.assertTrue(path.read_bytes() == data, f"{path.name} was overwritten")
                self.assertEqual(4.0, scorer.duration(target))
                self.assertNotIn("human_vs_human", document["rows"][0])
                self.assertIn("human_vs_human_error", document["rows"][0])
                self.assertEqual(3, document["summary"][arm]["n"])
                self.assertEqual(1.0, document["summary"][arm]["dur_ratio"])
                self.assertEqual(5, len(pairs))
                self.assertEqual(10, len(calls))
                self.assertFalse(any(Path(b).name == "anchor_00001.wav" for _, b in pairs))

    def test_repeated_non_target_material_cannot_fabricate_the_minimum(self):
        for kind in ("same path", "copy", "row id"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = rows_for(root, [4, 4, 4])
                if kind == "same path":
                    rows[2]["human_wav"] = rows[1]["human_wav"]
                elif kind == "copy":
                    shutil.copyfile(root / rows[1]["human_wav"], root / rows[2]["human_wav"])
                else:
                    rows[2]["id"] = rows[1]["id"]
                before = {path: path.read_bytes() for path in root.glob("*.wav")}
                document, pairs, calls, _ = self.run_score(root, rows)
                self.assertEqual(3, len(document["rows"]))
                self.assertEqual(3, len(pairs))
                self.assertEqual(6, len(calls))
                self.assertNotIn("human_vs_human", document["summary"])
                self.assertEqual(3, document["summary"]["clone"]["n"])
                for row in document["rows"]:
                    self.assertNotIn("human_vs_human", row)
                    self.assertIn("human_vs_human_error", row)
                for path, data in before.items():
                    self.assertTrue(path.read_bytes() == data, f"{path.name} was overwritten")
