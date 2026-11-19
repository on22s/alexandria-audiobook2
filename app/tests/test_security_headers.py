"""Baseline security headers are applied to every HTTP response.

The SPA is embedded in a frame by Pinokio, so there is deliberately no
X-Frame-Options and no frame-ancestors directive. The CSP is report-only, so a
violation can never block the app; these tests pin that shape as well.
"""
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import app as app_module
import routers.editor as editor
import routers.system as system

HEADER_NAMES = (
    "x-content-type-options",
    "referrer-policy",
    "content-security-policy-report-only",
)


class SecurityHeadersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_module.app, raise_server_exceptions=False)

    def assert_baseline_headers(self, response):
        for name in HEADER_NAMES:
            self.assertIn(name, response.headers, f"missing {name} on {response.status_code} response")
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        self.assertEqual("no-referrer", response.headers["referrer-policy"])
        self.assertTrue(response.headers["content-security-policy-report-only"])

    def test_spa_shell_has_baseline_headers_and_keeps_its_cache_control(self):
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        self.assert_baseline_headers(response)
        self.assertEqual("no-cache, no-store, must-revalidate", response.headers["cache-control"])

    def test_json_api_route_has_baseline_headers(self):
        response = self.client.get("/api/config")
        self.assertEqual(200, response.status_code)
        self.assert_baseline_headers(response)

    def test_static_asset_has_baseline_headers(self):
        response = self.client.get("/static/js/app-core.js")
        self.assertEqual(200, response.status_code)
        self.assert_baseline_headers(response)

    def test_404_has_baseline_headers(self):
        response = self.client.get("/no-such-route-security-headers")
        self.assertEqual(404, response.status_code)
        self.assert_baseline_headers(response)

    def test_editor_error_response_keeps_nosniff_through_the_middleware(self):
        """The editor's explicit nosniff may be removed: the middleware supplies it."""
        with tempfile.TemporaryDirectory() as root, patch.object(editor, "DATA_DIR", root):
            response = self.client.get("/api/m4b_cover/preview", params={
                "expected_book_token": "0" * 64,
                "sha256": "1" * 64,
            })
        self.assertEqual(409, response.status_code, response.text)
        self.assert_baseline_headers(response)
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_editor_success_response_keeps_nosniff_through_the_middleware(self):
        raw = b"\x89PNG\r\n\x1a\nfixture cover bytes"
        params = {"expected_book_token": "0" * 64, "sha256": "1" * 64}
        with patch.object(editor, "_get_uploaded_cover_preview_sync", return_value=(raw, "image/png")):
            response = self.client.get("/api/m4b_cover/preview", params=params)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(raw, response.content)
        self.assert_baseline_headers(response)
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_host_refusal_gate_response_has_baseline_headers(self):
        response = self.client.get("/", headers={"Host": "evil.example.com"})
        self.assertEqual(400, response.status_code)
        self.assert_baseline_headers(response)

    def test_origin_refusal_gate_response_has_baseline_headers(self):
        response = self.client.post("/", headers={"Origin": "https://evil.example.com"})
        self.assertEqual(403, response.status_code)
        self.assert_baseline_headers(response)

    def test_unhandled_error_500_has_baseline_headers(self):
        """The Exception handler doubles as Starlette's outer ServerErrorMiddleware
        handler, so it must apply the same headers the middleware cannot reach."""
        with patch.object(system, "get_runtime_info", side_effect=RuntimeError("probe")):
            response = self.client.get("/")
        self.assertEqual(500, response.status_code)
        self.assert_baseline_headers(response)

    def test_no_framing_restriction_and_no_report_endpoint(self):
        response = self.client.get("/")
        self.assertNotIn("x-frame-options", response.headers)
        csp = response.headers.get("content-security-policy-report-only", "")
        self.assertNotIn("frame-ancestors", csp)
        self.assertNotIn("report-uri", csp)
        self.assertNotIn("report-to", csp)
        self.assertNotIn("content-security-policy", response.headers)

    def test_csp_covers_the_resources_the_spa_actually_loads(self):
        csp = self.client.get("/").headers.get("content-security-policy-report-only", "")
        for directive in (
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",  # inline anti-flash block + inline handlers
            "style-src 'self' 'unsafe-inline'",   # inline <style> and style="..." attributes
            "img-src 'self' data: blob:",         # inline data: SVG backgrounds
            "media-src 'self' blob: data:",       # createObjectURL audio previews
            "connect-src 'self'",                 # every fetch() targets /api/...
            "object-src 'none'",
        ):
            self.assertIn(directive, csp)

    def test_route_set_header_value_is_not_clobbered(self):
        dispatch = getattr(app_module, "_security_headers_middleware", None)
        self.assertIsNotNone(dispatch, "security headers middleware must be a named dispatch function")
        probe = FastAPI()
        probe.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)

        @probe.get("/custom")
        def custom():
            return Response("ok", headers={
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy-Report-Only": "default-src 'none'",
            })

        with TestClient(probe) as client:
            response = client.get("/custom")
        self.assertEqual(200, response.status_code)
        self.assertEqual("same-origin", response.headers["referrer-policy"])
        self.assertEqual("default-src 'none'", response.headers["content-security-policy-report-only"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])


if __name__ == "__main__":
    unittest.main()
