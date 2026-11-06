"""Book-bound pause previews and one atomic saved-line application."""
import copy
import hashlib

from book_state_transaction import ensure_book_state, get_book_snapshot, get_book_operation_token
from editor_poll_snapshot import get_snapshot_digest
from narration_markup import FORMAT, RECEIPT_KEY, get_narration_markup_preview
from project import SOURCE_FACT_FIELDS, get_chapter_text_digest, get_chunk_chapter_start, is_smart_chapter_text
from take_retention import admit_take_history_edit
from utils import file_lock


MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_APPLICATIONS = 8


def _get_file_digest(path):
    if not path:
        return None
    path = str(path)
    if path.startswith(("\\", "//")):
        raise ValueError("Use local saved inputs before previewing narration markup")
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("Cannot read a saved markup input; check file access and preview again") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Narration markup preview requires inputs of at most 8 MiB each")
    return hashlib.sha256(raw).hexdigest()


def _get_inputs(manager):
    try:
        snapshot = get_book_snapshot(manager.root_dir)
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read the saved book inputs; repair them before previewing markup") from exc
    return {"book_token": get_book_operation_token(manager.root_dir),
            "script": snapshot["script_sha256"], "voices": get_snapshot_digest(snapshot["voices"]),
            "source": _get_file_digest(snapshot["identity"].get("input_file_path")),
            "config": _get_file_digest(manager.config_path)}


def _get_target(chunks, index, expected_uid):
    if not 0 <= index < len(chunks) or chunks[index]["uid"] != expected_uid:
        raise ValueError("Saved line changed; reload Editor and preview again")
    return chunks[index]


def _get_preview(chunk, inputs):
    receipt = chunk.get(RECEIPT_KEY)
    if (isinstance(receipt, dict) and receipt.get("format") == FORMAT
            and receipt.get("spoken_text") == chunk["text"]):
        parsed = {"format": FORMAT, "original_text": chunk["text"],
                  "spoken_text": chunk["text"], "pause_after": None,
                  "spans": [], "changed": False}
    else:
        parsed = get_narration_markup_preview(chunk["text"])
    if parsed["changed"] and chunk.get("pronunciation_rules"):
        raise ValueError("This line has occurrence pronunciation decisions. Review and remove them explicitly before applying markup, then recreate them for the changed text")
    if parsed["changed"] and isinstance(receipt, dict):
        history = receipt.get("prior_applications", [])
        if not isinstance(history, list) or len(history) >= MAX_APPLICATIONS - 1:
            raise ValueError("This line's markup history is full or invalid. Preserve its receipts and use a new authored line for further markup")
    existing = chunk.get("pause_after")
    proposed = parsed["pause_after"]
    return {**parsed, "uid": chunk["uid"], "book_token": inputs["book_token"],
            "binding": get_snapshot_digest([inputs, chunk]),
            "existing_pause": existing,
            "conflict": proposed is not None and existing is not None and proposed != existing,
            "previous_application": copy.deepcopy(chunk.get(RECEIPT_KEY))}


def _get_result_digest(chunk):
    keys = ("uid", "text", "instruct", "speaker", "pause_after", "take", "status",
            "audio_path", "audio_revision", "source_entry_indices", "chapter", "chapter_title", "chapter_origin")
    return get_snapshot_digest({key: chunk.get(key) for key in keys})


def get_saved_narration_markup_preview(manager, index, expected_book_token, expected_uid):
    with ensure_book_state(manager.root_dir, recover=False):
        inputs = _get_inputs(manager)
        if inputs["book_token"] != expected_book_token:
            raise ValueError("Selected book changed; reload Editor before previewing markup")
        chunks = manager.get_saved_chunks()
        chunk = _get_target(chunks, index, expected_uid)
        preview = _get_preview(chunk, inputs)
        if _get_inputs(manager) != inputs:
            raise ValueError("Saved inputs changed; repeat the markup preview")
        return preview


def apply_saved_narration_markup(manager, index, expected_book_token, expected_uid,
                                binding, decision):
    if decision not in ("apply", "keep", "replace"):
        raise ValueError("Choose how to resolve the proposed pause")
    with ensure_book_state(manager.root_dir, recover=False), admit_take_history_edit(manager.root_dir):
        with manager._chunks_lock, file_lock(manager.chunks_path):
            inputs = _get_inputs(manager)
            if inputs["book_token"] != expected_book_token:
                raise ValueError("Selected book changed; repeat the markup preview")
            chunks = manager._get_saved_chunks_locked()
            chunk = _get_target(chunks, index, expected_uid)
            receipt = chunk.get(RECEIPT_KEY)
            if isinstance(receipt, dict) and receipt.get("binding") == binding:
                if (receipt.get("decision") == decision and receipt.get("inputs") == inputs
                        and receipt.get("result") == _get_result_digest(chunk)):
                    return {"status": "already_applied", "uid": expected_uid}
                raise ValueError("Applied line or inputs changed; reload Editor before continuing")
            preview = _get_preview(chunk, inputs)
            if binding != preview["binding"]:
                raise ValueError("Saved line or inputs changed; repeat the markup preview")
            if not preview["changed"]:
                raise ValueError("No directive or escaped opener to apply in this saved line")
            if preview["conflict"] and decision == "apply":
                raise ValueError("Existing pause conflicts: explicitly keep it or replace it")
            if not preview["conflict"] and decision != "apply":
                raise ValueError("Pause choices changed; repeat the markup preview")
            updated = copy.deepcopy(chunk)
            source_facts = {key: updated.pop(key) for key in SOURCE_FACT_FIELDS if key in updated}
            chapter_start = get_chunk_chapter_start(chunk)
            if chapter_start is None:
                chapter_start = chunk.get("spoken") is not True and is_smart_chapter_text(chunk["text"].strip())
            updated["text"] = preview["spoken_text"]
            if chapter_start is not None:
                updated["chapter_origin"] = {"is_heading": chapter_start,
                    "text_sha256": get_chapter_text_digest(updated["text"])}
                if "chapter_origin" in chunk:
                    source_facts["chapter_origin"] = copy.deepcopy(chunk["chapter_origin"])
            if preview["pause_after"] is not None and decision != "keep":
                updated["pause_after"] = preview["pause_after"]
            updated.update(status="pending", audio_path=None, error=None)
            for key in ("drift", "outlier", "speech_verification", "speech_retry_attempts", "word_timing"):
                if key in updated:
                    updated[key] = None
            result = _get_result_digest(updated)
            updated[RECEIPT_KEY] = {"format": preview["format"], "binding": binding,
                "decision": decision, "inputs": inputs, "result": result,
                "original_text": preview["original_text"], "spoken_text": preview["spoken_text"],
                "spans": preview["spans"], "pause_after": updated.get("pause_after")}
            updated[RECEIPT_KEY]["source_facts"] = source_facts
            if isinstance(receipt, dict):
                updated[RECEIPT_KEY]["prior_applications"] = copy.deepcopy([
                    *receipt.get("prior_applications", []),
                    {key: value for key, value in receipt.items() if key != "prior_applications"}])
            if _get_inputs(manager) != inputs:
                raise ValueError("Saved inputs changed; repeat the markup preview")
            chunks[index] = updated
            manager._save_chunks_locked(chunks, uids_backfilled=True)
            return {"status": "applied", "uid": expected_uid}
