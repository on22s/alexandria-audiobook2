"""Route tests for the thin /api/calibre/* endpoints (issue #1734, slice 3).

Production FastAPI routes via TestClient against a real local OPDS server;
only DATA_DIR/UPLOADS_DIR, the lazy service singleton and the OS keyring
are injected. No browser, no LLM/TTS, no live catalog.
"""
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import calibre_catalog as cat
import calibre_opds as opds
import calibre_import as importer
import routers.script as script
from tests.test_calibre_opds import feed_xml, start_server, stop_server
from tests.test_calibre_catalog import FakeKeyring
from tests.test_calibre_import import EPUB_BIG, EPUB_HEADERS

USER, PW = "synthetic-user", "synthetic-pass"


class RouteMixin(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data_dir = self._td.name
        self.uploads = Path(self.data_dir) / "uploads"
        self.uploads.mkdir()
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
        app = FastAPI()
        app.include_router(script.router)
        self.client = TestClient(app)
        self._patch = [
            mock.patch.object(script, "DATA_DIR", self.data_dir),
            mock.patch.object(script, "UPLOADS_DIR", str(self.uploads)),
            mock.patch.dict(script._calibre_services,
                            {self.data_dir: (self.catalog, self.service)}),
        ]
        for patcher in self._patch:
            patcher.start()
            self.addCleanup(patcher.stop)

    def poll_captured(self, job_token, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self.client.get("/api/calibre/capture/status",
                                   params={"job_token": job_token})
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            if body["status"] != "capturing":
                return body
            time.sleep(0.05)
        raise AssertionError(f"capture did not finish: {body}")

    def configure(self, username=USER, password=PW):
        resp = self.client.post("/api/calibre/config", json={
            "root_url": f"{self.origin}/opds", "username": username,
            "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def browse_preview(self):
        resp = self.client.post("/api/calibre/browse-root")
        self.assertEqual(resp.status_code, 200, resp.text)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        return entries[0]["preview_token"]


class ConfigRouteTests(RouteMixin):
    def test_password_dict_rejected_without_echo(self):
        resp = self.client.post("/api/calibre/config", json={
            "root_url": f"{self.origin}/opds", "username": USER,
            "password": {"leak": "hunter2-secret-value"}})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("hunter2-secret-value", resp.text)
        self.assertFalse(self.catalog.get_config()["configured"])

    def test_password_int_and_extra_fields_rejected(self):
        resp = self.client.post("/api/calibre/config", json={
            "root_url": f"{self.origin}/opds", "username": USER,
            "password": 123456})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("123456", resp.text.split("detail")[0] or "")
        resp = self.client.post("/api/calibre/browse-root", json={})
        # The empty-body config path is fine; unknown body fields are not:
        resp = self.client.post("/api/calibre/capture", json={
            "token": "a" * 32, "url": "http://evil.example/x.epub",
            "path": "/tmp/x"})
        self.assertEqual(resp.status_code, 422)

    def test_config_roundtrip_and_anonymous(self):
        cfg = self.configure()
        self.assertEqual(cfg["revision"], 1)
        resp = self.client.get("/api/calibre/config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["root_url"], f"{self.origin}/opds")


class FlowRouteTests(RouteMixin):
    def test_full_flow_to_existing_source_preview(self):
        # Prior book must be preserved verbatim (select_active=False).
        prior = self.uploads / "prior-book.txt"
        prior.write_text("Prior synthetic book text.", encoding="utf-8")
        state = Path(self.data_dir) / "state.json"
        state.write_text(json.dumps({"input_file_path": str(prior)}),
                         encoding="utf-8")
        self.configure()
        preview_token = self.browse_preview()
        resp = self.client.get("/api/calibre/preview",
                               params={"preview_token": preview_token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.server.seen_paths, ["/opds"])
        provider = resp.json()["provider"]
        self.assertEqual(provider["title"], "One")
        self.assertEqual(provider["authors"], ["Synthetic Author"])
        resp = self.client.post("/api/calibre/capture",
                                json={"token": preview_token})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "capturing")
        status = self.poll_captured(resp.json()["job_token"])
        self.assertEqual(status["status"], "captured")
        self.assertEqual(status["sha256"], hashlib.sha256(EPUB_BIG).hexdigest())
        resp = self.client.post("/api/calibre/import", json={
            "confirmation_token": status["confirmation_token"],
            "sha256": status["sha256"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        upload = resp.json()["upload"]
        self.assertTrue(upload["stored_filename"].endswith(".txt"))
        self.assertIn("extraction_report", upload)
        self.assertIsNone(upload["book_identity_token"])
        stored = (self.uploads / upload["stored_filename"]).read_text(encoding="utf-8")
        self.assertIn("Synthetic chapter sentence.", stored)
        # Existing source-scope preview endpoint lists the stored file.
        resp = self.client.get("/api/uploads")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(upload["stored_filename"],
                      [u["filename"] for u in resp.json()])
        # Prior book + active state untouched.
        self.assertEqual(prior.read_text(encoding="utf-8"),
                         "Prior synthetic book text.")
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["input_file_path"],
                         str(prior))


    def test_redirect_review_via_routes_approve_and_replay(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        self.server.route("/opds", feed_xml(
            "F", [("1", "One", "Synthetic Author", "S", "1", "1.epub",
                   "application/epub+zip")]))
        self.server.route("/1.epub", b"", status=302,
                          headers={"Location": f"{other.origin}/book.epub"})
        other.route("/book.epub", EPUB_BIG, headers=dict(EPUB_HEADERS))
        self.configure()
        preview_token = self.browse_preview()
        resp = self.client.post("/api/calibre/capture", json={"token": preview_token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "capturing")
        status = self.poll_captured(resp.json()["job_token"])
        self.assertEqual(status["status"], "review_required")
        self.assertTrue(status["review_token"])
        self.assertEqual(status["review_operation"], "capture")
        self.assertIn(other.origin, status["review_destination_url"])
        self.assertEqual(other.seen_paths, [])
        resp = self.client.post("/api/calibre/review/approve",
                                json={"token": status["review_token"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["approved"])
        resp = self.client.post("/api/calibre/capture", json={"token": preview_token})
        self.assertEqual(resp.status_code, 200)
        status = self.poll_captured(resp.json()["job_token"])
        self.assertEqual(status["status"], "captured")
        self.assertEqual(status["sha256"], hashlib.sha256(EPUB_BIG).hexdigest())
        self.assertEqual(other.seen_paths, ["/book.epub"])
        self.assertEqual(other.seen_auth, [("/book.epub", None)])

    def test_tampered_sha_refused_at_import(self):
        self.configure()
        preview_token = self.browse_preview()
        resp = self.client.post("/api/calibre/capture",
                                json={"token": preview_token})
        status = self.poll_captured(resp.json()["job_token"])
        resp = self.client.post("/api/calibre/import", json={
            "confirmation_token": status["confirmation_token"],
            "sha256": "f" * 64})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(list(self.uploads.iterdir()), [])

    def test_cross_origin_review_via_routes_credentials_a_only(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        self.server.route("/opds", feed_xml(
            "F", [("1", "One", "Synthetic Author", "S", "1",
                   f"{other.origin}/book.epub", "application/epub+zip")]))
        other.route("/book.epub", EPUB_BIG, headers=dict(EPUB_HEADERS))
        self.configure()
        preview_token = self.browse_preview()
        resp = self.client.post("/api/calibre/capture",
                                json={"token": preview_token})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["review_required"])
        review_token = resp.json()["review_token"]
        # Caller-supplied destination fields are forbidden.
        resp = self.client.post("/api/calibre/review/approve", json={
            "token": review_token,
            "destination_url": f"{other.origin}/other.epub"})
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post("/api/calibre/review/approve",
                                json={"token": review_token})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["approved"])
        resp = self.client.post("/api/calibre/capture",
                                json={"token": preview_token})
        self.assertEqual(resp.status_code, 200)
        status = self.poll_captured(resp.json()["job_token"])
        self.assertEqual(status["status"], "captured")
        self.assertEqual(other.seen_auth, [("/book.epub", None)])


if __name__ == "__main__":
    unittest.main()
