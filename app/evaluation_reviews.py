"""Append-only human evaluation-review records with blind A/B sessions.

Pure and self-contained (no FastAPI, no app globals) so it is fully unit-testable
and cannot mutate app state. The router layer loads + integrity-checks the
evaluation evidence, then hands this module the validated probe pairs and an
evidence fingerprint.

Design guarantees (Phase 6 success criteria):
- Blind labels never disclose identity before submission — the client payload
  contains only ``A``/``B`` audio; the label->role map lives only in the pending
  session file on the server.
- Stale/changed evidence cannot receive a rating — submit rejects when the
  current evidence fingerprint differs from the one captured at session open.
- Human feedback never promotes anything — this module only reads/writes review
  JSON; it has no access to promotion code.
- History is bounded, evidence-attributable, and removable.
"""

import datetime
import json
from contextlib import nullcontext
import os
import random

from utils import atomic_json_write, file_lock, get_unique_id, safe_load_json, secure_filename
from voice_manifest import validate_adapter_name

STORE_VERSION = 1
MAX_REVIEWS = 50
SESSION_MAX_AGE_SECONDS = 6 * 3600
MAX_NOTE_CHARS = 1000
MIN_RATING = 1
MAX_RATING = 5
VALID_CHOICES = ("A", "B", "tie")


class ReviewError(Exception):
    """Expected, user-correctable rejection (maps to HTTP 409 at the router)."""


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sessions_dir(reviews_dir):
    return os.path.join(reviews_dir, "_sessions")


def _store_path(reviews_dir, adapter_id):
    try:
        validate_adapter_name(adapter_id)
    except ValueError as error:
        raise ReviewError("Invalid adapter id") from error
    return os.path.join(reviews_dir, f"{adapter_id}.json")


def _session_path(reviews_dir, session_id):
    safe = secure_filename(session_id)
    if not safe or safe != session_id:
        raise ReviewError("Invalid session id")
    return os.path.join(_sessions_dir(reviews_dir), f"{safe}.json")


def _get_session_created_at(session):
    if not isinstance(session, dict):
        return None
    try:
        created = datetime.datetime.fromisoformat(session.get("created_at", ""))
    except (TypeError, ValueError):
        return None
    return created if created.tzinfo is not None else None


def _is_session_expired(session, max_age_seconds=SESSION_MAX_AGE_SECONDS, now=None):
    created = _get_session_created_at(session)
    if created is None:
        return True
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return created < now - datetime.timedelta(seconds=max_age_seconds)


def _decision_path(reviews_dir, session_id):
    return os.path.join(reviews_dir, "_pending_decisions",
                        os.path.basename(_session_path(reviews_dir, session_id)))


def _get_decision_journal(path):
    try:
        with open(path, encoding="utf-8") as handle:
            journal = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ReviewError("Could not read pending review decision") from error
    if not isinstance(journal, dict) or journal.get("version") != STORE_VERSION:
        raise ReviewError("Pending review decision is invalid")
    session, record = journal.get("session"), journal.get("record")
    if not isinstance(session, dict) or not isinstance(record, dict):
        raise ReviewError("Pending review decision is invalid")
    session_id, adapter_id = session.get("session_id"), session.get("adapter_id")
    if not isinstance(session_id, str) or not isinstance(adapter_id, str):
        raise ReviewError("Pending review decision identity is invalid")
    _store_path(os.path.dirname(path), adapter_id)  # validate the stored identity
    if os.path.basename(_session_path(os.path.dirname(path), session_id)) != os.path.basename(path):
        raise ReviewError("Pending review decision identity is invalid")
    if (not {"id", "created_at", "adapter_id", "candidate_id", "blind", "evidence",
             "build", "automated", "human"}.issubset(record)
            or _get_session_created_at(session) is None):
        raise ReviewError("Pending review decision record is invalid")
    human = record.get("human")
    if (record.get("id") != "hr_" + session_id or record.get("adapter_id") != adapter_id
            or record.get("candidate_id") != session.get("candidate_id")
            or record.get("evidence") != session.get("fingerprint")
            or record.get("build") != (session.get("build") or {})
            or record.get("automated") != (session.get("automated") or {})
            or record.get("blind") != bool(session.get("blind"))
            or not isinstance(human, dict)
            or human.get("choice_role") not in ("production", "candidate", "tie")
            or not isinstance(human.get("notes"), str)
            or len(human["notes"]) > MAX_NOTE_CHARS
            or _get_session_created_at(record) is None):
        raise ReviewError("Pending review decision record is invalid")
    if _clean_rating(human.get("rating")) != human.get("rating"):
        raise ReviewError("Pending review decision rating is invalid")
    return journal


def _sync_directory(path):
    # The deletion is the consumption marker; persist it before publishing history.
    if os.name == "posix":
        fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove_decision_journal(path):
    journal = _get_decision_journal(path)
    if journal is None:
        raise ReviewError("Pending review decision disappeared before recovery finished")
    try:
        os.unlink(path)
        try:
            _sync_directory(path)
        except OSError:
            # A failed deletion sync must retain the replay barrier: a later
            # append/cleanup cannot age out its ID while deletion is uncertain.
            atomic_json_write(journal, path)
            raise
    except OSError as error:
        raise ReviewError("Could not finish pending review decision recovery") from error


def _get_store_with_review(store, record):
    reviews = store.get("reviews") if isinstance(store, dict) else None
    reviews = reviews if isinstance(reviews, list) else []
    matches = [item for item in reviews if isinstance(item, dict) and item.get("id") == record["id"]]
    if matches:
        if len(matches) != 1 or matches[0] != record:
            raise ReviewError("Pending review decision conflicts with recorded history")
        return store
    return {"version": STORE_VERSION, "reviews": (reviews + [record])[-MAX_REVIEWS:]}


def _publish_consumed_decision(store, record, session_path, store_path, journal_path):
    """Commit one consumed decision; foreground submit and recovery share this boundary."""
    updated = _get_store_with_review(store, record)
    _sync_directory(session_path)
    if updated is not store:
        atomic_json_write(updated, store_path)
    _sync_directory(store_path)
    _remove_decision_journal(journal_path)
    return updated


def _apply_pending_decisions(reviews_dir, adapter_id):
    """Recover this adapter while its store lock is held; never take a session lock."""
    store_path = _store_path(reviews_dir, adapter_id)
    store = safe_load_json(store_path, default={"version": STORE_VERSION, "reviews": []})
    pending_dir = os.path.dirname(_decision_path(reviews_dir, "review"))
    if not os.path.isdir(pending_dir):
        return store
    for name in sorted(os.listdir(pending_dir)):
        if name.startswith(".") or not name.endswith(".json"):
            continue
        path = os.path.join(pending_dir, name)
        journal = _get_decision_journal(path)
        if journal is None or journal["session"]["adapter_id"] != adapter_id:
            continue
        session = journal["session"]
        session_path = _session_path(reviews_dir, session["session_id"])
        try:
            with open(session_path, encoding="utf-8") as handle:
                original = json.load(handle)
            original_present = True
        except FileNotFoundError:
            original_present = False
        except (OSError, ValueError) as error:
            raise ReviewError("Could not verify pending review decision session") from error
        if original_present:
            if original != session:
                raise ReviewError("Pending review decision session identity changed")
            # Preparation alone never accepts a vote. The original remains retryable.
            _remove_decision_journal(path)
            continue
        try:
            store = _publish_consumed_decision(
                store, journal["record"], session_path, store_path, path)
        except OSError as error:
            raise ReviewError("Could not publish pending review decision") from error
    return store


def evidence_fingerprint(production_result, candidate_result):
    """The bound identity of a review: spec + checkpoint hashes from both sides.

    Two reviews concern the same evidence iff these four values match. Pulled from
    each evaluation result's version-2 ``evidence`` block.
    """
    prod = production_result.get("evidence") or {}
    cand = candidate_result.get("evidence") or {}
    return {
        "production_spec_sha256": prod.get("evaluation_spec_sha256"),
        "candidate_spec_sha256": cand.get("evaluation_spec_sha256"),
        "production_checkpoint_sha256": prod.get("checkpoint_sha256"),
        "candidate_checkpoint_sha256": cand.get("checkpoint_sha256"),
    }


def create_session(reviews_dir, adapter_id, candidate_id, fingerprint, pairs,
                   audio_paths_by_role, build=None, automated_recommended=None,
                   blind=True):
    """Persist a pending blind session and return an identity-free skeleton.

    ``pairs`` is the validated probe list ``[{id, text, seed}, ...]``.
    ``audio_paths_by_role`` is ``{"production": {probe_id: abs_path}, "candidate":
    {probe_id: abs_path}}`` — the on-disk audio for each side. It is stored
    server-side only and reached via :func:`get_session_audio_path`, so the
    client never receives a path or URL that would disclose which side is which
    (a candidate's real URL contains ``/candidates/`` and would leak identity).

    The returned ``pairs`` carry only ``id``/``text``; the router attaches
    identity-neutral proxy URLs (``.../audio/A/<probe_id>``).
    """
    os.makedirs(_sessions_dir(reviews_dir), exist_ok=True)
    prune_sessions(reviews_dir)
    session_id = get_unique_id("review")

    roles = ["production", "candidate"]
    if blind:
        random.SystemRandom().shuffle(roles)
    labels = {"A": roles[0], "B": roles[1]}

    audio = {label: dict((audio_paths_by_role.get(role) or {}))
             for label, role in labels.items()}

    session = {
        "session_id": session_id,
        "adapter_id": adapter_id,
        "candidate_id": candidate_id,
        "created_at": _utc_now(),
        "blind": blind,
        "labels": labels,  # server-only; never returned to the client
        "audio": audio,    # server-only label->probe->path map for the proxy
        "fingerprint": fingerprint,
        "build": build or {},
        "automated": {"recommended_candidate": automated_recommended},
    }
    atomic_json_write(session, _session_path(reviews_dir, session_id))

    # Client-safe: only probe id + text. No labels, roles, paths, or fingerprint.
    client_pairs = [{"id": pair.get("id"), "text": pair.get("text", "")}
                    for pair in pairs]
    return {"session_id": session_id, "blind": blind, "pairs": client_pairs}


def get_session_audio_path(reviews_dir, session_id, label, probe_id, adapter_id=None):
    """Resolve a blind label + probe id to its real audio path, or raise.

    The router validates the returned path is inside the models dir before
    streaming it.
    """
    session = safe_load_json(_session_path(reviews_dir, session_id), default=None)
    if _is_session_expired(session):
        raise ReviewError("Review session is unknown or has expired")
    if adapter_id is not None and session.get("adapter_id") != adapter_id:
        raise ReviewError("Review session does not belong to this adapter")
    path = ((session.get("audio") or {}).get(label) or {}).get(probe_id)
    if not path:
        raise ReviewError("Unknown review audio")
    return path


def _clean_rating(rating):
    if rating is None:
        return None
    try:
        value = int(rating)
    except (TypeError, ValueError):
        raise ReviewError("Rating must be an integer 1-5 or omitted")
    if not MIN_RATING <= value <= MAX_RATING:
        raise ReviewError("Rating must be between 1 and 5")
    return value


def submit(reviews_dir, adapter_id, session_id, choice, current_fingerprint,
           rating=None, notes=""):
    """Resolve a blind choice into a persisted, evidence-bound review record.

    Rejects unknown/expired sessions and any evidence change since session open.
    Returns the revealed result (identities + which label was production), with
    the automated recommendation kept as a separate field from human preference.
    """
    if choice not in VALID_CHOICES:
        raise ReviewError(f"Choice must be one of {', '.join(VALID_CHOICES)}")
    rating = _clean_rating(rating)
    note_text = ("" if notes is None else str(notes))[:MAX_NOTE_CHARS]

    session_path = _session_path(reviews_dir, session_id)
    os.makedirs(_sessions_dir(reviews_dir), exist_ok=True)  # file_lock needs the parent dir
    store_path = _store_path(reviews_dir, adapter_id)
    # Every public store operation recovers before it can trim or delete history.
    # Keep session -> store ordering; recovery itself never acquires session locks.
    with file_lock(session_path):
        with file_lock(store_path):
            store = _apply_pending_decisions(reviews_dir, adapter_id)
            session = safe_load_json(session_path, default=None)
            if _is_session_expired(session):
                raise ReviewError("Review session is unknown or has expired")
            if session.get("adapter_id") != adapter_id:
                raise ReviewError("Review session does not belong to this adapter")
            if session.get("session_id") != session_id:
                raise ReviewError("Review session identity is invalid; reopen the review")
            if session.get("fingerprint") != current_fingerprint:
                # Evidence changed (retrained/promoted/rolled back) since the session
                # opened — the human listened to audio that no longer represents state.
                raise ReviewError("Evaluation evidence changed since the review opened; "
                                  "reopen the review")

            labels = session.get("labels") or {}
            choice_role = "tie" if choice == "tie" else labels.get(choice)
            if choice_role not in ("production", "candidate", "tie"):
                raise ReviewError("Review session labels are invalid; reopen the review")

            record = {
                "id": "hr_" + session_id,
                "created_at": _utc_now(),
                "adapter_id": adapter_id,
                "candidate_id": session.get("candidate_id"),
                "blind": bool(session.get("blind")),
                "evidence": current_fingerprint,
                "build": session.get("build") or {},
                "automated": session.get("automated") or {},
                "human": {"choice_role": choice_role, "rating": rating, "notes": note_text},
            }
            journal_path = _decision_path(reviews_dir, session_id)
            os.makedirs(os.path.dirname(journal_path), exist_ok=True)
            try:
                atomic_json_write({"version": STORE_VERSION, "session": session,
                                   "record": record}, journal_path)
                _sync_directory(journal_path)
                _sync_directory(os.path.dirname(journal_path))
            except OSError as error:
                raise ReviewError("Could not prepare review decision; session remains retryable") from error
            try:
                os.unlink(session_path)
            except OSError as error:
                _remove_decision_journal(journal_path)
                raise ReviewError("Could not consume review session; no decision was recorded") from error
            try:
                _publish_consumed_decision(store, record, session_path, store_path, journal_path)
            except OSError as error:
                raise ReviewError("Review decision is pending recovery") from error

    return {
        "recorded": True,
        "review_id": record["id"],
        "revealed": {"labels": labels, "choice_role": choice_role},
        "human": record["human"],
        "automated": record["automated"],  # kept separate from human preference
    }


def list_reviews(reviews_dir, adapter_id):
    """Return this adapter's review history, newest first (bounded by MAX_REVIEWS)."""
    path = _store_path(reviews_dir, adapter_id)
    if not os.path.isdir(reviews_dir):
        return []
    with file_lock(path):
        store = _apply_pending_decisions(reviews_dir, adapter_id)
        if not store or not isinstance(store.get("reviews"), list):
            return []
        return list(reversed(store["reviews"]))


def summarize(reviews_dir, adapter_id):
    """Compact human-review tally for the models list (never the full records).

    Kept separate from the automated recommendation by the caller — this only
    reports what humans preferred.
    """
    return get_review_summary(list_reviews(reviews_dir, adapter_id))


def get_review_summary(reviews):
    """Summarize an already-loaded history with the existing human-choice policy."""
    tally = {"production": 0, "candidate": 0, "tie": 0}
    for review in reviews:
        role = (review.get("human") or {}).get("choice_role")
        if role in tally:
            tally[role] += 1
    return {
        "count": len(reviews),
        "preferred_production": tally["production"],
        "preferred_candidate": tally["candidate"],
        "tie": tally["tie"],
        "latest_at": reviews[0].get("created_at") if reviews else None,
    }


def cleanup(reviews_dir, adapter_id):
    """Delete this adapter's review history, reporting count and bytes freed."""
    if not os.path.isdir(reviews_dir):
        return {"removed_count": 0, "freed_bytes": 0}
    path = _store_path(reviews_dir, adapter_id)
    with file_lock(path):
        store = _apply_pending_decisions(reviews_dir, adapter_id)
        removed = len(store.get("reviews", [])) if isinstance(store, dict) else 0
        freed = 0
        try:
            freed = os.path.getsize(path)
            os.unlink(path)
        except OSError:
            freed = 0
    return {"removed_count": removed, "freed_bytes": freed}


def prune_sessions(reviews_dir, max_age_seconds=SESSION_MAX_AGE_SECONDS):
    """Delete abandoned pending sessions older than the age budget."""
    sessions = _sessions_dir(reviews_dir)
    if not os.path.isdir(sessions):
        return []
    now = datetime.datetime.now(datetime.timezone.utc)
    removed = []
    for name in os.listdir(sessions):
        if name.startswith(".") or not name.endswith(".json"):
            continue
        session_id = name[:-5]
        path = _session_path(reviews_dir, session_id)
        with file_lock(path):
            record = safe_load_json(path, default=None)
            if not os.path.exists(path) or not _is_session_expired(record, max_age_seconds, now):
                continue
            journal_path = _decision_path(reviews_dir, session_id)
            adapter_id = record.get("adapter_id") if isinstance(record, dict) else None
            lock = file_lock(_store_path(reviews_dir, adapter_id)) if adapter_id else nullcontext()
            with lock:
                journal = _get_decision_journal(journal_path)
                if journal is not None and journal["session"] != record:
                    raise ReviewError("Pending review decision session identity changed")
                if journal is not None:
                    _remove_decision_journal(journal_path)
                try:
                    os.unlink(path)
                    _sync_directory(path)
                    removed.append(name)
                except OSError:
                    pass
    return removed
