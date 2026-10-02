"""Native worker fixtures verify reuse without claiming real ECAPA inference."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import voice_reference as reference


class ReferenceScoreCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = self.root / "app"
        (self.app / "experiments").mkdir(parents=True)
        self.worker = self.app / "experiments/_ecapa_batch.py"
        self.counter = self.root / "calls"
        self.mode = self.root / "mode"
        self.mode.write_text("valid")
        self.worker.write_text("""import json,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2]
pairs=json.load(sys.stdin)
with (root/'calls').open('a') as f:f.write('called\\n')
time.sleep(.02)
mode=(root/'mode').read_text()
if mode=='failed':raise SystemExit(7)
if mode=='malformed':print('not JSON');raise SystemExit(0)
if mode=='mutated':
 path=Path(pairs[0][0]);path.write_bytes(path.read_bytes()+b'changed while scoring')
values=[.9]*len(pairs)
if mode=='partial':values[0]=None
if mode=='nonfinite':values[0]=float('nan')
if mode=='short':values=values[:-1]
print('provider banner');print(json.dumps(values))
""")
        self.python = self.root / "env/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("#!" + sys.executable + "\nimport os,sys\nos.execv(" + repr(sys.executable)
                               + ", [" + repr(sys.executable) + "]+sys.argv[1:])\n")
        self.python.chmod(0o755)
        metadata = self.root / "env/lib/python3.10/site-packages/speechbrain-1.dist-info/METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("Version: 1\n")
        self.metadata = metadata
        self.asset = self.root / "ab_test_runtime/ecapa/embedding_model.ckpt"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_bytes(b"synthetic model identity")
        self.paths = []
        for i in range(3):
            path = self.root / (str(i) + ".wav")
            path.write_bytes(("native worker audio fixture " + str(i)).encode())
            self.paths.append(str(path))
        self.pairs = [(self.paths[0], self.paths[1]), (self.paths[0], self.paths[2]),
                      (self.paths[1], self.paths[2])]
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(reference, "APP", str(self.app)))
        stack.enter_context(patch.object(reference, "REPO", str(self.root)))
        stack.enter_context(patch.object(reference, "get_speaker_model_python", return_value=str(self.python)))
        stack.enter_context(patch.dict(os.environ, {"ALEXANDRIA_DATA_DIR": str(self.root)}))
        reference._REFERENCE_SCORE_CACHE.clear()
        self.addCleanup(reference._REFERENCE_SCORE_CACHE.clear)

    def calls(self):
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def score(self, pairs=None):
        return reference._speaker_similarities(pairs or self.pairs, timeout=10, dataset_root=self.root)

    def test_content_reuse_across_extraction_paths_keeps_rank_and_detached_results(self):
        result = self.score()
        result[0] = 0
        self.assertEqual(self.score(), [.9] * 3)
        extracted = self.root / "another extraction"
        extracted.mkdir()
        paths = []
        for i, path in enumerate(self.paths):
            target = extracted / (str(i) + ".wav")
            shutil.copyfile(path, target)
            paths.append(str(target))
        ranked = reference.rank_reference_samples(paths, dataset_root=extracted)
        self.assertEqual(ranked, [(0, .9), (1, .9), (2, .9)])
        self.assertEqual(self.calls(), 1)
        self.assertEqual([Path(p).read_bytes() for p in paths], [Path(p).read_bytes() for p in self.paths])

    def test_simultaneous_same_content_is_single_flight(self):
        barrier = threading.Barrier(4)
        values, errors = [], []
        def run():
            try:
                barrier.wait()
                values.append(self.score())
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(values, [[.9] * 3] * 4)
        self.assertEqual(self.calls(), 1)

    def test_audio_worker_interpreter_package_and_model_mutations_invalidate(self):
        self.assertEqual(self.score(), [.9] * 3)
        for path in (Path(self.paths[0]), self.worker, self.python, self.metadata, self.asset):
            before = self.calls()
            with path.open("ab") as stream:
                stream.write(b"\n# changed identity\n")
            self.assertEqual(self.score(), [.9] * 3)
            self.assertEqual(self.calls(), before + 1)
            self.assertEqual(self.score(), [.9] * 3)
            self.assertEqual(self.calls(), before + 1)

    def test_failed_partial_nonfinite_or_changing_input_never_seed_cache(self):
        for mode in ("failed", "malformed", "partial", "nonfinite", "short", "mutated"):
            with self.subTest(mode=mode):
                reference._REFERENCE_SCORE_CACHE.clear()
                self.mode.write_text(mode)
                before = self.calls()
                self.score()
                self.score()
                self.assertEqual(self.calls(), before + 2)
                self.assertEqual(len(reference._REFERENCE_SCORE_CACHE), 0)

    def test_ttl_and_lru_bound_force_fresh_worker_without_disabling_containment(self):
        with patch.object(reference, "REFERENCE_SCORE_CACHE_LIMIT", 2):
            self.score()
            key = next(iter(reference._REFERENCE_SCORE_CACHE))
            reference._REFERENCE_SCORE_CACHE[key] = (time.monotonic() - reference.REFERENCE_SCORE_CACHE_TTL - 1, (.9,) * 3)
            self.score()
            self.assertEqual(self.calls(), 2)
            first = Path(self.paths[0]).read_bytes()
            for content in (b"version 2", b"version 3", first):
                Path(self.paths[0]).write_bytes(content)
                self.score()
                self.assertLessEqual(len(reference._REFERENCE_SCORE_CACHE), 2)
            self.assertEqual(self.calls(), 5)
            outside = self.root.parent / "outside.wav"
            before = self.calls()
            with self.assertRaisesRegex(ValueError, "outside"):
                self.score([(str(outside), self.paths[0])])
            self.assertEqual(self.calls(), before)


if __name__ == "__main__":
    unittest.main()
