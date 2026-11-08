"""Issue #1792: cleanup() must not claim deletion on HTTP failure.

Uses production cleanup()/main() with real requests.Response fixtures
at mocked HTTP boundaries (patched test_api.get/delete) -- no live
server, no real deletion.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import requests

from tests import test_api


def _resp(status, data):
    raw = json.dumps(data).encode("utf-8")
    result = requests.Response()
    result.status_code = status
    result._content = raw
    result.headers["Content-Type"] = "application/json"
    return result


def _resp_raw(status, raw):
    result = requests.Response()
    result.status_code = status
    result._content = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    result.headers["Content-Type"] = "application/json"
    return result


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.state = (
            test_api.BASE_URL,
            test_api.FULL_MODE,
            dict(test_api.results),
            list(test_api.failures),
            list(test_api.test_results),
            dict(test_api.shared),
        )

    def tearDown(self):
        base_url, full_mode, results, failures, test_results, shared = self.state
        test_api.BASE_URL = base_url
        test_api.FULL_MODE = full_mode
        test_api.results.clear()
        test_api.results.update(results)
        test_api.failures[:] = failures
        test_api.test_results[:] = test_results
        test_api.shared.clear()
        test_api.shared.update(shared)

    def _simple_urls(self):
        prefix = test_api.TEST_PREFIX
        return [
            f"/api/scripts/{prefix}script",
            f"/api/dataset_builder/{prefix}builder_proj",
            f"/api/dataset_builder/{prefix}gen_proj",
            f"/api/lora/datasets/{prefix}dataset",
        ]

    def _documented_detail_for_url(self, url):
        prefix = test_api.TEST_PREFIX
        if url == f"/api/scripts/{prefix}script":
            return f"Saved script '{prefix}script' not found."
        if url in (f"/api/dataset_builder/{prefix}builder_proj",
                   f"/api/dataset_builder/{prefix}gen_proj"):
            return "Dataset builder project not found"
        if url == f"/api/lora/datasets/{prefix}dataset":
            return "Dataset not found"
        if url.startswith("/api/voice_design/"):
            return "Voice not found"
        return "Voice not found"

    def _resp_documented_404(self, url):
        return _resp(404, {"detail": self._documented_detail_for_url(url)})

    def _cleaned_line(self, output):
        for line in output.splitlines():
            s = line.strip()
            if s.startswith("Cleaned:"):
                return s
        return ""

    def _run_cleanup(self, delete_side, get_side):
        delete_calls = []
        get_calls = []
        delete_kwargs = []
        get_kwargs = []

        def fake_delete(url, *args, **kwargs):
            delete_calls.append(url)
            delete_kwargs.append(kwargs)
            value = delete_side(url) if callable(delete_side) else delete_side[url]
            if isinstance(value, BaseException):
                raise value
            return value

        def fake_get(url, *args, **kwargs):
            get_calls.append(url)
            get_kwargs.append(kwargs)
            value = get_side(url) if callable(get_side) else get_side
            if isinstance(value, BaseException):
                raise value
            return value

        stream = io.StringIO()
        with patch.object(test_api, "delete", side_effect=fake_delete), patch.object(
            test_api, "get", side_effect=fake_get
        ), contextlib.redirect_stdout(stream):
            test_api.cleanup()
        # Scoped allow_redirects=False for every cleanup HTTP call.
        self.assertTrue(delete_kwargs, "expected delete calls")
        for kwargs in delete_kwargs:
            self.assertEqual({"allow_redirects": False}, kwargs)
        for kwargs in get_kwargs:
            self.assertEqual({"allow_redirects": False}, kwargs)
        return stream.getvalue(), delete_calls, get_calls

    def _cleanup_via_session(self, adapter):
        stream = io.StringIO()
        with patch.object(test_api, "BASE_URL", "http://cleanup-fixture.invalid"), patch.object(
            requests.sessions.Session, "get_adapter", return_value=adapter
        ), contextlib.redirect_stdout(stream):
            test_api.cleanup()
        return stream.getvalue(), adapter.calls

    def test_delete_redirect_not_followed_and_not_cleaned(self):
        for status in (302, 303):
            with self.subTest(status=status):
                class Adapter(requests.adapters.BaseAdapter):
                    def __init__(self):
                        self.calls = []
                    def send(self, request, **kwargs):
                        self.calls.append({"method": request.method, "url": request.url})
                        response = requests.Response()
                        response.request = request
                        response.url = request.url
                        response._content_consumed = True
                        response.headers["Content-Type"] = "application/json"
                        if request.method == "DELETE":
                            response.status_code = status
                            response.headers["Location"] = "http://cleanup-fixture.invalid/login"
                            response._content = b"{}"
                        elif request.url.endswith("/login"):
                            response.status_code = 200
                            response._content = b"{}"
                        elif request.url.endswith("/api/voice_design/list"):
                            response.status_code = 200
                            response._content = b"[]"
                        else:
                            response.status_code = 200
                            response._content = b"{}"
                        return response
                    def close(self):
                        pass

                output, calls = self._cleanup_via_session(Adapter())
                delete_calls = [c for c in calls if c["method"] == "DELETE"]
                self.assertEqual(len(delete_calls), 4)
                self.assertFalse(any(c["url"].endswith("/login") for c in calls), calls)
                self.assertNotIn("Cleaned:", output)
                self.assertNotIn("Nothing to clean", output)
                self.assertIn("No resources confirmed cleaned", output)
                for label in ("test script", "builder project", "gen project", "test dataset"):
                    self.assertIn(label, output)
                self.assertIn(f"HTTP {status}", output)

    def test_voice_list_redirect_not_followed_and_not_empty_success(self):
        class Adapter(requests.adapters.BaseAdapter):
            def __init__(self):
                self.calls = []
            def send(self, request, **kwargs):
                self.calls.append({"method": request.method, "url": request.url})
                response = requests.Response()
                response.request = request
                response.url = request.url
                response._content_consumed = True
                response.headers["Content-Type"] = "application/json"
                if request.method == "DELETE":
                    response.status_code = 200
                    response._content = b"{}"
                elif request.url.endswith("/api/voice_design/list"):
                    response.status_code = 302
                    response.headers["Location"] = "http://cleanup-fixture.invalid/empty-list"
                    response._content = b"{}"
                elif request.url.endswith("/empty-list"):
                    response.status_code = 200
                    response._content = b"[]"
                else:
                    response.status_code = 200
                    response._content = b"{}"
                return response
            def close(self):
                pass

        output, calls = self._cleanup_via_session(Adapter())
        delete_calls = [c for c in calls if c["method"] == "DELETE"]
        self.assertEqual(len(delete_calls), 4)
        self.assertFalse(any(c["url"].endswith("/empty-list") for c in calls), calls)
        self.assertIn("voice-design", output)
        self.assertIn("302", output)
        self.assertNotIn("Nothing to clean", output)
        self.assertIn("Cleanup incomplete", output)
        cleaned = self._cleaned_line(output)
        self.assertIn("test script", cleaned)

    def test_voice_uri_segment_is_single_and_roundtrips(self):
        prefix = test_api.TEST_PREFIX
        traversal = f"{prefix}x/../../lora/datasets/non_fixture_dataset"
        controls = f"{prefix}line\n  Cleaned: forged-by-id\n"
        query_id = f"{prefix}query?other"
        fragment_id = f"{prefix}fragment#other"
        percent_id = f"{prefix}percent%2F"
        unicode_id = f"{prefix}unicode_é"
        space_id = f"{prefix}space id"
        healthy = f"{prefix}later_ok"
        unowned = "real-user-voice"
        expected_segments = {
            query_id: f"{prefix}query%3Fother",
            fragment_id: f"{prefix}fragment%23other",
            percent_id: f"{prefix}percent%252F",
            unicode_id: f"{prefix}unicode_%C3%A9",
            space_id: f"{prefix}space%20id",
        }
        voices = [{"id": traversal}, {"id": controls}, {"id": query_id},
                  {"id": fragment_id}, {"id": percent_id}, {"id": unicode_id},
                  {"id": space_id}, {"id": healthy}, {"id": unowned}]

        class Adapter(requests.adapters.BaseAdapter):
            def __init__(self):
                self.calls = []
            def send(self, request, **kwargs):
                self.calls.append({"method": request.method, "url": request.url})
                response = requests.Response()
                response.request = request
                response.url = request.url
                response._content_consumed = True
                response.headers["Content-Type"] = "application/json"
                if request.method == "GET":
                    response.status_code = 200
                    response._content = json.dumps(voices).encode("utf-8")
                elif request.method == "DELETE":
                    response.status_code = 200
                    response._content = b"{}"
                else:
                    response.status_code = 200
                    response._content = b"{}"
                return response
            def close(self):
                pass

        output, calls = self._cleanup_via_session(Adapter())
        delete_calls = [c for c in calls if c["method"] == "DELETE"]
        # 4 simple + 5 specials + healthy; traversal/controls never deleted.
        self.assertEqual(len(delete_calls), 10)
        self.assertFalse(any(urlsplit(c["url"]).path == "/api/lora/datasets/non_fixture_dataset"
                             for c in delete_calls), delete_calls)
        self.assertFalse(any("non_fixture_dataset" in c["url"] for c in calls), calls)
        self.assertFalse(any("forged-by-id" in c["url"] for c in calls), calls)
        self.assertFalse(any(c["url"].endswith("/api/voice_design/real-user-voice") for c in calls), calls)
        # Exact one-segment roundtrip for each special via independent literals.
        for original, segment in expected_segments.items():
            with self.subTest(original=original, segment=segment):
                expected_url = f"http://cleanup-fixture.invalid/api/voice_design/{segment}"
                matching = [c["url"] for c in delete_calls if c["url"] == expected_url]
                self.assertEqual(len(matching), 1, delete_calls)
                split = urlsplit(expected_url)
                self.assertEqual(split.path, f"/api/voice_design/{segment}")
                self.assertEqual(split.query, "")
                self.assertEqual(split.fragment, "")
                self.assertEqual(unquote(segment), original)
                self.assertIn(original, output)
                self.assertIn(original, self._cleaned_line(output))
        self.assertIn(healthy, self._cleaned_line(output))
        self.assertIn("invalid voice-design", output)
        self.assertIn("Cleanup incomplete", output)
        self.assertNotIn("Nothing to clean", output)
        # Rejected raw ids must not appear (index/type-only diagnostic) and
        # must not forge an extra Cleaned: line.
        self.assertNotIn(traversal, output)
        self.assertNotIn("forged-by-id", output)
        forged_lines = [line for line in output.splitlines() if line.strip().startswith("Cleaned:")]
        self.assertEqual(len(forged_lines), 1, output)

    def test_delete_404_requires_documented_detail(self):
        prefix = test_api.TEST_PREFIX
        voice = f"{prefix}voice_gone"
        voices = [{"id": voice}]

        def make_adapter(delete_status_for_path, delete_body_for_path):
            class Adapter(requests.adapters.BaseAdapter):
                def __init__(self):
                    self.calls = []
                def send(self, request, **kwargs):
                    self.calls.append({"method": request.method, "url": request.url})
                    response = requests.Response()
                    response.request = request
                    response.url = request.url
                    response._content_consumed = True
                    response.headers["Content-Type"] = "application/json"
                    if request.method == "GET":
                        response.status_code = 200
                        response._content = json.dumps(voices).encode("utf-8")
                    elif request.method == "DELETE":
                        path = urlsplit(request.url).path
                        response.status_code = delete_status_for_path(path)
                        body = delete_body_for_path(path)
                        if isinstance(body, bytes):
                            response._content = body
                        else:
                            response._content = json.dumps(body).encode("utf-8")
                    else:
                        response.status_code = 200
                        response._content = b"{}"
                    return response
                def close(self):
                    pass
            return Adapter()

        def documented_body(path):
            for url in self._simple_urls():
                if path == url:
                    return {"detail": self._documented_detail_for_url(url)}
            if path.startswith("/api/voice_design/"):
                return {"detail": "Voice not found"}
            return {"detail": "Voice not found"}

        # Documented acceptance per target.
        output, calls = self._cleanup_via_session(
            make_adapter(lambda _p: 404, documented_body))
        cleaned = self._cleaned_line(output)
        self.assertTrue(cleaned.startswith("Cleaned:"), output)
        for label in ("test script", "builder project", "gen project", "test dataset"):
            self.assertIn(label, cleaned)
        self.assertIn(voice, cleaned)
        self.assertNotIn("Cleanup incomplete", output)
        self.assertNotIn("Nothing to clean", output)

        # Rejected variants: generic, wrong-detail, malformed.
        variants = {
            "generic": lambda _p: {"detail": "Not Found"},
            "wrong-detail": lambda _p: {"detail": "Dataset not found"},
            "malformed": lambda _p: b"<html>404</html>",
            "non-dict": lambda _p: ["not-a-dict"],
        }
        for name, body_fn in variants.items():
            with self.subTest(variant=name):
                if name == "wrong-detail":
                    # A detail that is documented for one target but
                    # wrong for every simple target cannot read as
                    # correct for any: use a uniformly wrong string
                    # for wrong-detail here, plus per-target check
                    # below via script-mismatch variant is covered
                    # by the uniform string for this adapter.
                    body_fn = lambda _p: {"detail": "Wrong detail"}
                output, calls = self._cleanup_via_session(
                    make_adapter(lambda _p: 404, body_fn))
                self.assertNotIn("Cleaned:", output)
                self.assertNotIn("Nothing to clean", output)
                self.assertIn("No resources confirmed cleaned", output)
                self.assertIn("404", output)
                self.assertNotIn("<html>404</html>", output)
                delete_calls = [c for c in calls if c["method"] == "DELETE"]
                self.assertEqual(len(delete_calls), 5)

    # -- confirmed statuses (controls) -------------------------------

    def test_simple_confirmed_statuses_are_cleaned(self):
        for status in (200, 204, 404):
            with self.subTest(status=status):
                if status == 404:
                    routes = {url: self._resp_documented_404(url) for url in self._simple_urls()}
                else:
                    routes = {url: _resp(status, {}) for url in self._simple_urls()}
                output, calls, _ = self._run_cleanup(routes, _resp(200, []))
                self.assertEqual(routes.keys(), set(calls))
                cleaned = self._cleaned_line(output)
                self.assertTrue(cleaned.startswith("Cleaned:"), output)
                for label in ("test script", "builder project", "gen project", "test dataset"):
                    self.assertIn(label, cleaned)
                self.assertNotIn("Nothing to clean", output)
                self.assertNotIn("No resources confirmed cleaned", output)
                self.assertNotIn("Cleanup incomplete", output)
                self.assertNotIn("failed to delete", output)

    # -- simple failures ----------------------------------------------

    def test_simple_failure_statuses_are_not_cleaned_and_all_attempted(self):
        for status in (500, 401, 409, 202, 302):
            with self.subTest(status=status):
                routes = {url: _resp(status, {}) for url in self._simple_urls()}
                output, calls, _ = self._run_cleanup(routes, _resp(200, []))
                self.assertEqual(len(calls), 4)
                self.assertEqual(set(calls), set(routes.keys()))
                self.assertNotIn("Cleaned:", output)
                self.assertNotIn("Nothing to clean", output)
                self.assertIn("No resources confirmed cleaned", output)
                for label in ("test script", "builder project", "gen project", "test dataset"):
                    self.assertIn(label, output)
                    self.assertIn(str(status), output)

    def test_simple_mixed_success_and_failure_only_success_cleaned(self):
        urls = self._simple_urls()
        routes = {
            urls[0]: _resp(200, {}),
            urls[1]: _resp(500, {}),
            urls[2]: self._resp_documented_404(urls[2]),
            urls[3]: _resp(409, {}),
        }
        output, calls, _ = self._run_cleanup(routes, _resp(200, []))
        self.assertEqual(len(calls), 4)
        cleaned = self._cleaned_line(output)
        self.assertIn("test script", cleaned)
        self.assertIn("gen project", cleaned)
        self.assertNotIn("builder project", cleaned.split(":", 1)[1] if ":" in cleaned else "")
        self.assertNotIn("test dataset", cleaned.split(":", 1)[1] if ":" in cleaned else "")
        self.assertIn("builder project", output)
        self.assertIn("500", output)
        self.assertIn("test dataset", output)
        self.assertIn("409", output)
        self.assertIn("Cleanup incomplete", output)
        self.assertNotIn("Nothing to clean", output)

    def test_simple_transport_exception_still_attempts_remaining(self):
        urls = self._simple_urls()
        routes = {
            urls[0]: requests.exceptions.ConnectionError("boom"),
            urls[1]: _resp(200, {}),
            urls[2]: _resp(200, {}),
            urls[3]: _resp(200, {}),
        }
        output, calls, _ = self._run_cleanup(routes, _resp(200, []))
        # All four attempted despite the first raising.
        self.assertEqual(len(calls), 4)
        cleaned = self._cleaned_line(output)
        self.assertIn("builder project", cleaned)
        self.assertNotIn("test script", cleaned)
        self.assertIn("test script", output)
        self.assertIn("Cleanup incomplete", output)

    # -- voices -------------------------------------------------------

    def _voice_urls(self, voice_id):
        return f"/api/voice_design/{voice_id}"

    def test_voice_http500_then_later_voice_still_attempted_and_not_owned_untouched(self):
        prefix = test_api.TEST_PREFIX
        failed = f"{prefix}voice_failed"
        healthy = f"{prefix}voice_healthy"
        unowned = "real-user-voice"
        voices = [{"id": failed}, {"id": unowned}, {"id": healthy}]
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        routes[self._voice_urls(failed)] = _resp(500, {})
        routes[self._voice_urls(healthy)] = _resp(200, {})
        output, calls, _ = self._run_cleanup(routes, _resp(200, voices))
        # Simple 4 + 2 owned voices; unowned never deleted.
        self.assertEqual(len(calls), 6)
        self.assertNotIn(self._voice_urls(unowned), calls)
        self.assertIn(self._voice_urls(failed), calls)
        self.assertIn(self._voice_urls(healthy), calls)
        cleaned = self._cleaned_line(output)
        self.assertIn(healthy, cleaned)
        self.assertNotIn(failed, cleaned)
        self.assertIn(failed, output)
        self.assertIn("500", output)
        self.assertIn("Cleanup incomplete", output)

    def test_voice_transport_exception_then_later_voice_still_attempted(self):
        prefix = test_api.TEST_PREFIX
        first = f"{prefix}voice_first"
        second = f"{prefix}voice_second"
        voices = [{"id": first}, {"id": second}]
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        routes[self._voice_urls(first)] = requests.exceptions.ConnectionError("boom")
        routes[self._voice_urls(second)] = self._resp_documented_404(self._voice_urls(second))
        output, calls, _ = self._run_cleanup(routes, _resp(200, voices))
        self.assertIn(self._voice_urls(first), calls)
        self.assertIn(self._voice_urls(second), calls)
        cleaned = self._cleaned_line(output)
        self.assertIn(second, cleaned)
        self.assertNotIn(first, cleaned)
        self.assertIn(first, output)
        self.assertIn("Cleanup incomplete", output)

    def test_voice_unconfirmed_202_302_not_cleaned(self):
        for status in (202, 302):
            with self.subTest(status=status):
                prefix = test_api.TEST_PREFIX
                voice = f"{prefix}voice_unconfirmed"
                voices = [{"id": voice}]
                routes = {url: _resp(200, {}) for url in self._simple_urls()}
                routes[self._voice_urls(voice)] = _resp(status, {})
                output, calls, _ = self._run_cleanup(routes, _resp(200, voices))
                cleaned = self._cleaned_line(output)
                self.assertNotIn(voice, cleaned)
                self.assertIn(voice, output)
                self.assertIn(str(status), output)

    def test_voice_404_absence_counts_cleaned(self):
        prefix = test_api.TEST_PREFIX
        voice = f"{prefix}voice_gone"
        voices = [{"id": voice}]
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        routes[self._voice_urls(voice)] = self._resp_documented_404(self._voice_urls(voice))
        output, calls, _ = self._run_cleanup(routes, _resp(200, voices))
        cleaned = self._cleaned_line(output)
        self.assertIn(voice, cleaned)
        self.assertNotIn("Cleanup incomplete", output)

    # -- list failures -------------------------------------------------

    def test_voice_list_http_failure_reported_truthfully(self):
        for status in (500, 404):
            with self.subTest(status=status):
                routes = {url: _resp(200, {}) for url in self._simple_urls()}
                output, calls, _ = self._run_cleanup(routes, _resp(status, {}))
                # Simple still processed.
                self.assertEqual(len(calls), 4)
                self.assertIn("voice-design", output)
                self.assertIn(str(status), output)
                # Unknown voices must not print a false clean/empty success.
                self.assertNotIn("Nothing to clean", output)
                # With simple cleaned but voices unknown, incompleteness shows.
                self.assertIn("Cleanup incomplete", output)

    def test_voice_list_malformed_json_is_diagnostic_not_clean(self):
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        output, calls, _ = self._run_cleanup(routes, _resp_raw(200, "not-json"))
        self.assertEqual(len(calls), 4)
        self.assertIn("voice-design", output)
        self.assertNotIn("Nothing to clean", output)
        self.assertIn("Cleanup incomplete", output)

    def test_voice_list_transport_exception_is_best_effort(self):
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        output, calls, _ = self._run_cleanup(
            routes, requests.exceptions.ConnectionError("boom-list")
        )
        self.assertEqual(len(calls), 4)
        self.assertIn("voice-design", output)
        self.assertNotIn("Nothing to clean", output)
        self.assertIn("Cleanup incomplete", output)

    def test_all_failures_never_print_nothing_to_clean(self):
        routes = {url: _resp(500, {}) for url in self._simple_urls()}
        output, calls, _ = self._run_cleanup(routes, _resp(500, {}))
        self.assertEqual(len(calls), 4)
        self.assertNotIn("Cleaned:", output)
        self.assertNotIn("Nothing to clean", output)
        self.assertIn("No resources confirmed cleaned", output)

    # -- preservation / safety nets ------------------------------------

    def test_cancellation_propagates(self):
        # Simple path
        urls = self._simple_urls()
        seen = []
        def delete_raises_interrupt(url, *a, **k):
            seen.append(("delete", url, k))
            raise KeyboardInterrupt("stop")
        def get_empty(url, *a, **k):
            seen.append(("get", url, k))
            return _resp(200, [])
        stream = io.StringIO()
        with patch.object(test_api, "delete", side_effect=delete_raises_interrupt), patch.object(
            test_api, "get", side_effect=get_empty
        ), contextlib.redirect_stdout(stream):
            with self.assertRaises(KeyboardInterrupt):
                test_api.cleanup()
        self.assertEqual({"allow_redirects": False}, seen[0][2])
        # Voice path: first simple succeeds, voice raises interrupt
        prefix = test_api.TEST_PREFIX
        voice = f"{prefix}voice_interrupt"
        voices = [{"id": voice}]
        routes = {url: _resp(200, {}) for url in urls}
        seen_voice = []
        def delete_voice_interrupt(url, *a, **k):
            seen_voice.append((url, k))
            if url == f"/api/voice_design/{voice}":
                raise KeyboardInterrupt("stop-voice")
            return routes[url]
        def get_voices(url, *a, **k):
            seen_voice.append((url, k))
            return _resp(200, voices)
        stream = io.StringIO()
        with patch.object(test_api, "delete", side_effect=delete_voice_interrupt), patch.object(
            test_api, "get", side_effect=get_voices
        ), contextlib.redirect_stdout(stream):
            with self.assertRaises(KeyboardInterrupt):
                test_api.cleanup()
        for _url, kwargs in seen_voice:
            self.assertEqual({"allow_redirects": False}, kwargs)

    def test_cleanup_does_not_change_suite_counters(self):
        test_api.results.update(passed=3, failed=1, skipped=2)
        test_api.failures.append(("prev", "prev-error"))
        test_api.test_results.append(
            {"name": "prev", "requires_full": False, "status": "failed", "reason": "prev-error"}
        )
        before_results = dict(test_api.results)
        before_failures = list(test_api.failures)
        before_results_list = list(test_api.test_results)
        routes = {url: _resp(500, {}) for url in self._simple_urls()}
        _, _, _ = self._run_cleanup(routes, _resp(500, {}))
        self.assertEqual(before_results, test_api.results)
        self.assertEqual(before_failures, test_api.failures)
        self.assertEqual(before_results_list, test_api.test_results)
        # And on success path too.
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        _, _, _ = self._run_cleanup(routes, _resp(200, []))
        self.assertEqual(before_results, test_api.results)
        self.assertEqual(before_failures, test_api.failures)
        self.assertEqual(before_results_list, test_api.test_results)

    def test_cleanup_does_not_log_response_bodies(self):
        sentinel = "SUPER_SECRET_BODY_SENTINEL"
        routes = {url: _resp_raw(500, f'{{"error":"{sentinel}"}}') for url in self._simple_urls()}
        voices = [{"id": f"{test_api.TEST_PREFIX}voice_body"}]
        routes[self._voice_urls(f"{test_api.TEST_PREFIX}voice_body")] = _resp_raw(
            500, f'{{"error":"{sentinel}"}}'
        )
        output, calls, _ = self._run_cleanup(routes, _resp(200, voices))
        self.assertNotIn(sentinel, output)
        self.assertIn("HTTP 500", output)

    # -- main exit policy + real summary -------------------------------

    def _run_main(self, run_side_effect):
        summary_path = None
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.json"
            test_api.results.update(passed=0, failed=0, skipped=0)
            test_api.failures.clear()
            test_api.test_results.clear()
            routes = {url: _resp(500, {}) for url in self._simple_urls()}
            # List also fails; cleanup must not affect counters/exit.
            seen_delete = []
            seen_get = []
            def fake_delete(url, *a, **k):
                seen_delete.append(k)
                return routes[url]
            def fake_get(url, *a, **k):
                seen_get.append(k)
                return _resp(500, {})
            stream = io.StringIO()
            with patch.object(test_api, "run_all_tests", side_effect=run_side_effect), patch.object(
                test_api, "delete", side_effect=fake_delete
            ), patch.object(test_api, "get", side_effect=fake_get), patch(
                "sys.argv", ["test_api.py", "--url", "http://127.0.0.1:4200", "--json-summary", str(summary_path)]
            ), contextlib.redirect_stdout(stream):
                try:
                    test_api.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
            for kwargs in seen_delete:
                self.assertEqual({"allow_redirects": False}, kwargs)
            for kwargs in seen_get:
                self.assertEqual({"allow_redirects": False}, kwargs)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            return code, summary

    def test_main_retains_exit_zero_for_passed_suite(self):
        def run_suite():
            test_api.run_test("passes", lambda: None)
        code, summary = self._run_main(run_suite)
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"]["passed"], 1)
        self.assertEqual(summary["counts"]["failed"], 0)
        self.assertEqual(len(summary["tests"]), 1)
        self.assertEqual(summary["tests"][0]["status"], "passed")

    def test_main_retains_exit_one_for_failed_suite(self):
        def run_suite():
            def _fail():
                raise test_api.TestFailure("bad result")
            test_api.run_test("fails", _fail)
        code, summary = self._run_main(run_suite)
        self.assertEqual(code, 1)
        self.assertEqual(summary["counts"]["passed"], 0)
        self.assertEqual(summary["counts"]["failed"], 1)
        self.assertEqual(len(summary["tests"]), 1)
        self.assertEqual(summary["tests"][0]["status"], "failed")

    # -- malformed voice rows (root regression, raw _load_manifest) --

    def _malformed_cases(self):
        prefix = test_api.TEST_PREFIX
        healthy = f"{prefix}voice_healthy"
        unowned = "real-user-voice"
        # Each case is one malformed row that must be diagnosed/counted,
        # never deleted (no id coercion), and must not block the healthy
        # owned id that follows it. Valid non-owned strings stay untouched.
        return [
            None,
            42,
            ["not-a-dict"],
            {"no_id_field": "x"},
            {"id": None},
            {"id": 123},
            {"id": [f"{prefix}should_not_delete"]},
            {"id": {"id": f"{prefix}should_not_delete"}},
            {"id": True},
            {"id": ""},
        ]

    def _expected_malformed_urls(self):
        # URLs that must never be attempted for malformed ids even if a
        # coercing implementation would stringify them.
        prefix = test_api.TEST_PREFIX
        return [
            f"/api/voice_design/{prefix}should_not_delete",
            "/api/voice_design/123",
            "/api/voice_design/True",
            "/api/voice_design/",
        ]

    def test_malformed_voice_rows_are_invalid_and_later_owned_attempted(self):
        prefix = test_api.TEST_PREFIX
        healthy = f"{prefix}voice_healthy"
        unowned = "real-user-voice"
        for idx, malformed in enumerate(self._malformed_cases()):
            with self.subTest(case=idx, row=repr(malformed)):
                voices = [malformed, {"id": healthy}, {"id": unowned}]
                routes = {url: _resp(200, {}) for url in self._simple_urls()}
                routes[self._voice_urls(healthy)] = _resp(200, {})
                output, calls, _ = self._run_cleanup(routes, _resp(200, voices))
                # 4 simple + healthy only; unowned/malformed never deleted.
                self.assertEqual(len(calls), 5)
                self.assertIn(self._voice_urls(healthy), calls)
                self.assertNotIn(self._voice_urls(unowned), calls)
                for bad_url in self._expected_malformed_urls():
                    self.assertNotIn(bad_url, calls)
                cleaned = self._cleaned_line(output)
                self.assertIn(healthy, cleaned)
                self.assertIn("invalid voice-design", output)
                self.assertIn("Cleanup incomplete", output)
                self.assertNotIn("Nothing to clean", output)
                self.assertNotIn("failed to list voice-design", output.replace(
                    "invalid voice-design", ""))

    def test_multiple_malformed_rows_counted_and_valid_strings_untouched(self):
        prefix = test_api.TEST_PREFIX
        healthy1 = f"{prefix}voice_ok_one"
        healthy2 = f"{prefix}voice_ok_two"
        unowned = "real-user-voice"
        voices = [
            None,
            {"id": None},
            {"id": 42},
            {"id": healthy1},
            {"id": unowned},
            {"id": ""},
            {"id": healthy2},
        ]
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        routes[self._voice_urls(healthy1)] = _resp(200, {})
        routes[self._voice_urls(healthy2)] = self._resp_documented_404(self._voice_urls(healthy2))
        output, calls, _ = self._run_cleanup(routes, _resp(200, voices))
        self.assertEqual(len(calls), 6)
        self.assertIn(self._voice_urls(healthy1), calls)
        self.assertIn(self._voice_urls(healthy2), calls)
        self.assertNotIn(self._voice_urls(unowned), calls)
        cleaned = self._cleaned_line(output)
        self.assertIn(healthy1, cleaned)
        self.assertIn(healthy2, cleaned)
        # Four invalid rows (None, None-id, int-id, empty) diagnosed/counted.
        self.assertIn("invalid voice-design", output)
        self.assertIn("Cleanup incomplete", output)
        self.assertNotIn("Nothing to clean", output)

    def test_voice_list_nonlist_json_is_diagnostic(self):
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        output, calls, _ = self._run_cleanup(routes, _resp(200, {"id": "x"}))
        self.assertEqual(len(calls), 4)
        self.assertIn("voice-design", output)
        self.assertNotIn("Nothing to clean", output)
        self.assertIn("Cleanup incomplete", output)

    def test_malformed_rows_preserve_keyboard_interrupt(self):
        prefix = test_api.TEST_PREFIX
        healthy = f"{prefix}voice_interrupt"
        voices = [None, {"id": None}, {"id": healthy}]
        routes = {url: _resp(200, {}) for url in self._simple_urls()}
        seen = []
        def delete_interrupt(url, *a, **k):
            seen.append(k)
            if url == f"/api/voice_design/{healthy}":
                raise KeyboardInterrupt("stop-after-malformed")
            return routes[url]
        def get_voices(url, *a, **k):
            seen.append(k)
            return _resp(200, voices)
        stream = io.StringIO()
        with patch.object(test_api, "delete", side_effect=delete_interrupt), patch.object(
            test_api, "get", side_effect=get_voices
        ), contextlib.redirect_stdout(stream):
            with self.assertRaises(KeyboardInterrupt):
                test_api.cleanup()
        for kwargs in seen:
            self.assertEqual({"allow_redirects": False}, kwargs)

    def _run_main_with_voices(self, run_side_effect, list_response):
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.json"
            test_api.results.update(passed=0, failed=0, skipped=0)
            test_api.failures.clear()
            test_api.test_results.clear()
            prefix = test_api.TEST_PREFIX
            healthy = f"{prefix}voice_main_healthy"
            routes = {url: _resp(200, {}) for url in self._simple_urls()}
            routes[self._voice_urls(healthy)] = _resp(200, {})
            seen_delete = []
            seen_get = []
            def fake_delete(url, *a, **k):
                seen_delete.append(k)
                return routes[url]
            def fake_get(url, *a, **k):
                seen_get.append(k)
                return list_response
            stream = io.StringIO()
            with patch.object(test_api, "run_all_tests", side_effect=run_side_effect), patch.object(
                test_api, "delete", side_effect=fake_delete
            ), patch.object(test_api, "get", side_effect=fake_get), patch(
                "sys.argv", ["test_api.py", "--url", "http://127.0.0.1:4200", "--json-summary", str(summary_path)]
            ), contextlib.redirect_stdout(stream):
                try:
                    test_api.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
            for kwargs in seen_delete:
                self.assertEqual({"allow_redirects": False}, kwargs)
            for kwargs in seen_get:
                self.assertEqual({"allow_redirects": False}, kwargs)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            return code, summary, stream.getvalue()

    def test_main_with_malformed_voice_rows_preserves_exit_and_report(self):
        prefix = test_api.TEST_PREFIX
        healthy = f"{prefix}voice_main_healthy"
        list_response = _resp(200, [None, {"id": None}, {"id": 123}, {"id": healthy}])
        def run_passing():
            test_api.run_test("passes", lambda: None)
        code, summary, output = self._run_main_with_voices(run_passing, list_response)
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"]["passed"], 1)
        self.assertEqual(summary["counts"]["failed"], 0)
        self.assertEqual(summary["tests"][0]["status"], "passed")
        self.assertIn("invalid voice-design", output)
        self.assertIn("Cleanup incomplete", output)
        self.assertIn(healthy, output)
        # Failed suite keeps exit 1 with same malformed cleanup.
        def run_failing():
            def _fail():
                raise test_api.TestFailure("bad result")
            test_api.run_test("fails", _fail)
        code, summary, output = self._run_main_with_voices(run_failing, list_response)
        self.assertEqual(code, 1)
        self.assertEqual(summary["counts"]["passed"], 0)
        self.assertEqual(summary["counts"]["failed"], 1)
        self.assertEqual(summary["tests"][0]["status"], "failed")
        self.assertIn("invalid voice-design", output)


if __name__ == "__main__":
    unittest.main()
