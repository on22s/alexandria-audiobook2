"""Exercise analyze checkpoints without loading GPU or plotting dependencies."""

import ast
import argparse
import datetime
import random
import sys
from contextlib import redirect_stdout
import io
import multiprocessing
import os
from pathlib import Path
import pickle
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import numpy as np
from scipy.spatial.distance import pdist, squareform, cdist
from voice_analysis_cache import get_voice_analysis_stage_identity


SOURCE = Path(__file__).resolve().parent.parent.parent / "tools/voice_lab/voice_analysis.py"


class StopAfterSimilarity(Exception):
    pass


def load_analysis_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"run_analyze", "normalize_group_key", "_load_pickle_cache",
             "_atomic_pickle_dump", "list_wavs_in_zip", "main", "write_pipeline_summary",
             "get_analysis_file_hashes", "get_dedup_narrator_evidence",
             "get_completed_analysis_phase", "get_narrator_pipeline_completion",
             "get_analysis_selected_wavs", "get_embedding_projection"}
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    model = SimpleNamespace(_alexandria_model_id="fixture-ecapa",
        _alexandria_state_sha256="1" * 64,
        _alexandria_model_files={"fixture": Path(__file__)},
        _alexandria_dependency_versions={"fixture": "1"})
    namespace = {"__file__": str(SOURCE), "fixture_model": model,
                 "get_voice_analysis_stage_identity": get_voice_analysis_stage_identity,"Path": Path, "argparse": argparse, "datetime": datetime, "random": random, "sys": sys,
                 "DEFAULT_ZIPS2": Path("unused"), "PROJECT_ROOT": SOURCE.parents[2],
                 "normalize_device": lambda value: value, "resolve_device": lambda _value: "cpu",
                 "torch": SimpleNamespace(version=SimpleNamespace(hip=None)),
                 "load_model": Mock(return_value=model), "__doc__": "Voice analysis",
                 "EXCLUDE_ZIPS": set(), "tempfile": tempfile, "os": os, "pickle": pickle, "re": re, "np": np, "zipfile": zipfile,
                 "ANALYZE_SAMPLES": 0, "pdist": pdist, "squareform": squareform, "cdist": cdist,
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
                self.ns["run_analyze"](self.ns["fixture_model"], "cpu", self.zips, self.output)
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
        self.run_analysis()
        self.ns["extract_embedding"].reset_mock()
        original = self.cache.read_bytes()
        self.run_analysis()
        self.ns["extract_embedding"].assert_not_called()
        self.assertEqual(original, self.cache.read_bytes())


class AnalysisSeedTests(unittest.TestCase):
    def test_cli_seed_controls_real_numpy_sample_selection_and_checkpoint(self):
        numpy_state = np.random.get_state()
        python_state = random.getstate()
        self.addCleanup(np.random.set_state, numpy_state)
        self.addCleanup(random.setstate, python_state)
        for split in (False, True):
            with self.subTest(split=split), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                zips = root / "zips"
                deduped = zips / "_deduped"
                deduped.mkdir(parents=True)
                with zipfile.ZipFile(deduped / "voice.zip", "w") as archive:
                    for index in range(60):
                        name = f"{index}.wav" if not split else f"{'train' if index < 30 else 'val'}/{index}.wav"
                        archive.writestr(name, b"mocked decode")
                outputs = []
                for index, seed in enumerate((42, 42, 43, -1, -1, 2**40 + 7, 2**40 + 7)):
                    ns = load_analysis_functions()
                    ns["ANALYZE_SAMPLES"] = 8
                    output = root / f"analysis_{index}"
                    argv = ["voice_analysis.py", "--phase", "analyze", "--device", "cpu",
                            "--zips2", str(zips), "--analyze-out", str(output),
                            "--dedup-out", str(root / "dedup"), "--seed", str(seed)]
                    with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                        with self.assertRaises(StopAfterSimilarity):
                            ns["main"]()
                    with (output / "embeddings_cache.pkl").open("rb") as handle:
                        artifact = pickle.load(handle)
                    selected = [name for _path, name in artifact["wav_names"]["voice"]]
                    self.assertEqual(8, len(selected))
                    self.assertEqual(8, len(set(selected)))
                    self.assertEqual((8, 2), artifact["embeddings"]["voice"].shape)
                    if split:
                        self.assertTrue(all(name.startswith("train/") for name in selected))
                        self.assertFalse(any(name.startswith("val/") for name in selected))
                    outputs.append(selected)
                self.assertEqual(outputs[0], outputs[1])
                self.assertNotEqual(outputs[0], outputs[2])
                self.assertEqual(outputs[3], outputs[4])
                self.assertEqual(outputs[5], outputs[6])


class PipelineSummaryCorruptCacheTests(unittest.TestCase):
    def test_summary_quarantines_corruption_and_analysis_rebuilds_real_checkpoint(self):
        for content in (b"", b"not a pickle", b"\x80\x04\x95"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                zips = root / "zips"
                narrator = zips / "voice"
                narrator.mkdir(parents=True)
                deduped = zips / "_deduped"
                deduped.mkdir()
                for directory in (narrator, deduped):
                    with zipfile.ZipFile(directory / "voice.zip", "w") as archive:
                        archive.writestr("train/one.wav", b"mocked decode")
                        archive.writestr("train/two.wav", b"mocked decode")
                dedup = root / "dedup"
                dedup.mkdir()
                (dedup / "dedup_voice.png").write_bytes(b"existing plot")
                analyze = root / "analyze"
                analyze.mkdir()
                cache = analyze / "embeddings_cache.pkl"
                cache.write_bytes(content)
                original_zip = (deduped / "voice.zip").read_bytes()
                ns = load_analysis_functions()
                with redirect_stdout(io.StringIO()) as logs:
                    ns["write_pipeline_summary"](zips, dedup, analyze)
                self.assertEqual(content, cache.read_bytes())
                self.assertIn("[DEDUP]   voice", (dedup / "pipeline_summary.log").read_text())
                with redirect_stdout(io.StringIO()) as rebuild_logs, self.assertRaises(StopAfterSimilarity):
                    ns["run_analyze"](ns["fixture_model"], "cpu", deduped, analyze)
                self.assertIn("unreadable cache", rebuild_logs.getvalue())
                self.assertEqual(content, cache.with_suffix(".pkl.corrupt").read_bytes())
                with cache.open("rb") as handle:
                    rebuilt = pickle.load(handle)
                self.assertEqual((2, 2), rebuilt["embeddings"]["voice"].shape)
                self.assertEqual(["train/one.wav", "train/two.wav"],
                                 [name for _path, name in rebuilt["wav_names"]["voice"]])
                self.assertEqual(original_zip, (deduped / "voice.zip").read_bytes())
                self.assertEqual(b"existing plot", (dedup / "dedup_voice.png").read_bytes())

    def test_valid_and_absent_caches_still_publish_summary_without_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zips, dedup, analyze = [root / name for name in ("zips", "dedup", "analyze")]
            for directory in (zips, dedup, analyze):
                directory.mkdir()
            ns = load_analysis_functions()
            cache = analyze / "embeddings_cache.pkl"
            for content in (None, pickle.dumps({"embeddings": {}, "prosody": {}, "wav_names": {}})):
                with self.subTest(content=content):
                    if content is not None:
                        cache.write_bytes(content)
                    with redirect_stdout(io.StringIO()):
                        ns["write_pipeline_summary"](zips, dedup, analyze)
                    self.assertIn("0 narrator folders total", (dedup / "pipeline_summary.log").read_text())
                    self.assertEqual(content is not None, cache.exists())
                    if content is not None:
                        self.assertEqual(content, cache.read_bytes())
                    self.assertFalse(cache.with_suffix(".pkl.corrupt").exists())


class AtomicPickleWriterTests(unittest.TestCase):
    def test_overlapping_processes_publish_whole_snapshots_and_keep_staging_private(self):
        ns = load_analysis_functions()
        context = multiprocessing.get_context('fork')
        paused, release, ready, publish = [context.Event() for _ in range(4)]
        outcomes = context.Queue()
        first = {'writer':'first','rows':list(range(2000))}
        second = {'writer':'second','rows':['different snapshot'] * 1000}
        original_dump, original_replace = pickle.dump, os.replace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'embeddings_cache.pkl')
            old = pickle.dumps({'writer':'previous','rows':[]})
            path.write_bytes(old)
            unrelated = Path(tmp, 'unrelated.tmp')
            unrelated.write_bytes(b'not owned by either writer')

            def worker(value, delay):
                try:
                    if delay:
                        def dump(value, handle):
                            data = pickle.dumps(value)
                            split = len(data) // 2
                            handle.write(data[:split]); handle.flush()
                            paused.set()
                            if not release.wait(5):
                                raise RuntimeError('test did not release serializer')
                            handle.write(data[split:])
                        def replace(source, destination):
                            ready.set()
                            if not publish.wait(5):
                                raise RuntimeError('test did not release publication')
                            original_replace(source,destination)
                        pickle.dump = dump
                        os.replace = replace
                    ns['_atomic_pickle_dump'](value,path)
                    outcomes.put((value['writer'],None))
                except BaseException as error:
                    outcomes.put((value['writer'],type(error).__name__ + ': ' + str(error)))
                finally:
                    pickle.dump = original_dump
                    os.replace = original_replace

            a = context.Process(target=worker,args=(first,True))
            b = context.Process(target=worker,args=(second,False))
            a.start()
            try:
                self.assertTrue(paused.wait(5),'first writer must pause after partial serialization')
                self.assertEqual(old,path.read_bytes(),'partial pickle must not touch published cache')
                b.start(); b.join(5)
                self.assertFalse(b.is_alive(),'independent snapshot writer must complete')
                self.assertEqual(second,pickle.loads(path.read_bytes()))
                release.set()
                self.assertTrue(ready.wait(5),'first serialization must reach atomic publication')
                self.assertEqual(second,pickle.loads(path.read_bytes()),
                                 'fully serialized but unpublished first snapshot must leave second intact')
                publish.set(); a.join(5)
                self.assertFalse(a.is_alive())
                results = dict(outcomes.get(timeout=2) for _ in range(2))
                self.assertEqual({'first':None,'second':None},results)
                self.assertEqual(first,pickle.loads(path.read_bytes()))
                self.assertEqual({path.name,unrelated.name},{p.name for p in Path(tmp).iterdir()})
                self.assertEqual(b'not owned by either writer',unrelated.read_bytes())
            finally:
                release.set(); publish.set()
                for process in (a,b):
                    if process.pid is not None:
                        process.join(2)
                        if process.is_alive():
                            process.terminate();process.join(2)
                outcomes.close();outcomes.join_thread()

    def test_failed_serialization_or_replace_keeps_previous_cache_and_removes_only_owned_temp(self):
        ns = load_analysis_functions()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp,'cache.pkl')
            old = pickle.dumps({'existing':[1,2,3]})
            unrelated = Path(tmp,'cache.pkl.unrelated.tmp')
            unrelated.write_bytes(b'other writer')
            for failure in ('serialize','replace'):
                with self.subTest(failure=failure):
                    path.write_bytes(old)
                    def fail_dump(value, handle):
                        handle.write(b'partial pickle')
                        raise OSError('serialization failed')
                    with patch.object(pickle,'dump',side_effect=fail_dump if failure=='serialize' else pickle.dump), \
                         patch.object(os,'replace',side_effect=OSError('replace failed') if failure=='replace' else os.replace):
                        with self.assertRaisesRegex(OSError,failure if failure=='replace' else 'serialization'):
                            ns['_atomic_pickle_dump']({'new':[4]},path)
                    self.assertEqual(old,path.read_bytes())
                    self.assertEqual(b'other writer',unrelated.read_bytes())
                    self.assertEqual({path.name,unrelated.name},{p.name for p in Path(tmp).iterdir()})
