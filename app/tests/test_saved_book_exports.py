"""Book loading must invalidate exports and exclude running/starting exporters."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import core
from project import CHAPTER_EXPORT_DIR
from routers import editor, scripts_library as library


class SavedBookExportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        (self.scripts / "book_b.json").write_text("[]")
        self.active = self.root / "annotated_script.json"
        self.active.write_text('[{"text":"old"}]')
        self.exports = [self.root / "cloned_audiobook.mp3", self.root / "audiobook.m4b",
                        self.root / "audacity_export.zip", self.root / CHAPTER_EXPORT_DIR]
        for path in self.exports[:3]:
            path.write_bytes(b"old book audio")
        self.exports[3].mkdir()
        (self.exports[3] / "chapter.mp3").write_bytes(b"old chapter")
        (self.exports[3] / "manifest.json").write_text(json.dumps(
            {"chapters": [{"file": "chapter.mp3"}]}))
        self.state = {name: {"running": False} for name in (
            "audio", "script", "review", "persona", "nicknames",
            "audacity_export", "m4b_export", "chapter_export")}
        for module in (library, editor):
            for name, value in (("DATA_DIR", str(self.root)),
                                ("AUDIOBOOK_PATH", str(self.exports[0])),
                                ("M4B_PATH", str(self.exports[1]))):
                self.stack.enter_context(patch.object(module, name, value, create=True))
        for name, value in (("SCRIPT_PATH", str(self.active)),
                            ("SCRIPTS_DIR", str(self.scripts)),
                            ("VOICE_CONFIG_PATH", str(self.root / "voice_config.json")),
                            ("CHUNKS_PATH", str(self.root / "chunks.json")),
                            ("process_state", self.state)):
            self.stack.enter_context(patch.object(library, name, value))
        self.stack.enter_context(patch.object(core, "process_state", self.state))
        self.stack.enter_context(patch.object(library, "_save_active_book_id"))
        self.stack.enter_context(patch.object(library, "_get_saved_book_id", return_value="b"))

    def load(self, name="book_b"):
        return asyncio.run(library.load_script(library.ScriptLoadRequest(name=name)))

    def test_load_removes_all_previous_book_downloads(self):
        self.load()
        self.assertEqual("[]", self.active.read_text())
        self.assertFalse(any(path.exists() for path in self.exports))
        for download in (editor.get_audiobook, editor.get_audiobook_m4b,
                         editor.export_zip, editor.get_audacity_export,
                         editor.download_chapters_zip,
                         lambda: editor.download_chapter("chapter.mp3")):
            with self.subTest(download=download), self.assertRaises(HTTPException) as error:
                asyncio.run(download())
            self.assertEqual(404, error.exception.status_code)
        self.assertEqual([], asyncio.run(editor.list_chapter_exports())["chapters"])

    def test_each_busy_writer_refuses_load_without_discarding_exports(self):
        for task in self.state:
            with self.subTest(task=task):
                self.state[task]["running"] = True
                try:
                    with self.assertRaises(HTTPException) as error:
                        self.load()
                    self.assertEqual(409, error.exception.status_code)
                    self.assertIn(task, error.exception.detail)
                    self.assertTrue(all(path.exists() for path in self.exports))
                    self.assertIn("old", self.active.read_text())
                finally:
                    self.state[task]["running"] = False

    def test_missing_book_preserves_exports(self):
        with self.assertRaises(HTTPException) as error:
            self.load("missing")
        self.assertEqual(404, error.exception.status_code)
        self.assertTrue(all(path.exists() for path in self.exports))

    def test_load_without_exports_succeeds(self):
        for path in self.exports[:3]:
            path.unlink()
        import shutil
        shutil.rmtree(self.exports[3])
        self.assertEqual("loaded", self.load()["status"])

    def test_invalidation_failure_aborts_before_active_script_mutation(self):
        real_remove = library.os.remove
        def remove(path):
            if str(path) == str(self.exports[1]):
                raise PermissionError("export is open")
            return real_remove(path)
        with patch.object(library.os, "remove", side_effect=remove):
            with self.assertRaises(PermissionError):
                self.load()
        self.assertIn("old", self.active.read_text())
        self.assertTrue(self.exports[1].exists())
        core.claim_gpu_task("audacity_export")  # Failed loads release the reservation mutex.

    def test_export_reservation_waits_until_book_switch_finishes(self):
        copying, release_copy, attempted, reserved = (threading.Event() for _ in range(4))
        mutex = threading.Lock()
        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "export-reservation":
                    attempted.set()
                mutex.acquire()
            def __exit__(self, *args):
                mutex.release()
        lock = ObservedLock()
        real_copy = library.shutil.copy2
        def copy(src, dst):
            if dst == str(self.active):
                copying.set()
                if not release_copy.wait(3):
                    raise RuntimeError("test did not release copy")
            return real_copy(src, dst)
        errors, observations = [], []
        def load():
            try:
                self.load()
            except Exception as error:
                errors.append(error)
        def reserve():
            try:
                core.claim_gpu_task("audacity_export")
                observations.append((self.active.read_text(), self.exports[0].exists()))
                reserved.set()
            except Exception as error:
                errors.append(error)
        with patch.object(core, "_gpu_lock", lock), \
             patch.object(library, "_gpu_lock", lock, create=True), \
             patch.object(library.shutil, "copy2", side_effect=copy):
            loader = threading.Thread(target=load)
            exporter = threading.Thread(target=reserve, name="export-reservation")
            loader.start()
            try:
                self.assertTrue(copying.wait(2))
                exporter.start()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(reserved.wait(0.1), "export reserved during book switch")
            finally:
                release_copy.set()
                loader.join(3)
                if exporter.ident is not None:
                    exporter.join(3)
        self.assertFalse(loader.is_alive() or exporter.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([("[]", False)], observations)
