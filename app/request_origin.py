"""Refuse cross-site and DNS-rebinding requests (GHSA-vp9p-437w-q5x9).

CORS only decides whether a page may READ a response; a page the owner visits
can still SEND a form POST to the local server and trigger it. Basic auth is
off by default. So every request passes two checks here:

- HOST (DNS rebinding, every method): a rebinding page reaches this server
  under a hostname the attacker controls. Allowed are IP literals (they cannot
  be rebound, and are how LAN and Docker access arrive), `localhost` and
  `*.localhost` (Pinokio's `https://<port>.localhost` proxy), single-label
  names with no dot (a machine name such as `mybox`, or TestClient's
  `testserver`: rebinding needs a public domain the attacker controls, and a
  dotless name is resolved locally, not by the attacker's DNS), and hostnames
  listed in ALEXANDRIA_ALLOWED_HOSTS.
- ORIGIN (cross-site requests, unsafe methods only): the Origin header, else
  the Referer's origin. Browsers send Origin on every cross-site POST, so a
  request carrying neither is a non-browser client (curl, scripts) and is
  allowed. Allowed sources are the request's own host, CORS_ORIGINS /
  ALEXANDRIA_ALLOWED_ORIGINS, and local sources - localhost, *.localhost, or a
  loopback/private IP - which a public web page cannot have. The Pinokio proxy
  and LAN access arrive that way however the proxy forwards Host. Residual
  risk: another app served locally (another *.localhost) is trusted.

One pure function decides; app.py only turns its answer into a response.
"""
import ipaddress
from urllib.parse import urlsplit

SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")
DEFAULT_PORTS = {"http": 80, "https": 443}


def _split_host(value):
    """'Host' header -> (hostname, port or None), lower-cased; None if malformed."""
    try:
        parts = urlsplit("//" + (value or "").strip())
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    return (hostname, port) if hostname else None


def _split_origin(value):
    """Origin or Referer -> (scheme, hostname, port with default filled), or None."""
    try:
        parts = urlsplit((value or "").strip())
        scheme, hostname, port = parts.scheme.lower(), parts.hostname, parts.port
    except ValueError:
        return None
    if scheme not in DEFAULT_PORTS or not hostname:
        return None
    return scheme, hostname, port or DEFAULT_PORTS[scheme]


def _is_ip(hostname):
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _is_localhost_name(hostname):
    return hostname == "localhost" or hostname.endswith(".localhost")


def is_allowed_host(host, allowed_hosts=()):
    """Is this Host header one a DNS-rebinding page cannot produce?"""
    split = _split_host(host)
    if not split:
        return False
    hostname = split[0]
    return bool(_is_ip(hostname)) or _is_localhost_name(hostname) or "." not in hostname \
        or hostname in {h.strip().lower() for h in allowed_hosts if h.strip()}


def is_allowed_source(source, host, allowed_origins=()):
    """Is a browser request from `source` (an origin) one this server trusts?"""
    split = _split_origin(source)
    if not split:
        return False
    scheme, hostname, port = split
    allowed = {_split_origin(o) for o in allowed_origins}
    if split in allowed:
        return True
    own = _split_host(host)
    if own and own[0] == hostname and (own[1] or DEFAULT_PORTS[scheme]) == port:
        return True
    ip = _is_ip(hostname)
    return _is_localhost_name(hostname) or bool(ip and (ip.is_loopback or ip.is_private))


def get_request_refusal(method, host, origin, referer, allowed_origins=(), allowed_hosts=()):
    """-> None to allow, else ("host" | "origin", reason)."""
    if not is_allowed_host(host, allowed_hosts):
        return ("host", f"Host {host!r} is not allowed. Add it to ALEXANDRIA_ALLOWED_HOSTS "
                        "if you reach the app by that name.")
    if (method or "").upper() in SAFE_METHODS:
        return None
    if origin is not None and origin.strip():
        source = origin.strip()
    elif referer:
        split = _split_origin(referer)
        source = f"{split[0]}://{split[1]}:{split[2]}" if split else "null"
    else:
        return None
    # "null" (sandboxed frames, file://) has no scheme, so it is never allowed
    if not is_allowed_source(source, host, allowed_origins):
        return ("origin", f"Cross-site request from {source!r} refused. Add the origin to "
                          "ALEXANDRIA_ALLOWED_ORIGINS if you use the app from there.")
    return None
