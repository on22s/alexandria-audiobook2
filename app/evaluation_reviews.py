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
import os
import random

from utils import atomic_json_write, file_lock, get_unique_id, safe_load_json, secure_filename

STORE_VERSION = 1
MAX_REVIEWS = 50
SESSION_MAX_AGE_SECONDS = 6 * 3600
MAX_NOTE_CHARS = 1000
VALID_CHOICES = ("A", "B", "tie")


class ReviewError(Exception):
    """Expected, user-correctable rejection (maps to HTTP 409 at the router)."""


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sessions_dir(reviews_dir):
    return os.path.join(reviews_dir, "_sessions")


def _store_path(reviews_dir, adapter_id):
    safe = secure_filename(adapter_id)
    if not safe or safe != adapter_id:
        raise ReviewError("Invalid adapter id")
    return os.path.join(reviews_dir, f"{safe}.json")


def _session_path(reviews_dir, session_id):
    safe = secure_filename(session_id)
    if not safe or safe != session_id:
        raise ReviewError("Invalid session id")
    return os.path.join(_sessions_dir(reviews_dir), f"{safe}.json")


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


def get_session_audio_path(reviews_dir, session_id, label, probe_id):
    """Resolve a blind label + probe id to its real audio path, or raise.

    The router validates the returned path is inside the models dir before
    streaming it.
    """
    session = safe_load_json(_session_path(reviews_dir, session_id), default=None)
    if not session:
        raise ReviewError("Review session is unknown or has expired")
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
    if not 1 <= value <= 5:
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
    session = safe_load_json(session_path, default=None)
    if not session:
        raise ReviewError("Review session is unknown or has expired")
    if session.get("adapter_id") != adapter_id:
        raise ReviewError("Review session does not belong to this adapter")
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
        "id": get_unique_id("hr"),
        "created_at": _utc_now(),
        "adapter_id": adapter_id,
        "candidate_id": session.get("candidate_id"),
        "blind": bool(session.get("blind")),
        "evidence": current_fingerprint,
        "build": session.get("build") or {},
        "automated": session.get("automated") or {},
        "human": {"choice_role": choice_role, "rating": rating, "notes": note_text},
    }

    store_path = _store_path(reviews_dir, adapter_id)
    os.makedirs(reviews_dir, exist_ok=True)
    with file_lock(store_path):
        store = safe_load_json(store_path, default=None) or {"version": STORE_VERSION, "reviews": []}
        reviews = store.get("reviews")
        if not isinstance(reviews, list):
            reviews = []
        reviews.append(record)
        # Bounded: keep the newest MAX_REVIEWS, drop the oldest. Append-only in
        # the sense that existing records are never edited, only aged out.
        store = {"version": STORE_VERSION, "reviews": reviews[-MAX_REVIEWS:]}
        atomic_json_write(store, store_path)

    try:
        os.unlink(session_path)
    except OSError:
        pass

    return {
        "recorded": True,
        "review_id": record["id"],
        "revealed": {"labels": labels, "choice_role": choice_role},
        "human": record["human"],
        "automated": record["automated"],  # kept separate from human preference
    }


def list_reviews(reviews_dir, adapter_id):
    """Return this adapter's review history, newest first (bounded by MAX_REVIEWS)."""
    store = safe_load_json(_store_path(reviews_dir, adapter_id), default=None)
    if not store or not isinstance(store.get("reviews"), list):
        return []
    return list(reversed(store["reviews"]))


def cleanup(reviews_dir, adapter_id):
    """Delete this adapter's review history, reporting count and bytes freed."""
    path = _store_path(reviews_dir, adapter_id)
    store = safe_load_json(path, default=None)
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
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=max_age_seconds)
    removed = []
    for name in os.listdir(sessions):
        if not name.endswith(".json"):
            continue
        record = safe_load_json(os.path.join(sessions, name), default=None) or {}
        try:
            created = datetime.datetime.fromisoformat(record.get("created_at", ""))
        except (TypeError, ValueError):
            created = None
        if created is None or created < cutoff:
            try:
                os.unlink(os.path.join(sessions, name))
                removed.append(name)
            except OSError:
                pass
    return removed
