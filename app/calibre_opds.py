"""Bounded Calibre OPDS navigation + EPUB capture foundation (issue #1734, slice 1).

Independently implemented OPDS 1.x client behaviour; NOT a copy of any
upstream download implementation. No route, config, UI or import wiring here
(next slice). Contracts for the next slice:

- ``fetch_feed(url, ...)`` / ``collect_pages(...)`` -> ``OpdsFeed`` with
  resolved links against the *actual* response URL, stable entry identity,
  and an EPUB-only ``acquisition`` link (or a ``rejected_reason``).
- ``capture_epub(entry_or_url, staging_dir, ...)`` -> ``CapturedEpub``
  descriptor pointing at an owned staged file (never a remote filename).
  The next slice must hand ``CapturedEpub.path`` bytes to the *existing*
  import authority (``routers/script.py:upload_file``) and delete the file
  after handoff; it must not publish from this module.
- Credentials: ``OpdsAuth`` is origin-bound to the configured root origin
  only. Cross-origin destinations require an explicit ``DestinationPolicy``
  allow-list reviewed by the caller *before* the request; there is no
  wildcard / auto-authorize-all mode.
"""
import base64
import hashlib
import inspect
import os
import socket
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

EPUB_MIME = "application/epub+zip"
_ACQ_RELS = {"http://opds-spec.org/acquisition", "acquisition"}
_DRM_MARKERS = ("drm", "protected", "adobe.adept", "obfuscated-swdrm",
                "rights.hashes", "license")
_SECRET_QUERY_KEYS = ("token", "key", "secret", "password", "passwd", "auth",
                      "session", "credential", "api_key", "apikey")
_NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/terms/",
       "opds": "http://opds-spec.org/2010/catalog", "dcterms": "http://purl.org/dc/terms/"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class OpdsError(RuntimeError):
    """Base error; messages are pre-redacted (no secrets / auth / full URLs with queries)."""


class CrossOriginReviewRequired(OpdsError):
    def __init__(self, destination_origin, destination_url=None):
        self.destination_origin = destination_origin
        # Exact validated pending destination URL (host+port+path), or
        # None for legacy callers that only knew the origin.
        self.destination_url = destination_url
        msg = f"cross-origin destination requires explicit review: {destination_origin}"
        if destination_url:
            msg += f" ({safe_url(destination_url)})"
        super().__init__(msg)


class PaginationLoopError(OpdsError):
    pass


class OversizeError(OpdsError):
    pass


class CancelledError(OpdsError):
    pass


class ProtectedOrUnsupportedError(OpdsError):
    pass


class AuthFailureError(OpdsError):
    pass


class BrokenLinkError(OpdsError):
    pass


@dataclass(frozen=True)
class Limits:
    max_feed_bytes: int = 2_000_000
    max_download_bytes: int = 200_000_000
    max_entries_per_page: int = 200
    max_pages: int = 40
    max_redirects: int = 5
    timeout_seconds: float = 20.0
    chunk_bytes: int = 64 * 1024

    def __post_init__(self):
        for name in ("max_feed_bytes", "max_download_bytes", "max_entries_per_page",
                     "max_pages", "max_redirects", "chunk_bytes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not (0 < self.timeout_seconds <= 120):
            raise ValueError("timeout_seconds must be in (0, 120]")


@dataclass(frozen=True)
class DestinationPolicy:
    """Explicit allow-list of origins the caller has reviewed.

    ``allowed_origins`` is a frozenset of ``scheme://host[:port]`` strings.
    ``allowed_exact_urls`` is an optional frozenset of exact validated
    destination URLs (used by the catalog service so an approval covers
    one exact URL, never a whole origin).
    There is deliberately no ``allow_all`` flag and no wildcard matching:
    an origin not in the set raises ``CrossOriginReviewRequired`` before
    any request to it is issued. Same-origin (relative/absolute on the
    configured root origin) needs no entry. Private/LAN hosts (e.g.
    192.168.x.x Calibre servers) are allowed when explicitly listed --
    no blanket private-IP block.
    """
    allowed_origins: frozenset = frozenset()
    allowed_exact_urls: frozenset = frozenset()

    def __post_init__(self):
        for o in self.allowed_origins:
            if "*" in o:
                raise ValueError("wildcard origins are not allowed; list each origin explicitly")
        object.__setattr__(self, "allowed_origins",
                           frozenset(_origin(o) for o in self.allowed_origins))
        object.__setattr__(self, "allowed_exact_urls",
                           frozenset(_validate_url(u) for u in self.allowed_exact_urls))

    def requires_review(self, url, root_origin):
        if _origin(url) == root_origin:
            return False
        if _validate_url(url) in self.allowed_exact_urls:
            return False
        return _origin(url) not in self.allowed_origins

    def assert_allowed(self, url, root_origin):
        if self.requires_review(url, root_origin):
            raise CrossOriginReviewRequired(_origin(url), _validate_url(url))


@dataclass(frozen=True)
class OpdsAuth:
    """Locally stored credentials for the configured root origin only."""
    username: str
    password: str

    def __repr__(self):
        return "OpdsAuth(username=<redacted>, password=<redacted>)"

    def header_value(self):
        return "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()


@dataclass(frozen=True)
class OpdsLink:
    href: str
    rel: str = ""
    type: str = ""
    title: str = ""

    def to_dict(self):
        return {"href": self.href, "rel": self.rel, "type": self.type, "title": self.title}


@dataclass(frozen=True)
class OpdsEntry:
    stable_id: str
    title: str
    authors: tuple = ()
    series: str = ""
    series_index: str = ""
    subtitle: str = ""
    links: tuple = ()
    acquisition: object = None          # OpdsLink or None
    rejected_reason: str = ""           # set when a DRM/unsupported acquisition was refused
    published: str = ""
    updated: str = ""
    summary: str = ""

    def to_dict(self):
        return {"stable_id": self.stable_id, "title": self.title,
                "authors": list(self.authors), "series": self.series,
                "series_index": self.series_index, "subtitle": self.subtitle,
                "links": [link.to_dict() for link in self.links],
                "acquisition": self.acquisition.to_dict() if self.acquisition else None,
                "rejected_reason": self.rejected_reason,
                "published": self.published, "updated": self.updated,
                "summary": self.summary}


@dataclass(frozen=True)
class OpdsFeed:
    feed_url: str
    title: str = ""
    entries: tuple = ()
    next_link: object = None
    search_link: object = None
    start_link: object = None

    def to_dict(self):
        return {"feed_url": self.feed_url, "title": self.title,
                "entries": [e.to_dict() for e in self.entries],
                "next_link": self.next_link.to_dict() if self.next_link else None,
                "search_link": self.search_link.to_dict() if self.search_link else None,
                "start_link": self.start_link.to_dict() if self.start_link else None}


@dataclass(frozen=True)
class CapturedEpub:
    """Owned staged EPUB bytes. ``path`` is inside the caller staging dir,
    owned by this capture: the importer next slice must consume it via the
    existing upload route and delete it afterwards. Quota accounting for
    this staged file is the importer's decision (see module docstring)."""
    path: str
    sha256: str
    size_bytes: int
    final_url: str
    mime_type: str
    entry_stable_id: str = ""

    def to_dict(self):
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes,
                "final_url": self.final_url, "mime_type": self.mime_type,
                "entry_stable_id": self.entry_stable_id}


# --- URL helpers -----------------------------------------------------------

def _origin(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OpdsError(f"unsupported or missing origin for URL (scheme={parts.scheme or 'none'})")
    netloc = parts.hostname.lower()
    if parts.port:
        netloc += f":{parts.port}"
    return f"{parts.scheme}://{netloc}"


def _validate_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise OpdsError(f"only http/https URLs are allowed (scheme={parts.scheme or 'none'})")
    if parts.username or parts.password:
        raise OpdsError("URLs with embedded credentials are not allowed")
    if not parts.hostname:
        raise OpdsError("URL is missing a host")
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        if any(s in key.lower() for s in _SECRET_QUERY_KEYS):
            raise OpdsError(f"secret-like query parameter is not allowed: {key}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def safe_url(url):
    """URL for diagnostics: query values of secret-like keys redacted, no userinfo."""
    try:
        parts = urlsplit(url)
        safe_q = urlencode([(k, "<redacted>" if any(s in k.lower() for s in _SECRET_QUERY_KEYS) else v)
                            for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path, safe_q, ""))
    except Exception:
        return "<unparseable-url>"


def resolve_link(base_url, href):
    if not href or not href.strip():
        raise BrokenLinkError("empty link href")
    return _validate_url(urljoin(base_url, href.strip()))


# --- Transport -------------------------------------------------------------
# Transport protocol: get(url, headers, timeout) -> response object with
# attributes status_code, headers (mapping), url (final request URL), and
# methods iter_content(chunk_bytes), close(). Inject a fake in unit tests or
# use the real requests transport against a synthetic local HTTP server.
# ``timeout`` is the remaining operation budget for this hop; transports
# supporting it also accept ``deadline`` (monotonic absolute time) and
# ``should_cancel`` (callable returning True) to bound all waits. Fakes
# with the legacy 3-arg signature still work: the deadline/cancel contract
# is then enforced by the layer above the transport.
#
# Operation deadline / cancellation contract: timeout_seconds is a shared
# operation budget checked between hops/pages/reads and after headers return.
# Body read1 calls refresh the remaining socket timeout. Cancellation is
# cooperative at those boundaries. DNS, multiple-address connection attempts,
# and trickled headers/chunk framing inside stdlib may exceed that budget;
# this is not a hard wall-clock limit or guaranteed cancellation latency.

def _check_cancel(should_cancel):
    if should_cancel is not None and should_cancel():
        raise CancelledError("cancelled")


def _deadline_remaining(deadline, now=None):
    if deadline is None:
        return None
    return deadline - (time.monotonic() if now is None else now)


def _raise_if_deadline_expired(deadline):
    rem = _deadline_remaining(deadline)
    if rem is not None and rem <= 0:
        raise requests.Timeout("operation deadline exceeded")


def _set_sock_timeout(conn, raw, seconds):
    """Best-effort remaining-budget socket timeout (seconds > 0 required)."""
    sock = getattr(conn, "sock", None) if conn is not None else None
    if sock is None:
        try:
            sock = raw.fp.raw._sock  # type: ignore[attr-defined]
        except Exception:
            sock = None
    if sock is not None:
        try:
            sock.settimeout(seconds)
        except (OSError, ValueError):
            pass


class _HttpResponse:
    def __init__(self, raw, url, conn=None, deadline=None, should_cancel=None):
        self._raw = raw
        self._conn = conn
        self._deadline = deadline
        self._should_cancel = should_cancel
        self.status_code = raw.status
        self.headers = dict(raw.getheaders())
        self.url = url

    def iter_content(self, chunk_size):
        # read1 (not read) so a dribble cannot fill the buffer for longer
        # than the deadline between deadline checks: each recv yields
        # promptly and the remaining-budget socket timeout is refreshed
        # before every read.
        _read = getattr(self._raw, "read1", None) or self._raw.read
        while True:
            _check_cancel(self._should_cancel)
            rem = _deadline_remaining(self._deadline)
            if rem is not None:
                if rem <= 0:
                    raise requests.Timeout("operation deadline exceeded")
                _set_sock_timeout(self._conn, self._raw, rem)
            try:
                chunk = _read(chunk_size)
            except (TimeoutError, socket.timeout, requests.Timeout) as exc:
                raise requests.Timeout(str(exc)) from exc
            if not chunk:
                return
            yield chunk

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass


class RequestsTransport:
    """Real HTTP(S) transport over stdlib http.client (no proxy env, no
    automatic redirects, no urllib3 retries masking resets).

    Retry classification (F1): only the TCP/TLS *connect* phase is
    retried. Once ``connect()`` succeeds the request is considered sent:
    any ``OSError`` from ``request()``/``getresponse()`` is fatal and is
    never reissued, because the server may already have processed the GET
    and re-sending would duplicate a processed request. Connect retries
    are bounded by the same total operation deadline (``deadline`` if
    given, else the per-call ``timeout`` budget)."""

    def get(self, url, headers, timeout, deadline=None, should_cancel=None):
        import http.client
        import ssl
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if deadline is None:
            deadline = time.monotonic() + timeout
        last_exc = None
        for attempt in range(5):
            _check_cancel(should_cancel)
            rem = _deadline_remaining(deadline)
            if rem is None or rem <= 0:
                raise requests.Timeout("operation deadline exceeded")
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=rem,
                                                   context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(host, port, timeout=rem)
            # Phase 1 (connection only): no HTTP request has been sent, so
            # a reset here is safe to retry within the total budget.
            try:
                conn.connect()
            except (TimeoutError, socket.timeout, requests.Timeout) as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                raise requests.Timeout(str(exc)) from exc
            except OSError as exc:
                last_exc = exc
                try:
                    conn.close()
                except Exception:
                    pass
                if attempt < 4:
                    rem2 = _deadline_remaining(deadline)
                    if rem2 is None or rem2 <= 0:
                        break
                    time.sleep(min(0.05 * (attempt + 1), rem2))
                    continue
                break
            # Phase 2 (request already being sent): never reissue.
            try:
                _check_cancel(should_cancel)
                rem3 = _deadline_remaining(deadline)
                if rem3 is None or rem3 <= 0:
                    raise requests.Timeout("operation deadline exceeded")
                _set_sock_timeout(conn, None, rem3)
                conn.request("GET", path, headers=headers)
                rem4 = _deadline_remaining(deadline)
                if rem4 is None or rem4 <= 0:
                    raise requests.Timeout("operation deadline exceeded")
                _set_sock_timeout(conn, None, rem4)
                raw = conn.getresponse()
                try:
                    _check_cancel(should_cancel)
                    if _deadline_remaining(deadline) <= 0:
                        raise requests.Timeout("operation deadline exceeded")
                except BaseException:
                    raw.close()
                    raise
                return _HttpResponse(raw, url, conn=conn, deadline=deadline,
                                     should_cancel=should_cancel)
            except CancelledError:
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            except (TimeoutError, socket.timeout, requests.Timeout) as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                raise requests.Timeout(str(exc)) from exc
            except OSError as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                raise requests.ConnectionError(str(exc)) from exc
        raise requests.ConnectionError(str(last_exc)) from last_exc


def _close_quietly(response):
    try:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _read_capped(response, limit, what="feed", deadline=None, should_cancel=None,
                 max_bytes=None):
    """Consume the body incrementally, stopping at the first over-cap chunk
    (F2). The iterator is closed when we stop early so a large/oversize
    body is never materialised before the guard fires; ``response.close()``
    itself stays with the caller. ``CancelledError``/``OversizeError``
    propagate; a transport timeout becomes ``OpdsError`` timed out."""
    cap = limit.max_feed_bytes if max_bytes is None else max_bytes
    body = bytearray()
    total = 0
    iterator = response.iter_content(limit.chunk_bytes)
    try:
        for chunk in iterator:
            _check_cancel(should_cancel)
            rem = _deadline_remaining(deadline)
            if rem is not None and rem <= 0:
                raise requests.Timeout("operation deadline exceeded")
            if not chunk:
                continue
            total += len(chunk)
            if total > cap:
                raise OversizeError(f"{what} exceeds {cap} byte cap")
            body.extend(chunk)
    except requests.Timeout as exc:
        raise OpdsError(f"request timed out reading {what}") from exc
    except OSError as exc:
        raise BrokenLinkError(f"connection reset while reading {what}") from exc
    finally:
        close_iter = getattr(iterator, "close", None)
        if callable(close_iter):
            try:
                close_iter()
            except Exception:
                pass
    return bytes(body)


def _transport_get(transport, url, headers, timeout, deadline, should_cancel):
    """Call the transport with deadline/cancel when it supports them;
    fall back to the legacy 3-arg signature for injected fakes."""
    try:
        sig = inspect.signature(transport.get)
        params = list(sig.parameters.values())
        supports = (any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
                    or any(p.name in ("deadline", "should_cancel") for p in params)
                    or len(params) >= 4)
    except (TypeError, ValueError):
        supports = False
    if supports:
        return transport.get(url, headers, timeout, deadline=deadline,
                             should_cancel=should_cancel)
    _check_cancel(should_cancel)
    rem = _deadline_remaining(deadline)
    if rem is not None and rem <= 0:
        raise requests.Timeout("operation deadline exceeded")
    return transport.get(url, headers, timeout)


def _request(url, *, root_origin, policy, auth, transport, limit, deadline=None,
             should_cancel=None):
    """Single logical GET with manual redirects. Returns (response, final_url).
    Caller owns closing the returned response. ``deadline`` is the monotonic
    absolute budget for the whole logical operation (all hops); each hop
    receives only the remaining budget. The configured ``root_origin`` never
    rebinds (F4): redirects and pagination stay anchored to it for both
    policy and credential scoping."""
    current = _validate_url(url)
    policy.assert_allowed(current, root_origin)
    for hop in range(limit.max_redirects + 1):
        _check_cancel(should_cancel)
        rem = _deadline_remaining(deadline)
        if rem is not None and rem <= 0:
            raise OpdsError(f"request timed out for {safe_url(current)}")
        hop_timeout = limit.timeout_seconds if rem is None else rem
        headers = {"Accept": "application/atom+xml, application/xml, text/xml, */*",
                   "User-Agent": "AA2-CalibreOPDS/1"}
        if auth is not None and _origin(current) == root_origin:
            headers["Authorization"] = auth.header_value()
        try:
            response = _transport_get(transport, current, headers, hop_timeout,
                                      deadline, should_cancel)
        except CancelledError:
            raise
        except requests.Timeout as exc:
            raise OpdsError(f"request timed out for {safe_url(current)}") from exc
        except requests.ConnectionError as exc:
            raise BrokenLinkError(f"connection failed for {safe_url(current)}") from exc
        except requests.RequestException as exc:
            raise OpdsError(f"request failed for {safe_url(current)}: {type(exc).__name__}") from exc
        status = getattr(response, "status_code", 0) or 0
        if status in _REDIRECT_STATUSES:
            location = (getattr(response, "headers", {}) or {}).get("Location") or \
                       (getattr(response, "headers", {}) or {}).get("location")
            _close_quietly(response)
            if not location:
                raise BrokenLinkError(f"redirect without Location for {safe_url(current)}")
            nxt = resolve_link(current, location)
            # Review gate: cross-origin destinations must be pre-approved
            # BEFORE fetching them, and credentials are never forwarded.
            policy.assert_allowed(nxt, root_origin)
            if hop >= limit.max_redirects:
                raise OpdsError(f"too many redirects (>{limit.max_redirects})")
            current = nxt
            continue
        if status in (401, 403):
            _close_quietly(response)
            raise AuthFailureError(f"authentication failed (HTTP {status}) for {safe_url(current)}")
        if status == 404:
            _close_quietly(response)
            raise BrokenLinkError(f"not found for {safe_url(current)}")
        if status < 200 or status >= 300:
            _close_quietly(response)
            raise OpdsError(f"HTTP {status} for {safe_url(current)}")
        final = getattr(response, "url", None) or current
        return response, _validate_url(final)
    raise OpdsError("redirect loop")


# --- Parsing ---------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _children(elem, name):
    return [c for c in elem if _local(c.tag) == name]


def _child(elem, name):
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _text(elem):
    return (elem.text or "").strip() if elem is not None else ""


class _FeedTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Parser callback sees declarations regardless of XML byte encoding.
        raise OpdsError("DOCTYPE is not allowed in OPDS feeds (entity-expansion defence)")


def parse_feed(data, feed_url):
    if not isinstance(data, (bytes, bytearray)):
        raise OpdsError("feed payload must be bytes")
    if b"<!DOCTYPE" in bytes(data).upper():
        raise OpdsError("DOCTYPE is not allowed in OPDS feeds (entity-expansion defence)")
    try:
        root = ET.fromstring(bytes(data), parser=ET.XMLParser(target=_FeedTreeBuilder()))
    except (ET.ParseError, ValueError) as exc:
        raise OpdsError(f"invalid OPDS XML: {exc}") from exc
    if _local(root.tag) != "feed":
        raise OpdsError("OPDS payload is not an Atom feed")
    entries = []
    seen = set()
    for entry_elem in _children(root, "entry"):
        if len(entries) >= 10_000:
            break
        entry = _parse_entry(entry_elem, feed_url)
        if entry.stable_id in seen:
            continue
        seen.add(entry.stable_id)
        entries.append(entry)
    links = {}
    for link_elem in _children(root, "link"):
        href = link_elem.get("href") or ""
        if not href:
            continue
        try:
            resolved = resolve_link(feed_url, href)
        except OpdsError:
            continue
        links[link_elem.get("rel") or ""] = OpdsLink(
            href=resolved, rel=link_elem.get("rel") or "",
            type=link_elem.get("type") or "", title=link_elem.get("title") or "")
    return OpdsFeed(feed_url=feed_url, title=_text(_child(root, "title")),
                    entries=tuple(entries),
                    next_link=links.get("next"), search_link=links.get("search"),
                    start_link=links.get("start"))


def _parse_entry(elem, feed_url):
    links = []
    acquisition = None
    rejected = ""
    for link_elem in _children(elem, "link"):
        href = link_elem.get("href") or ""
        if not href:
            continue
        rel = (link_elem.get("rel") or "").strip()
        mime = (link_elem.get("type") or "").strip().lower()
        try:
            resolved = resolve_link(feed_url, href)
        except OpdsError:
            continue
        link = OpdsLink(href=resolved, rel=rel, type=mime,
                        title=link_elem.get("title") or "")
        links.append(link)
        lowered = f"{rel} {mime}".lower()
        if any(marker in lowered for marker in _DRM_MARKERS):
            rejected = "protected/DRM acquisition refused"
            continue
        if rel in _ACQ_RELS or rel.startswith("acquisition") or \
                rel == "http://opds-spec.org/acquisition/open-access":
            if mime == EPUB_MIME and acquisition is None:
                acquisition = link
            elif mime and mime != EPUB_MIME:
                rejected = rejected or f"unsupported acquisition type: {mime}"
    authors = tuple(_text(_child(a, "name")) for a in _children(elem, "author")
                    if _text(_child(a, "name")))
    series = series_index = ""
    for cat in _children(elem, "category"):
        scheme = (cat.get("scheme") or "").lower()
        if "series" in scheme:
            label = cat.get("label") or cat.get("term") or ""
            if " - " in label:
                series, series_index = [p.strip() for p in label.rsplit(" - ", 1)]
            else:
                series = label.strip()
            break
    ident = _text(_child(elem, "id")) or _text(_child(elem, "identifier"))
    if not ident:
        ident = acquisition.href if acquisition else \
            (f"{_text(_child(elem, 'title'))}|{'/'.join(authors)}" if _text(_child(elem, 'title')) else "")
    if acquisition is not None:
        # An EPUB is accepted even if other (non-EPUB) links were refused,
        # unless a DRM marker was present -- DRM wins and refuses the entry.
        if rejected and "protected" in rejected:
            acquisition, rejected = None, rejected
        else:
            rejected = rejected if not acquisition else ""
    return OpdsEntry(
        stable_id=ident, title=_text(_child(elem, "title")), authors=authors,
        series=series, series_index=series_index,
        subtitle=_text(_child(elem, "subtitle")), links=tuple(links),
        acquisition=acquisition, rejected_reason=rejected,
        published=_text(_child(elem, "published")), updated=_text(_child(elem, "updated")),
        summary=_text(_child(elem, "summary"))[:2000])


# --- Public API ------------------------------------------------------------

def fetch_feed(url, *, root_url=None, policy=None, auth=None, transport=None,
               limit=None, should_cancel=None, deadline=None):
    """Fetch and parse one OPDS feed page. No crawling: callers click pages.

    ``should_cancel`` (optional callable returning True) raises
    ``CancelledError``; it is polled per hop and per body chunk, subject to the blocking-I/O
    limitations documented in the transport contract. ``deadline`` (monotonic absolute
    time) is how callers share one total-operation budget across pages:
    when omitted, a fresh budget of ``limit.timeout_seconds`` is used."""
    limit = limit or Limits()
    transport = transport or RequestsTransport()
    policy = policy or DestinationPolicy()
    root_origin = _origin(root_url or url)
    if deadline is None:
        deadline = time.monotonic() + limit.timeout_seconds
    response, final_url = _request(url, root_origin=root_origin, policy=policy,
                                   auth=auth, transport=transport, limit=limit,
                                   deadline=deadline, should_cancel=should_cancel)
    try:
        body = _read_capped(response, limit, "feed", deadline=deadline,
                            should_cancel=should_cancel)
    finally:
        _close_quietly(response)
    feed = parse_feed(body, final_url)
    if len(feed.entries) > limit.max_entries_per_page:
        raise OversizeError(f"feed has {len(feed.entries)} entries, cap is {limit.max_entries_per_page}")
    return feed


def collect_pages(start_url, *, root_url=None, policy=None, auth=None,
                  transport=None, limit=None, should_cancel=None, deadline=None):
    """Follow explicit ``next`` links from a user-chosen page, bounded.
    Raises PaginationLoopError on a repeated URL, OversizeError past caps.
    Duplicate entries across pages are de-duplicated by stable_id.

    F4: the trusted origin is anchored once from ``root_url`` (or
    ``start_url``) and passed unchanged to every page, so a cross-origin
    ``next`` link on an allowed origin can never rebind it. Credentials
    stay scoped to that original origin for the whole traversal. One total
    deadline (``limit.timeout_seconds`` unless ``deadline`` is given)
    bounds the entire traversal; ``should_cancel`` is polled per page and
    per chunk with a worst-case latency of one blocking read."""
    limit = limit or Limits()
    if deadline is None:
        deadline = time.monotonic() + limit.timeout_seconds
    anchored_root = root_url if root_url is not None else _origin(start_url)
    visited = []
    seen_urls = set()
    entries = []
    seen_ids = set()
    url = start_url
    feed_url = ""
    title = ""
    for _ in range(limit.max_pages):
        _check_cancel(should_cancel)
        rem = _deadline_remaining(deadline)
        if rem is not None and rem <= 0:
            raise OpdsError(f"request timed out for {safe_url(url)}")
        if url in seen_urls:
            raise PaginationLoopError(f"pagination loop at {safe_url(url)}")
        seen_urls.add(url)
        visited.append(url)
        feed = fetch_feed(url, root_url=anchored_root, policy=policy, auth=auth,
                          transport=transport, limit=limit,
                          should_cancel=should_cancel, deadline=deadline)
        if not feed_url:
            feed_url, title = feed.feed_url, feed.title
        for entry in feed.entries:
            if entry.stable_id not in seen_ids:
                seen_ids.add(entry.stable_id)
                entries.append(entry)
        if feed.next_link is None:
            return OpdsFeed(feed_url=feed.feed_url, title=feed.title,
                            entries=tuple(entries), next_link=None,
                            search_link=feed.search_link, start_link=feed.start_link)
        nxt = feed.next_link.href
        if nxt in seen_urls:
            raise PaginationLoopError(f"pagination loop at {safe_url(nxt)}")
        url = nxt
    raise OversizeError(f"more than {limit.max_pages} feed pages")


def capture_epub(target, staging_dir, *, root_url=None, policy=None, auth=None,
                 transport=None, limit=None, should_cancel=None):
    """Stream an EPUB acquisition to an owned staged file.

    ``target`` is an ``OpdsEntry`` (uses its EPUB ``acquisition`` link) or a
    URL string (must be the already-previewed acquisition href; DRM/protected
    URLs are refused by the caller holding the parsed entry -- this function
    re-checks the response MIME and refuses non-EPUB). ``staging_dir`` must
    exist. On cancel/oversize/error the partial file is deleted; pre-existing
    files are never touched. Returns ``CapturedEpub`` for the next slice to
    hand to the existing import route. One total deadline of
    ``limit.timeout_seconds`` bounds redirects + body; ``should_cancel`` is
    polled per hop and per body chunk with a worst-case latency of one
    blocking socket read bounded by the remaining deadline (see transport
    contract). The MIME is validated before any staged file/fd is created.
    """
    limit = limit or Limits()
    transport = transport or RequestsTransport()
    policy = policy or DestinationPolicy()
    if isinstance(target, OpdsEntry):
        if target.acquisition is None:
            raise ProtectedOrUnsupportedError(
                target.rejected_reason or "entry has no EPUB acquisition link")
        url = target.acquisition.href
        entry_id = target.stable_id
    else:
        url = str(target)
        entry_id = ""
    staging = Path(staging_dir)
    if not staging.is_dir():
        raise OpdsError("staging directory does not exist")
    root_origin = _origin(root_url or url)
    deadline = time.monotonic() + limit.timeout_seconds
    response, final_url = _request(url, root_origin=root_origin, policy=policy,
                                   auth=auth, transport=transport, limit=limit,
                                   deadline=deadline, should_cancel=should_cancel)
    try:
        # F3: validate the MIME before allocating any staged file/fd, so an
        # invalid type leaks nothing and touches no other file. If the
        # allocation itself fails the finally below still closes the
        # response and no partial file is left behind.
        mime = ((getattr(response, "headers", {}) or {}).get("Content-Type") or EPUB_MIME).split(";")[0].strip().lower()
        if mime not in (EPUB_MIME, "application/octet-stream", "application/zip"):
            raise ProtectedOrUnsupportedError(f"unsupported download type: {mime}")
        fd, tmp_name = tempfile.mkstemp(prefix="opds-", suffix=".epub", dir=str(staging))
        tmp_path = Path(tmp_name)
        try:
            try:
                out = os.fdopen(fd, "wb")
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            digest = hashlib.sha256()
            size = 0
            first = True
            try:
                for chunk in response.iter_content(limit.chunk_bytes):
                    if should_cancel is not None and should_cancel():
                        raise CancelledError("download cancelled")
                    rem = _deadline_remaining(deadline)
                    if rem is not None and rem <= 0:
                        raise requests.Timeout("operation deadline exceeded")
                    if not chunk:
                        continue
                    if first:
                        if not bytes(chunk[:4]).startswith(b"PK"):
                            raise ProtectedOrUnsupportedError("download is not an EPUB (missing ZIP/PK header)")
                        first = False
                    size += len(chunk)
                    if size > limit.max_download_bytes:
                        raise OversizeError(f"download exceeds {limit.max_download_bytes} byte cap")
                    digest.update(chunk)
                    out.write(chunk)
            except requests.Timeout as exc:
                raise OpdsError("request timed out reading download") from exc
            finally:
                try:
                    out.close()
                except Exception:
                    pass
            if size == 0:
                raise ProtectedOrUnsupportedError("empty download")
            captured = CapturedEpub(path=str(tmp_path), sha256=digest.hexdigest(),
                                    size_bytes=size, final_url=final_url,
                                    mime_type=mime, entry_stable_id=entry_id)
            return captured
        except BaseException:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise
    finally:
        _close_quietly(response)


def discard_capture(captured):
    """Delete an owned staged capture (cancel path). Never deletes anything
    outside a file this module created in a staging dir owned by the caller:
    the path must end in .epub and live in an existing directory we created
    it in -- callers must only pass CapturedEpub values returned above."""
    path = Path(captured.path)
    try:
        if path.is_file() and path.suffix == ".epub":
            path.unlink()
    except OSError:
        pass
