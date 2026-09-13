"""Merge cancel, export progress, export zip, and the LLM model picker."""
import asyncio
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import HTTPException

import project as project_module
from project import ProjectManager
from routers import editor as editor_module
from routers import system as system_module


def _three_chunks(tmp):
    chunks = []
    for i in range(3):
        path = os.path.join(tmp, f"line{i}.wav")
        open(path, "wb").close()
        chunks.append({"audio_path": f"line{i}.wav", "speaker": "Narrator", "text": f"t{i}."})
    return chunks


class MergeCancelTests(unittest.TestCase):
    def test_cancel_before_load_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            with patch.object(pm, "load_chunks", return_value=_three_chunks(tmp)), \
                 patch("project.AudioSegment.from_file") as load:
                ok, msg = pm.merge_audio(cancel_check=lambda: True)
            self.assertEqual((False, "Merge cancelled"), (ok, msg))
            load.assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(tmp, "cloned_audiobook.mp3")))

    def test_cancel_mid_load_stops_at_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            calls = []

            def cancel_after_two():
                calls.append(1)
                return len(calls) > 2
            with patch.object(pm, "load_chunks", return_value=_three_chunks(tmp)), \
                 patch("project.AudioSegment.from_file", return_value="audio") as load:
                ok, msg = pm.merge_audio(cancel_check=cancel_after_two)
            self.assertEqual((False, "Merge cancelled"), (ok, msg))
            self.assertEqual(2, load.call_count)

    def test_audacity_cancel_reports_export_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            with patch.object(pm, "load_chunks", return_value=_three_chunks(tmp)):
                ok, msg = pm.export_audacity(cancel_check=lambda: True)
            self.assertEqual((False, "Export cancelled"), (ok, msg))

    def test_no_cancel_check_keeps_old_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            with patch.object(pm, "load_chunks", return_value=[]):
                self.assertEqual((False, "No audio segments found"), pm.merge_audio())


class ExportProgressTests(unittest.TestCase):
    def test_loading_progress_reports_every_fifty_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            chunks = [{"audio_path": "missing.wav"} for _ in range(120)]
            seen = []
            with patch.object(pm, "load_chunks", return_value=chunks):
                pm._load_chunks_with_audio(
                    progress_callback=project_module._loading_progress(seen.append))
            self.assertEqual(["Loading audio 50/120", "Loading audio 100/120"], seen)

    def test_loading_progress_is_none_without_callback(self):
        self.assertIsNone(project_module._loading_progress(None))


class ExportZipRouteTests(unittest.TestCase):
    def test_404_when_nothing_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(editor_module, "AUDIOBOOK_PATH", os.path.join(tmp, "a.mp3")), \
                 patch.object(editor_module, "M4B_PATH", os.path.join(tmp, "b.m4b")):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(editor_module.export_zip())
        self.assertEqual(404, ctx.exception.status_code)

    def test_zip_holds_only_the_files_that_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = os.path.join(tmp, "a.mp3")
            with open(mp3, "wb") as fh:
                fh.write(b"MP3BYTES")
            with patch.object(editor_module, "AUDIOBOOK_PATH", mp3), \
                 patch.object(editor_module, "M4B_PATH", os.path.join(tmp, "b.m4b")):
                resp = asyncio.run(editor_module.export_zip())
            async def drain():
                return b"".join([c async for c in resp.body_iterator])
            body = asyncio.run(drain())
        self.assertEqual("application/zip", resp.media_type)
        self.assertIn("alexandria_export.zip", resp.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertEqual(["audiobook.mp3"], zf.namelist())
            self.assertEqual(b"MP3BYTES", zf.read("audiobook.mp3"))


class _FakeModels:
    def __init__(self, ids):
        self._ids = ids

    def list(self):
        return type("R", (), {"data": [type("M", (), {"id": i})() for i in self._ids]})()


class _FakeClient:
    made = []

    def __init__(self, config, timeout, respect_profile_timeout=True):
        _FakeClient.made.append((config, timeout))
        self.models = _FakeModels(["zeta", "alpha", "alpha"])


class LlmModelsRouteTests(unittest.TestCase):
    def test_model_listing_accepts_the_key_only_in_a_post_body(self):
        routes = [route for route in system_module.router.routes
                  if route.path == "/api/llm/models"]
        self.assertEqual(1, len(routes))
        self.assertEqual({"POST"}, routes[0].methods)

    def test_lists_sorted_unique_ids_and_appends_v1(self):
        _FakeClient.made.clear()
        with patch("llm_provider.make_llm_client", _FakeClient):
            result = asyncio.run(system_module.llm_models(
                system_module.LlmModelsRequest(base_url="http://h:1234/", api_key="k")))
        self.assertEqual({"models": ["alpha", "zeta"]}, result)
        self.assertEqual("http://h:1234/v1", _FakeClient.made[0][0]["base_url"])
        self.assertEqual("k", _FakeClient.made[0][0]["api_key"])

    def test_unreachable_endpoint_is_reported_not_raised(self):
        def boom(*a, **k):
            raise ConnectionError("refused")
        with patch("llm_provider.make_llm_client", boom):
            result = asyncio.run(system_module.llm_models(
                system_module.LlmModelsRequest(base_url="http://h:1234/v1")))
        self.assertEqual([], result["models"])
        self.assertIn("refused", result["error"])

    def test_blank_base_url_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(system_module.llm_models(system_module.LlmModelsRequest(base_url="  ")))
        self.assertEqual(400, ctx.exception.status_code)


if __name__ == "__main__":
    unittest.main()
