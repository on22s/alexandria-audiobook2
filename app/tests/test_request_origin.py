"""Cross-site and DNS-rebinding requests are refused (GHSA-vp9p-437w-q5x9),
and the ways the owner really reaches the app still work: direct loopback,
Pinokio's *.localhost proxy, LAN IPs, Docker, and clients with no Origin."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from request_origin import get_request_refusal, is_allowed_host

CORS = ["http://127.0.0.1:4200", "http://localhost:4200"]
APP_DIR = Path(__file__).resolve().parent.parent


def refusal(method="POST", host="127.0.0.1:4200", origin=None, referer=None,
            origins=CORS, hosts=()):
    result = get_request_refusal(method, host, origin, referer, origins, hosts)
    return result[0] if result else None


class HostTests(unittest.TestCase):
    def test_rebinding_hostnames_are_refused_on_every_method(self):
        for host in ("evil.example", "evil.example:4200", "localhost.evil.com",
                     "evil.localhost.example.com", "", None, "[bad"):
            for method in ("GET", "POST"):
                self.assertEqual("host", refusal(method, host), (host, method))

    def test_ip_localhost_and_dotless_hosts_are_allowed(self):
        for host in ("127.0.0.1:4200", "10.0.0.12:42000", "[::1]:4200", "203.0.113.9",
                     "localhost:4200", "4200.localhost", "LOCALHOST", "testserver", "mybox:4200"):
            self.assertTrue(is_allowed_host(host), host)

    def test_a_configured_hostname_is_allowed(self):
        self.assertFalse(is_allowed_host("box.lan:4200"))
        self.assertTrue(is_allowed_host("box.lan:4200", [" Box.LAN "]))


class OriginTests(unittest.TestCase):
    def test_cross_site_unsafe_requests_are_refused(self):
        for origin in ("http://evil.example", "https://attacker.example", "null", "NULL",
                       "http://1.2.3.4", "http://evil.localhost.example.com",
                       "http://localhost.evil.com", "ftp://127.0.0.1", "garbage"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                self.assertEqual("origin", refusal(method, origin=origin), (origin, method))

    def test_a_cross_site_referer_alone_is_refused(self):
        self.assertEqual("origin", refusal(referer="http://evil.example/page?x=1"))
        self.assertEqual("origin", refusal(referer="not a url"))

    def test_safe_methods_are_not_origin_checked(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            self.assertIsNone(refusal(method, origin="http://evil.example"))

    def test_same_origin_and_local_sources_are_allowed(self):
        cases = [("127.0.0.1:4200", "http://127.0.0.1:4200"),
                 ("127.0.0.1:4200", "https://4200.localhost"),        # Pinokio proxy, Host rewritten
                 ("4200.localhost", "https://4200.localhost"),        # Pinokio proxy, Host passed through
                 ("10.0.0.12:42000", "http://10.0.0.12:42000"),       # LAN access
                 ("127.0.0.1:9999", "http://localhost:4200"),         # the CORS fixture's shape
                 ("box", "http://box"),                               # same origin, default port
                 ("box:443", "https://box"),
                 ("192.168.1.5:4200", "http://192.168.1.5:4200")]
        for host, origin in cases:
            self.assertIsNone(refusal(host=host, origin=origin), (host, origin))

    def test_a_same_name_on_another_port_is_not_same_origin(self):
        self.assertEqual("origin", refusal(host="box:4200", origin="http://box:8080"))

    def test_a_local_referer_is_allowed_and_no_origin_or_referer_is_a_non_browser_client(self):
        self.assertIsNone(refusal(referer="http://127.0.0.1:4200/static/index.html"))
        self.assertIsNone(refusal(origin=None, referer=None))
        self.assertIsNone(refusal(origin="  ", referer=None))

    def test_configured_origins_are_allowed(self):
        self.assertEqual("origin", refusal(origin="https://audio.example.com"))
        self.assertIsNone(refusal(origin="https://audio.example.com",
                                  origins=CORS + ["https://audio.example.com"]))


class AppGateTests(unittest.TestCase):
    """Through the real app: the advisory's four routes."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_module
        cls.client = TestClient(app_module.app)

    def test_the_advisory_routes_are_refused_before_their_handlers(self):
        for path in ("/api/chunks/0/insert", "/api/benchmark/cancel",
                     "/api/lora/models/some_adapter/promote", "/api/merge"):
            for origin in ("http://evil.example", "https://attacker.example"):
                response = self.client.post(path, headers={"Origin": origin})
                self.assertEqual(403, response.status_code, (path, origin, response.text))
                self.assertIn("Cross-site request", response.text)

    def test_a_refused_request_changes_nothing_and_an_allowed_one_does(self):
        from routers import benchmark
        state = {"running": True, "cancel": False, "logs": []}
        with patch.dict(benchmark.process_state, {"benchmark": state}):
            refused = self.client.post("/api/benchmark/cancel", headers={"Origin": "http://evil.example"})
            self.assertEqual((403, False), (refused.status_code, state["cancel"]))
            allowed = self.client.post("/api/benchmark/cancel", headers={"Origin": "http://testserver"})
            self.assertEqual((200, True), (allowed.status_code, state["cancel"]))

    def test_a_rebinding_host_is_refused_even_for_reads_and_static_files(self):
        for path in ("/api/config", "/static/index.html"):
            response = self.client.get(path, headers={"Host": "evil.example"})
            self.assertEqual(400, response.status_code, path)
            self.assertIn("ALEXANDRIA_ALLOWED_HOSTS", response.text)

    def test_a_cors_preflight_is_still_answered(self):
        response = self.client.options("/api/merge", headers={
            "Origin": "http://localhost:4200", "Access-Control-Request-Method": "POST"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("http://localhost:4200", response.headers.get("access-control-allow-origin"))


class AuthOrderTests(unittest.TestCase):
    def test_the_gate_runs_before_basic_auth_and_auth_still_applies(self):
        """With a password set (a fresh interpreter, since the auth gate is
        registered at import): a cross-site POST is refused 403 without
        credentials; a same-origin one still needs them."""
        probe = (
            "from fastapi.testclient import TestClient\n"
            "import app, base64\n"
            "c = TestClient(app.app)\n"
            "auth = {'Authorization': 'Basic ' + base64.b64encode(b'alexandria:pw').decode()}\n"
            "print(c.post('/api/benchmark/cancel', headers={'Origin': 'http://evil.example'}).status_code,\n"
            "      c.post('/api/benchmark/cancel', headers={'Origin': 'http://evil.example', **auth}).status_code,\n"
            "      c.post('/api/benchmark/cancel').status_code,\n"
            "      c.post('/api/benchmark/cancel', headers=auth).status_code)\n")
        with tempfile.TemporaryDirectory() as data_dir:
            env = {**os.environ, "ALEXANDRIA_AUTH_PASSWORD": "pw", "ALEXANDRIA_DATA_DIR": data_dir}
            result = subprocess.run([sys.executable, "-c", probe], cwd=APP_DIR, env=env,
                                    capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stderr[-2000:])
        # refused before auth; refused with auth too; no creds -> 401; creds -> the handler (400: none running)
        self.assertEqual("403 403 401 400", result.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
