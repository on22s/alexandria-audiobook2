"""Calibre OPDS -> existing upload authority import service (issue #1734, slice 3).

Bounded behaviour:
- Capture starts only from a server-held metadata preview token; the caller
  supplies no URL/path/entry fields. A fresh capture is an explicit network
  action; a metadata preview alone never imports.
- Cross-origin acquisition/redirect destinations require an exact-URL review
  from ``calibre_catalog`` (server-held token, single-use); credentials are
  never forwarded to a non-root origin.
- One live captured EPUB per service; global staging cap 200 MB and never
  above ``MAX_SCRIPT_UPLOAD_BYTES`` (supplied by the route, which owns the
  upload authority constant). The existing upload quota stays authoritative
  at import time.
- Every captured file is verified (identity + SHA-256 over one open read-only
  handle) and the verified bytes are snapshotted before any handoff. The
  handoff always consumes that immutable snapshot via BytesIO; the staged
  path is never reopened for publication, so a later swap of the named stage
  file cannot change what the authority publishes.
- Import calls the existing ``routers.script.upload_file`` with an UploadFile
  over the snapshot bytes, a synthetic safe ``.epub`` filename,
  ``select_active=False`` and ``include_extraction_report=True``. This module
  duplicates no extraction/publication/selection logic and does not claim
  permanent retention of the original EPUB (the local pipeline publishes the
  extracted TXT + source companion; the captured original is consumed and
  its staged file then deleted).

No route, config persistence or UI lives here; routes live in
``routers/script.py`` and call exactly these verbs.
"""
import hashlib
import io
import os
import threading
import time
import uuid
import zipfile

import calibre_opds
from calibre_catalog import CatalogError
from calibre_opds import CancelledError, CrossOriginReviewRequired, OpdsError

_GLOBAL_STAGING_CAP = 200 * 1024 * 1024
_DEFAULT_TTL = 600.0
_DEFAULT_MAX_JOBS = 50


class ImportRefused(ValueError):
    """User-facing refusal with an HTTP status for the route wrapper."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _display_url(url):
    from calibre_catalog import _display_url as catalog_display_url
    return catalog_display_url(url)


def _fd_sha256_and_bytes(path, expected_size, max_bytes):
    """Hash + read a staged capture through one open read-only handle.

    Returns (sha256, raw_bytes) snapshot. Raises ImportRefused if the file
    identity or size no longer matches what capture recorded.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(fd)
        if info.st_size != expected_size:
            raise ImportRefused("Captured file changed since capture; capture again.", 409)
        digest = hashlib.sha256()
        raw = bytearray()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise ImportRefused("Captured file exceeds the staging cap; capture again.", 413)
        if os.fstat(fd).st_size != expected_size:
            raise ImportRefused("Captured file changed since capture; capture again.", 409)
        return digest.hexdigest(), bytes(raw)
    finally:
        os.close(fd)


def _validate_epub_bytes(raw):
    """Bounded ZIP/package validation via shared validators only.

    validate_epub_archive already enforces member count, total expanded bytes
    and per-member expansion ratio (zip-bomb defence). We additionally refuse
    zip-encrypted members and META-INF/encryption.xml without any DRM bypass.
    PK header alone is insufficient: a real container + OPF package is
    required. Obfuscated-font EPUBs (urn:font:swType) are not specially
    handled here; extraction simply reads spine XHTML as usual.
    """
    from alexandria_alignment import validate_epub_archive
    try:
        validate_epub_archive("", archive_bytes=raw)
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ImportRefused(f"EPUB archive refused: {exc}", 400) from None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if "META-INF/encryption.xml" in names:
                raise ImportRefused("Protected/encrypted EPUBs are not supported.", 400)
            for member in archive.infolist():
                if member.flag_bits & 0x1:
                    raise ImportRefused("Protected/encrypted EPUBs are not supported.", 400)
            if "META-INF/container.xml" not in names:
                raise ImportRefused("EPUB archive refused: missing META-INF/container.xml.", 400)
            if not any(name.lower().endswith(".opf") for name in names):
                raise ImportRefused("EPUB archive refused: missing OPF package.", 400)
    except zipfile.BadZipFile as exc:
        raise ImportRefused(f"EPUB archive refused: {exc}", 400) from None


class CalibreImportService:
    def __init__(self, data_dir, *, catalog, max_upload_bytes, limit=None,
                 staging_cap=_GLOBAL_STAGING_CAP, ttl=_DEFAULT_TTL,
                 max_jobs=_DEFAULT_MAX_JOBS, clock=None):
        if not isinstance(max_upload_bytes, int) or max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        self._data_dir = str(data_dir)
        self._catalog = catalog
        self._cap = min(int(staging_cap), _GLOBAL_STAGING_CAP, max_upload_bytes)
        self._ttl = float(ttl)
        self._max_jobs = int(max_jobs)
        self._clock = clock or time.monotonic
        base_limit = limit or calibre_opds.Limits()
        import dataclasses
        self._limit = dataclasses.replace(base_limit, max_download_bytes=self._cap)
        # RLock: purge values are read under the lock, file deletes happen
        # outside it; workers never hold it across network/import.
        self._lock = threading.RLock()
        self._live_job = None          # job_token owning the single live slot
        self._jobs = {}                # job_token -> job record
        self._confirmations = {}       # confirmation_token -> confirmation record
        self._staging_dir = os.path.join(self._data_dir, "calibre_staging")

    # --- staging / lifecycle helpers -----------------------------------------

    def _ensure_staging(self):
        os.makedirs(self._staging_dir, exist_ok=True)

    @staticmethod
    def _delete_owned(path):
        """Delete one owned staged file. True when gone/missing, False on OSError."""
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
            return True
        except OSError:
            return False

    def _purge_locked(self, now):
        """Drop expired jobs/confirmations; returns owned paths to delete.

        Active capturing workers are only cancelled (their finally owns
        cleanup); importing confirmations keep both record and file.
        """
        dead = []
        for token, job in list(self._jobs.items()):
            if job["status"] in ("capturing", "pending") and now - job["created_at"] > self._ttl:
                job["cancel"].set()
        for token, job in list(self._jobs.items()):
            if job["status"] in ("captured", "failed", "cancelled", "discarded", "stale",
                                 "review_required") \
                    and now - job["updated_at"] > self._ttl:
                if job.get("path"):
                    dead.append(job["path"])
                    job["path"] = None
                del self._jobs[token]
                if self._live_job == token:
                    self._live_job = None
        for token, conf in list(self._confirmations.items()):
            if now - conf["created_at"] > self._ttl and not conf["import_started"]:
                if conf.get("path"):
                    dead.append(conf["path"])
                del self._confirmations[token]
        return dead

    def purge_expired(self):
        """Explicit maintenance: drop expired records and delete owned files.

        Safe to call concurrently; deleting a tracked file only happens for
        expired, non-importing records (cleanup failure leaves the file as
        on-disk residue that blocks new captures until removed).
        """
        with self._lock:
            dead = self._purge_locked(self._clock())
        for path in dead:
            self._delete_owned(path)

    def _purge(self):
        self.purge_expired()

    def _disk_staging_files(self):
        """[(path, size)] for owned staged captures (opds-*.epub) on disk."""
        try:
            names = os.listdir(self._staging_dir)
        except (FileNotFoundError, OSError):
            return []
        out = []
        for name in names:
            if not (name.startswith("opds-") and name.endswith(".epub")):
                continue
            path = os.path.join(self._staging_dir, name)
            try:
                if os.path.isfile(path):
                    out.append((path, os.path.getsize(path)))
            except OSError:
                out.append((path, 0))
        return out

    def _tracked_paths_locked(self):
        tracked = set()
        for job in self._jobs.values():
            if job.get("path"):
                tracked.add(job["path"])
        for conf in self._confirmations.values():
            if conf.get("path"):
                tracked.add(conf["path"])
        return tracked

    # --- capture --------------------------------------------------------------

    def start_capture(self, preview_token):
        """Reserve the single live slot + mint a cancelable job (no network yet).

        Returns (job_token). Purges expired records first (deleting their
        owned files) and refuses while untracked on-disk residue remains,
        so quota is never reset by a new service instance or config change.
        """
        binding = self._catalog.get_capture_binding(preview_token)
        started = self._clock()
        with self._lock:
            dead = self._purge_locked(started)
            for path in dead:
                self._delete_owned(path)
            if self._live_job is not None:
                raise ImportRefused(
                    "Another Calibre capture is already in progress. "
                    "Wait for it, cancel it, or discard it first.", 409)
            tracked = self._tracked_paths_locked()
            residue = [p for p, _size in self._disk_staging_files() if p not in tracked]
            if residue:
                raise ImportRefused(
                    "Previous Calibre staging residue requires maintenance "
                    "before another capture.", 409)
            total = sum(size for _p, size in self._disk_staging_files())
            if total > self._cap:
                raise ImportRefused("Calibre staging exceeds its cap; capture refused.", 413)
            job_token = uuid.uuid4().hex
            job = {"token": job_token, "status": "capturing",
                   "revision": binding["revision"], "created_at": started,
                   "updated_at": started, "cancel": threading.Event(),
                   "path": None, "error": "", "confirmation_token": None,
                   "preview_token": preview_token, "review_token": None,
                   "review_destination_url": None, "review_operation": None}
            self._jobs[job_token] = job
            if len(self._jobs) > self._max_jobs:
                oldest = min(self._jobs, key=lambda k: self._jobs[k]["created_at"])
                if oldest != job_token:
                    old = self._jobs.pop(oldest, None)
                    if old and old.get("path"):
                        path = old["path"]
                        old["path"] = None
                        # Unlock for delete would complicate the reservation;
                        # unlink of one small staged file under the lock keeps
                        # the live-slot accounting atomic.
                        self._delete_owned(path)
            self._live_job = job_token
        worker = threading.Thread(target=self._run_capture, args=(job_token, binding),
                                  daemon=True)
        worker.start()
        return job_token

    def _run_capture(self, job_token, binding):
        with self._lock:
            job = self._jobs.get(job_token)
        if job is None:
            return
        captured = None
        handed_off = False
        try:
            if job["cancel"].is_set():
                raise CancelledError("cancelled")
            if self._catalog.get_config().get("revision") != binding["revision"]:
                raise ImportRefused("The catalog configuration changed. Capture again.", 410)
            self._ensure_staging()
            policy = calibre_opds.DestinationPolicy(
                allowed_exact_urls=frozenset(binding["approved_urls"] or ()))
            try:
                captured = calibre_opds.capture_epub(
                    binding["acquisition_href"], self._staging_dir,
                    root_url=binding["root_url"], policy=policy,
                    auth=binding["auth"], limit=self._limit,
                    should_cancel=job["cancel"].is_set)
            except CrossOriginReviewRequired as exc:
                # Mint an exact redirect review under the original preview
                # (action/revision/session); the status then exposes the
                # token so the route can approve and the capture replayed.
                review_token = None
                review_url = None
                review_operation = None
                if exc.destination_url:
                    try:
                        review = self._catalog.mint_capture_redirect_review(
                            job.get("preview_token"), exc.destination_url,
                            exc.destination_origin)
                        review_token = review["review_token"]
                        review_url = review["destination_url"]
                        review_operation = review["operation"]
                    except (CatalogError, OpdsError, ValueError):
                        review_token = None
                with self._lock:
                    live = self._jobs.get(job_token)
                    if live is not None:
                        live["status"] = "review_required"
                        live["error"] = str(exc)
                        live["review_token"] = review_token
                        live["review_destination_url"] = review_url
                        live["review_operation"] = review_operation
                        live["updated_at"] = self._clock()
                self._release_live(job_token)
                return
            except CancelledError:
                self._set_status(job_token, "cancelled", error="Capture cancelled.")
                self._release_live(job_token)
                return
            except (OpdsError, OSError) as exc:
                self._set_status(job_token, "failed", error=str(exc) or "Capture failed.")
                self._release_live(job_token)
                return
            # Verify, validate, then re-check cancellation/revision before
            # minting any confirmation so a stale or cancelled worker can
            # never repopulate. Only the immutable snapshot crosses over.
            digest, raw = _fd_sha256_and_bytes(captured.path, captured.size_bytes,
                                               self._cap)
            if digest != captured.sha256:
                raise ImportRefused(
                    "Captured file failed verification; capture again.", 409)
            _validate_epub_bytes(raw)
            if job["cancel"].is_set():
                raise CancelledError("cancelled")
            if self._catalog.get_config().get("revision") != binding["revision"]:
                raise ImportRefused("The catalog configuration changed. Capture again.", 410)
            if job["cancel"].is_set():
                raise CancelledError("cancelled")
            # Provider + stable id stay immutable OPDS origin values; EPUB
            # package metadata is handled only by the upload authority.
            provider = dict(binding["provider"])
            stable_id = binding["stable_id"]
            confirmation_token = uuid.uuid4().hex
            conf = {"token": confirmation_token, "job_token": job_token,
                    "revision": binding["revision"],
                    "stable_id": stable_id,
                    "acquisition_href": binding["acquisition_href"],
                    "final_url": captured.final_url,
                    "sha256": captured.sha256, "size_bytes": captured.size_bytes,
                    "path": captured.path, "provider": provider,
                    "created_at": self._clock(), "import_started": False}
            with self._lock:
                live = self._jobs.get(job_token)
                if live is None:
                    raise ImportRefused("Capture was discarded; capture again.", 410)
                if live["cancel"].is_set():
                    raise CancelledError("cancelled")
                self._confirmations[confirmation_token] = conf
                live["path"] = captured.path
                live["confirmation_token"] = confirmation_token
                live["status"] = "captured"
                live["updated_at"] = self._clock()
            handed_off = True
        except CancelledError:
            if not handed_off:
                self._set_status(job_token, "cancelled", error="Capture cancelled.")
                self._release_live(job_token)
        except ImportRefused as exc:
            if not handed_off:
                self._set_status(job_token, "failed", error=str(exc))
                self._release_live(job_token)
        except Exception as exc:  # never leak an orphan or unhandled worker failure
            if not handed_off:
                self._set_status(job_token, "failed",
                                 error=f"Capture failed: {type(exc).__name__}")
                self._release_live(job_token)
        finally:
            if captured is not None and not handed_off:
                try:
                    calibre_opds.discard_capture(captured)
                except Exception:
                    pass

    def _set_status(self, job_token, status, error=""):
        with self._lock:
            job = self._jobs.get(job_token)
            if job is not None:
                job["status"] = status
                job["error"] = error
                job["updated_at"] = self._clock()

    def _release_live(self, job_token):
        with self._lock:
            if self._live_job == job_token:
                self._live_job = None

    def get_capture_status(self, job_token):
        """Pure read of one job (explicit ``purge_expired`` maintains records)."""
        with self._lock:
            job = self._jobs.get(job_token)
            if job is None:
                raise ImportRefused("Unknown or expired capture job.", 410)
            status = {"job_token": job_token, "status": job["status"],
                      "error": job["error"],
                      "confirmation_token": job["confirmation_token"],
                      "review_token": job.get("review_token"),
                      "review_destination_url": job.get("review_destination_url"),
                      "review_operation": job.get("review_operation")}
            if job["status"] == "captured" and job["confirmation_token"]:
                conf = self._confirmations.get(job["confirmation_token"])
                if conf is not None:
                    status["provider"] = dict(conf["provider"])
                    status["source_identity"] = {
                        "stable_id": conf["stable_id"],
                        "source_url": _display_url(conf["acquisition_href"]),
                        "final_url": _display_url(conf["final_url"])}
                    status["sha256"] = conf["sha256"]
                    status["size_bytes"] = conf["size_bytes"]
                    status["config_revision"] = conf["revision"]
            return status

    def cancel_capture(self, job_token):
        with self._lock:
            job = self._jobs.get(job_token)
            if job is None:
                raise ImportRefused("Unknown or expired capture job.", 410)
            if job["status"] in ("captured", "imported"):
                raise ImportRefused("Capture already completed; discard it instead.", 409)
            job["cancel"].set()
            if job["status"] == "review_required":
                # The worker already released the live slot and returned, so
                # no active worker will ever observe the cancel event. Move
                # this non-active job to terminal cancelled under the lock;
                # release the slot only if this job still owns it.
                job["status"] = "cancelled"
                job["error"] = "Capture cancelled."
                job["updated_at"] = self._clock()
                if self._live_job == job_token:
                    self._live_job = None
                return {"job_token": job_token, "status": "cancelled",
                        "cancelling": False}
            return {"job_token": job_token, "status": job["status"],
                    "cancelling": job["status"] == "capturing"}

    def discard_capture(self, confirmation_token):
        self.purge_expired()
        with self._lock:
            conf = self._confirmations.get(confirmation_token)
            if conf is None:
                raise ImportRefused("Unknown or expired capture. Capture again.", 410)
            if conf["import_started"]:
                raise ImportRefused("This capture is already being imported.", 409)
            conf = self._confirmations.pop(confirmation_token, None)
            path = conf["path"]
            job = self._jobs.get(conf["job_token"])
            if job is not None:
                job["status"] = "discarded"
                job["path"] = None
                job["updated_at"] = self._clock()
            self._release_live(conf["job_token"])
        self._delete_owned(path)
        return {"discarded": True, "confirmation_token": confirmation_token}

    # --- confirmed import -------------------------------------------------------

    def import_confirmed(self, confirmation_token, sha256, uploader):
        """Reserve one-time, snapshot the verified bytes, hand off to upload_file.

        ``uploader`` takes (raw_bytes, filename) -> upload result, supplied by
        the route so this module never imports the router module. The bytes
        are the immutable snapshot verified against the confirmed SHA/size;
        later mutation of the named staged path cannot change them. Only a
        successful authority return marks the job ``imported``; earlier
        failures mark it ``failed`` and mid-import failures stay truthfully
        failed. The staged file is then deleted once (owned capture only,
        never a published book); the token stays consumed after reservation.
        """
        self.purge_expired()
        with self._lock:
            conf = self._confirmations.get(confirmation_token)
            if conf is None:
                raise ImportRefused("Unknown or expired capture. Capture again.", 410)
            if conf["import_started"]:
                raise ImportRefused("This capture was already imported. Capture again.", 409)
            if sha256 != conf["sha256"]:
                raise ImportRefused("Captured file changed; capture again.", 409)
            conf["import_started"] = True
            job = self._jobs.get(conf["job_token"])
            if job is not None:
                job["status"] = "importing"
                job["error"] = ""
                job["updated_at"] = self._clock()
            path = conf["path"]
            payload = {"provider": dict(conf["provider"]),
                        "stable_id": conf["stable_id"],
                        "acquisition_href": conf["acquisition_href"],
                        "final_url": conf["final_url"],
                        "sha256": conf["sha256"], "size_bytes": conf["size_bytes"],
                        "revision": conf["revision"], "job_token": conf["job_token"]}
        error = ""
        result = None
        try:
            if self._catalog.get_config().get("revision") != payload["revision"]:
                raise ImportRefused("The catalog configuration changed. Capture again.", 410)
            digest, raw = _fd_sha256_and_bytes(path, payload["size_bytes"], self._cap)
            if digest != payload["sha256"]:
                raise ImportRefused("Captured file changed; capture again.", 409)
            synthetic = f"calibre-{payload['sha256'][:16]}.epub"
            result = uploader(raw, synthetic)
        except ImportRefused as exc:
            error = str(exc)
            raise
        except Exception as exc:
            error = f"Import failed: {type(exc).__name__}: {exc}"
            raise
        else:
            return {"upload": result, "provider": payload["provider"],
                    "source_identity": {
                        "stable_id": payload["stable_id"],
                        "source_url": _display_url(payload["acquisition_href"]),
                        "final_url": _display_url(payload["final_url"])},
                    "sha256": payload["sha256"], "size_bytes": payload["size_bytes"]}
        finally:
            with self._lock:
                self._confirmations.pop(confirmation_token, None)
                job = self._jobs.get(payload["job_token"])
                if job is not None:
                    if error:
                        job["status"] = "failed"
                        job["error"] = error
                    else:
                        job["status"] = "imported"
                        job["error"] = ""
                    job["path"] = None
                    job["updated_at"] = self._clock()
                if self._live_job == payload["job_token"]:
                    self._live_job = None
            self._delete_owned(path)

    # --- config-change hook (route calls after a successful save) -------------

    def on_config_changed(self):
        """Cancel live captures; stale unconfirmed ones; preserve importing.

        The worker owns the live slot until it actually exits or hands off:
        capturing jobs are only cancelled (their finally cleans up and
        releases), never cleared here, so a new capture cannot exceed one
        slot while the old worker still owns bytes. Pending confirmations
        are dropped and their owned files deleted; an already-importing
        confirmation, its immutable snapshot and its job are preserved so
        the explicit import can finish with the original bytes only.
        """
        dead = []
        with self._lock:
            for token, job in list(self._jobs.items()):
                status = job["status"]
                if status in ("capturing", "pending"):
                    job["cancel"].set()
                elif status == "captured":
                    job["status"] = "stale"
                    job["cancel"].set()
                    if job.get("path"):
                        dead.append(job["path"])
                        job["path"] = None
                elif status == "review_required":
                    job["status"] = "stale"
                    job["cancel"].set()
                # "importing"/"imported" jobs are left untouched.
            for token, conf in list(self._confirmations.items()):
                if not conf["import_started"]:
                    if conf.get("path") not in dead and conf.get("path"):
                        # Job owns the same path already queued; avoid a
                        # duplicate append by checking the job entry above.
                        if conf["path"] not in dead:
                            dead.append(conf["path"])
                    del self._confirmations[token]
            live = self._jobs.get(self._live_job) if self._live_job else None
            live_status = live["status"] if live else None
            if live_status not in ("capturing", "pending", "importing"):
                self._live_job = None
        for path in dead:
            self._delete_owned(path)
