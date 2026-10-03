"""Pure reconciliation of pass-3 delivery retries with existing editor audio."""
import copy
import json
import uuid
from collections import defaultdict, deque

from project import CHUNK_GENERATION_FIELDS, group_into_chunks


BASIC_FIELDS = ("speaker", "text", "instruct", "pause_after")


def get_delivery_retry_chunks(before, after, chunks):
    """Preserve unchanged native groups; detach audio only for regrouped rows.

    Refuse an edited/reordered editor snapshot rather than replacing its work.
    Caller owns snapshot validation and atomic multi-file publication.
    """
    if (len(before) != len(after) or any(
            ({key: value for key, value in old.items()
              if key not in ("instruct", "instruct_unchecked")} !=
             {key: value for key, value in new.items()
              if key not in ("instruct", "instruct_unchecked")})
            or (old.get("instruct_unchecked") is not True and old != new)
            for old, new in zip(before, after))):
        raise ValueError("Delivery retry changed frozen or unaffected entries.")
    if chunks is None:
        return None
    old_groups = group_into_chunks(before, include_source_indices=True)
    new_groups = group_into_chunks(after, include_source_indices=True)
    if len(old_groups) != len(chunks) or any(
            any(group.get(field) != chunk.get(field) for field in BASIC_FIELDS)
            for group, chunk in zip(old_groups, chunks)):
        raise ValueError("Editor text or delivery changed; review those edits before retrying delivery.")
    if any(any(field in chunk for field in CHUNK_GENERATION_FIELDS
               if field not in BASIC_FIELDS) for chunk in chunks):
        raise ValueError("Editor voice/style overrides need manual delivery review; retry would replace them.")

    def key(group):
        return json.dumps({field: group.get(field) for field in
                           (*BASIC_FIELDS, "source_entry_indices")}, sort_keys=True)

    reusable = defaultdict(deque)
    for group, chunk in zip(old_groups, chunks):
        reusable[key(group)].append(chunk)
    result = []
    for index, group in enumerate(new_groups):
        candidates = reusable[key(group)]
        if candidates:
            row = copy.deepcopy(candidates.popleft())
        else:
            row = {field: copy.deepcopy(value) for field, value in group.items()
                   if field != "source_entry_indices"}
            row.update(uid=uuid.uuid4().hex, status="pending", audio_path=None)
        row["id"] = index
        result.append(row)
    return result


def ensure_delivery_review_snapshot(data_dir):
    """Read a current book and hydrated editor snapshot under native guards."""
    from book_state_transaction import ensure_book_state
    from project import ProjectManager
    from utils import file_lock
    manager = ProjectManager(data_dir)
    with ensure_book_state(data_dir), manager._chunks_lock, file_lock(manager.chunks_path):
        return _ensure_delivery_review_snapshot_locked(data_dir, manager)


def _ensure_delivery_review_snapshot_locked(data_dir, manager):
    import hashlib
    from pathlib import Path
    from book_state_transaction import get_book_snapshot
    from chunk_status_journal import get_chunk_status_journal_path
    from generation_checkpoint_shards import get_generation_checkpoint_artifacts
    from three_pass_generate import three_pass_checkpoint_path, three_pass_manifest_path
    root = Path(data_dir)
    snapshot = get_book_snapshot(data_dir)
    entries = json.loads(snapshot["script_bytes"])
    if not isinstance(entries, list) or any(not isinstance(row, dict) or
            not isinstance(row.get("text"), str) or not isinstance(row.get("speaker"), str)
            for row in entries):
        raise ValueError("Delivery review requires a valid annotated script.")
    chunks_path = Path(manager.chunks_path)
    journal_path = Path(get_chunk_status_journal_path(str(chunks_path)))
    chunks = copy.deepcopy(manager._read_chunks()) if chunks_path.exists() or journal_path.exists() else None
    checkpoint_path = three_pass_checkpoint_path(str(root / "annotated_script.json"))
    paths = [root / "annotated_script.json", chunks_path, journal_path,
             Path(three_pass_manifest_path(str(root / "annotated_script.json")))]
    paths.extend(Path(path) for path in get_generation_checkpoint_artifacts(checkpoint_path))
    artifacts = {}
    for path in paths:
        if path.is_dir():
            children = sorted(path.iterdir())
            if any(child.is_symlink() or not child.is_file() for child in children):
                raise ValueError("Invalid generation checkpoint shard contents.")
            value = json.dumps({child.name: hashlib.sha256(child.read_bytes()).hexdigest()
                                for child in children}, sort_keys=True).encode()
        else:
            value = path.read_bytes() if path.exists() else None
        artifacts[str(path.relative_to(root))] = value
    token_data = {"identity": snapshot["identity"], "artifacts": {
        name: hashlib.sha256(value).hexdigest() if value is not None else None
        for name, value in artifacts.items()}}
    token = hashlib.sha256(json.dumps(token_data, sort_keys=True).encode()).hexdigest()
    return {"token": token, "entries": entries, "chunks": chunks, "artifacts": artifacts}


def apply_delivery_retry(data_dir, snapshot, entries, cancel_check=None, retry_record=None):
    """Atomically publish a retry only into its exact original book/artifacts."""
    from pathlib import Path
    from book_state_transaction import ensure_book_state, apply_book_state_locked
    from chunk_status_journal import get_chunk_status_journal_path
    from generation_checkpoint_deltas import load_generation_delta_checkpoint
    from generation_checkpoint_shards import get_generation_checkpoint_artifacts
    from project import ProjectManager
    from three_pass_generate import (get_delivery_review_info, three_pass_checkpoint_path,
                                     three_pass_manifest_path)
    from utils import file_lock
    root = Path(data_dir)
    manager = ProjectManager(data_dir)
    with ensure_book_state(data_dir), manager._chunks_lock, file_lock(manager.chunks_path):
        current = _ensure_delivery_review_snapshot_locked(data_dir, manager)
        if current["token"] != snapshot["token"]:
            raise ValueError("Book or editor changed during delivery retry; results were not published.")
        chunks = get_delivery_retry_chunks(snapshot["entries"], entries, current["chunks"])
        values = {"annotated_script.json": entries}
        removals = []
        if chunks is not None:
            values[Path(manager.chunks_path).relative_to(root).as_posix()] = chunks
            journal = Path(get_chunk_status_journal_path(manager.chunks_path))
            if journal.exists():
                removals.append(journal.relative_to(root).as_posix())
        manifest_path = Path(three_pass_manifest_path(str(root / "annotated_script.json")))
        manifest = (json.loads(manifest_path.read_bytes()) if manifest_path.exists()
                    else {"status": "delivery_retry_only"})
        if not isinstance(manifest, dict) or not isinstance(manifest.get("delivery_retries", []), list):
            raise ValueError("Invalid generation manifest; delivery retry was not published.")
        manifest["delivery_review"] = get_delivery_review_info(entries)
        manifest["delivery_retries"] = manifest.get("delivery_retries", []) + [{
            "source_snapshot": snapshot["token"],
            "entries": get_delivery_review_info(snapshot["entries"])["entries"],
            "remaining": get_delivery_review_info(entries),
            **copy.deepcopy(retry_record or {}),
        }]
        values[manifest_path.relative_to(root).as_posix()] = manifest
        checkpoint_path = three_pass_checkpoint_path(str(root / "annotated_script.json"))
        if Path(checkpoint_path).exists():
            checkpoint = load_generation_delta_checkpoint(checkpoint_path)
            if isinstance(checkpoint, dict) and checkpoint.get("annotated") == snapshot["entries"]:
                checkpoint["annotated"] = entries
                values[Path(checkpoint_path).relative_to(root).as_posix()] = checkpoint
                removals.extend(Path(path).relative_to(root).as_posix()
                                for path in get_generation_checkpoint_artifacts(checkpoint_path)[1:])
        if cancel_check:
            cancel_check()
        replacements = {name: json.dumps(value, ensure_ascii=False, indent=2,
                                         allow_nan=False).encode("utf-8")
                        for name, value in values.items()}
        apply_book_state_locked(data_dir, replacements, list(dict.fromkeys(removals)))
    return get_delivery_review_info(entries)
