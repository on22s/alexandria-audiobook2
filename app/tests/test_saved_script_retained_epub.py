"""A loaded saved script reaches the retained EPUB uploaded with its TXT (#2349).

The retained original EPUB is stored beside the extracted TXT in the upload
folder. Loading a saved script makes that script the selected source, so a
lookup that only checks beside the source misses the upload folder entirely
and read-aloud export wrongly reported the retained bytes as missing.

Also pins the shared verified lookup used by the build and both Editor sites
(#2355): the binding only supplies a candidate, the origin/receipt hash gates
its bytes, and the recovery text is accurate for both source kinds.
"""
import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from pydub import AudioSegment

import core
import epub_readaloud
import readalong_export
from project import ProjectManager
from routers import editor
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

    def test_missing_retained_bytes_without_a_binding_offers_only_the_restore(self):
        """A save with no source binding cannot recover through a re-upload.

        `save` without an active root binding writes no
        `.selection_binding.json`; the upload route then refuses the same
        chain and leaves the selection pending (#2355 L1). The advice must be
        the restore that actually works, and following it must rebuild.
        """
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.assertEqual([], list((Path(core.SCRIPTS_DIR)).glob(
                "*.selection_binding.json")))
            retained = self.only_retained_upload(root)
            kept = retained.read_bytes()
            retained.unlink()
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Retained EPUB bytes are missing", message)
            self.assertIn(".source.epub", message)
            self.assertIn("recorded EPUB origin", message)
            self.assertIn("source's folder or the upload folder", message)
            self.assertIn("The saved book has no original-source binding", message)
            self.assertIn("re-uploading the EPUB cannot reattach this saved book",
                          message)
            self.assertIn("Restore the original EPUB there as a matching "
                          "'.source.epub' file", message)
            self.assertNotIn("re-upload the byte-identical EPUB", message)
            self.assertNotIn("keeps rendered audio", message)
            # Follow the advice this message gives: restore the retained bytes.
            retained.write_bytes(kept)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)

    def test_missing_retained_bytes_with_a_verified_binding_offers_the_reupload(self):
        """A verified binding makes the byte-identical re-upload true (#2354).

        The re-upload behaviour (same book, all rendered rows kept) is pinned
        end to end in test_saved_book_source_reselection's reporter flow; here
        the restore half of the same message is followed to a green build.
        """
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            retained = self.only_retained_upload(root)
            kept = retained.read_bytes()
            retained.unlink()
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Retained EPUB bytes are missing", message)
            self.assertIn("re-upload the byte-identical EPUB", message)
            self.assertIn("keeps rendered audio when the saved script is unchanged",
                          message)
            self.assertIn("becomes the selected source instead of the saved script",
                          message)
            self.assertIn("different edition starts a new book and resets rendered audio",
                          message)
            self.assertNotIn("Do not re-import the book", message)
            self.assertNotIn("retained-upload list", message)
            retained.write_bytes(kept)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)

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

    # --- messages (#2355 L1/L5) ------------------------------------------

    def test_direct_txt_missing_message_offers_the_identical_reupload(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_direct_txt(api, root, epub_bytes)
            for path in Path(core.UPLOADS_DIR).glob("*.source.epub"):
                path.unlink()
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("in the source's folder", message)
            self.assertNotIn("or the upload folder", message)
            self.assertIn("re-upload the byte-identical EPUB", message)
            self.assertIn("reattached under the same book", message)
            self.assertIn("Re-uploading a different edition resets rendered audio",
                          message)
            self.assertNotIn("Do not re-import the book", message)
            self.assertNotIn("retained-upload list", message)

    def test_byte_identical_reupload_restores_the_retained_epub(self):
        """Pins the direct-TXT message's advice: the upload route reattaches."""
        with self.fixture() as (root, api, epub_bytes):
            self.upload_direct_txt(api, root, epub_bytes)
            self.render_chunks(root)
            before = json.loads((root / "state.json").read_text())
            self.only_retained_upload(root).unlink()
            reuploaded = self.post_upload(api, "Synthetic.epub", epub_bytes)
            self.assertEqual(200, reuploaded.status_code, reuploaded.text)
            self.assertTrue(reuploaded.json().get("reused"))
            self.assertTrue(reuploaded.json().get("retained_epub"))
            after = json.loads((root / "state.json").read_text())
            self.assertEqual(before["active_book_id"], after["active_book_id"])
            self.only_retained_upload(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)

    def test_direct_txt_outside_uploads_scans_only_its_own_folder(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_direct_txt(api, root, epub_bytes)
            moved = self.move_source_to_batch_dir(root)
            (Path(core.UPLOADS_DIR) / "duplicate.source.epub").write_bytes(epub_bytes)
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)
            receipt = json.loads((root / "readaloud.receipt.json").read_text())
            self.assertEqual(hashlib.sha256(epub_bytes).hexdigest(),
                             receipt["retained_epub_sha256"])
            for path in moved.parent.glob("*.source.epub"):
                path.unlink()
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("imports a new book and does not restore this one", message)
            self.assertNotIn("re-upload the byte-identical EPUB", message)

    def test_scan_directory_aliases_are_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "Uploads"
            primary.mkdir()
            extras = [str(primary), str(primary) + os.sep,
                      str(primary / ".." / "Uploads")]
            alias = root / "alias"
            try:
                alias.symlink_to(primary, target_is_directory=True)
            except OSError:
                pass
            else:
                extras += [str(alias), str(alias) + os.sep]
            directories = readalong_export._dedupe_retained_scan_dirs(
                str(primary) + os.sep, extras)
            self.assertEqual([str(primary) + os.sep], directories)

    def test_scan_key_folds_case_and_realpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upper = root / "Uploads"
            upper.mkdir()
            self.assertEqual(readalong_export._retained_scan_key(str(upper)),
                             readalong_export._retained_scan_key(str(upper) + os.sep))
            with patch.object(os.path, "normcase", side_effect=str.lower):
                self.assertEqual(os.path.realpath(str(upper)).lower(),
                                 readalong_export._retained_scan_key(str(upper)))
            # The dedupe honors the folding key, so a case variant is one scan.
            lower = root / "uploads"
            lower.mkdir()
            with patch.object(readalong_export, "_retained_scan_key",
                              side_effect=str.lower):
                directories = readalong_export._dedupe_retained_scan_dirs(
                    str(upper), [str(lower)])
            self.assertEqual([str(upper)], directories)

    def test_saved_source_resolution_refusal_logs_the_code_and_falls_back(self):
        from script_selection_binding import SavedSourceResolutionError
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            saved = json.loads((root / "state.json").read_text())["input_file_path"]
            with patch("script_selection_binding.resolve_saved_book_origin",
                       side_effect=SavedSourceResolutionError(
                           "missing_binding", "no binding")):
                with self.assertLogs("readalong_export", level="DEBUG") as logs:
                    self.assertIsNone(
                        readalong_export.get_saved_book_retained_epub_path(
                            str(root), saved))
            self.assertIn("missing_binding", "\n".join(logs.output))
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)

    def test_shared_lookup_never_returns_unverified_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "book.txt"
            source.write_bytes(b"source text")
            retained = root / "book.source.epub"
            retained.write_bytes(b"retained bytes")
            digest = hashlib.sha256(b"retained bytes").hexdigest()
            path, content = readalong_export.get_verified_retained_epub(
                str(source), digest, read_bytes=True)
            self.assertEqual(str(retained), path)
            self.assertEqual(b"retained bytes", content)
            path, content = readalong_export.get_verified_retained_epub(
                str(source), digest)
            self.assertEqual(str(retained), path)
            self.assertIsNone(content)
            with self.assertRaisesRegex(ValueError, "do not match"):
                readalong_export.get_verified_retained_epub(
                    str(source), hashlib.sha256(b"other").hexdigest())
            with self.assertRaisesRegex(ValueError, "no retained EPUB provenance"):
                readalong_export.get_verified_retained_epub(str(source), ())

    def test_stale_provenance_message_is_consistent_with_the_recovery_advice(self):
        with self.fixture() as (root, api, epub_bytes):
            txt = self.upload_direct_txt(api, root, epub_bytes)
            txt.write_bytes(b"changed source text")
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Source provenance is stale", message)
            self.assertIn("re-upload the byte-identical source file", message)
            self.assertIn("without creating a new book or resetting rendered audio",
                          message)
            self.assertNotIn("re-upload the source book", message)
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            saved = Path(json.loads((root / "state.json").read_text())["input_file_path"])
            original = saved.read_bytes()
            saved.write_bytes(original + b"\n")
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Source provenance is stale", message)
            self.assertIn("'.publication.json' companion no longer matches the saved "
                          "script", message)
            self.assertIn("Restore the saved script's original bytes", message)
            self.assertNotIn("missing '.publication.json' companion", message)
            self.assertNotIn("re-upload the byte-identical source book", message)
            # Follow the advice: restoring the script's original bytes rebuilds.
            saved.write_bytes(original)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)

    def test_old_save_without_companions_offers_only_the_restore(self):
        """A pre-companion save has no automatic recovery (#2355 L1 limits)."""
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.assertEqual([], list((Path(core.SCRIPTS_DIR)).glob(
                "*.selection_binding.json")))
            saved = Path(json.loads((root / "state.json").read_text())["input_file_path"])
            companion = saved.with_name(saved.name + ".publication.json")
            kept = companion.read_bytes()
            companion.unlink()
            self.render_chunks(root)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertFalse(ok)
            self.assertIn("Source provenance is stale", message)
            self.assertIn("the saved book has no '.publication.json' companion",
                          message)
            self.assertIn("The saved book has no original-source binding", message)
            self.assertIn("Restore the saved book's '.publication.json' companion "
                          "if you have a copy", message)
            self.assertIn("re-uploading the source cannot reattach this saved book",
                          message)
            self.assertNotIn("re-upload the byte-identical source book", message)
            self.assertNotIn("keeps rendered audio", message)
            # Follow the advice: restoring the companion (a backup here) rebuilds.
            companion.write_bytes(kept)
            ok, message = readalong_export.build_epub_export(
                ProjectManager(str(root)), lambda: False)
            self.assertTrue(ok, message)

    def test_uploads_folder_wording_uses_the_scanned_directory(self):
        """Message and scan share one uploads dir, never a re-imported global."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"; uploads.mkdir()
            other = root / "other"; other.mkdir()
            txt = uploads / "book.txt"; txt.write_bytes(b"synthetic source text")
            digest = hashlib.sha256(b"an unrelated retained file").hexdigest()
            # The global points elsewhere; only the scanned dir may decide the
            # wording (Rule 15).
            with patch.object(core, "UPLOADS_DIR", str(other)):
                with self.assertRaises(ValueError) as caught:
                    readalong_export.get_retained_epub_candidate_path(
                        str(txt), origin_hashes={digest},
                        uploads_dir=str(uploads))
                self.assertIn("re-upload the byte-identical EPUB",
                              str(caught.exception))
                self.assertNotIn("imports a new book", str(caught.exception))
                self.assertTrue(readalong_export._source_folder_is_uploads(
                    str(txt), uploads_dir=str(uploads)))
                self.assertFalse(readalong_export._source_folder_is_uploads(
                    str(txt), uploads_dir=str(other)))

    def test_saved_source_resolver_reads_poll_cancellation(self):
        """A cancelled build is observed during the walk, not after it."""
        from audio_generation_owner import AudioGenerationCancelled
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            saved = json.loads((root / "state.json").read_text())["input_file_path"]
            polls = []

            def cancel():
                polls.append(None)
                return len(polls) >= 2

            with self.assertRaises(AudioGenerationCancelled):
                readalong_export.get_saved_book_retained_epub_path(
                    str(root), saved, cancel_check=cancel)
            self.assertGreaterEqual(len(polls), 2,
                                    "cancellation must be polled inside the read")

    def test_every_saved_source_hop_read_polls_cancellation(self):
        """Cancelling during the script, binding or terminal read aborts the walk.

        The poll-every-read behaviour is per call site (#2355 N1): dropping
        ``cancel_check`` from any one of the three reads must fail this test.
        """
        import project_scope_files
        from audio_generation_owner import AudioGenerationCancelled
        from script_selection_binding import resolve_saved_book_origin
        real_capture = project_scope_files.capture_scope_file
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            state = json.loads((root / "state.json").read_text())
            saved = Path(state["input_file_path"])
            terminal = next(Path(core.UPLOADS_DIR).glob("*.txt"))
            hops = {
                "script": saved,
                "binding": Path(core.SCRIPTS_DIR) / "Saved.selection_binding.json",
                "terminal": terminal,
            }

            def active_capture(target):
                active = {"relative": None}

                def capture(capture_root, relative, **kwargs):
                    previous = active["relative"]
                    active["relative"] = os.path.normpath(relative)
                    try:
                        return real_capture(capture_root, relative, **kwargs)
                    finally:
                        active["relative"] = previous

                def cancel():
                    return active["relative"] == os.path.normpath(
                        os.path.relpath(str(target), str(root)))

                return capture, cancel

            for name, target in hops.items():
                with self.subTest(hop=name):
                    capture, cancel = active_capture(target)
                    with patch.object(project_scope_files, "capture_scope_file",
                                      new=capture):
                        with self.assertRaises(AudioGenerationCancelled):
                            resolve_saved_book_origin(
                                str(root), str(saved), cancel_check=cancel)

    # --- Editor call sites (#2355 L2/L5) ---------------------------------

    def test_download_site_uses_the_binding_and_gates_on_the_receipt(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            self.render_chunks(root)
            receipt = self.build_readaloud(root)
            uploads = Path(core.UPLOADS_DIR)
            canonical = self.only_retained_upload(root)
            duplicate = uploads / "one.source.epub"
            duplicate.write_bytes(epub_bytes)
            with self.editor_data_dir(root), \
                    patch.object(readalong_export, "get_retained_epub_candidate_path",
                                 side_effect=AssertionError(
                                     "a verified binding hit must not scan uploads")):
                checked, raw = editor._validated_epub_bytes()
            self.assertEqual(receipt["retained_epub_sha256"],
                             checked["retained_epub_sha256"])
            self.assertEqual(receipt["epub_sha256"], hashlib.sha256(raw).hexdigest())
            duplicate.unlink()
            with self.editor_data_dir(root):
                # A wrong canonical falls through to the renamed match.
                canonical.write_bytes(b"replaced canonical bytes")
                (uploads / "renamed.source.epub").write_bytes(epub_bytes)
                checked, _raw = editor._validated_epub_bytes()
                self.assertEqual(receipt["retained_epub_sha256"],
                                 checked["retained_epub_sha256"])
                # A wrong canonical with no matching copy is refused, never
                # accepted on the binding's word.
                (uploads / "renamed.source.epub").unlink()
                with self.assertRaises(HTTPException) as caught:
                    editor._validated_epub_bytes()
                self.assertEqual(409, caught.exception.status_code)
                self.assertIn("retained source is unavailable", caught.exception.detail)

    def test_timing_site_hash_gates_the_binding_candidate(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            self.render_chunks(root)
            receipt = dict(self.build_readaloud(root), timing_mode="qwen",
                           timing_device="cpu", timing_language="English")
            capability = {"devices": [{"device": "cpu"}], "device": "cpu",
                          "dtype": "float16", "languages": ["English"]}
            policy = {"timing_mode": "qwen", "device": "cpu"}
            reads = []
            real_language = epub_readaloud.get_epub_language_from_path

            def record(path, *args, **kwargs):
                reads.append(str(path))
                return real_language(path, *args, **kwargs)

            uploads = Path(core.UPLOADS_DIR)
            canonical = self.only_retained_upload(root)
            with self.editor_data_dir(root), \
                    patch.object(editor, "_get_readaloud_timing_preference",
                                 return_value={"timing_mode": "auto",
                                               "timing_device": "auto",
                                               "timing_language": None}), \
                    patch("qwen_runtime.get_qwen_capability",
                          return_value=capability), \
                    patch("qwen_runtime.get_qwen_device_policy",
                          return_value=policy), \
                    patch("qwen_runtime.get_qwen_model_candidates",
                          return_value=[]), \
                    patch("epub_readaloud.get_epub_language_from_path",
                          side_effect=record):
                (uploads / "one.source.epub").write_bytes(epub_bytes)
                with patch.object(readalong_export,
                                  "get_retained_epub_candidate_path",
                                  side_effect=AssertionError(
                                      "a verified binding hit must not scan uploads")):
                    with self.assertRaises(HTTPException) as caught:
                        editor._require_readaloud_timing_current(receipt)
                self.assertNotIn("malformed", caught.exception.detail)
                self.assertEqual([str(canonical)], reads)
                # Wrong canonical bytes fall back to the renamed good copy;
                # trusting the binding would read the wrong language.
                (uploads / "one.source.epub").unlink()
                canonical.write_bytes(b"replaced canonical bytes")
                (uploads / "renamed.source.epub").write_bytes(epub_bytes)
                reads.clear()
                with self.assertRaises(HTTPException) as caught:
                    editor._require_readaloud_timing_current(receipt)
                self.assertNotIn("malformed", caught.exception.detail)
                self.assertEqual([str(uploads / "renamed.source.epub")], reads)

    def test_timing_missing_receipt_digest_does_not_repeat_rebuild(self):
        """A receipt without the digest gets one rebuild instruction, not two."""
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            self.render_chunks(root)
            receipt = dict(self.build_readaloud(root), timing_mode="qwen",
                           timing_device="cpu", timing_language="English")
            receipt.pop("retained_epub_sha256", None)
            capability = {"devices": [{"device": "cpu"}], "device": "cpu",
                          "dtype": "float16", "languages": ["English"]}
            policy = {"timing_mode": "qwen", "device": "cpu"}
            with self.editor_data_dir(root), \
                    patch.object(editor, "_get_readaloud_timing_preference",
                                 return_value={"timing_mode": "auto",
                                               "timing_device": "auto",
                                               "timing_language": None}), \
                    patch("qwen_runtime.get_qwen_capability",
                          return_value=capability), \
                    patch("qwen_runtime.get_qwen_device_policy",
                          return_value=policy), \
                    patch("qwen_runtime.get_qwen_model_candidates",
                          return_value=[]):
                with self.assertRaises(HTTPException) as caught:
                    editor._require_readaloud_timing_current(receipt)
            self.assertEqual(409, caught.exception.status_code)
            detail = caught.exception.detail
            self.assertIn("has no retained EPUB digest", detail)
            self.assertEqual(1, detail.lower().count("rebuild it."), detail)

    def test_download_missing_message_names_a_usable_recovery(self):
        with self.fixture() as (root, api, epub_bytes):
            self.upload_and_load(api, root, epub_bytes)
            self.bind_saved_script(root)
            self.render_chunks(root)
            self.build_readaloud(root)
            for path in Path(core.UPLOADS_DIR).glob("*.source.epub"):
                path.unlink()
            with self.editor_data_dir(root):
                with self.assertRaises(HTTPException) as caught:
                    editor._validated_epub_bytes()
            self.assertEqual(409, caught.exception.status_code)
            self.assertIn("Retained EPUB bytes are missing", caught.exception.detail)
            self.assertIn("re-upload the byte-identical EPUB",
                          caught.exception.detail)
            self.assertIn("starts a new book and resets rendered audio",
                          caught.exception.detail)
            self.assertNotIn("Do not re-import the book", caught.exception.detail)
            self.assertNotIn("retained-upload list", caught.exception.detail)
            self.assertNotIn("Re-upload the EPUB and rebuild", caught.exception.detail)

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

    def post_upload(self, api, name, epub_bytes, *, select_active=False):
        async def go():
            transport = httpx.ASGITransport(app=api, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://fixture") as client:
                return await client.post(
                    "/api/upload",
                    params={"select_active": "true" if select_active else "false"},
                    files={"file": (name, epub_bytes, "application/epub+zip")})
        return asyncio.run(go())

    def upload_direct_txt(self, api, root, epub_bytes):
        """Upload without save/load: the selected source stays the TXT."""
        uploaded = self.post_upload(api, "Synthetic.epub", epub_bytes)
        self.assertEqual(200, uploaded.status_code, uploaded.text)
        txt = Path(uploaded.json()["path"])
        self.assertEqual(txt.parent, Path(core.UPLOADS_DIR))
        (root / "state.json").write_text(json.dumps({
            "active_book_id": "book", "input_file_path": str(txt),
            "book_generation": "generation-a"}))
        return txt

    def move_source_to_batch_dir(self, root):
        uploads = Path(core.UPLOADS_DIR)
        txt = next(uploads.glob("*.txt"))
        retained = self.only_retained_upload(root)
        batch = root / "batch"
        batch.mkdir()
        moved = batch / txt.name
        txt.rename(moved)
        (uploads / (txt.name + ".publication.json")).rename(
            batch / (txt.name + ".publication.json"))
        retained.rename(batch / "renamed.source.epub")
        (root / "state.json").write_text(json.dumps({
            "active_book_id": "book", "input_file_path": str(moved),
            "book_generation": "generation-a"}))
        return moved

    def build_readaloud(self, root):
        ok, message = readalong_export.build_epub_export(
            ProjectManager(str(root)), lambda: False)
        self.assertTrue(ok, message)
        return json.loads((root / "readaloud.receipt.json").read_text())

    @contextlib.contextmanager
    def editor_data_dir(self, root):
        root = Path(root)
        with patch.object(editor, "DATA_DIR", str(root)), \
                patch.object(editor, "UPLOADS_DIR", str(root / "uploads")), \
                patch.object(editor, "SCRIPTS_DIR", str(root / "scripts")), \
                patch.object(editor, "ROOT_DIR", str(root)), \
                patch.object(editor, "AUDIOBOOK_PATH",
                             str(root / "cloned_audiobook.mp3")), \
                patch.object(editor, "project_manager",
                             ProjectManager(str(root))), \
                patch.object(editor, "process_state", {}):
            yield

    def bind_saved_script(self, root):
        """Write the saved-book and active bindings the resolver walks (#2356).

        A real save copies the active binding beside the saved script and a
        real load installs the active binding for the loaded script. Both are
        written here so the upload route's identical-source rebind is
        exercised exactly as the routes leave it.
        """
        from book_state_transaction import get_book_operation_token
        from script_selection_binding import (BINDING_NAME,
                                              make_script_selection_binding)

        state = json.loads((root / "state.json").read_text())
        saved = Path(state["input_file_path"])
        extracted = next((Path(core.UPLOADS_DIR)).glob("*.txt"))
        token = get_book_operation_token(str(root))
        binding = make_script_selection_binding(
            token, hashlib.sha256(extracted.read_bytes()).hexdigest(),
            saved.read_bytes(), source_path=str(extracted))
        (Path(core.SCRIPTS_DIR) / "Saved.selection_binding.json").write_text(
            json.dumps(binding))
        active = make_script_selection_binding(
            token, hashlib.sha256(saved.read_bytes()).hexdigest(),
            saved.read_bytes(), source_path=str(saved))
        (Path(root) / BINDING_NAME).write_text(json.dumps(active))

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
