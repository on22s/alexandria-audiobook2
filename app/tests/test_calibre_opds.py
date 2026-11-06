"""Synthetic real-HTTP tests for app/calibre_opds.py (issue #1734, slice 1).

Every transport test runs against a real local raw-socket HTTP server
serving synthetic OPDS/EPUB bytes -- no mocks of the wire, no live
catalog, no private books. Fake-transport tests cover only pure
policy/URL logic plus streaming/lifecycle guarantees (F2/F3) where the
wire would be artificial.

Why a raw-socket receiver (same pattern as the Audiobookshelf tests,
``test_audiobookshelf_delivery.RawAbsServer``): the stdlib
``ThreadingHTTPServer`` intermittently resets loopback connections on
Windows with rapid fresh connections (~1 in 7). A production client must
not replay an already-sent request to mask that (F1), so the receiver
itself is made deterministic instead: explicit HTTP framing, a graceful
close (FIN after the response drains, never an RST: drain reads +
SO_LINGER), a large listen backlog, per-instance state, and tracked
handler threads that ``stop()`` joins before asserting no handler error.

Test servers keep per-instance state on the server object (F6): the old
class-level ``Handler.routes``/``seen_*`` conflated two origins into one
log, which made cross-origin assertions meaningless. Assertions below are
exact (``assertEqual`` on full request/auth logs), never relaxed with
``>=`` or "retries may repeat": a correct client issues each request once
(F1), so any duplicate means the test fails.
"""
import socket
import struct
import threading
import time
import unittest
from pathlib import Path
import tempfile
from unittest import mock

import calibre_opds as opds

EPUB_BYTES = b"PK\x03\x04" + b"synthetic-epub-payload" * 64
ATOM = 'xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog" xmlns:dc="http://purl.org/dc/terms/"'
AUTH_USER, AUTH_PASS = "synthetic-user", "synthetic-pass"
EXPECTED_AUTH = opds.OpdsAuth(AUTH_USER, AUTH_PASS).header_value()


def feed_xml(title, entries, links=""):
    body = "".join(
        f"<entry><id>urn:synthetic:{eid}</id><title>{t}</title>"
        f"<author><name>{a}</name></author>"
        f'<category scheme="http://opds-spec.org/2010/categories/series" label="{s} - {i}"/>'
        f'<link href="{href}" rel="http://opds-spec.org/acquisition" type="{mime}"/>'
        f"</entry>"
        for eid, t, a, s, i, href, mime in entries)
    return f"<?xml version='1.0'?><feed {ATOM}><title>{title}</title>{links}{body}</feed>".encode()


class RawOpdsServer:
    """Per-instance raw-socket HTTP/1.1 receiver (see module docstring).

    Routes: ``(status, headers, body)`` normally; ``("stall", delay)`` and
    ``("dribble", total, interval, content_type)`` for F5 deadline tests.
    ``stop()`` closes the listener, joins the accept and handler threads,
    and raises if a handler recorded a genuine error (client-closed
    connections during timeout tests are expected and ignored).
    """

    _MAX_HEADER_BYTES = 65536
    _REASONS = {200: "OK", 302: "Found", 401: "Unauthorized",
                404: "Not Found", 500: "Internal Server Error"}

    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self.routes = {}
        self.seen_paths = []
        self.seen_auth = []      # (path, Authorization or None), in order
        self.errors = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._handlers = set()
        self._thread = None

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def route(self, path, body, status=200, headers=None):
        self.routes[path] = (status, headers or {"Content-Type": "application/atom+xml"}, body)

    def route_stall(self, path, delay):
        self.routes[path] = ("stall", delay)

    def route_dribble(self, path, total, interval, content_type="application/atom+xml",
                      prefix=b"x" * 64):
        self.routes[path] = ("dribble", total, interval, content_type, prefix)

    def start(self):
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.listener.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            handler.join(timeout=10)
        if self.errors:
            raise AssertionError(f"RawOpdsServer errors: {self.errors}")

    def _record_error(self, label, error):
        with self._lock:
            self.errors.append(f"{label}: {type(error).__name__}: {error}")

    def _serve(self):
        try:
            self.listener.settimeout(0.2)
        except OSError:
            return
        while not self._stop.is_set():
            try:
                connection, _addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            try:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            handler = threading.Thread(target=self._handle, args=(connection,), daemon=True)
            with self._lock:
                self._handlers.add(handler)
            handler.start()

    def _read_request(self, connection):
        connection.settimeout(5)
        raw = b""
        while b"\r\n\r\n" not in raw:
            if len(raw) > self._MAX_HEADER_BYTES:
                raise ValueError("header too large")
            block = connection.recv(65536)
            if not block:
                return None
            raw += block
        head = raw.split(b"\r\n\r\n", 1)[0]
        lines = head.decode("iso-8859-1").split("\r\n")
        method, path, _version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ": " in line:
                key, value = line.split(": ", 1)
                headers[key.lower()] = value
        return method, path, headers

    def _handle(self, connection):
        response_sent = False
        try:
            request = self._read_request(connection)
            if request is None:
                return
            method, path, headers = request
            with self._lock:
                self.seen_paths.append(path)
                self.seen_auth.append((path, headers.get("authorization")))
                route = self.routes.get(path, (404, {}, b"nope"))
            if route and route[0] == "stall":
                time.sleep(route[1])
                body = b"<feed/>"
                try:
                    connection.sendall(self._response(200, {"Content-Type": "application/atom+xml"}, body))
                except OSError:
                    pass  # client already timed out; expected in stall tests
                response_sent = True
            elif route and route[0] == "dribble":
                _tag, total, interval, content_type, prefix = route
                head = (f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                        f"Content-Length: {total}\r\nConnection: close\r\n\r\n").encode("iso-8859-1")
                connection.sendall(head)
                response_sent = True
                sent = 0
                while sent < total:
                    if sent == 0 and prefix:
                        chunk = prefix[:min(len(prefix), total)]
                    else:
                        chunk = b"x" * min(64, total - sent)
                    connection.sendall(chunk)
                    sent += len(chunk)
                    time.sleep(interval)
            else:
                status, rheaders, body = route
                if isinstance(body, str):
                    body = body.encode()
                connection.sendall(self._response(status, rheaders, body))
                response_sent = True
            # Graceful close: drain until the client closes (FIN, never RST).
            try:
                connection.settimeout(0.3)
                while connection.recv(65536):
                    pass
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client closed early (timeout/cancel tests); expected
        except Exception as error:
            if not response_sent and not isinstance(error, (ConnectionResetError, BrokenPipeError)):
                self._record_error("handle", error)
        finally:
            try:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 5))
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
            with self._lock:
                self._handlers.discard(threading.current_thread())

    def _response(self, status, headers, body):
        reason = self._REASONS.get(status, "Status")
        lines = [f"HTTP/1.1 {status} {reason}"]
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        lines.append(f"Content-Length: {len(body)}")
        lines.append("Connection: close")
        return "\r\n".join(lines).encode("iso-8859-1") + b"\r\n\r\n" + body


def start_server():
    return RawOpdsServer().start()


def stop_server(server):
    server.stop()


class ServerMixin:
    def setUp(self):
        self.server = start_server()
        self.port = self.server.port
        self.root = f"http://127.0.0.1:{self.port}/opds"
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        stop_server(self.server)

    def route(self, path, body, status=200, headers=None):
        self.server.routes[path] = (status, headers or {"Content-Type": "application/atom+xml"}, body)

    def fetch(self, url, **kw):
        kw.setdefault("root_url", self.root)
        kw.setdefault("policy", opds.DestinationPolicy())
        kw.setdefault("limit", opds.Limits(timeout_seconds=5))
        return opds.fetch_feed(url, **kw)


class FeedParsingTests(ServerMixin, unittest.TestCase):
    def test_prefix_paths_resolve_against_feed_url(self):
        self.route("/lib/opds/books", feed_xml("F", [("1", "Title A", "Author A", "Series", "2", "files/a.epub", "application/epub+zip")]))
        feed = self.fetch(f"{self.origin}/lib/opds/books")
        self.assertEqual(feed.feed_url, f"{self.origin}/lib/opds/books")
        self.assertEqual(feed.entries[0].acquisition.href, f"{self.origin}/lib/opds/files/a.epub")
        self.assertEqual(feed.entries[0].series, "Series")
        self.assertEqual(feed.entries[0].series_index, "2")

    def test_drm_and_non_epub_rejected(self):
        self.route("/opds/drm", feed_xml("F", [
            ("1", "DRM Book", "A", "S", "1", "x.epub", "application/vnd.adobe.adept+xml"),
            ("2", "PDF Book", "A", "S", "1", "x.pdf", "application/pdf"),
            ("3", "Good", "A", "S", "1", "x.epub", "application/epub+zip")]))
        feed = self.fetch(f"{self.origin}/opds/drm")
        self.assertEqual(feed.entries[0].rejected_reason, "protected/DRM acquisition refused")
        self.assertIsNone(feed.entries[0].acquisition)
        self.assertIn("unsupported", feed.entries[1].rejected_reason)
        self.assertIsNotNone(feed.entries[2].acquisition)

    def test_doctype_rejected_and_oversize_feed(self):
        self.route("/opds/doctype", b"<!DOCTYPE feed [<!ENTITY x 'y'>]><feed " + ATOM.encode() + b"/>")
        with self.assertRaises(opds.OpdsError):
            self.fetch(f"{self.origin}/opds/doctype")
        big = feed_xml("F", [("1", "T", "A", "S", "1", "a.epub", "application/epub+zip")]) + b" " * 5000
        self.route("/opds/big", big)
        with self.assertRaises(opds.OversizeError):
            self.fetch(f"{self.origin}/opds/big", limit=opds.Limits(max_feed_bytes=1000, timeout_seconds=5))

    def test_doctype_beyond_first_4096_rejected(self):
        # F6: DOCTYPE pushed past 4096 bytes by filler must still be caught.
        tail = b"<!DOCTYPE feed [<!ENTITY x 'y'>]><feed " + ATOM.encode() + b"/>"
        body = b"<?xml version='1.0'?>" + b" " * 5000 + tail
        self.route("/opds/doctype-late", body)
        with self.assertRaises(opds.OpdsError) as ctx:
            self.fetch(f"{self.origin}/opds/doctype-late")
        self.assertIn("DOCTYPE", str(ctx.exception))

    def test_broken_link_and_auth_failure(self):
        with self.assertRaises(opds.BrokenLinkError):
            self.fetch(f"{self.origin}/missing")
        self.route("/opds/needauth", b"no", status=401)
        with self.assertRaises(opds.AuthFailureError):
            self.fetch(f"{self.origin}/opds/needauth")

    def test_duplicate_entries_deduped(self):
        self.route("/opds/dup", feed_xml("F", [
            ("1", "Same", "A", "S", "1", "a.epub", "application/epub+zip"),
            ("1", "Same", "A", "S", "1", "a.epub", "application/epub+zip")]))
        feed = self.fetch(f"{self.origin}/opds/dup")
        self.assertEqual(len(feed.entries), 1)


class PaginationTests(ServerMixin, unittest.TestCase):
    def test_pagination_collects_and_dedupes(self):
        self.route("/opds/p1", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                        '<link href="p2" rel="next"/>'))
        self.route("/opds/p2", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip"),
                                              ("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        feed = opds.collect_pages(f"{self.origin}/opds/p1", root_url=self.root,
                                  policy=opds.DestinationPolicy(), limit=opds.Limits(timeout_seconds=5))
        self.assertEqual([e.title for e in feed.entries], ["One", "Two"])

    def test_pagination_loop_refused(self):
        self.route("/opds/loop", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                          '<link href="loop" rel="next"/>'))
        with self.assertRaises(opds.PaginationLoopError):
            opds.collect_pages(f"{self.origin}/opds/loop", root_url=self.root,
                               policy=opds.DestinationPolicy(), limit=opds.Limits(timeout_seconds=5))

    def test_cross_origin_next_allowed_gets_no_credentials(self):
        # F4: TWO independent servers (per-instance state, F6). A page 1
        # links across to B page 2; B is explicitly allowed. B must be
        # fetched (entries collected) with NO Authorization; A must retain
        # it. Anchoring is done once; B must not rebind the trusted origin.
        other = start_server()
        try:
            b_origin = other.origin
            other.route("/opds/p2", feed_xml("B", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
            self.route("/opds/p1", feed_xml("A", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                            f'<link href="{b_origin}/opds/p2" rel="next"/>'))
            policy = opds.DestinationPolicy(frozenset({b_origin}))
            feed = opds.collect_pages(f"{self.origin}/opds/p1", policy=policy,
                                      auth=opds.OpdsAuth(AUTH_USER, AUTH_PASS),
                                      limit=opds.Limits(timeout_seconds=5))
            self.assertEqual([e.title for e in feed.entries], ["One", "Two"])
            # Exact per-server logs: one GET each (F1: no duplicate GET).
            self.assertEqual(self.server.seen_paths, ["/opds/p1"])
            self.assertEqual(self.server.seen_auth, [("/opds/p1", EXPECTED_AUTH)])
            self.assertEqual(other.seen_paths, ["/opds/p2"])
            self.assertEqual(other.seen_auth, [("/opds/p2", None)])
        finally:
            stop_server(other)

    def test_cross_origin_next_disallowed_never_fetched(self):
        other = start_server()
        try:
            b_origin = other.origin
            other.route("/opds/p2", feed_xml("B", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
            self.route("/opds/p1", feed_xml("A", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                            f'<link href="{b_origin}/opds/p2" rel="next"/>'))
            with self.assertRaises(opds.CrossOriginReviewRequired):
                opds.collect_pages(f"{self.origin}/opds/p1",
                                   policy=opds.DestinationPolicy(),
                                   auth=opds.OpdsAuth(AUTH_USER, AUTH_PASS),
                                   limit=opds.Limits(timeout_seconds=5))
            self.assertEqual(other.seen_paths, [])  # review gate fires before B is fetched
        finally:
            stop_server(other)


class CaptureTests(ServerMixin, unittest.TestCase):
    def _entry(self):
        self.route("/opds/book.epub", EPUB_BYTES, headers={"Content-Type": "application/epub+zip"})
        return opds.OpdsEntry(stable_id="urn:x", title="T", acquisition=opds.OpdsLink(href=f"{self.origin}/opds/book.epub", rel="http://opds-spec.org/acquisition", type="application/epub+zip"))

    def test_capture_streams_with_sha_and_preserves_bytes(self):
        entry = self._entry()
        with tempfile.TemporaryDirectory() as td:
            before = set(Path(td).iterdir())
            cap = opds.capture_epub(entry, td, root_url=self.root, policy=opds.DestinationPolicy(),
                                    limit=opds.Limits(timeout_seconds=5))
            self.assertEqual(Path(cap.path).read_bytes(), EPUB_BYTES)
            self.assertEqual(cap.size_bytes, len(EPUB_BYTES))
            self.assertNotIn(Path(cap.path), before)  # owned new artifact only
            opds.discard_capture(cap)
            self.assertFalse(Path(cap.path).exists())

    def test_capture_oversize_and_cancel_delete_partial(self):
        entry = self._entry()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(opds.OversizeError):
                opds.capture_epub(entry, td, root_url=self.root, policy=opds.DestinationPolicy(),
                                  limit=opds.Limits(max_download_bytes=10, timeout_seconds=5))
            self.assertEqual(list(Path(td).iterdir()), [])
            with self.assertRaises(opds.CancelledError):
                opds.capture_epub(entry, td, root_url=self.root, policy=opds.DestinationPolicy(),
                                  limit=opds.Limits(timeout_seconds=5), should_cancel=lambda: True)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_capture_non_epub_refused(self):
        self.route("/opds/book.epub", b"not a zip at all", headers={"Content-Type": "application/epub+zip"})
        entry = opds.OpdsEntry(stable_id="urn:x", title="T", acquisition=opds.OpdsLink(href=f"{self.origin}/opds/book.epub", rel="http://opds-spec.org/acquisition", type="application/epub+zip"))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(opds.ProtectedOrUnsupportedError):
                opds.capture_epub(entry, td, root_url=self.root, policy=opds.DestinationPolicy(),
                                  limit=opds.Limits(timeout_seconds=5))
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_capture_invalid_mime_leaves_no_file_and_keeps_unrelated(self):
        # F3 (real wire): invalid Content-Type must be refused before any
        # staging allocation; a pre-existing unrelated file stays; the
        # response still closes (server sees exactly one GET).
        self.route("/opds/book.epub", EPUB_BYTES, headers={"Content-Type": "text/html"})
        entry = opds.OpdsEntry(stable_id="urn:x", title="T", acquisition=opds.OpdsLink(href=f"{self.origin}/opds/book.epub", rel="http://opds-spec.org/acquisition", type="application/epub+zip"))
        with tempfile.TemporaryDirectory() as td:
            keep = Path(td) / "keep.txt"
            keep.write_bytes(b"unrelated")
            with self.assertRaises(opds.ProtectedOrUnsupportedError):
                opds.capture_epub(entry, td, root_url=self.root, policy=opds.DestinationPolicy(),
                                  limit=opds.Limits(timeout_seconds=5))
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["keep.txt"])
            self.assertEqual(keep.read_bytes(), b"unrelated")
            self.assertEqual(self.server.seen_paths, ["/opds/book.epub"])


class _FakeResponse:
    """Streaming fake used only for F2/F3 lifecycle accounting."""

    def __init__(self, status_code=200, headers=None, chunks=EPUB_BYTES, url="http://127.0.0.1/x"):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.url = url
        self._chunks = list(chunks) if isinstance(chunks, (list, tuple)) else [chunks]
        self.closed = False
        self.read_count = 0
        self.generator_closed = False

    def _gen(self, chunk_size):
        try:
            for chunk in self._chunks:
                self.read_count += 1
                yield chunk
        finally:
            # GeneratorExit path: records that the stream was stopped
            # early (closed before exhaustion) instead of being left
            # suspended by the consumer.
            self.generator_closed = True

    def iter_content(self, chunk_size):
        return self._gen(chunk_size)

    def close(self):
        self.closed = True


class _FakeTransport:
    def __init__(self, response):
        self.response = response
        self.get_calls = []

    def get(self, url, headers, timeout):
        self.get_calls.append((url, dict(headers), timeout))
        return self.response


class ReadCapTests(unittest.TestCase):
    def test_read_capped_stops_at_first_over_cap_chunk(self):
        # F2: cap 1000 bytes with 64-byte chunks must stop after ~16
        # chunks, not consume the full 2000-chunk body; the generator must
        # be closed (not left suspended) and the response closed.
        fake = _FakeResponse(chunks=[b"x" * 64] * 2000)
        # Direct unit check of the streaming guard.
        with self.assertRaises(opds.OversizeError):
            opds._read_capped(fake, opds.Limits(max_feed_bytes=1000, chunk_bytes=64, timeout_seconds=5), "feed")
        self.assertEqual(fake.read_count, 16)  # 15 fit, 16th crosses the cap
        self.assertTrue(fake.generator_closed)
        self.assertFalse(fake.closed)  # caller owns closing
        # Via fetch_feed: the same early stop + response close must hold.
        fake2 = _FakeResponse(chunks=[b"x" * 64] * 2000)
        transport = _FakeTransport(fake2)
        with self.assertRaises(opds.OversizeError):
            opds.fetch_feed("http://127.0.0.1/opds", transport=transport,
                            limit=opds.Limits(max_feed_bytes=1000, chunk_bytes=64, timeout_seconds=5))
        self.assertEqual(fake2.read_count, 16)
        self.assertTrue(fake2.generator_closed)
        self.assertTrue(fake2.closed)
        self.assertEqual(len(transport.get_calls), 1)

    def test_response_closed_on_success_error_and_redirect(self):
        # F6: every error/success path closes the response (conn included
        # for the real transport via _HttpResponse.close).
        good = _FakeResponse(chunks=feed_xml("F", []), url="http://127.0.0.1/opds")
        feed = opds.fetch_feed("http://127.0.0.1/opds", transport=_FakeTransport(good),
                               limit=opds.Limits(timeout_seconds=5))
        self.assertEqual(feed.entries, ())
        self.assertTrue(good.closed)
        missing = _FakeResponse(status_code=404)
        with self.assertRaises(opds.BrokenLinkError):
            opds.fetch_feed("http://127.0.0.1/missing", transport=_FakeTransport(missing),
                            limit=opds.Limits(timeout_seconds=5))
        self.assertTrue(missing.closed)
        unauth = _FakeResponse(status_code=401)
        with self.assertRaises(opds.AuthFailureError):
            opds.fetch_feed("http://127.0.0.1/opds", transport=_FakeTransport(unauth),
                            limit=opds.Limits(timeout_seconds=5))
        self.assertTrue(unauth.closed)

    def test_fetch_feed_cancel_before_any_request(self):
        fake = _FakeResponse(chunks=feed_xml("F", []))
        transport = _FakeTransport(fake)
        with self.assertRaises(opds.CancelledError):
            opds.fetch_feed("http://127.0.0.1/opds", transport=transport,
                            limit=opds.Limits(timeout_seconds=5), should_cancel=lambda: True)
        self.assertEqual(transport.get_calls, [])


class CaptureLifecycleTests(unittest.TestCase):
    def _entry_for_fake(self, url="http://127.0.0.1/book.epub"):
        return opds.OpdsEntry(stable_id="urn:x", title="T",
                              acquisition=opds.OpdsLink(href=url, rel="acquisition", type="application/epub+zip"))

    def test_invalid_mime_no_allocation_and_response_closed(self):
        fake = _FakeResponse(headers={"Content-Type": "text/html"}, chunks=EPUB_BYTES)
        transport = _FakeTransport(fake)
        with tempfile.TemporaryDirectory() as td:
            keep = Path(td) / "keep.txt"
            keep.write_bytes(b"unrelated")
            with self.assertRaises(opds.ProtectedOrUnsupportedError):
                opds.capture_epub(self._entry_for_fake(), td, transport=transport,
                                  limit=opds.Limits(timeout_seconds=5))
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["keep.txt"])
            self.assertTrue(fake.closed)
            self.assertEqual(fake.read_count, 0)  # body never consumed

    def test_mkstemp_failure_closes_response_and_leaves_nothing(self):
        fake = _FakeResponse(headers={"Content-Type": "application/epub+zip"}, chunks=EPUB_BYTES)
        transport = _FakeTransport(fake)
        with tempfile.TemporaryDirectory() as td:
            keep = Path(td) / "keep.txt"
            keep.write_bytes(b"unrelated")
            with mock.patch.object(opds.tempfile, "mkstemp",
                                   side_effect=OSError("simulated allocation failure")):
                with self.assertRaises(OSError):
                    opds.capture_epub(self._entry_for_fake(), td, transport=transport,
                                      limit=opds.Limits(timeout_seconds=5))
            self.assertTrue(fake.closed)
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["keep.txt"])
            self.assertEqual(keep.read_bytes(), b"unrelated")


class RedirectAndAuthTests(ServerMixin, unittest.TestCase):
    def test_same_origin_redirect_followed_and_auth_scoped(self):
        self.route("/opds/redir", b"", status=302, headers={"Location": "/opds/final"})
        self.route("/opds/final", feed_xml("F", [("1", "T", "A", "S", "1", "a.epub", "application/epub+zip")]))
        feed = self.fetch(f"{self.origin}/opds/redir",
                          auth=opds.OpdsAuth(AUTH_USER, AUTH_PASS))
        self.assertEqual(feed.feed_url, f"{self.origin}/opds/final")
        # Exact logs (F1): one GET per hop, auth on both same-origin hops.
        self.assertEqual(self.server.seen_paths, ["/opds/redir", "/opds/final"])
        self.assertEqual(self.server.seen_auth,
                         [("/opds/redir", EXPECTED_AUTH), ("/opds/final", EXPECTED_AUTH)])
        self.assertNotIn(AUTH_PASS, feed.feed_url)

    def test_cross_origin_redirect_requires_review_before_fetch(self):
        self.route("/opds/redir", b"", status=302, headers={"Location": "http://127.0.0.1:9/evil"})
        with self.assertRaises(opds.CrossOriginReviewRequired) as ctx:
            self.fetch(f"{self.origin}/opds/redir", auth=opds.OpdsAuth(AUTH_USER, AUTH_PASS))
        self.assertEqual(self.server.seen_paths, ["/opds/redir"])  # never fetched evil
        self.assertNotIn(AUTH_PASS, str(ctx.exception))

    def test_cross_origin_allowed_gets_no_credentials(self):
        # TWO independent servers (F4/F6): redirect from A to B. B is
        # fetched with NO Authorization; A retains credentials on its hop.
        other = start_server()
        try:
            b_origin = other.origin
            other.route("/opds/final", feed_xml("F", [("1", "T", "A", "S", "1", "a.epub", "application/epub+zip")]))
            self.route("/opds/redir", b"", status=302,
                       headers={"Location": f"{b_origin}/opds/final"})
            policy = opds.DestinationPolicy(frozenset({b_origin}))
            feed = self.fetch(f"{self.origin}/opds/redir", policy=policy,
                              auth=opds.OpdsAuth(AUTH_USER, AUTH_PASS))
            self.assertEqual(feed.entries[0].title, "T")
            self.assertEqual(feed.feed_url, f"{b_origin}/opds/final")
            self.assertEqual(self.server.seen_paths, ["/opds/redir"])
            self.assertEqual(self.server.seen_auth, [("/opds/redir", EXPECTED_AUTH)])
            self.assertEqual(other.seen_paths, ["/opds/final"])
            self.assertEqual(other.seen_auth, [("/opds/final", None)])
        finally:
            stop_server(other)

    def test_explicitly_allowed_cross_origin_gets_no_credentials(self):
        self.route("/opds/redir", b"", status=302, headers={"Location": f"http://127.0.0.1:{self.port}/opds/final"})
        # same host:port is same origin -- use a policy listing it explicitly anyway
        self.route("/opds/final", feed_xml("F", [("1", "T", "A", "S", "1", "a.epub", "application/epub+zip")]))
        feed = self.fetch(f"{self.origin}/opds/redir",
                          policy=opds.DestinationPolicy(frozenset({self.origin})),
                          auth=opds.OpdsAuth(AUTH_USER, AUTH_PASS))
        self.assertEqual(feed.entries[0].title, "T")
        self.assertEqual(self.server.seen_paths, ["/opds/redir", "/opds/final"])


class DeadlineTests(ServerMixin, unittest.TestCase):
    def test_pre_header_stall_times_out(self):
        # F5: server stalls before headers; the client must fail within
        # the total budget, not hang until the handler finishes.
        stall = start_server()
        stall.route_stall("/opds/slow", 3)
        try:
            origin = stall.origin
            started = time.monotonic()
            with self.assertRaises(opds.OpdsError) as ctx:
                opds.fetch_feed(f"{origin}/opds/slow",
                                limit=opds.Limits(timeout_seconds=0.6))
            elapsed = time.monotonic() - started
            self.assertIn("timed out", str(ctx.exception).lower())
            self.assertLess(elapsed, 2.5)  # total bound enforced
            self.assertEqual(stall.seen_paths, ["/opds/slow"])  # exactly once: no replay (F1)
        finally:
            stop_server(stall)

    def test_slow_dribble_body_times_out(self):
        # F5: body dribbles in 64-byte chunks every 100ms (each read is
        # fast), but the total (64KB) would take minutes. Only a total
        # operation deadline can stop this; a per-read timeout cannot.
        dribble = start_server()
        dribble.route_dribble("/opds/dribble", 64 * 1024, 0.1, "application/atom+xml")
        try:
            origin = dribble.origin
            started = time.monotonic()
            with self.assertRaises(opds.OpdsError) as ctx:
                opds.fetch_feed(f"{origin}/opds/dribble",
                                limit=opds.Limits(timeout_seconds=0.8, chunk_bytes=4096))
            elapsed = time.monotonic() - started
            self.assertIn("timed out", str(ctx.exception).lower())
            self.assertLess(elapsed, 3.0)
            self.assertEqual(dribble.seen_paths, ["/opds/dribble"])
        finally:
            stop_server(dribble)

    def test_slow_dribble_download_times_out_and_staging_clean(self):
        dribble = start_server()
        dribble.route_dribble("/book.epub", 64 * 1024, 0.1, "application/epub+zip",
                              prefix=b"PK\x03\x04" + b"x" * 60)
        try:
            origin = dribble.origin
            entry = opds.OpdsEntry(
                stable_id="urn:x", title="T",
                acquisition=opds.OpdsLink(href=f"{origin}/book.epub",
                                          rel="http://opds-spec.org/acquisition",
                                          type="application/epub+zip"))
            started = time.monotonic()
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(opds.OpdsError) as ctx:
                    opds.capture_epub(entry, td,
                                      limit=opds.Limits(timeout_seconds=0.8, chunk_bytes=4096))
                self.assertIn("timed out", str(ctx.exception).lower())
                self.assertEqual(list(Path(td).iterdir()), [])  # partial removed
            self.assertLess(time.monotonic() - started, 3.0)
        finally:
            stop_server(dribble)

    def test_feed_cancellation(self):
        # should_cancel is propagated to the feed path: an already-set
        # callback raises before any request is issued.
        self.route("/opds/p1", feed_xml("F", []))
        with self.assertRaises(opds.CancelledError):
            opds.fetch_feed(f"{self.origin}/opds/p1", root_url=self.root,
                            policy=opds.DestinationPolicy(), limit=opds.Limits(timeout_seconds=5),
                            should_cancel=lambda: True)
        self.assertEqual(self.server.seen_paths, [])

    def test_collect_pages_cancellation_between_pages(self):
        self.route("/opds/p1", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                        '<link href="p2" rel="next"/>'))
        self.route("/opds/p2", feed_xml("F", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        # Cancel once the first page has actually arrived.
        with self.assertRaises(opds.CancelledError):
            opds.collect_pages(f"{self.origin}/opds/p1", root_url=self.root,
                               policy=opds.DestinationPolicy(), limit=opds.Limits(timeout_seconds=5),
                               should_cancel=lambda: len(self.server.seen_paths) >= 1)
        self.assertEqual(self.server.seen_paths, ["/opds/p1"])


class PolicyAndUrlTests(unittest.TestCase):
    def test_credentials_and_secret_queries_refused(self):
        for bad in ("http://user:pass@127.0.0.1/x", "http://127.0.0.1/x?token=abc",
                    "ftp://127.0.0.1/x", "http://127.0.0.1/x?api_key=abc"):
            with self.assertRaises(opds.OpdsError, msg=bad):
                opds._validate_url(bad)
        self.assertNotIn("abc", opds.safe_url("http://127.0.0.1/x?token=abc&ok=1"))

    def test_policy_has_no_wildcard_or_allow_all(self):
        with self.assertRaises(ValueError):
            opds.DestinationPolicy(frozenset({"*"}))
        self.assertFalse(hasattr(opds.DestinationPolicy, "allow_all"))
        pol = opds.DestinationPolicy(frozenset({"http://192.168.1.10:8080"}))
        self.assertFalse(pol.requires_review("http://192.168.1.10:8080/opds", "http://192.168.1.10:8080"))
        self.assertTrue(pol.requires_review("http://evil.example/x", "http://192.168.1.10:8080"))

    def test_auth_repr_redacted(self):
        self.assertNotIn(AUTH_PASS, repr(opds.OpdsAuth(AUTH_USER, AUTH_PASS)))



    def test_cross_origin_review_carries_exact_destination_url(self):
        a_srv, other = start_server(), start_server()
        try:
            b_origin = other.origin
            target = f"{b_origin}/opds/p2"
            other.route("/opds/p2", feed_xml("B", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
            a_srv.route("/opds/redir", b"", status=302, headers={"Location": target})
            with self.assertRaises(opds.CrossOriginReviewRequired) as ctx:
                opds.fetch_feed(f"{a_srv.origin}/opds/redir",
                                policy=opds.DestinationPolicy(),
                                auth=opds.OpdsAuth(AUTH_USER, AUTH_PASS),
                                limit=opds.Limits(timeout_seconds=5))
            exc = ctx.exception
            self.assertEqual(exc.destination_origin, b_origin)
            self.assertEqual(exc.destination_url, target)
            self.assertNotIn(AUTH_PASS, str(exc))
            self.assertEqual(other.seen_paths, [])
            pol = opds.DestinationPolicy(allowed_exact_urls=frozenset({target}))
            self.assertFalse(pol.requires_review(target, a_srv.origin))
            self.assertTrue(pol.requires_review(f"{b_origin}/opds/other", a_srv.origin))
            pol2 = opds.DestinationPolicy(frozenset({b_origin}))
            self.assertFalse(pol2.requires_review(f"{b_origin}/opds/other", a_srv.origin))
        finally:
            stop_server(a_srv)
            stop_server(other)


class ParentBoundaryTests(unittest.TestCase):
    def test_doctype_rejected_across_xml_encodings(self):
        for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding):
                xml = ('<?xml version="1.0" encoding="%s"?>'
                       '<!DOCTYPE feed [<!ENTITY x "expanded-internal-entity">]>'
                       '<feed xmlns="http://www.w3.org/2005/Atom">'
                       '<title>&x;</title></feed>') % ('UTF-16' if encoding.startswith('utf-16') else encoding)
                with self.assertRaisesRegex(opds.OpdsError, 'DOCTYPE'):
                    opds.parse_feed(xml.encode(encoding), 'http://localhost/opds')

    def test_headers_returning_after_deadline_close_connection(self):
        clock = [0.0]
        conn = mock.Mock()
        def late_response():
            clock[0] = 2.0
            return mock.Mock(getheaders=lambda: [], status=200)
        conn.getresponse.side_effect = late_response
        with mock.patch('http.client.HTTPConnection', return_value=conn), \
             mock.patch.object(opds.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(opds.requests.Timeout):
                opds.RequestsTransport().get('http://localhost/opds', {}, 1.0)
        conn.close.assert_called_once()
        conn.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
