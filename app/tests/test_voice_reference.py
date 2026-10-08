"""The reference clip must represent the dataset it anchors.

WHAT THIS PROTECTS. `train_lora.py` extracts the speaker embedding from ONE
reference clip and applies it to every training sample. The dataset builder
chose `ref_index = 0` - whatever came first - and never checked it.

Measured across the 75 shipped adapters:

    reference mismatched (<0.3):  7 adapters, 6 of them poor  (86%)
    reference matching:          67 adapters, 9 of them poor  (13%)

6.4x more likely to fail, correlation +0.76 with adapter quality. The worst
case was anchored to a clip scoring -0.026 against its own dataset - actively
not that speaker - while a 0.882 clip sat unused in the same data.

The tests use a stubbed similarity function. What is under test is the
SELECTION LOGIC and its refusal behaviour, not the speaker model: whether the
medoid is identified, and whether an unavailable model degrades to "no opinion"
rather than to a confident wrong answer.
"""
import os
import sys
import tempfile
import unittest
import contextlib
import io
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_reference


class SiblingInterpreterLayoutTests(unittest.TestCase):
    def test_default_constructor_and_resolver_select_platform_layout_with_existing_precedence(self):
        import ast
        from pathlib import Path
        sys.path.insert(0, str(Path(voice_reference.__file__).resolve().parent.parent))
        from app_venv import get_app_python
        source = Path(voice_reference.__file__).read_text()
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == 'SIBLING_PY' for target in node.targets))
        statement = compile(ast.Module(body=[node], type_ignores=[]), voice_reference.__file__, 'exec')
        for platform, relative in (('win32', ('Scripts', 'python.exe')), ('linux', ('bin', 'python'))):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                expected = root / 'alexandria-audiobook.git' / 'app' / 'env' / relative[0] / relative[1]
                expected.parent.mkdir(parents=True); expected.write_bytes(b'fixture interpreter')
                configured = root / 'configured-python'; configured.write_bytes(b'configured fixture')
                namespace = {'os': os, 'REPO': str(root / 'alexandria-audiobook2.git'),
                             'get_app_python': get_app_python}
                with patch.dict(os.environ):
                    os.environ.pop('ALEXANDRIA_SIBLING_PYTHON', None)
                    with patch.object(sys, 'platform', platform):
                        exec(statement, namespace)
                with patch.object(voice_reference, 'SIBLING_PY', namespace['SIBLING_PY']), \
                        patch('importlib.util.find_spec', return_value=None):
                    self.assertEqual(str(expected), voice_reference.get_speaker_model_python({}))
                    self.assertEqual(str(configured), voice_reference.get_speaker_model_python({'rocm_python': str(configured)}))
                    expected.unlink()
                    self.assertIsNone(voice_reference.get_speaker_model_python({}))
                with patch.dict(os.environ, {'ALEXANDRIA_SIBLING_PYTHON': str(configured)}):
                    exec(statement, namespace)
                self.assertEqual(str(configured), namespace['SIBLING_PY'])
                with patch('importlib.util.find_spec', return_value=object()):
                    self.assertEqual(sys.executable, voice_reference.get_speaker_model_python({'rocm_python': str(configured)}))


def rank_reference_samples(paths, **kwargs):
    return voice_reference.rank_reference_samples(paths, dataset_root=os.path.dirname(paths[0]), **kwargs)


def select_reference_sample(paths, **kwargs):
    return voice_reference.select_reference_sample(paths, dataset_root=os.path.dirname(paths[0]), **kwargs)


def make_clips(n):
    """n touchable files; contents irrelevant, similarity is stubbed."""
    tmp = tempfile.TemporaryDirectory()
    paths = []
    for i in range(n):
        p = os.path.join(tmp.name, f"sample_{i:03d}.wav")
        with open(p, "wb") as fh:
            fh.write(b"\0")
        paths.append(p)
    return tmp, paths


def similarity_from_groups(groups):
    """Stub: clips in the same group are similar, across groups they are not.

    Mirrors the real failure - a dataset whose clips are mostly one speaker
    with a misdiarized minority.
    """
    def fn(pairs, timeout=600, **kwargs):
        out = []
        for a, b in pairs:
            ga = next(g for g, members in groups.items() if a in members)
            gb = next(g for g, members in groups.items() if b in members)
            out.append(0.85 if ga == gb else 0.05)
        return out
    return fn


class MedoidSelectionTest(unittest.TestCase):

    def test_it_picks_the_majority_speaker_not_the_first_clip(self):
        """THE BUG. Clip 0 belongs to the minority speaker, so anchoring to it
        would train the whole adapter on the wrong voice."""
        tmp, paths = make_clips(6)
        self.addCleanup(tmp.cleanup)
        groups = {"intruder": {paths[0]}, "narrator": set(paths[1:])}
        with patch.object(voice_reference, "_speaker_similarities",
                          similarity_from_groups(groups)):
            pick, score = select_reference_sample(paths)
        self.assertIsNotNone(pick)
        self.assertNotEqual(pick, 0, "picked the intruder clip")
        self.assertGreater(score, 0.5)

    def test_a_clean_dataset_yields_a_high_score(self):
        tmp, paths = make_clips(5)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          similarity_from_groups({"all": set(paths)})):
            pick, score = select_reference_sample(paths)
        self.assertIsNotNone(pick)
        self.assertAlmostEqual(score, 0.85, places=2)

    def test_no_model_means_no_opinion_not_a_guess(self):
        """Returning 0 on failure would be indistinguishable from a real
        decision and would hide the very defect this exists to catch."""
        tmp, paths = make_clips(5)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          lambda pairs, timeout=600, **kwargs: None):
            self.assertEqual(select_reference_sample(paths), (None, None))

    def test_a_truncated_result_is_refused(self):
        """Fewer scores than pairs means something went wrong mid-batch;
        scoring on a partial result would silently weight some clips more."""
        tmp, paths = make_clips(5)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          lambda pairs, timeout=600, **kwargs: [0.9]):
            self.assertEqual(select_reference_sample(paths), (None, None))

    def test_nonfinite_similarity_batches_are_declined_with_a_warning(self):
        tmp, paths = make_clips(4)
        self.addCleanup(tmp.cleanup)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), \
                 patch.object(voice_reference, "_speaker_similarities", return_value=[value] * 6):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual((None, None), select_reference_sample(paths))
                self.assertIn("invalid similarity", output.getvalue())

    def test_one_nonfinite_pair_cannot_turn_a_partial_matrix_into_a_recommendation(self):
        tmp, paths = make_clips(4)
        self.addCleanup(tmp.cleanup)
        for value in (float("nan"), float("inf"), float("-inf")):
            for position in range(6):
                with self.subTest(value=value, position=position):
                    scores = [0.85] * 6
                    scores[position] = value
                    with patch.object(voice_reference, "_speaker_similarities", return_value=scores):
                        self.assertEqual([], rank_reference_samples(paths))

    def test_finite_matrix_retains_known_medoid_and_weak_clip_scores(self):
        tmp, paths = make_clips(4)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          return_value=[0.1, 0.1, 0.1, 0.9, 0.8, 0.8]):
            self.assertEqual([(1, 0.8), (2, 0.8), (3, 0.8), (0, 0.1)],
                             rank_reference_samples(paths))
            self.assertEqual((1, 0.8), select_reference_sample(paths))
            self.assertEqual((None, 0.1), select_reference_sample(paths, reference_rank=3))

    def test_too_few_clips_is_refused(self):
        tmp, paths = make_clips(2)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(select_reference_sample(paths), (None, None))

    def test_missing_files_are_skipped_not_compared(self):
        tmp, paths = make_clips(4)
        self.addCleanup(tmp.cleanup)
        os.unlink(paths[1])
        seen = {}

        def fn(pairs, timeout=600, **kwargs):
            seen["paths"] = {p for pair in pairs for p in pair}
            return [0.8] * len(pairs)
        with patch.object(voice_reference, "_speaker_similarities", fn):
            pick, _ = select_reference_sample(paths)
        self.assertIsNotNone(pick)
        self.assertNotIn(paths[1], seen["paths"])

    def test_the_returned_index_addresses_the_original_list(self):
        """The caller maps this index back onto its own sample list, so an
        index into the filtered subset would select the wrong clip."""
        tmp, paths = make_clips(5)
        self.addCleanup(tmp.cleanup)
        os.unlink(paths[0])
        groups = {"odd": {paths[1]}, "rest": set(paths[2:])}
        with patch.object(voice_reference, "_speaker_similarities",
                          similarity_from_groups(groups)):
            pick, _ = select_reference_sample(paths)
        self.assertIn(pick, (2, 3, 4),
                      "index does not address the original list")

    def test_the_comparison_count_is_bounded(self):
        """This runs inside a save request and the pair count is quadratic."""
        tmp, paths = make_clips(40)
        self.addCleanup(tmp.cleanup)
        seen = {}

        def fn(pairs, timeout=600, **kwargs):
            seen["n"] = len(pairs)
            return [0.8] * len(pairs)
        with patch.object(voice_reference, "_speaker_similarities", fn):
            select_reference_sample(paths)
        cap = voice_reference.MAX_CLIPS
        self.assertLessEqual(seen["n"], cap * (cap - 1) // 2)

    def test_an_explicit_rank_selects_a_different_strong_candidate(self):
        tmp, paths = make_clips(5)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          lambda pairs, timeout=600, **kwargs: [0.85] * len(pairs)):
            ranked = rank_reference_samples(paths)
            first, _ = select_reference_sample(paths, reference_rank=0)
            second, score = select_reference_sample(paths, reference_rank=1)
        self.assertEqual(paths.index(paths[ranked[1][0]]), second)
        self.assertNotEqual(first, second)
        self.assertEqual(0.85, score)

    def test_out_of_range_rank_is_refused(self):
        tmp, paths = make_clips(3)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          lambda pairs, timeout=600, **kwargs: [0.85] * len(pairs)):
            self.assertEqual((None, None), select_reference_sample(
                paths, reference_rank=3))


if __name__ == "__main__":
    unittest.main()


class WeakMedoidTest(unittest.TestCase):
    """The best of a bad lot is not a recommendation.

    Retraining ten adapters with an explicit medoid recovered nine. The tenth,
    `breathy_baritone_30s_m_fantasy`, went 0.705 -> 0.597 - and its medoid
    scored 0.49, the lowest in the batch. On a dataset where even the most
    representative clip is mediocre, replacing a reference that happened to be
    good makes things worse.
    """

    def test_a_weak_medoid_is_reported_but_not_recommended(self):
        tmp, paths = make_clips(5)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          lambda pairs, timeout=600, **kwargs: [0.30] * len(pairs)):
            pick, score = select_reference_sample(paths)
        self.assertIsNone(pick, "recommended the best of a bad lot")
        self.assertAlmostEqual(score, 0.30, places=2,
                               msg="the score must still be reported so the "
                                   "caller can log why it declined")

    def test_a_strong_medoid_is_still_recommended(self):
        tmp, paths = make_clips(5)
        self.addCleanup(tmp.cleanup)
        with patch.object(voice_reference, "_speaker_similarities",
                          lambda pairs, timeout=600, **kwargs: [0.85] * len(pairs)):
            pick, score = select_reference_sample(paths)
        self.assertIsNotNone(pick)
        self.assertGreaterEqual(score, voice_reference.MIN_USABLE_SIMILARITY)

    def test_the_threshold_sits_between_the_measured_cases(self):
        """0.49 regressed an adapter; 0.71-0.85 recovered nine of them."""
        self.assertGreater(voice_reference.MIN_USABLE_SIMILARITY, 0.49)
        self.assertLess(voice_reference.MIN_USABLE_SIMILARITY, 0.71)


class EffectiveReferenceSampleTests(unittest.TestCase):
    def test_capped_sample_requires_three_clips_before_similarity_or_recommendation(self):
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as tmp:
            paths = [os.path.join(tmp, str(i) + '.wav') for i in range(3)]
            for path in paths:
                sf.write(path, np.sin(np.arange(1600) / 10) * .1, 16000)
            for cap in (0, 1, 2):
                with self.subTest(cap=cap), patch.object(voice_reference, '_speaker_similarities', return_value=[.9]) as similarities:
                    self.assertEqual([], rank_reference_samples(paths, max_clips=cap))
                    self.assertEqual((None, None), select_reference_sample(paths, max_clips=cap))
                    similarities.assert_not_called()
            with patch.object(voice_reference, '_speaker_similarities', return_value=[.9] * 3) as similarities:
                self.assertEqual((0, .9), select_reference_sample(paths, max_clips=3))
                self.assertEqual(3, len(similarities.call_args.args[0]))


class ConfiguredReferenceWorkerTests(unittest.TestCase):
    def test_actual_ranking_uses_runtime_voice_lab_python_and_preserves_config(self):
        import importlib.util
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "worker.json"
            worker = root / "configured python"
            worker.write_text("#!" + sys.executable + "\n"
                + "import json,sys\nfrom pathlib import Path\n"
                + "pairs=json.load(sys.stdin)\n"
                + "Path(" + repr(str(capture)) + ").write_text(json.dumps({'argv':sys.argv,'pairs':pairs}))\n"
                + "print('provider stand-in banner')\nprint('[0.9,0.8,0.85]')\n")
            worker.chmod(0o755)
            config = root / "config.json"
            config.write_text(json.dumps({"voicelab": {"rocm_python": str(worker)}, "unrelated": 19}))
            config_bytes = config.read_bytes()
            paths = []
            for index in range(3):
                path = root / f"clip_{index}.wav"
                path.write_bytes(b"CPU provider stand-in; not ECAPA input")
                paths.append(str(path))
            with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": str(root)}), \
                 patch.object(importlib.util, "find_spec", return_value=None), \
                 patch.object(voice_reference, "SIBLING_PY", str(root / "absent-sibling")):
                pick, score = voice_reference.select_reference_sample(paths)
            self.assertEqual(1, pick)
            self.assertAlmostEqual(.875, score)
            captured = json.loads(capture.read_text())
            self.assertEqual(str(worker), captured["argv"][0])
            self.assertEqual(str(Path(voice_reference.APP) / "experiments/_ecapa_batch.py"), captured["argv"][1])
            self.assertEqual([[paths[0], paths[1]], [paths[0], paths[2]], [paths[1], paths[2]]], captured["pairs"])
            self.assertEqual(config_bytes, config.read_bytes())
            self.assertEqual([b"CPU provider stand-in; not ECAPA input"] * 3,
                             [Path(path).read_bytes() for path in paths])

    def test_missing_or_failed_configured_worker_keeps_no_opinion_behavior(self):
        import importlib.util
        import json
        from pathlib import Path
        for mode in ("missing", "failed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worker = root / "selected-python"
                if mode == "failed":
                    worker.write_text("#!" + sys.executable + "\nraise SystemExit(7)\n")
                    worker.chmod(0o755)
                config = root / "config.json"
                config.write_text(json.dumps({"voicelab": {"rocm_python": str(worker)}}))
                before = config.read_bytes()
                with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": str(root)}), \
                     patch.object(importlib.util, "find_spec", return_value=None), \
                     patch.object(voice_reference, "SIBLING_PY", str(root / "absent-sibling")):
                    self.assertIsNone(voice_reference._speaker_similarities([("a.wav", "b.wav")]))
                self.assertEqual(before, config.read_bytes())


    def test_malformed_runtime_voice_lab_config_declines_with_diagnostic(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(json.dumps({"voicelab": ["not a configuration object"]}))
            before = config.read_bytes()
            output = io.StringIO()
            with patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": str(root)}), \
                 patch.object(voice_reference, "get_speaker_model_python") as resolve, \
                 patch.object(voice_reference.subprocess, "run") as run, \
                 contextlib.redirect_stdout(output):
                self.assertIsNone(voice_reference._speaker_similarities([("a.wav", "b.wav")]))
            self.assertIn("Invalid Voice Lab configuration", output.getvalue())
            resolve.assert_not_called()
            run.assert_not_called()
            self.assertEqual(before, config.read_bytes())
