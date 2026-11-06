"""Service-level tests for app/calibre_import.py (issue #1734, slice 3).

Real HTTP (raw-socket servers from test_calibre_opds) + real catalog service
with a synthetic injected keyring; the real upload authority is invoked for
the happy path (only its DATA_DIR/UPLOADS_DIR are pointed at a tmp dir).
No live catalog, no private books.
"""
import asyncio
import hashlib
import io
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import calibre_import as importer
import calibre_catalog as cat
import calibre_opds as opds
from tests.test_calibre_opds import feed_xml, start_server, stop_server
from tests.test_calibre_catalog import FakeKeyring

USER, PW = "synthetic-user", "synthetic-pass"


def build_epub(body_text="Synthetic chapter sentence. " * 50, extra_members=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?>'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("book.opf",
                   '<?xml version="1.0"?>'
                   '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>Injected OPF Title</dc:title><dc:creator>Injected OPF Author</dc:creator>'
                   '<dc:language>en</dc:language>'
                   '<dc:identifier id="BookID">urn:synthetic:opf-1</dc:identifier></metadata>'
                   '<manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="body"/></spine></package>')
        z.writestr("body.xhtml",
                   '<?xml version="1.0"?>'
                   '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>' + body_text + '</p></body></html>')
        for name, data in (extra_members or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


EPUB_SMALL = build_epub()
_FILLER = b"".join(hashlib.sha256(f"filler-{i}".encode()).digest() for i in range(3000))
EPUB_BIG = build_epub(extra_members={"filler.bin": _FILLER})  # > 64 KiB, incompressible
EPUB_HEADERS = {"Content-Type": "application/epub+zip"}


class ImportMixin(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data_dir = self._td.name
        self.server = start_server()
        self.addCleanup(stop_server, self.server)
        self.origin = self.server.origin
        self.vault = FakeKeyring()
        self.catalog = cat.CalibreCatalogService(
            self.data_dir, limit=opds.Limits(timeout_seconds=10),
            keyring_factory=lambda: self.vault)
        self.service = importer.CalibreImportService(
            self.data_dir, catalog=self.catalog, max_upload_bytes=512 * 1024 ** 2)
        self.server.route("/opds", feed_xml(
            "F", [("1", "One", "Synthetic Author", "S", "1", "1.epub",
                   "application/epub+zip")]))
        self.server.route("/1.epub", EPUB_BIG, headers=dict(EPUB_HEADERS))

    def browse_preview(self):
        self.catalog.save_config(f"{self.origin}/opds", username=USER, password=PW)
        page = self.catalog.browse_root_page()
        return page["entries"][0]["preview_token"]

    def wait_captured(self, job_token, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.service.get_capture_status(job_token)
            if status["status"] in ("captured", "failed", "cancelled",
                                    "discarded", "stale", "review_required"):
                return status
            time.sleep(0.05)
        raise AssertionError(f"capture did not finish: {status}")

    def staging_files(self):
        staging = Path(self.data_dir) / "calibre_staging"
        return [p.name for p in staging.iterdir()] if staging.is_dir() else []


class CaptureValidateTests(ImportMixin):
    def test_big_epub_byte_identity_and_real_authority_import(self):
        import routers.script as script
        uploads = Path(self.data_dir) / "uploads"
        uploads.mkdir()
        self.assertGreater(len(EPUB_BIG), 64 * 1024)
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "captured")
        self.assertEqual(status["sha256"], hashlib.sha256(EPUB_BIG).hexdigest())
        self.assertEqual(status["size_bytes"], len(EPUB_BIG))
        self.assertEqual(status["provider"]["title"], "One")
        self.assertEqual(status["provider"]["authors"], ["Synthetic Author"])
        self.assertEqual(status["source_identity"]["stable_id"], "urn:synthetic:1")

        def uploader(raw_bytes, synthetic):
            async def run():
                from fastapi import UploadFile
                handle = io.BytesIO(raw_bytes)
                try:
                    return await script.upload_file(
                        file=UploadFile(file=handle, filename=synthetic),
                        select_active=False, include_extraction_report=True,
                        docx_preview=False, docx_confirmed=False,
                        docx_preview_digest=None)
                finally:
                    handle.close()
            return asyncio.run(run())

        with mock.patch.object(script, "UPLOADS_DIR", str(uploads)), \
             mock.patch.object(script, "DATA_DIR", self.data_dir):
            result = self.service.import_confirmed(
                status["confirmation_token"], status["sha256"], uploader)
        upload = result["upload"]
        self.assertTrue(upload["stored_filename"].endswith(".txt"))
        self.assertIn("extraction_report", upload)
        txt = (uploads / upload["stored_filename"]).read_text(encoding="utf-8")
        self.assertIn("Synthetic chapter sentence.", txt)
        # Provenance companion records the exact captured archive digest.
        companions = [p for p in uploads.iterdir()
                      if p.name != upload["stored_filename"]]
        self.assertTrue(companions)
        self.assertIn(hashlib.sha256(EPUB_BIG).hexdigest(),
                      b"".join(p.read_bytes() for p in companions).decode("utf-8", "replace"))
        self.assertEqual(result["source_identity"]["final_url"],
                         f"{self.origin}/1.epub")
        self.assertEqual(self.staging_files(), [])  # staged original consumed

    def test_malformed_pk_download_fails_cleanly(self):
        self.server.route("/1.epub", b"PK\x03\x04" + b"not a zip" * 200,
                          headers=dict(EPUB_HEADERS))
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "failed")
        self.assertIsNone(status["confirmation_token"])
        self.assertEqual(self.staging_files(), [])

    def test_validate_rejects_pk_only_and_missing_package(self):
        for raw in (b"PK\x03\x04" + b"x" * 100,
                    build_epub(extra_members={})[:-10]):
            with self.assertRaises(importer.ImportRefused):
                importer._validate_epub_bytes(raw)

    def test_validate_rejects_encryption_xml_and_encrypted_members(self):
        with self.assertRaises(importer.ImportRefused):
            importer._validate_epub_bytes(build_epub(
                extra_members={"META-INF/encryption.xml": "<encryption/>"}))
        # Set the encryption flag on every member header (local + central).
        raw = bytearray(EPUB_SMALL)
        for marker in (b"PK\x03\x04", b"PK\x01\x02"):
            start = 0
            while True:
                idx = raw.find(marker, start)
                if idx < 0:
                    break
                raw[idx + (6 if marker == b"PK\x03\x04" else 8)] |= 0x01
                start = idx + 4
        with self.assertRaises(importer.ImportRefused):
            importer._validate_epub_bytes(bytes(raw))

    def test_validate_rejects_zipbomb_ratio(self):
        raw = build_epub(extra_members={"filler.bin": b"A" * (3 * 1024 * 1024)})
        with self.assertRaises(importer.ImportRefused):
            importer._validate_epub_bytes(raw)


class TamperLifecycleTests(ImportMixin):
    def test_tampered_staged_file_refused_before_authority(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        staged = self.staging_files()[0]
        (Path(self.data_dir) / "calibre_staging" / staged).write_bytes(
            b"PK\x03\x04" + b"replaced" * 500)
        uploaded = []
        with self.assertRaises(importer.ImportRefused):
            self.service.import_confirmed(
                status["confirmation_token"], status["sha256"],
                lambda raw, name: uploaded.append(name))
        self.assertEqual(uploaded, [])

    def test_wrong_digest_then_duplicate_import_refused(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        with self.assertRaises(importer.ImportRefused):
            self.service.import_confirmed(
                status["confirmation_token"], "0" * 64,
                lambda raw, name: {"stored_filename": "x.txt"})
        result = self.service.import_confirmed(
            status["confirmation_token"], status["sha256"],
            lambda raw, name: {"stored_filename": "x.txt"})
        self.assertEqual(result["upload"]["stored_filename"], "x.txt")
        with self.assertRaises(importer.ImportRefused):
            self.service.import_confirmed(
                status["confirmation_token"], status["sha256"],
                lambda raw, name: {"stored_filename": "y.txt"})

    def test_concurrent_capture_refused_while_live(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        with self.assertRaises(importer.ImportRefused):
            self.service.start_capture(token)
        self.service.discard_capture(status["confirmation_token"])
        job2 = self.service.start_capture(token)
        self.assertEqual(self.wait_captured(job2)["status"], "captured")
        self.service.discard_capture(
            self.service.get_capture_status(job2)["confirmation_token"])

    def test_cancel_during_dribble_cleans_up(self):
        self.server.route_dribble(
            "/1.epub", total=2 * 1024 * 1024, interval=0.02,
            content_type="application/epub+zip",
            prefix=b"PK\x03\x04" + b"s" * 60)
        token = self.browse_preview()
        job = self.service.start_capture(token)
        self.service.cancel_capture(job)
        status = self.wait_captured(job, timeout=20.0)
        self.assertEqual(status["status"], "cancelled")
        self.assertIsNone(status["confirmation_token"])
        self.assertEqual(self.staging_files(), [])

    def test_missing_password_refused(self):
        token = self.browse_preview()  # preview valid while password exists
        self.vault.store.clear()  # vault entry removed out from under us
        with self.assertRaises(cat.CatalogError) as ctx:
            self.catalog.get_capture_binding(token)
        self.assertIn("password", str(ctx.exception))
        with self.assertRaises(cat.CatalogError):
            self.service.start_capture(token)

    def test_config_change_stales_pending_capture(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "captured")
        self.catalog.save_config(f"{self.origin}/opds2", username="")
        self.service.on_config_changed()
        with self.assertRaises(importer.ImportRefused):
            self.service.import_confirmed(
                status["confirmation_token"], status["sha256"],
                lambda raw, name: {"stored_filename": "x.txt"})
        self.assertEqual(self.staging_files(), [])

    def test_ttl_expires_unconfirmed_capture(self):
        now = [time.monotonic()]
        service = importer.CalibreImportService(
            self.data_dir, catalog=self.catalog, max_upload_bytes=512 * 1024 ** 2,
            ttl=5.0, clock=lambda: now[0])
        self.service = service
        token = self.browse_preview()
        job = service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "captured")
        now[0] += 10.0  # advance past TTL without waiting
        with self.assertRaises(importer.ImportRefused):
            service.discard_capture(status["confirmation_token"])
        self.assertEqual(self.staging_files(), [])


class CrossOriginTests(ImportMixin):
    def test_exact_approval_credentials_stay_on_origin_a(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        self.server.route("/opds", feed_xml(
            "F", [("1", "One", "Synthetic Author", "S", "1",
                   f"{other.origin}/book.epub", "application/epub+zip")]))
        other.route("/book.epub", EPUB_BIG, headers=dict(EPUB_HEADERS))
        token = self.browse_preview()
        review = self.catalog.request_capture_review(token)
        self.assertTrue(review["review_required"])
        self.assertNotIn("origin_b", str(review))  # opaque token only
        approved = self.catalog.approve_capture_review(review["review_token"])
        self.assertTrue(approved["approved"])
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "captured")
        self.assertEqual(other.seen_auth, [("/book.epub", None)])
        self.assertEqual(self.server.seen_auth[0][0], "/opds")
        self.assertEqual(self.server.seen_auth[0][1],
                         opds.OpdsAuth(USER, PW).header_value())
        self.service.discard_capture(status["confirmation_token"])



class SnapshotTests(ImportMixin):
    def test_path_swap_after_verify_publishes_confirmed_snapshot(self):
        import routers.script as script
        uploads = Path(self.data_dir) / "uploads"
        uploads.mkdir()
        prior = uploads / "prior.txt"
        prior.write_text("Prior synthetic book text.", encoding="utf-8")
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "captured")
        sha_a = status["sha256"]
        staged = Path(self.data_dir) / "calibre_staging" / self.staging_files()[0]
        epub_b = build_epub(body_text="COMPLETELY DIFFERENT BODY " * 200)
        sha_b = hashlib.sha256(epub_b).hexdigest()
        self.assertNotEqual(sha_a, sha_b)
        seen_raw = []

        def uploader(raw_bytes, synthetic):
            seen_raw.append(raw_bytes)
            staged.write_bytes(epub_b)

            async def run():
                from fastapi import UploadFile
                handle = io.BytesIO(raw_bytes)
                try:
                    return await script.upload_file(
                        file=UploadFile(file=handle, filename=synthetic),
                        select_active=False, include_extraction_report=True,
                        docx_preview=False, docx_confirmed=False,
                        docx_preview_digest=None)
                finally:
                    handle.close()
            return asyncio.run(run())

        with mock.patch.object(script, "UPLOADS_DIR", str(uploads)), \
             mock.patch.object(script, "DATA_DIR", self.data_dir):
            result = self.service.import_confirmed(
                status["confirmation_token"], sha_a, uploader)
        self.assertEqual(hashlib.sha256(seen_raw[0]).hexdigest(), sha_a)
        self.assertEqual(result["sha256"], sha_a)
        self.assertEqual(result["provider"]["title"], "One")
        txt = (uploads / result["upload"]["stored_filename"]).read_text(encoding="utf-8")
        self.assertIn("Synthetic chapter sentence.", txt)
        self.assertNotIn("COMPLETELY DIFFERENT BODY", txt)
        companions = [p for p in uploads.iterdir()
                      if p.name != result["upload"]["stored_filename"]]
        combined = b"".join(p.read_bytes() for p in companions).decode("utf-8", "replace")
        self.assertIn(sha_a, combined)
        self.assertNotIn(sha_b, combined)
        self.assertEqual(prior.read_text(encoding="utf-8"), "Prior synthetic book text.")
        self.assertEqual(self.staging_files(), [])


class PreviewPurityTests(ImportMixin):
    def test_preview_is_pure_opds_metadata_zero_requests(self):
        token = self.browse_preview()
        before = list(self.server.seen_paths)
        preview = self.catalog.get_metadata_preview(token)
        self.assertEqual(preview["provider"]["title"], "One")
        self.assertEqual(preview["provider"]["authors"], ["Synthetic Author"])
        self.assertEqual(preview["provider"]["series"], "S")
        self.assertEqual(preview["stable_id"], "urn:synthetic:1")
        self.assertEqual(preview["user_editable"]["title"], "One")
        preview["user_editable"]["title"] = "Edited"
        again = self.catalog.get_metadata_preview(token)
        self.assertEqual(again["provider"]["title"], "One")
        self.assertEqual(again["user_editable"]["title"], "One")
        self.assertEqual(self.server.seen_paths, before)
        self.assertNotIn("/1.epub", self.server.seen_paths)
        self.assertEqual(self.staging_files(), [])

    def test_import_provenance_preserves_opds_origin(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "captured")
        self.assertEqual(status["provider"]["title"], "One")
        self.assertEqual(status["provider"]["authors"], ["Synthetic Author"])
        self.assertEqual(status["source_identity"]["stable_id"], "urn:synthetic:1")
        self.assertNotEqual(status["provider"]["title"], "Injected OPF Title")
        self.assertNotEqual(status["source_identity"]["stable_id"], "urn:synthetic:opf-1")
        self.service.discard_capture(status["confirmation_token"])


class RedirectReviewTests(ImportMixin):
    def test_redirect_chain_a_b_c_reviews_and_credential_scoping(self):
        other_b = start_server()
        self.addCleanup(stop_server, other_b)
        other_c = start_server()
        self.addCleanup(stop_server, other_c)
        self.server.route("/opds", feed_xml(
            "F", [("1", "One", "Synthetic Author", "S", "1", "1.epub",
                   "application/epub+zip")]))
        self.server.route("/1.epub", b"", status=302,
                          headers={"Location": f"{other_b.origin}/book.epub"})
        other_b.route("/book.epub", b"", status=302,
                      headers={"Location": f"{other_c.origin}/book.epub"})
        other_c.route("/book.epub", EPUB_BIG, headers=dict(EPUB_HEADERS))
        token = self.browse_preview()
        self.assertFalse(self.catalog.request_capture_review(token)["review_required"])
        job = self.service.start_capture(token)
        st = self.wait_captured(job)
        self.assertEqual(st["status"], "review_required")
        self.assertTrue(st["review_token"])
        self.assertEqual(st["review_operation"], "capture")
        self.assertIn(other_b.origin, st["review_destination_url"])
        self.assertEqual(other_b.seen_paths, [])
        self.assertEqual(other_c.seen_paths, [])
        approved = self.catalog.approve_review(st["review_token"])
        self.assertTrue(approved["approved"])
        job2 = self.service.start_capture(token)
        st2 = self.wait_captured(job2)
        self.assertEqual(st2["status"], "review_required")
        self.assertTrue(st2["review_token"])
        self.assertIn(other_c.origin, st2["review_destination_url"])
        self.assertEqual(other_b.seen_paths, ["/book.epub"])
        self.assertEqual(other_b.seen_auth, [("/book.epub", None)])
        self.assertEqual(other_c.seen_paths, [])
        self.catalog.approve_review(st2["review_token"])
        job3 = self.service.start_capture(token)
        st3 = self.wait_captured(job3)
        self.assertEqual(st3["status"], "captured")
        self.assertEqual(st3["sha256"], hashlib.sha256(EPUB_BIG).hexdigest())
        self.assertEqual(other_c.seen_auth, [("/book.epub", None)])
        self.assertTrue(any(auth is not None for _p, auth in self.server.seen_auth))
        self.assertTrue(all(auth is None for _p, auth in other_b.seen_auth + other_c.seen_auth))
        self.service.discard_capture(st3["confirmation_token"])

    def test_cancel_review_required_is_terminal_and_new_capture_possible(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        self.server.route("/opds", feed_xml(
            "F", [("1", "One", "Synthetic Author", "S", "1", "1.epub",
                   "application/epub+zip")]))
        self.server.route("/1.epub", b"", status=302,
                          headers={"Location": f"{other.origin}/book.epub"})
        other.route("/book.epub", EPUB_BIG, headers=dict(EPUB_HEADERS))
        token = self.browse_preview()
        job = self.service.start_capture(token)
        st = self.wait_captured(job)
        self.assertEqual(st["status"], "review_required")
        cancelled = self.service.cancel_capture(job)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["cancelling"])
        self.assertEqual(self.service.get_capture_status(job)["status"],
                         "cancelled")
        self.catalog.approve_review(st["review_token"])
        job2 = self.service.start_capture(token)
        st2 = self.wait_captured(job2)
        self.assertEqual(st2["status"], "captured")
        self.assertEqual(st2["sha256"], hashlib.sha256(EPUB_BIG).hexdigest())
        self.service.discard_capture(st2["confirmation_token"])

    def test_changed_redirect_target_requires_fresh_review(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        self.server.route("/opds", feed_xml(
            "F", [("1", "One", "Synthetic Author", "S", "1", "1.epub",
                   "application/epub+zip")]))
        self.server.route("/1.epub", b"", status=302,
                          headers={"Location": f"{other.origin}/first.epub"})
        other.route("/first.epub", b"", status=302,
                    headers={"Location": f"{other.origin}/second.epub"})
        other.route("/second.epub", EPUB_BIG, headers=dict(EPUB_HEADERS))
        token = self.browse_preview()
        job = self.service.start_capture(token)
        st = self.wait_captured(job)
        self.assertEqual(st["status"], "review_required")
        self.assertIn("first.epub", st["review_destination_url"])
        self.catalog.approve_review(st["review_token"])
        job2 = self.service.start_capture(token)
        st2 = self.wait_captured(job2)
        self.assertEqual(st2["status"], "review_required")
        self.assertIn("second.epub", st2["review_destination_url"])
        self.assertEqual(other.seen_paths, ["/first.epub"])
        self.catalog.approve_review(st2["review_token"])
        job3 = self.service.start_capture(token)
        st3 = self.wait_captured(job3)
        self.assertEqual(st3["status"], "captured")
        self.service.discard_capture(st3["confirmation_token"])


class TruthfulStatusTests(ImportMixin):
    def test_uploader_failure_marks_failed_not_imported(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        conf = status["confirmation_token"]

        def failing(raw, name):
            raise RuntimeError("authority boom")

        with self.assertRaises(RuntimeError):
            self.service.import_confirmed(conf, status["sha256"], failing)
        st2 = self.service.get_capture_status(job)
        self.assertEqual(st2["status"], "failed")
        self.assertIn("RuntimeError", st2["error"])
        self.assertEqual(self.staging_files(), [])
        with self.assertRaises(importer.ImportRefused):
            self.service.import_confirmed(conf, status["sha256"], failing)

    def test_success_importing_then_imported(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        observed = []

        def ok(raw, name):
            observed.append(self.service.get_capture_status(job)["status"])
            return {"stored_filename": "x.txt"}

        result = self.service.import_confirmed(
            status["confirmation_token"], status["sha256"], ok)
        self.assertEqual(observed, ["importing"])
        self.assertEqual(result["upload"]["stored_filename"], "x.txt")
        self.assertEqual(self.service.get_capture_status(job)["status"], "imported")

    def test_discard_race_does_not_pop_importing_confirmation(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        conf = status["confirmation_token"]

        def probing(raw, name):
            with self.assertRaises(importer.ImportRefused):
                self.service.discard_capture(conf)
            return {"stored_filename": "x.txt"}

        result = self.service.import_confirmed(conf, status["sha256"], probing)
        self.assertEqual(result["upload"]["stored_filename"], "x.txt")
        self.assertEqual(self.service.get_capture_status(job)["status"], "imported")

    def test_tamper_marks_failed_not_imported(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        staged = self.staging_files()[0]
        (Path(self.data_dir) / "calibre_staging" / staged).write_bytes(
            b"PK\x03\x04" + b"replaced" * 500)
        with self.assertRaises(importer.ImportRefused):
            self.service.import_confirmed(
                status["confirmation_token"], status["sha256"],
                lambda raw, name: {"stored_filename": "x.txt"})
        self.assertEqual(self.service.get_capture_status(job)["status"], "failed")


class ConfigChangeTests(ImportMixin):
    def test_config_change_before_confirmation_prevents_mint(self):
        original = importer._validate_epub_bytes

        def validating_and_changing(raw):
            original(raw)
            self.catalog.save_config(f"{self.origin}/opds2", username="")
            self.service.on_config_changed()

        token = self.browse_preview()
        with mock.patch.object(importer, "_validate_epub_bytes",
                               side_effect=validating_and_changing):
            job = self.service.start_capture(token)
            st = self.wait_captured(job)
        self.assertNotEqual(st["status"], "captured")
        self.assertIsNone(st["confirmation_token"])
        self.assertIn(st["status"], ("failed", "cancelled", "stale"))
        self.assertEqual(self.staging_files(), [])

    def test_config_change_during_import_preserves_snapshot(self):
        import routers.script as script
        uploads = Path(self.data_dir) / "uploads"
        uploads.mkdir()
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        sha = status["sha256"]

        def changing_uploader(raw, synthetic):
            self.catalog.save_config(f"{self.origin}/opds2", username="")
            self.service.on_config_changed()

            async def run():
                from fastapi import UploadFile
                handle = io.BytesIO(raw)
                try:
                    return await script.upload_file(
                        file=UploadFile(file=handle, filename=synthetic),
                        select_active=False, include_extraction_report=True,
                        docx_preview=False, docx_confirmed=False,
                        docx_preview_digest=None)
                finally:
                    handle.close()
            return asyncio.run(run())

        with mock.patch.object(script, "UPLOADS_DIR", str(uploads)), \
             mock.patch.object(script, "DATA_DIR", self.data_dir):
            result = self.service.import_confirmed(
                status["confirmation_token"], sha, changing_uploader)
        self.assertEqual(result["sha256"], sha)
        self.assertEqual(self.service.get_capture_status(job)["status"], "imported")
        txt = (uploads / result["upload"]["stored_filename"]).read_text(encoding="utf-8")
        self.assertIn("Synthetic chapter sentence.", txt)
        self.assertEqual(self.staging_files(), [])

    def test_config_change_does_not_clear_capturing_slot(self):
        self.server.route_dribble(
            "/1.epub", total=2 * 1024 * 1024, interval=0.02,
            content_type="application/epub+zip",
            prefix=b"PK\x03\x04" + b"s" * 60)
        token = self.browse_preview()
        job = self.service.start_capture(token)
        time.sleep(0.15)
        self.service.on_config_changed()
        with self.assertRaises(importer.ImportRefused):
            self.service.start_capture(token)
        st = self.wait_captured(job, timeout=20.0)
        self.assertIn(st["status"], ("cancelled", "failed"))
        self.assertEqual(self.staging_files(), [])


class ResidueTests(ImportMixin):
    def test_injected_hash_error_cleans_up(self):
        token = self.browse_preview()
        with mock.patch.object(importer, "_fd_sha256_and_bytes",
                               side_effect=OSError("boom")):
            job = self.service.start_capture(token)
            st = self.wait_captured(job)
        self.assertEqual(st["status"], "failed")
        self.assertEqual(self.staging_files(), [])

    def test_validation_error_orphans_no_file(self):
        self.server.route("/1.epub", b"PK\x03\x04" + b"x" * 200,
                          headers=dict(EPUB_HEADERS))
        token = self.browse_preview()
        job = self.service.start_capture(token)
        st = self.wait_captured(job)
        self.assertEqual(st["status"], "failed")
        self.assertEqual(self.staging_files(), [])

    def test_restart_residue_blocks_new_capture(self):
        staging = Path(self.data_dir) / "calibre_staging"
        staging.mkdir(exist_ok=True)
        residue = staging / "opds-residue.epub"
        residue.write_bytes(EPUB_SMALL)
        service2 = importer.CalibreImportService(
            self.data_dir, catalog=self.catalog, max_upload_bytes=512 * 1024 ** 2)
        token = self.browse_preview()
        with self.assertRaises(importer.ImportRefused):
            service2.start_capture(token)
        self.assertTrue(residue.exists())
        self.assertEqual(self.staging_files(), ["opds-residue.epub"])
        residue.unlink()
        job = service2.start_capture(token)
        deadline = time.monotonic() + 15.0
        status = None
        while time.monotonic() < deadline:
            status = service2.get_capture_status(job)
            if status["status"] in ("captured", "failed", "cancelled", "stale",
                                    "review_required"):
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "captured")
        service2.discard_capture(status["confirmation_token"])

    def test_cleanup_failure_retains_residue_quota(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        staged_path = Path(self.data_dir) / "calibre_staging" / self.staging_files()[0]
        with mock.patch("os.unlink", side_effect=OSError("cannot delete")):
            self.service.discard_capture(status["confirmation_token"])
        self.assertTrue(staged_path.exists())
        with self.assertRaises(importer.ImportRefused):
            self.service.start_capture(token)
        staged_path.unlink()
        job2 = self.service.start_capture(token)
        self.assertEqual(self.wait_captured(job2)["status"], "captured")
        self.service.discard_capture(
            self.service.get_capture_status(job2)["confirmation_token"])

    def test_overlapping_instance_blocked_by_residue(self):
        token = self.browse_preview()
        job = self.service.start_capture(token)
        status = self.wait_captured(job)
        service2 = importer.CalibreImportService(
            self.data_dir, catalog=self.catalog, max_upload_bytes=512 * 1024 ** 2)
        with self.assertRaises(importer.ImportRefused):
            service2.start_capture(token)
        self.service.discard_capture(status["confirmation_token"])
        job2 = service2.start_capture(token)
        deadline = time.monotonic() + 15.0
        status2 = None
        while time.monotonic() < deadline:
            status2 = service2.get_capture_status(job2)
            if status2["status"] in ("captured", "failed", "cancelled", "stale",
                                     "review_required"):
                break
            time.sleep(0.05)
        self.assertEqual(status2["status"], "captured")
        service2.discard_capture(status2["confirmation_token"])

    def test_ttl_purge_is_explicit_not_via_get(self):
        now = [time.monotonic()]
        service = importer.CalibreImportService(
            self.data_dir, catalog=self.catalog, max_upload_bytes=512 * 1024 ** 2,
            ttl=5.0, clock=lambda: now[0])
        self.service = service
        token = self.browse_preview()
        job = service.start_capture(token)
        status = self.wait_captured(job)
        self.assertEqual(status["status"], "captured")
        self.assertEqual(len(self.staging_files()), 1)
        now[0] += 10.0
        service.get_capture_status(job)
        self.assertEqual(len(self.staging_files()), 1)
        service.purge_expired()
        self.assertEqual(self.staging_files(), [])
        with self.assertRaises(importer.ImportRefused):
            service.get_capture_status(job)



if __name__ == "__main__":
    unittest.main()
