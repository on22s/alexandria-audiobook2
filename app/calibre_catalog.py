"""Service foundation for ONE optional user-configured OPDS catalog (issue #1734, slice 2).

No routes, UI, download or import live here (next slice). This module owns
only: non-secret config persistence (root URL + public username), the OS
secure-store password for that exact root, server-held bounded browse
sessions, cross-origin review tokens, and metadata preview tokens. The
route wrapper (next slice) should call exactly these verbs:

- reads (pure): ``get_config()``, ``get_metadata_preview(preview_token)``
- mutations: ``save_config(...)``, ``browse_root_page(...)``,
  ``browse_next_page(nav_token, ...)``, ``approve_cross_origin_review(token)``

Security contracts:

- The config file holds ONLY ``root_url``, ``username`` and ``revision``.
  The password lives exclusively in the OS credential store (``keyring``,
  resolved lazily per operation -- never at load/startup/save without a
  password). Plaintext/file/fail backends are refused; backend exceptions
  are sanitized (never echoed). Anonymous configs never touch the vault,
  so they work when the vault is absent or failing.
- Credentials are origin-bound to the exact configured root by
  ``calibre_opds`` (``root_url`` is anchored on every fetch, including
  approved cross-origin replays, which therefore never send credentials
  to the target origin).
- Browse accepts NO caller-supplied URL: only the configured root or a
  server-minted navigation token. One page per call; no crawling.
- Cross-origin approval takes ONLY a review token (never a client-supplied
  origin), binds exactly one destination URL (host+port+path) + one session
  + one nav action + one config revision, and is single-use. Approving one
  exact URL never trusts the whole origin: a changed redirect target needs
  its own review. Saving the config invalidates every browse/preview/review
  token, so stale state cannot restore old catalog authority.
- Browse snapshots (root, username, revision) under the lock and resolves
  credentials from that snapshot only; a config change observed before the
  request is dispatched rejects it, and an in-flight fetch always uses its
  own original credentials, with results discarded if the config changed.
"""
import copy
import os
import threading
import time
import uuid

import calibre_opds
from calibre_opds import CrossOriginReviewRequired, OpdsError

_SECRET_QUERY_KEYS = ("token", "key", "secret", "password", "passwd", "auth",
                      "session", "credential", "api_key", "apikey")

# OPDS 1.x entry navigation is an entry link pointing AT an Atom feed.
# Allow-listed relations: explicit ``subsection`` (OPDS category/subsection
# links), Atom-default ``alternate`` and no-relation (Atom default), all
# only when the MIME base is an Atom/XML feed type. HTML/image/PDF,
# ``related`` and acquisition/DRM links are never navigation.
_ATOM_MIME_TYPES = frozenset({"application/atom+xml", "application/xml", "text/xml"})
_NAV_RELS = frozenset({"subsection", "alternate", ""})
_DRM_MARKERS = ("drm", "protected", "adobe.adept", "obfuscated-swdrm",
                "rights.hashes", "license")


def _is_navigation_link(link):
    """Whether an ``OpdsEntry`` link is a feed-navigation target.

    ``link`` is an ``OpdsLink`` already resolved against the final feed URL
    by ``calibre_opds``. Acquisition/DRM links are excluded first, so EPUB
    acquisition and protected/DRM links never become navigation even on a
    mislabelled Atom MIME.
    """
    rel_raw = (link.rel or "").strip()
    rel = rel_raw.lower()
    mime_base = (link.type or "").split(";")[0].strip().lower()
    if mime_base not in _ATOM_MIME_TYPES:
        return False
    lowered = f"{rel_raw} {link.type or ''}".lower()
    if any(marker in lowered for marker in _DRM_MARKERS):
        return False
    if rel in ("http://opds-spec.org/acquisition", "acquisition") \
            or rel.startswith("acquisition") \
            or rel == "http://opds-spec.org/acquisition/open-access":
        return False
    if rel in _NAV_RELS or rel.endswith("/subsection"):
        return True
    return False


def _display_url(url):
    """Safe/redacted URL for returned dicts; unlike calibre_opds.safe_url
    this preserves the port (Calibre commonly runs on :8080). Secret-like
    query values are redacted and userinfo is dropped."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parts.port:
            netloc += f":{parts.port}"
        safe_q = urlencode([(k, "<redacted>" if any(s in k.lower() for s in _SECRET_QUERY_KEYS) else v)
                            for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, netloc, parts.path, safe_q, ""))
    except Exception:
        return "<unparseable-url>"

CONFIG_NAME = "calibre_catalog_config.json"
CREDENTIAL_SERVICE = "alexandria-calibre-opds"

_DEFAULT_SESSION_TTL = 600.0
_DEFAULT_MAX_SESSIONS = 50
_DEFAULT_MAX_NAV_TOKENS = 200
_DEFAULT_MAX_PREVIEWS = 200
_DEFAULT_MAX_REVIEWS = 50
_DEFAULT_MAX_PAGES = 40
_DEFAULT_MAX_ENTRIES = 1000


class CatalogError(ValueError):
    """User-facing refusal with an HTTP status for the future route wrapper."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --- Credential store (lazy; mirrors audiobookshelf_delivery.py) -------------

def _is_secure_backend(backend):
    if backend is None:
        return False
    name = type(backend).__name__.lower()
    module = type(backend).__module__.lower()
    if "plaintext" in name or "plaintext" in module:
        return False
    if name in ("failkeyring", "nullkeyring"):
        return False
    if "fail" in module and "keyring" in module:
        return False
    return True


def _get_keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def _check_secure_backend(keyring):
    """Refuse plaintext/fail backends; sanitized message, never the secret."""
    get_keyring = getattr(keyring, "get_keyring", None)
    if get_keyring is None:
        return  # injected fakes without a backend: trusted by construction
    try:
        backend = get_keyring()
    except Exception:
        raise CatalogError(
            "The OS credential store is unavailable. Nothing was stored anywhere.", 503) from None
    if not _is_secure_backend(backend):
        raise CatalogError(
            "The OS credential store backend is not secure, so the password was refused. "
            "Configure a secure OS keyring backend, then retry.", 503)


def _validate_root_url(value):
    """Configured root: http/https, no embedded credentials, no query/fragment.

    Plain http is allowed (Calibre servers commonly run on LAN http); this
    is a deliberate, documented deviation from the Audiobookshelf loopback
    rule. Error messages never echo the supplied value.
    """
    raw = str(value or "").strip()
    if not raw or "\0" in raw or " " in raw:
        raise CatalogError("Enter the catalog root URL.", 400)
    try:
        normalized = calibre_opds._validate_url(raw)
    except OpdsError as exc:
        raise CatalogError(str(exc), 400) from None
    parts = calibre_opds.urlsplit(normalized)
    if parts.query or parts.fragment:
        raise CatalogError("The catalog root URL must not contain a query string or fragment.", 400)
    return normalized.rstrip("/") or normalized


def _validate_username(value):
    text = str(value or "").strip()
    if len(text) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text) or any(ch.isspace() for ch in text):
        raise CatalogError("The username is invalid.", 400)
    return text


class CalibreCatalogService:
    def __init__(self, data_dir, *, transport=None, limit=None,
                 session_ttl=_DEFAULT_SESSION_TTL, max_sessions=_DEFAULT_MAX_SESSIONS,
                 max_nav_tokens=_DEFAULT_MAX_NAV_TOKENS, max_previews=_DEFAULT_MAX_PREVIEWS,
                 max_reviews=_DEFAULT_MAX_REVIEWS, max_pages_per_session=_DEFAULT_MAX_PAGES,
                 max_entries_per_session=_DEFAULT_MAX_ENTRIES,
                 keyring_factory=None, clock=None):
        for name, val in (("session_ttl", session_ttl), ("max_sessions", max_sessions),
                          ("max_nav_tokens", max_nav_tokens), ("max_previews", max_previews),
                          ("max_reviews", max_reviews), ("max_pages_per_session", max_pages_per_session),
                          ("max_entries_per_session", max_entries_per_session)):
            if val <= 0:
                raise ValueError(f"{name} must be positive")
        self._data_dir = str(data_dir)
        self._transport = transport
        self._limit = limit or calibre_opds.Limits()
        self._ttl = float(session_ttl)
        self._max_sessions, self._max_nav, self._max_previews = max_sessions, max_nav_tokens, max_previews
        self._max_reviews, self._max_pages, self._max_entries = max_reviews, max_pages_per_session, max_entries_per_session
        self._keyring_factory = keyring_factory or _get_keyring
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._sessions, self._nav_tokens, self._previews, self._reviews = {}, {}, {}, {}
        self._config = self._load_config()  # file read only: no network, no vault
        self._recovery_required = os.path.lexists(self._recovery_path())

    # --- Config (pure file persistence; no outgoing requests) ----------------

    def _config_path(self):
        return os.path.join(self._data_dir, CONFIG_NAME)

    def _recovery_path(self):
        return self._config_path() + '.credential-pending'

    def _begin_credential_change(self):
        # Persist intent before touching the vault; a crash or rollback failure
        # must remain visible when a new service instance loads the old config.
        self._recovery_required = True
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._recovery_path(), 'xb') as pending:
                pending.write(b'Credential update pending; save configuration to recover.\n')
                pending.flush()
                os.fsync(pending.fileno())
        except FileExistsError:
            pass  # an earlier unresolved transaction already gates browsing
        except OSError:
            raise CatalogError('Could not record credential recovery state; no password was changed.', 503) from None

    def _finish_credential_change(self):
        try:
            os.unlink(self._recovery_path())
        except FileNotFoundError:
            pass
        except OSError:
            raise CatalogError('Could not clear credential recovery state. Save the catalog again to recover.', 503) from None
        self._recovery_required = False

    def _require_credentials_consistent(self):
        if self._recovery_required or os.path.lexists(self._recovery_path()):
            raise CatalogError('Credential configuration requires recovery. Save the catalog again before browsing.', 503)

    def _load_config(self):
        from utils import safe_load_json
        default = {"root_url": "", "username": "", "revision": 0}
        raw = safe_load_json(self._config_path(), {})
        if not isinstance(raw, dict):
            return default
        root, username = raw.get("root_url"), raw.get("username")
        revision = raw.get("revision")
        # Fail closed on malformed persisted fields (bool is not a revision
        # int); never echo a secret-bearing value, never overwrite the file.
        if not isinstance(root, str) or not isinstance(username, str):
            return default
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            return default
        try:
            if root:
                root = _validate_root_url(root)
            if username:
                username = _validate_username(username)
        except CatalogError:
            return default
        if not root and username:
            return default
        return {"root_url": root, "username": username, "revision": revision}

    def get_config(self):
        """Pure read of the non-secret config (never touches vault/network)."""
        with self._lock:
            cfg = dict(self._config)
        result = {"root_url": cfg["root_url"], "username": cfg["username"],
                  "configured": bool(cfg["root_url"]), "revision": cfg["revision"]}
        if self._recovery_required or os.path.lexists(self._recovery_path()):
            result['recovery_required'] = True
        return result

    def _resolve_keyring(self):
        try:
            keyring = self._keyring_factory()
        except Exception:
            raise CatalogError("The OS credential store is unavailable.", 503) from None
        if keyring is None:
            raise CatalogError(
                "No OS credential store is available (keyring is not installed). "
                "Refusing to store the password anywhere else.", 503)
        _check_secure_backend(keyring)
        return keyring

    def save_config(self, root_url=None, username=None, password=None):
        """Replace the non-secret config; password: None=keep, ""=clear, else store.

        The vault mutation and the config-file write are serialized under
        the config lock with every browse snapshot, so concurrent set/clear
        saves cannot interleave into username-without-password. If the
        config write fails after a vault write, the vault is rolled back
        to its prior values and the previous config is retained; if the
        rollback itself fails, tokens are dropped and the error says so
        explicitly -- success is never claimed for a mixed state. Any
        successful save bumps the revision and invalidates all
        browse/preview/review tokens. Anonymous saves never touch the vault.
        """
        from utils import atomic_json_write
        if root_url is None or not str(root_url).strip():
            root, user = "", ""
            if password not in (None, ""):
                raise CatalogError("No catalog root to bind a password to.", 400)
        else:
            root = _validate_root_url(root_url)
            user = _validate_username(username)
        with self._lock:
            was_recovering = self._recovery_required or os.path.lexists(self._recovery_path())
            if was_recovering and user and password is None:
                raise CatalogError('Supply the password again to recover the catalog configuration.', 503)
            prior_vault = {}
            keyring = None
            if password is not None:
                keyring = self._resolve_keyring()
                accounts = {root}
                if self._config["root_url"]:
                    accounts.add(self._config["root_url"])
                for account in accounts:
                    try:
                        prior_vault[account] = keyring.get_password(CREDENTIAL_SERVICE, account)
                    except Exception:
                        raise CatalogError(
                            "The OS credential store is unavailable, so the password was not changed.", 503) from None
                self._begin_credential_change()
                try:
                    if password == "":
                        keyring.delete_password(CREDENTIAL_SERVICE, root)
                    else:
                        keyring.set_password(CREDENTIAL_SERVICE, root, str(password))
                except Exception:
                    raise CatalogError(
                        "Could not store the password in the OS credential store. Is it locked or "
                        "unavailable? Save the catalog again to recover before browsing.", 503) from None
            revision = self._config["revision"] + 1
            config = {"root_url": root, "username": user, "revision": revision}
            try:
                atomic_json_write(config, self._config_path())
            except Exception:
                rollback_ok = True
                if keyring is not None:
                    for account, value in prior_vault.items():
                        try:
                            if value is None:
                                try:
                                    keyring.delete_password(CREDENTIAL_SERVICE, account)
                                except Exception:
                                    if keyring.get_password(CREDENTIAL_SERVICE, account) is not None:
                                        rollback_ok = False
                            else:
                                keyring.set_password(CREDENTIAL_SERVICE, account, value)
                        except Exception:
                            rollback_ok = False
                if rollback_ok:
                    if keyring is not None and not was_recovering:
                        self._finish_credential_change()
                    raise CatalogError(
                        "Could not persist the catalog configuration. The previous configuration "
                        "was retained.", 503) from None
                self._sessions.clear()
                self._nav_tokens.clear()
                self._previews.clear()
                self._reviews.clear()
                raise CatalogError(
                    "Could not persist the catalog configuration, and the credential store may "
                    "not match it. Save the catalog again to recover.", 503) from None
            self._config = config
            self._sessions.clear()
            self._nav_tokens.clear()
            self._previews.clear()
            self._reviews.clear()
            if keyring is not None or was_recovering:
                self._finish_credential_change()
        return {"root_url": root, "username": user, "configured": bool(root), "revision": revision}

    def _get_auth(self, root=None, user=None):
        """OpdsAuth for a (root, username) snapshot, or None (anonymous).

        Called by ``_browse`` with the snapshot taken under the lock, so
        credentials can never be re-resolved from a newer config; called
        with no arguments it reads the current config. Touches the vault
        only when a username is configured; fail-loud (never a silent
        anonymous downgrade) when the stored password is missing."""
        with self._lock:
            self._require_credentials_consistent()
        if root is None or user is None:
            with self._lock:
                root, user = self._config["root_url"], self._config["username"]
        if not root or not user:
            return None
        keyring = self._resolve_keyring()
        try:
            value = keyring.get_password(CREDENTIAL_SERVICE, root)
        except Exception:
            raise CatalogError(
                "The OS credential store is unavailable, so the password could not be read. "
                "Nothing was sent anywhere.", 503) from None
        if not value:
            raise CatalogError(
                "A username is configured but no password is stored in the OS credential "
                "store. Save the catalog again with a password before browsing.", 400)
        return calibre_opds.OpdsAuth(user, value)

    # --- Bounded server-held state -------------------------------------------

    def _purge_locked(self, now):
        for store in (self._sessions, self._nav_tokens, self._previews, self._reviews):
            for key in [k for k, rec in store.items() if now - rec["created_at"] > self._ttl]:
                store.pop(key, None)

    @staticmethod
    def _evict_oldest(store, cap):
        while len(store) > cap:
            oldest = min(store, key=lambda k: store[k]["created_at"])
            store.pop(oldest, None)

    def _new_session_locked(self, now):
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = {
            "session_id": session_id, "revision": self._config["revision"],
            "root_url": self._config["root_url"], "created_at": now,
            "visited_urls": [], "entry_ids": set(), "nav_keys": set(),
            "pages": 0, "total_entries": 0}
        self._evict_oldest(self._sessions, self._max_sessions)
        return self._sessions.get(session_id)

    def _mint_nav_locked(self, session_id, kind, target_url, now):
        token = uuid.uuid4().hex
        self._nav_tokens[token] = {"token": token, "session_id": session_id, "kind": kind,
                                   "target_url": target_url, "revision": self._config["revision"],
                                   "created_at": now, "consumed": False, "approved_urls": []}
        self._evict_oldest(self._nav_tokens, self._max_nav)
        return token

    def _validate_nav_locked(self, token, now):
        rec = self._nav_tokens.get(token)
        if rec is None:
            raise CatalogError("Unknown or expired browse token. Browse the catalog again.", 410)
        if rec["consumed"]:
            raise CatalogError("This browse token was already used. Use the newest token.", 410)
        if rec["revision"] != self._config["revision"]:
            raise CatalogError("The catalog configuration changed. Browse the catalog again.", 410)
        session = self._sessions.get(rec["session_id"])
        if session is None or session["revision"] != self._config["revision"]:
            raise CatalogError("The browse session expired. Browse the catalog again.", 410)
        return rec, session

    def _review_dict(self, review):
        return {"review_required": True, "review_token": review["token"],
                "destination_origin": review["destination_origin"],
                "destination_url": _display_url(review["destination_url"]),
                "operation": review["operation"], "config_revision": review["revision"],
                "session_id": review["session_id"], "nav_token": review["nav_token"]}

    def _mint_review_locked(self, session_id, nav_token, operation, destination_url, dest_origin, now):
        token = uuid.uuid4().hex
        self._reviews[token] = {"token": token, "session_id": session_id, "nav_token": nav_token,
                                "operation": operation, "target_url": destination_url,
                                "destination_url": destination_url,
                                "destination_origin": dest_origin,
                                "revision": self._config["revision"], "created_at": now}
        self._evict_oldest(self._reviews, self._max_reviews)
        return self._reviews.get(token)

    # --- Browse (explicit service calls only; one page per call) --------------

    def browse_root_page(self, cancel_event=None, should_cancel=None):
        """Fetch the configured root (never a caller URL). One page only."""
        with self._lock:
            if not self._config["root_url"]:
                raise CatalogError("No catalog configured. Save a catalog root first.", 400)
            now = self._clock()
            self._purge_locked(now)
            session = self._new_session_locked(now)
            if session is None:
                raise CatalogError("Too many browse sessions; try again.", 503)
            token = self._mint_nav_locked(session["session_id"], "root",
                                          session["root_url"], now)
        return self._browse(token, "root", cancel_event, should_cancel)

    def browse_next_page(self, nav_token, cancel_event=None, should_cancel=None):
        """Fetch the exact server-held target bound to ``nav_token`` (one page).

        ``nav_token`` must come from a previous page dict or an approved
        cross-origin review; arbitrary caller URLs are not accepted here."""
        if cancel_event is not None and should_cancel is None:
            should_cancel = cancel_event.is_set
        with self._lock:
            self._purge_locked(self._clock())
            rec, _session = self._validate_nav_locked(nav_token, self._clock())
            kind = rec["kind"]
        return self._browse(nav_token, kind, cancel_event, should_cancel)

    def _browse(self, token, kind, cancel_event, should_cancel):
        with self._lock:
            rec, session = self._validate_nav_locked(token, self._clock())
            target = rec["target_url"]
            approved_urls = frozenset(rec.get("approved_urls") or ())
            rec["consumed"] = True
            session_id = session["session_id"]
            if kind == "start":
                # Restart as a fresh chain so the start target is not a
                # history loop; the old session simply expires unused.
                fresh = self._new_session_locked(self._clock())
                if fresh is None:
                    raise CatalogError("Too many browse sessions; try again.", 503)
                session, session_id = fresh, fresh["session_id"]
            elif kind in ("next", "root", "nav"):
                if target in session["visited_urls"]:
                    raise CatalogError("Pagination loop refused: this page was already visited.", 409)
                if session["pages"] >= self._max_pages:
                    raise CatalogError(f"Page limit reached ({self._max_pages} pages per session).", 413)
            # One consistent snapshot: root, username and revision are read
            # together under the lock; auth below is resolved from this
            # snapshot only, never from a newer config.
            root_url = session["root_url"]
            snap_user, snap_revision = self._config["username"], rec["revision"]
        auth = self._get_auth(root_url, snap_user)
        with self._lock:
            # Stale observed before dispatch: reject before any request.
            if self._config["revision"] != snap_revision or token not in self._nav_tokens:
                raise CatalogError("The catalog configuration changed. Browse the catalog again.", 410)
        policy = calibre_opds.DestinationPolicy(allowed_exact_urls=approved_urls)
        try:
            feed = calibre_opds.fetch_feed(
                target, root_url=root_url, policy=policy, auth=auth,
                transport=self._transport, limit=self._limit, should_cancel=should_cancel)
        except calibre_opds.CancelledError:
            with self._lock:  # cancelled before completing: token may be retried
                if token in self._nav_tokens:
                    self._nav_tokens[token]["consumed"] = False
            raise
        except CrossOriginReviewRequired as exc:
            with self._lock:
                if self._config["revision"] != rec["revision"]:
                    raise CatalogError("The catalog configuration changed. Browse the catalog again.", 410)
                if token in self._nav_tokens:
                    self._nav_tokens[token]["consumed"] = False  # retry after approval
                # The exact pending destination is the exception's validated
                # URL (the actual redirect target), not the original target.
                review = self._mint_review_locked(session_id, token, kind,
                                                  exc.destination_url or target,
                                                  exc.destination_origin, self._clock())
                if review is None:
                    raise CatalogError("Cross-origin review could not be recorded; try again.", 503)
                return self._review_dict(review)
        except OpdsError as exc:
            raise CatalogError(str(exc), 502) from None
        # Update server-held history; discard results if config changed mid-fetch.
        with self._lock:
            if self._config["revision"] != rec["revision"]:
                raise CatalogError("The catalog configuration changed. Browse the catalog again.", 410)
            live = self._sessions.get(session_id)
            if live is None:
                raise CatalogError("The browse session expired. Browse the catalog again.", 410)
            # Completion invalidates any pending review for this action.
            for rt in [k for k, v in self._reviews.items() if v["nav_token"] == token]:
                self._reviews.pop(rt, None)
            if feed.feed_url in live["visited_urls"]:
                raise CatalogError("Pagination loop refused: this feed was already visited.", 409)
            live["visited_urls"].append(feed.feed_url)
            live["pages"] += 1
            now = self._clock()
            live.setdefault("nav_keys", set())
            entries_out, fresh = [], []
            for entry in feed.entries:
                nav_links = [link for link in (entry.links or ())
                             if _is_navigation_link(link)]
                # De-duplicate nav links per entry by resolved target: one
                # token per distinct destination, never two tokens for the
                # same URL on one entry.
                seen_hrefs = set()
                distinct_nav = []
                for link in nav_links:
                    if link.href in seen_hrefs:
                        continue
                    seen_hrefs.add(link.href)
                    distinct_nav.append(link)
                new_book = (entry.acquisition is not None and not entry.rejected_reason
                            and entry.stable_id not in live["entry_ids"])
                key = (entry.stable_id,
                       tuple(sorted(link.href for link in distinct_nav)))
                if distinct_nav:
                    # Distinct cross-page sections keep their navigation even
                    # when the book's acquisition preview was already listed.
                    if key in live["nav_keys"]:
                        distinct_nav = []
                        if not new_book:
                            continue
                elif entry.stable_id in live["entry_ids"]:
                    continue
                fresh.append((entry, distinct_nav, key, new_book))
            if live["total_entries"] + len(fresh) > self._max_entries:
                raise CatalogError(
                    f"Entry limit reached ({self._max_entries} entries per session).", 413)
            for entry, distinct_nav, dedupe_key, new_book in fresh:
                if distinct_nav:
                    live["nav_keys"].add(dedupe_key)
                if not distinct_nav or new_book:
                    live["entry_ids"].add(entry.stable_id)
                live["total_entries"] += 1
                preview_token = None
                if new_book:
                    preview_token = uuid.uuid4().hex
                    self._previews[preview_token] = {
                        "token": preview_token, "session_id": session_id,
                        "revision": rec["revision"], "created_at": now,
                        "stable_id": entry.stable_id,
                        "acquisition_href": entry.acquisition.href,
                        "approved_urls": [],
                        "feed_url": feed.feed_url,
                        "provider": {"title": entry.title, "authors": list(entry.authors),
                                     "series": entry.series, "series_index": entry.series_index,
                                     "subtitle": entry.subtitle, "format": "epub"}}
                    self._evict_oldest(self._previews, self._max_previews)
                    if preview_token not in self._previews:
                        preview_token = None  # evicted immediately: no preview for this entry
                navigation = []
                for link in distinct_nav:
                    loop_refused = (link.href in live["visited_urls"]
                                    or link.href == feed.feed_url)
                    nav_token = None
                    if not loop_refused:
                        nav_token = self._mint_nav_locked(
                            session_id, "nav", link.href, now)
                        if nav_token not in self._nav_tokens:
                            nav_token = None  # evicted immediately: no navigation
                    navigation.append({
                        "title": link.title or entry.title,
                        "url": _display_url(link.href),
                        "nav_token": nav_token,
                        "loop_refused": loop_refused})
                entries_out.append({
                    "stable_id": entry.stable_id, "title": entry.title,
                    "authors": list(entry.authors), "series": entry.series,
                    "series_index": entry.series_index, "subtitle": entry.subtitle,
                    "format": "epub" if entry.acquisition else "",
                    "has_acquisition": entry.acquisition is not None,
                    "rejected_reason": entry.rejected_reason,
                    "source_url": _display_url(entry.acquisition.href) if entry.acquisition else None,
                    "preview_token": preview_token,
                    "navigation": navigation})
            next_token, next_loop = None, False
            if feed.next_link is not None:
                if feed.next_link.href in live["visited_urls"] or feed.next_link.href == feed.feed_url:
                    next_loop = True
                else:
                    next_token = self._mint_nav_locked(session_id, "next", feed.next_link.href, now)
            start_token = None
            if feed.start_link is not None:
                start_token = self._mint_nav_locked(session_id, "start", feed.start_link.href, now)
            # Later category/pagination mints can evict earlier tokens on a
            # large page. Return authority only for tokens still held now.
            for entry_out in entries_out:
                for nav in entry_out["navigation"]:
                    if nav["nav_token"] not in self._nav_tokens:
                        nav["nav_token"] = None
            return {"review_required": False, "title": feed.title,
                    "feed_url": _display_url(feed.feed_url), "entries": entries_out,
                    "next_token": next_token, "next_loop_refused": next_loop,
                    "start_token": start_token,
                    "search_url": _display_url(feed.search_link.href) if feed.search_link else None,
                    "config_revision": rec["revision"], "pages_fetched": live["pages"],
                    "entries_total": live["total_entries"]}

    # --- Cross-origin review (explicit approval, single-use, exact binding) ---

    def approve_cross_origin_review(self, review_token):
        """Approve exactly the bound destination URL for the bound session/action.

        Takes ONLY the review token: the approved URL is the server-held
        one, never a client-supplied value. Single-use; the retry replays
        with the ORIGINAL credential root, so the target origin receives no
        credentials even after approval. Approving one exact URL adds it to
        the pending action's bounded approved set -- it never trusts the
        origin, so a changed redirect target needs its own review."""
        with self._lock:
            self._purge_locked(self._clock())
            review = self._reviews.get(review_token)
            if review is None:
                raise CatalogError("Unknown or expired review token.", 410)
            if review["revision"] != self._config["revision"]:
                raise CatalogError("The catalog configuration changed. Review again.", 410)
            nav = self._nav_tokens.get(review["nav_token"])
            if nav is None or nav["consumed"]:
                raise CatalogError("The pending browse action expired. Browse again.", 410)
            approved = nav.setdefault("approved_urls", [])
            dest_url = review["destination_url"]
            if dest_url not in approved:
                if len(approved) >= max(1, self._limit.max_redirects):
                    raise CatalogError("Too many cross-origin destinations for this action.", 413)
                approved.append(dest_url)
            result = {"approved": True, "nav_token": nav["token"],
                      "destination_origin": review["destination_origin"],
                      "destination_url": _display_url(dest_url),
                      "operation": review["operation"], "config_revision": review["revision"],
                      "session_id": review["session_id"]}
            self._reviews.pop(review_token, None)
            return result

    # --- Metadata preview (pure read of a server-held entry) -------------------

    # --- Capture support (service-held snapshot + exact review) ---------------
    # Narrow additions for the import slice: the import service needs the
    # exact server-held acquisition href (not the redacted display URL) and
    # an exact-URL approval for cross-origin acquisition/redirects. Approvals
    # live on the preview record itself, so an approval covers exactly one
    # entry's acquisition URL and never trusts an origin.

    def _preview_locked(self, preview_token, now):
        rec = self._previews.get(preview_token)
        if rec is None:
            raise CatalogError("Unknown or expired preview token. Browse the catalog again.", 410)
        if rec["revision"] != self._config["revision"]:
            raise CatalogError("The catalog configuration changed. Browse the catalog again.", 410)
        session = self._sessions.get(rec["session_id"])
        if session is None or session["revision"] != self._config["revision"]:
            raise CatalogError("The browse session expired. Browse the catalog again.", 410)
        return rec, session

    def get_capture_binding(self, preview_token):
        """Exact server-held binding for a capture: href + credential snapshot."""
        with self._lock:
            self._purge_locked(self._clock())
            rec, session = self._preview_locked(preview_token, self._clock())
            root_url = session["root_url"]
            snap_user, snap_revision = session and self._config["username"], rec["revision"]
            binding = {"root_url": root_url, "revision": snap_revision,
                       "acquisition_href": rec["acquisition_href"],
                       "stable_id": rec["stable_id"], "session_id": rec["session_id"],
                       "approved_urls": frozenset(rec.get("approved_urls") or ()),
                       "provider": copy.deepcopy(rec["provider"])}
        auth = self._get_auth(root_url, snap_user)
        with self._lock:
            if self._config["revision"] != snap_revision:
                raise CatalogError("The catalog configuration changed. Capture again.", 410)
        binding["auth"] = auth
        return binding

    def request_capture_review(self, preview_token):
        with self._lock:
            self._purge_locked(self._clock())
            rec, session = self._preview_locked(preview_token, self._clock())
            href = rec["acquisition_href"]
            try:
                dest_origin = calibre_opds._origin(href)
                root_origin = calibre_opds._origin(session["root_url"])
            except OpdsError:
                raise CatalogError("Catalog entry has an invalid acquisition URL.", 400) from None
            if dest_origin == root_origin or href in (rec.get("approved_urls") or ()):
                return {"review_required": False, "source_url": _display_url(href),
                        "destination_origin": dest_origin,
                        "config_revision": rec["revision"]}
            review = self._mint_review_locked(rec["session_id"], preview_token, "capture",
                                              href, dest_origin, self._clock())
            if review is None:
                raise CatalogError("Cross-origin review could not be recorded; try again.", 503)
            return self._review_dict(review)

    def mint_capture_redirect_review(self, preview_token, destination_url,
                                          destination_origin):
        """Mint the exact redirect review for a capture under its preview.

        Used by the import worker when a same-origin acquisition redirects
        cross-origin: the pending destination is the exception's validated
        URL (not the original href), bound to the original preview/token,
        revision and session. Approval adds exactly this URL to the
        preview's approved set; a later changed target needs a fresh review.
        """
        if not preview_token or not destination_url:
            raise CatalogError("Unknown or expired preview token. Browse the catalog again.", 410)
        with self._lock:
            self._purge_locked(self._clock())
            rec, session = self._preview_locked(preview_token, self._clock())
            validated = calibre_opds._validate_url(destination_url)
            try:
                origin = destination_origin or calibre_opds._origin(validated)
            except OpdsError:
                raise CatalogError(
                    "Catalog entry has an invalid acquisition URL.", 400) from None
            review = self._mint_review_locked(rec["session_id"], preview_token, "capture",
                                              validated, origin, self._clock())
            if review is None:
                raise CatalogError("Cross-origin review could not be recorded; try again.", 503)
            return self._review_dict(review)

    def approve_capture_review(self, review_token):
        with self._lock:
            self._purge_locked(self._clock())
            review = self._reviews.get(review_token)
            if review is None:
                raise CatalogError("Unknown or expired review token.", 410)
            if review["operation"] != "capture":
                raise CatalogError("Unknown or expired review token.", 410)
            if review["revision"] != self._config["revision"]:
                raise CatalogError("The catalog configuration changed. Review again.", 410)
            rec, _session = self._preview_locked(review["nav_token"], self._clock())
            approved = rec.setdefault("approved_urls", [])
            dest_url = review["destination_url"]
            if dest_url not in approved:
                if len(approved) >= max(1, self._limit.max_redirects):
                    raise CatalogError("Too many cross-origin destinations for this entry.", 413)
                approved.append(dest_url)
            result = {"approved": True, "preview_token": rec["token"],
                      "destination_origin": review["destination_origin"],
                      "destination_url": _display_url(dest_url),
                      "operation": "capture", "config_revision": review["revision"],
                      "session_id": review["session_id"]}
            self._reviews.pop(review_token, None)
            return result

    def approve_review(self, review_token):
        """Dispatch on the server-held operation (browse page vs capture)."""
        with self._lock:
            review = self._reviews.get(review_token)
            if review is None:
                raise CatalogError("Unknown or expired review token.", 410)
            operation = review["operation"]
        # Delegate outside the lock request: each method re-validates.
        if operation == "capture":
            return self.approve_capture_review(review_token)
        return self.approve_cross_origin_review(review_token)

    def get_metadata_preview(self, preview_token):
        """Pure read: immutable provider metadata, kept separate from any
        future user-editable copy. Never accepts a caller URL or entry."""
        with self._lock:
            self._purge_locked(self._clock())
            rec = self._previews.get(preview_token)
            if rec is None:
                raise CatalogError("Unknown or expired preview token. Browse the catalog again.", 410)
            if rec["revision"] != self._config["revision"]:
                raise CatalogError("The catalog configuration changed. Browse the catalog again.", 410)
            provider = copy.deepcopy(rec["provider"])
            return {"preview_token": rec["token"], "config_revision": rec["revision"],
                    "provider": provider,
                    "user_editable": {"title": provider["title"],
                                      "authors": list(provider["authors"]),
                                      "series": provider["series"]},
                    "stable_id": rec["stable_id"],
                    "feed_url": _display_url(rec["feed_url"]),
                    "source_url": _display_url(rec["acquisition_href"])}
