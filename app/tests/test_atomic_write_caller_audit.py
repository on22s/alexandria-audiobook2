"""Every flipped writer must refuse a cross-device publish instead of copying.

We patch the shared writer's os.replace to raise EXDEV for staged JSON files
only, so unrelated renames inside the callers keep working. A writer that kept
the default fallback would copy over the destination non-atomically; these
tests require it to raise with shutil.move untouched and the old bytes intact.
The final test pins the deliberate exception: a recomputable cache still falls
back.
"""

import asyncio
import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import generation_checkpoint_deltas
import utils
import voice_config_store
from routers import voice_library as voice_library_router

_REAL_REPLACE = os.replace
_REAL_MOVE = shutil.move


def _raise_exdev_for_staged_json(src, dst, *args, **kwargs):
    if os.path.basename(os.fspath(src)).startswith(".tmp_"):
        raise OSError(errno.EXDEV, "cross-device staged JSON publish")
    return _REAL_REPLACE(src, dst, *args, **kwargs)


class AtomicWriteCallerAuditTests(unittest.TestCase):
    def _exdev(self):
        return (
            patch.object(utils.os, "replace", _raise_exdev_for_staged_json),
            patch.object(utils.shutil, "move", wraps=_REAL_MOVE),
        )

    def test_three_pass_checkpoint_writer_refuses_exdev_and_preserves_bytes(self):
        # GenerationCheckpointDeltas is the production three-pass checkpoint
        # writer; save_checkpoint is its public publication path.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            original = b'{"stage":"segment","saved":true}'
            path.write_bytes(original)
            writer = generation_checkpoint_deltas.GenerationCheckpointDeltas(str(path))
            replace_patch, move_patch = self._exdev()
            with replace_patch, move_patch as move:
                with self.assertRaises(OSError) as caught:
                    writer.save_checkpoint({"stage": "segment", "saved": False}, compact=True)
            self.assertEqual(errno.EXDEV, caught.exception.errno)
            move.assert_not_called()
            self.assertEqual(original, path.read_bytes())

    def test_voice_config_writer_refuses_exdev_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice_config.json"
            original = b'{\n  "ALICE": {"description": "before"}\n}'
            path.write_bytes(original)
            replace_patch, move_patch = self._exdev()
            with replace_patch, move_patch as move:
                with self.assertRaises(OSError) as caught:
                    voice_config_store.apply_voice_config_update(
                        str(path), lambda current: {**current, "ALICE": {"description": "after"}})
            self.assertEqual(errno.EXDEV, caught.exception.errno)
            move.assert_not_called()
            self.assertEqual(original, path.read_bytes())

    def test_voice_library_writer_refuses_exdev_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice_library.json"
            original = b'{"shared": {}, "casts": {}, "favorites": []}'
            path.write_bytes(original)
            replace_patch, move_patch = self._exdev()
            with patch.object(voice_library_router, "VOICE_LIBRARY_PATH", str(path)), \
                    patch.object(core, "VOICE_LIBRARY_PATH", str(path)), \
                    replace_patch, move_patch as move:
                with self.assertRaises(OSError) as caught:
                    asyncio.run(voice_library_router.voice_library_create_cast(
                        voice_library_router.CastCreateRequest(name="Test Cast")))
            self.assertEqual(errno.EXDEV, caught.exception.errno)
            move.assert_not_called()
            self.assertEqual(original, path.read_bytes())

    def test_llm_bench_concurrency_cache_refuses_exdev_and_preserves_config_bytes(self):
        # The cache data is disposable, but the target is the user's whole
        # config.json. llm_bench swallows OSError as best-effort, so the
        # observable contract is "config bytes unchanged, no copy fallback".
        import llm_bench

        def valid_find(_client, _model, _max, server_parallel_limit=None, on_measurement=None):
            if on_measurement is not None:
                on_measurement(1, 10.0, 0.01)
            return 2

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = b'{"llm_local": {"model_name": "keep"}, "review": {"mode": "keep"}}'
            path.write_bytes(original)
            replace_patch, move_patch = self._exdev()
            with patch.object(llm_bench, "find_optimal_concurrency", side_effect=valid_find), \
                    patch.object(llm_bench.lmstudio_settings, "get_gpu_name_and_backend",
                                 return_value=("fixture", "fixture")), \
                    replace_patch, move_patch as move:
                result = llm_bench.ensure_cached_or_benchmarked_concurrency(
                    str(path), "local", "http://localhost:1234/v1", "model", object(),
                    max_concurrency=2,
                    status={"available": True, "parallel": 8, "context_length": 8192})
            self.assertEqual(2, result)
            move.assert_not_called()
            self.assertEqual(original, path.read_bytes())

    def test_chapter_timing_cache_keeps_fallback_and_writes(self):
        import chapter_timing_cache as timing

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / timing.CACHE_FILENAME
            original = b'{"stale": true}'
            path.write_bytes(original)
            replace_patch, move_patch = self._exdev()
            with patch.object(timing, "_policy", return_value={"schema": 1}), \
                    patch.object(timing, "read_source_identity", return_value=None), \
                    patch.object(timing.tts, "ensure_audio_export_active"), \
                    replace_patch, move_patch as move:
                result = timing.ensure_chapter_timing_facts(
                    tmp, [{"audio_path": "clip.wav"}], lambda name: str(root / name),
                    None, None, None, ())
            move.assert_called_once()
            self.assertEqual(1, result.skipped)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"schema": 1}, document["body"]["policy"])


if __name__ == "__main__":
    unittest.main()
