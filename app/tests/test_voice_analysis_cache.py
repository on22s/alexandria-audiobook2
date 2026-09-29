"""Exercise analyze checkpoints without loading GPU or plotting dependencies."""

import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import pickle
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import zipfile

import numpy as np
from scipy.spatial.distance import pdist, squareform


SOURCE = Path(__file__).resolve().parent.parent.parent / "tools/voice_lab/voice_analysis.py"


class StopAfterSimilarity(Exception):
    pass


def load_analysis_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"run_analyze", "normalize_group_key", "_load_pickle_cache",
             "_atomic_pickle_dump", "list_wavs_in_zip"}
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"os": os, "pickle": pickle, "re": re, "np": np, "zipfile": zipfile,
                 "ANALYZE_SAMPLES": 0, "pdist": pdist, "squareform": squareform,
                 "tqdm": lambda values, **_kwargs: values,
                 "plt": SimpleNamespace(subplots=Mock(side_effect=StopAfterSimilarity)),
                 "load_wav_from_zip": Mock(return_value=(np.ones(16), 16000)),
                 "extract_embedding": Mock(return_value=np.array([1.0, 2.0])),
                 "extract_prosody": Mock(return_value={"duration": 0.001})}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class VoiceAnalysisCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.zips = Path(self.temp.name, "zips")
        self.output = Path(self.temp.name, "analysis")
        self.zips.mkdir()
        self.output.mkdir()
        self.zip = self.zips / "voice.zip"
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("train/one.wav", b"mocked decode")
            archive.writestr("train/two.wav", b"mocked decode")
        self.cache = self.output / "embeddings_cache.pkl"
        self.ns = load_analysis_functions()

    def run_analysis(self):
        output = io.StringIO()
        error = None
        with redirect_stdout(output):
            try:
                self.ns["run_analyze"](None, "cpu", self.zips, self.output)
            except (StopAfterSimilarity, ValueError) as exc:
                error = exc
        with self.cache.open("rb") as handle:
            data = pickle.load(handle)
        self.assertIsInstance(data["embeddings"]["voice"], np.ndarray)
        np.testing.assert_array_equal([[1.0, 2.0], [1.0, 2.0]], data["embeddings"]["voice"])
        self.assertEqual([{"duration": 0.001}] * 2, data["prosody"]["voice"])
        self.assertEqual([(str(self.zip), "train/one.wav"), (str(self.zip), "train/two.wav")],
                         data["wav_names"]["voice"])
        self.assertIsNot(data["embeddings"], data["prosody"])
        self.assertIsNot(data["embeddings"], data["wav_names"])
        self.assertIsInstance(error, StopAfterSimilarity)
        self.assertFalse(self.cache.with_suffix(".pkl.tmp").exists())
        return output.getvalue()

    def test_fresh_run_writes_distinct_real_checkpoint_contents(self):
        self.run_analysis()
        self.assertEqual(2, self.ns["extract_embedding"].call_count)

    def test_poisoned_shared_dictionary_checkpoint_is_rebuilt(self):
        shared = {"voice": [(str(self.zip), "train/one.wav"), (str(self.zip), "train/two.wav")]}
        with self.cache.open("wb") as handle:
            pickle.dump({"embeddings": shared, "prosody": shared, "wav_names": shared}, handle)
        with self.cache.open("rb") as handle:
            old = pickle.load(handle)
        self.assertIs(old["embeddings"], old["wav_names"])
        output = self.run_analysis()
        self.assertIn("rebuilding", output)
        self.assertEqual(2, self.ns["extract_embedding"].call_count)

    def test_valid_independent_checkpoint_is_reused(self):
        with self.cache.open("wb") as handle:
            pickle.dump({"embeddings": {"voice": np.array([[1.0, 2.0], [1.0, 2.0]])},
                         "prosody": {"voice": [{"duration": 0.001}] * 2},
                         "wav_names": {"voice": [(str(self.zip), "train/one.wav"),
                                                  (str(self.zip), "train/two.wav")]}}, handle)
        original = self.cache.read_bytes()
        self.run_analysis()
        self.ns["extract_embedding"].assert_not_called()
        self.assertEqual(original, self.cache.read_bytes())
