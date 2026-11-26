"""A loaded saved script reaches the retained EPUB uploaded with its TXT (#2349).

The retained original EPUB is stored beside the extracted TXT in the upload
folder. Loading a saved script makes that script the selected source, so a
lookup that only checks beside the source misses the upload folder entirely
and read-aloud export wrongly reported the retained bytes as missing.
"""
import asyncio
import contextlib
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from pydub import AudioSegment

import core
import readalong_export
from project import ProjectManager
from routers import script as script_router
from routers import scripts_library
from tests.test_readalong_r4 import _make_source_epub_nested

CHAPTER_TEXTS = ["Chapter One", "Hello synthetic world.", "Second paragraph here.",
                 "Quoted line here.", "First list item."]


class SavedScriptRetainedEpubTests(unittest.TestCase):
    def test_loaded_saved_script_reaches_the_uploaded_retained_epub(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)
            self.assertTrue((root / "readaloud.epub").exists())
            receipt = json.loads((root / "readaloud.receipt.json").read_text())
            self.assertEqual(hashlib.sha256(epub_bytes).hexdigest(),
                             receipt["retained_epub_sha256"])

    def test_missing_retained_bytes_names_the_non_resetting_recovery(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            for path in Path(core.UPLOADS_DIR).glob("*.source.epub"):
                path.unlink()
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Retained EPUB bytes are missing", message)
            self.assertIn(".source.epub", message)
            self.assertIn("recorded EPUB origin", message)
            self.assertIn("do not re-import the book", message)
            self.assertIn("resets rendered audio", message)

    def test_different_retained_epub_is_not_mistaken_for_this_origin(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            uploads = Path(core.UPLOADS_DIR)
            for path in uploads.glob("*.source.epub"):
                path.unlink()
            (uploads / "other.source.epub").write_bytes(b"unrelated synthetic bytes")
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Retained EPUB bytes are missing", message)

    def test_duplicate_identical_retained_epubs_still_refuse_ambiguity(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            uploads = Path(core.UPLOADS_DIR)
            for path in uploads.glob("*.source.epub"):
                path.unlink()
            (uploads / "one.source.epub").write_bytes(epub_bytes)
            (uploads / "two.source.epub").write_bytes(epub_bytes)
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Multiple retained EPUB", message)

    def test_binding_named_canonical_epub_wins_over_an_identical_duplicate(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            uploads = Path(core.UPLOADS_DIR)
            canonical = self.only_retained_upload(root)
            self.assertEqual(epub_bytes, canonical.read_bytes())
            # Identical bytes that the old upload scan could not disambiguate.
            (uploads / "one.source.epub").write_bytes(epub_bytes)
            self.render_chunks(root)
            with patch("readalong_export.get_retained_epub_candidate_path",
                       side_effect=AssertionError(
                           "a verified binding hit must not scan uploads")):
                ok, message = readalong_export.build_epub_export(
                    ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)
            receipt = json.loads((root / "readaloud.receipt.json").read_text())
            self.assertEqual(hashlib.sha256(epub_bytes).hexdigest(),
                             receipt["retained_epub_sha256"])

    def test_binding_canonical_with_wrong_bytes_still_scans_for_the_match(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            canonical = self.only_retained_upload(root)
            canonical.write_bytes(b"replaced canonical bytes")
            (Path(core.UPLOADS_DIR) / "renamed.source.epub").write_bytes(epub_bytes)
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)
            receipt = json.loads((root / "readaloud.receipt.json").read_text())
            self.assertEqual(hashlib.sha256(epub_bytes).hexdigest(),
                             receipt["retained_epub_sha256"])

    def test_uploads_fallback_still_works_without_a_binding(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.assertEqual([], list(Path(core.SCRIPTS_DIR).glob(
                "*.selection_binding.json")))
            canonical = self.only_retained_upload(root)
            canonical.rename(canonical.with_name("renamed.source.epub"))
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)
            receipt = json.loads((root / "readaloud.receipt.json").read_text())
            self.assertEqual(hashlib.sha256(epub_bytes).hexdigest(),
                             receipt["retained_epub_sha256"])

    def test_damaged_or_changed_chain_falls_back_without_matching_unrelated_bytes(self):
        for damage in ("binding", "source"):
            with self.subTest(damage=damage), self.fixture() as (root, api, epub_bytes):
                self.upload_and_load(api, root, epub_bytes)
                self.bind_saved_script(root)
                uploads = Path(core.UPLOADS_DIR)
                if damage == "binding":
                    (Path(core.SCRIPTS_DIR) / "Saved.selection_binding.json").write_bytes(
                        b"{not a valid binding")
                else:
                    next(uploads.glob("*.txt")).write_bytes(b"changed extracted text")
                (uploads / "unrelated.source.epub").write_bytes(b"unrelated bytes")
                self.render_chunks(root)
                ok, message = readalong_export.build_epub_export(
                    ProjectManager(str(root)), lambda: False)
                self.assertTrue(ok, message)
                receipt = json.loads((root / "readaloud.receipt.json").read_text())
                self.assertEqual(hashlib.sha256(epub_bytes).hexdigest(),
                                 receipt["retained_epub_sha256"])

    # --- fixture ---------------------------------------------------------

    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            uploads = root / "uploads"; uploads.mkdir()
            scripts = root / "scripts"; scripts.mkdir()
            active = root / "annotated_script.json"
            voice = root / "voice_config.json"
            active.write_text(json.dumps([{"speaker": "A", "text": CHAPTER_TEXTS[1]}]))
            voice.write_text(json.dumps({"A": {"voice": "voice"}}))
            (root / "state.json").write_text(json.dumps({"active_book_id": "book"}))
            api = FastAPI()
            api.include_router(script_router.router)
            api.include_router(scripts_library.router)
            for module in (script_router, scripts_library, core):
                for key, value in (("DATA_DIR", root), ("UPLOADS_DIR", uploads),
                                   ("SCRIPTS_DIR", scripts), ("SCRIPT_PATH", active),
                                   ("VOICE_CONFIG_PATH", voice)):
                    if hasattr(module, key):
                        stack.enter_context(patch.object(module, key, str(value)))
            for module in (scripts_library, core):
                stack.enter_context(patch.object(module, "process_state", {}))
                for key, name in (("AUDIOBOOK_PATH", "book.mp3"), ("M4B_PATH", "book.m4b"),
                                  ("CHUNKS_PATH", "chunks.json")):
                    if hasattr(module, key):
                        stack.enter_context(patch.object(module, key, str(root / name)))
            yield root, api, _make_source_epub_nested()

    def only_retained_upload(self, root):
        retained = list((Path(core.UPLOADS_DIR)).glob("*.source.epub"))
        self.assertEqual(1, len(retained))
        return retained[0]

    def bind_saved_script(self, root):
        """Write the saved-book binding the shared resolver walks (#2356)."""
        from book_state_transaction import get_book_operation_token
        from script_selection_binding import make_script_selection_binding

        state = json.loads((root / "state.json").read_text())
        saved = Path(state["input_file_path"])
        extracted = next((Path(core.UPLOADS_DIR)).glob("*.txt"))
        binding = make_script_selection_binding(
            get_book_operation_token(str(root)),
            hashlib.sha256(extracted.read_bytes()).hexdigest(),
            saved.read_bytes(), source_path=str(extracted))
        (Path(core.SCRIPTS_DIR) / "Saved.selection_binding.json").write_text(
            json.dumps(binding))

    def upload_and_load(self, api, root, epub_bytes):
        async def go():
            transport = httpx.ASGITransport(app=api, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
                uploaded = await client.post(
                    "/api/upload", params={"select_active": "false"},
                    files={"file": ("Synthetic.epub", epub_bytes, "application/epub+zip")})
                self.assertEqual(200, uploaded.status_code, uploaded.text)
                txt = Path(uploaded.json()["path"])
                self.assertEqual(txt.parent, Path(core.UPLOADS_DIR))
                (root / "state.json").write_text(json.dumps({
                    "active_book_id": "book", "input_file_path": str(txt),
                    "book_generation": "generation-a"}))
                saved = await client.post("/api/scripts/save", json={"name": "Saved"})
                self.assertEqual(200, saved.status_code, saved.text)
                loaded = await client.post("/api/scripts/load", json={"name": "Saved"})
                self.assertEqual(200, loaded.status_code, loaded.text)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(str(Path(core.SCRIPTS_DIR) / "Saved.json"),
                             state["input_file_path"])
        asyncio.run(go())

    def render_chunks(self, root):
        rows = []
        for index, text in enumerate(CHAPTER_TEXTS):
            name = f"clip{index}.wav"
            segment = AudioSegment(data=b"\x00\x20" * 24000, sample_width=2,
                                   frame_rate=24000, channels=1)
            with segment.export(str(root / name), format="wav"):
                pass
            rows.append({"id": index, "uid": f"u{index}", "speaker": "A", "text": text,
                         "audio_path": name, "audio_revision": f"r{index}", "status": "done"})
        (root / "chunks.json").write_text(json.dumps(rows))
