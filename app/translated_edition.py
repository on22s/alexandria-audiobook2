"""Opt-in local translation preview for a saved Script, as a reviewable proposal.

This module is text-only and read-only toward the book: it reads the saved
Script bytes, builds a translation proposal for a bounded excerpt through an
injected translator, and saves the proposal under the book's own proposals
folder. It never writes Script or source, never changes any language
setting, and never produces audio.

Argos Translate is an optional import, never a dependency. ``get_installed_pairs``
reports only directly installed packages (never the remote index, never a
download) and never pivots through intermediate languages: a pair is reported
only when its own package is installed, and the preview refuses any pair
without a direct package.

Excerpt bound: the first ``PROPOSAL_ROW_LIMIT`` (25) Script rows. A proposal
is never labelled complete when any row is unsupported, failed, skipped,
flagged for review, or cancelled. When every excerpt row translated but the
Script has more rows than the excerpt covers, the status is
``excerpt_complete`` and never ``complete``.

Book binding: the Script is read and the proposal saved under the book
operation lock (``ensure_book_state``), with the book operation token
rechecked before publication. A proposal whose saved ``book_id`` is not the
current book is refused on read.

Everything here is a proposal until separately reviewed and admitted; nothing
in this module approves or publishes a translation.
"""
import importlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from book_state_transaction import (ensure_book_state, get_book_operation_token,
                                    get_book_snapshot)
from utils import atomic_json_write

PROPOSAL_ROW_LIMIT = 25
PROPOSALS_DIR = "translation_proposals"
PROPOSAL_KIND = "translation_proposal"
PROPOSAL_LABEL = "proposal, not approved"

_CONTROL_PATTERN = re.compile(r"\[[^\]\n]{1,64}\]|<[^>\n]{1,80}>|\{[A-Za-z_][A-Za-z0-9_]*\}|\\[a-zA-Z]+")
_NUMBER_PATTERN = re.compile(r"\d[\d,.]*")
_PROPOSAL_ID_PATTERN = re.compile(r"^[0-9a-f]{32,64}$")
_LANGUAGE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")


def get_installed_pairs():
    """Report directly installed Argos language pairs, or unavailable.

    Pure read of the local Argos install directory via
    ``argostranslate.package.get_installed_packages``. Never touches the
    remote package index, never downloads, never pivots through another
    language, and never raises for a missing package: a missing import is
    reported as ``available`` False so the UI can say so plainly.
    """
    try:
        from argostranslate import package as argos_package
    except Exception as error:
        return {"available": False, "reason": "unavailable",
                "error": str(error)[:200], "pairs": [],
                "runtime_version": None}
    try:
        runtime_version = None
        try:
            from importlib.metadata import version as _metadata_version
            runtime_version = _metadata_version("argostranslate")
        except Exception:
            runtime_version = None
        packages = argos_package.get_installed_packages()
    except Exception as error:
        return {"available": False, "reason": "unavailable",
                "error": str(error)[:200], "pairs": [],
                "runtime_version": None}
    pairs = []
    for pkg in packages:
        if getattr(pkg, "type", None) != "translate":
            continue
        from_code = getattr(pkg, "from_code", None)
        to_code = getattr(pkg, "to_code", None)
        if not from_code or not to_code or from_code == to_code:
            continue
        pairs.append({"from_code": str(from_code), "to_code": str(to_code),
                      "from_name": str(getattr(pkg, "from_name", "") or ""),
                      "to_name": str(getattr(pkg, "to_name", "") or ""),
                      "package_version": str(getattr(pkg, "package_version", "") or "")})
    pairs.sort(key=lambda pair: (pair["from_code"], pair["to_code"]))
    return {"available": True, "reason": "ready", "error": None,
            "pairs": pairs, "runtime_version": runtime_version}


def is_pair_supported(pairs, from_code, to_code):
    """One answer to 'is this exact pair directly installed?' (Rule 15)."""
    if not isinstance(from_code, str) or not isinstance(to_code, str):
        return False
    if not _LANGUAGE_CODE_PATTERN.match(from_code) or not _LANGUAGE_CODE_PATTERN.match(to_code):
        return False
    return any(pair.get("from_code") == from_code and pair.get("to_code") == to_code
               for pair in pairs or [])


def build_argos_translator(from_code, to_code):
    """Return a local Argos text translator for one directly installed pair.

    Pure read plus local inference on demand. Raises ValueError, without
    downloading anything, when the package is unavailable or the pair has no
    direct package (pivoting is never used silently).
    """
    installed = get_installed_pairs()
    if not installed["available"]:
        raise ValueError("Translation package is unavailable")
    if not is_pair_supported(installed["pairs"], from_code, to_code):
        raise ValueError(f"Language pair is not installed: {from_code} -> {to_code}")
    try:
        argos_translate = importlib.import_module("argostranslate.translate")
    except Exception as error:
        # A partially installed runtime must read as unavailable, never as a
        # server error that hides why the preview could not start.
        raise ValueError("Translation runtime is unavailable") from error

    def translate(text):
        return argos_translate.translate(text, from_code, to_code)

    return translate


def validate_language_code(code, field):
    """Reject anything that is not a short language-code-shaped token."""
    if not isinstance(code, str) or not _LANGUAGE_CODE_PATTERN.match(code or ""):
        raise ValueError(f"Invalid {field}")
    return code


def get_proposal_package_version(pairs, from_code, to_code):
    """Find the installed package version for a pair, or None (Rule 15 read)."""
    for pair in pairs or []:
        if pair.get("from_code") == from_code and pair.get("to_code") == to_code:
            return pair.get("package_version") or None
    return None


def _get_script_rows(script_bytes):
    try:
        rows = json.loads(script_bytes.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as error:
        raise ValueError("Saved Script is not valid JSON") from error
    if not isinstance(rows, list) or not rows:
        raise ValueError("Saved Script has no rows")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise ValueError("Saved Script rows need inspection")
    return rows


def _get_numbers(text):
    return _NUMBER_PATTERN.findall(text)


def _check_translated_row(source_text, translated_text):
    """Known-bad controls: dropped/changed numbers, empty, copied source."""
    flags = []
    if not translated_text.strip():
        flags.append("empty_translation")
    if translated_text == source_text:
        flags.append("copied_source")
    if _get_numbers(source_text) != _get_numbers(translated_text):
        flags.append("number_changed")
    return flags


def build_proposal(data_dir, *, source_language, target_language, translate_fn,
                   installed_pairs, row_limit=PROPOSAL_ROW_LIMIT, cancel_check=None,
                   runtime_version=None):
    """Build and atomically save a translation proposal for a bounded excerpt.

    The caller injects the translator. Speaker labels are carried
    untranslated; rows with control tags or markup are flagged unsupported
    and carried over unchanged; changed numbers and other known-bad outputs
    are flagged for review. ``translate_fn`` failures become per-row failures,
    never a silent success. The book operation lock is held for the initial
    read and for publication, never during translation, and the book token is
    rechecked before the proposal is saved. Returns the saved proposal dict.
    """
    validate_language_code(source_language, "source_language")
    validate_language_code(target_language, "target_language")
    if not callable(translate_fn):
        raise ValueError("A translator is required")
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise ValueError("Row limit must be a positive integer")
    if is_pair_supported(installed_pairs, source_language, target_language) is False:
        raise ValueError(f"Language pair is not installed: {source_language} -> {target_language}")
    try:
        with ensure_book_state(data_dir) as root:
            snapshot = get_book_snapshot(root, include_voices=False)
            book_token = get_book_operation_token(root)
    except FileNotFoundError as error:
        raise ValueError("Saved Script is missing") from error
    except TimeoutError as error:
        raise ValueError("Book is busy; the preview was not started") from error
    script_bytes = snapshot["script_bytes"]
    if script_bytes is None:
        raise ValueError("Saved Script is missing")
    script_sha256 = snapshot["script_sha256"]

    rows = _get_script_rows(script_bytes)
    excerpt = rows[:row_limit]
    proposal_rows = []
    cancelled = False
    for index, row in enumerate(excerpt):
        if cancel_check is not None and cancel_check():
            cancelled = True
        speaker = row.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise ValueError("Saved Script speaker labels need inspection")
        source_text = row["text"]
        if cancelled:
            proposal_rows.append({"index": index, "speaker": speaker,
                                  "source_text": source_text, "translated_text": "",
                                  "status": "cancelled", "flags": [], "error": None})
            continue
        if not source_text.strip():
            proposal_rows.append({"index": index, "speaker": speaker,
                                  "source_text": source_text, "translated_text": "",
                                  "status": "skipped", "flags": [], "error": "empty source row"})
            continue
        if _CONTROL_PATTERN.search(source_text):
            proposal_rows.append({"index": index, "speaker": speaker,
                                  "source_text": source_text, "translated_text": source_text,
                                  "status": "unsupported", "flags": [],
                                  "error": "control tags or markup are not translated"})
            continue
        try:
            translated_text = translate_fn(source_text)
        except Exception as error:
            proposal_rows.append({"index": index, "speaker": speaker,
                                  "source_text": source_text, "translated_text": "",
                                  "status": "failed", "flags": [],
                                  "error": str(error)[:200]})
            continue
        if not isinstance(translated_text, str):
            proposal_rows.append({"index": index, "speaker": speaker,
                                  "source_text": source_text, "translated_text": "",
                                  "status": "failed", "flags": [],
                                  "error": "translator returned a non-text value"})
            continue
        flags = _check_translated_row(source_text, translated_text)
        proposal_rows.append({"index": index, "speaker": speaker,
                              "source_text": source_text, "translated_text": translated_text,
                              "status": "translated", "flags": flags, "error": None})
    counts = {"translated": 0, "unsupported": 0, "failed": 0,
              "skipped": 0, "cancelled": 0, "flagged": 0}
    for proposal_row in proposal_rows:
        counts[proposal_row["status"]] += 1
        if proposal_row["flags"]:
            counts["flagged"] += 1
    if cancelled:
        status = "cancelled"
    elif counts["translated"] != len(excerpt) or counts["flagged"]:
        status = "partial"
    elif counts["translated"] == len(rows):
        status = "complete"
    else:
        # Every row in the bounded excerpt was translated, but the excerpt is
        # a prefix: this is not a complete edition and must not say it is.
        status = "excerpt_complete"
    proposal = {"kind": PROPOSAL_KIND, "label": PROPOSAL_LABEL,
                "proposal_id": uuid.uuid4().hex, "status": status,
                "source_language": source_language, "target_language": target_language,
                "source_script_sha256": script_sha256, "book_id": snapshot["book_id"],
                "row_limit": row_limit,
                "excerpt": {"start": 0, "row_count": len(excerpt), "total_rows": len(rows)},
                "package_version": get_proposal_package_version(
                    installed_pairs, source_language, target_language),
                "runtime_version": runtime_version if isinstance(runtime_version, str) else None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "counts": counts, "rows": proposal_rows}
    try:
        with ensure_book_state(data_dir) as root:
            current = get_book_snapshot(root, include_voices=False)
            if (get_book_operation_token(root) != book_token
                    or current["identity"] != snapshot["identity"]
                    or current["script_sha256"] != script_sha256):
                raise ValueError("Book changed during preview; proposal was not saved")
            target_dir = Path(root) / PROPOSALS_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            atomic_json_write(proposal, str(target_dir / f"{proposal['proposal_id']}.json"),
                              allow_nonatomic_fallback=False)
    except TimeoutError as error:
        raise ValueError("Book is busy; the proposal was not saved") from error
    return proposal


def get_proposal(data_dir, proposal_id):
    """Read one saved proposal by id; never lists or guesses ids.

    The read holds the book operation lock and refuses a proposal whose
    ``book_id`` is not the current book, so a preview left in a reused
    workspace is never shown as if it belonged to this book.
    """
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID_PATTERN.match(proposal_id):
        raise ValueError("Invalid proposal id")
    try:
        with ensure_book_state(data_dir) as root:
            snapshot = get_book_snapshot(root, allow_missing_script=True,
                                         include_voices=False)
            path = Path(root) / PROPOSALS_DIR / f"{proposal_id}.json"
            if not path.is_file():
                raise ValueError("Translation proposal not found")
            try:
                proposal = json.loads(path.read_bytes().decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError("Saved translation proposal cannot be read") from error
            if not isinstance(proposal, dict) or proposal.get("kind") != PROPOSAL_KIND:
                raise ValueError("Saved translation proposal needs inspection")
            if proposal.get("book_id") != snapshot["book_id"]:
                raise ValueError("Translation proposal belongs to a different book")
    except FileNotFoundError as error:
        raise ValueError("Translation proposal not found") from error
    except TimeoutError as error:
        raise ValueError("Book is busy; the proposal was not read") from error
    return proposal


def _valid_sha(value):
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value)


def is_proposal_stale(data_dir, proposal):
    """True when the saved Script no longer matches the proposal's source."""
    if not isinstance(proposal, dict) or not _valid_sha(proposal.get("source_script_sha256")):
        return True
    return get_current_script_sha(data_dir) != proposal["source_script_sha256"]


def get_current_script_sha(data_dir):
    """The current saved Script SHA, or None when the Script is missing.

    Single dispatch for the stale check; ``is_proposal_stale`` compares
    against this value. Read under the book operation lock like every other
    book snapshot.
    """
    try:
        with ensure_book_state(data_dir) as root:
            snapshot = get_book_snapshot(root, include_voices=False)
    except (OSError, ValueError):
        return None
    sha256 = snapshot.get("script_sha256")
    return sha256 if _valid_sha(sha256) else None
