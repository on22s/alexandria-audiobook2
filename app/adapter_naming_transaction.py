"""Durable, replayable adapter-ID renames using the shared manifest lock."""
import contextlib
import copy
import hashlib
import json
import os
import stat
import tempfile
import shutil

from adapter_publication import (
    NAMING_PUBLICATION_JOURNAL, is_cli_adapter_publication_pending,
    get_adapter_bundle_sha256, is_adapter_checkpoint_recovery_pending,
    get_adapter_checkpoint_generation_recovery_command,
    save_adapter_publication_bytes, sync_adapter_directory, sync_adapter_tree,
)
from utils import atomic_json_write, file_lock, is_path_inside


from voice_manifest import (validate_adapter_name as _validate_name,
                            get_adapter_id_alias_map, get_voice_manifest,
                            get_resolved_adapter_manifest_rows_locked,
                            get_updated_adapter_alias_registry, ADAPTER_ID_ALIASES_FILE)


def _get_child_path(models_dir, name):
    _validate_name(name)
    path = os.path.join(models_dir, name)
    root = os.path.normcase(os.path.realpath(models_dir))
    resolved = os.path.normcase(os.path.realpath(path))
    if resolved == root or not is_path_inside(resolved, root):
        raise ValueError(f"adapter path escapes models-dir: {name}")
    return path


def _get_change_path(models_dir, pair, key):
    path = _get_child_path(models_dir, pair[key])
    if 'rollback_backup' in pair:
        if pair.get('delete') is not True or pair['old'] != pair['new']:
            raise ValueError('rollback backup changes must be deletions')
        if os.path.islink(path):
            raise ValueError('unsafe rollback adapter path')
        path = _get_child_path(path, 'promotion_backups')
        if os.path.islink(path):
            raise ValueError('unsafe rollback backup parent')
        path = _get_child_path(path, pair['rollback_backup'])
        if os.path.islink(path):
            raise ValueError('unsafe rollback backup path')
    return path


def validate_naming_manifest(manifest):
    get_adapter_id_alias_map(manifest)


@contextlib.contextmanager
def lock_adapter_naming(models_dir, manifest_path):
    """Root admission remains shared even for an explicitly separate manifest."""
    with contextlib.ExitStack() as locks:
        root_manifest = os.path.join(models_dir, "manifest.json")
        locks.enter_context(file_lock(root_manifest))
        if os.path.normcase(os.path.abspath(manifest_path)) != os.path.normcase(os.path.abspath(root_manifest)):
            locks.enter_context(file_lock(manifest_path))
        yield


def _require_promotion_recovered(models_dir):
    checkpoint_recovery = get_adapter_checkpoint_generation_recovery_command(models_dir)
    if checkpoint_recovery:
        raise ValueError('checkpoint recovery is required: run ' + checkpoint_recovery)
    if is_cli_adapter_publication_pending(models_dir):
        raise ValueError("promotion recovery is required: run promote_adapters.py --recover")


def _move_bundle(source, target, digest):
    if os.path.islink(source) or not os.path.isdir(source):
        raise ValueError(f"adapter directory is missing or unsafe: {source}")
    if get_adapter_bundle_sha256(source) != digest:
        raise ValueError(f"adapter bundle changed or is corrupt: {source}")
    if os.path.lexists(target):
        raise ValueError(f"adapter target is occupied: {target}")
    os.rename(source, target)
    sync_adapter_directory(os.path.dirname(source))
    sync_adapter_directory(os.path.dirname(target))


def _get_snapshot(workspace, journal, key):
    record = journal.get(key)
    if not isinstance(record, dict) or not isinstance(record.get("present"), bool):
        raise ValueError("invalid naming metadata snapshot")
    filename = os.path.join(workspace, key + ".before")
    if os.path.islink(filename):
        raise ValueError("unsafe naming metadata snapshot")
    with open(filename, "rb") as handle:
        data = handle.read()
    if hashlib.sha256(data).hexdigest() != record.get("sha256"):
        raise ValueError("corrupt naming metadata snapshot")
    if record["present"] and (type(record.get("mode")) is not int or not 0 <= record["mode"] <= 0o7777):
        raise ValueError("invalid naming snapshot permissions")
    return data


def _restore_snapshot(target, data, record):
    if record["present"]:
        save_adapter_publication_bytes(target, data, mode=record["mode"])
    elif os.path.lexists(target):
        if os.path.islink(target) or not os.path.isfile(target):
            raise ValueError("unsafe naming metadata target")
        os.remove(target)
        sync_adapter_directory(os.path.dirname(target))


def recover_adapter_naming_locked(models_dir, manifest_path):
    """Undo pending renames; terminal journals resume cleanup only. Caller locks."""
    _require_promotion_recovered(models_dir)
    path = os.path.join(models_dir, NAMING_PUBLICATION_JOURNAL)
    if not os.path.lexists(path):
        return False
    with open(path, encoding="utf-8") as handle:
        journal = json.load(handle)
    if (not isinstance(journal, dict) or journal.get("version") not in (1, 2)
            or journal.get("manifest_path") != os.path.abspath(manifest_path)
            or journal.get("phase") not in ("collect", "publish", "restore", "committed", "recovered")):
        raise ValueError("invalid naming transaction journal")
    if journal['version'] == 2 and 'aliases' not in journal:
        raise ValueError('naming alias recovery snapshot is missing')
    component = journal.get("workspace")
    _validate_name(component)
    if not component.startswith(".naming-"):
        raise ValueError("invalid naming transaction workspace")
    workspace = _get_child_path(models_dir, component)
    if os.path.islink(workspace):
        raise ValueError("unsafe naming workspace")
    pairs = journal.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("invalid naming transaction pairs")
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("invalid naming transaction pair")
        _validate_name(pair.get("old")); _validate_name(pair.get("new"))
        if not isinstance(pair.get("sha256"), str):
            raise ValueError("invalid naming bundle hash")
        _get_change_path(models_dir, pair, 'old')
        _get_change_path(models_dir, pair, 'new')
    for key in ("old", "new"):
        if len({os.path.normcase(pair[key]) for pair in pairs}) != len(pairs):
            raise ValueError("duplicate naming transaction path")
    history = manifest_path + ".bak." + component[1:]
    if journal["phase"] not in ("committed", "recovered"):
        bundle_dir = _get_child_path(workspace, "adapters")
        if not os.path.isdir(bundle_dir) or os.path.islink(bundle_dir):
            raise ValueError("missing naming recovery bundles")
        original_manifest = _get_snapshot(workspace, journal, "manifest")
        original_backup = _get_snapshot(workspace, journal, "backup")
        original_aliases = _get_snapshot(workspace, journal, 'aliases') if 'aliases' in journal else None
        alias_path = os.path.join(models_dir, ADAPTER_ID_ALIASES_FILE)
        if original_aliases is not None and os.path.lexists(alias_path) and (os.path.islink(alias_path) or not os.path.isfile(alias_path)):
            raise ValueError('unsafe naming alias metadata target')
        for target in (manifest_path, manifest_path + ".bak", history):
            if os.path.lexists(target) and (os.path.islink(target) or not os.path.isfile(target)):
                raise ValueError("unsafe naming metadata target")
        if os.path.lexists(history):
            with open(history, "rb") as handle:
                archived = handle.read()
            if not journal["backup"]["present"] or archived != original_backup:
                raise ValueError("naming backup archive ownership changed")
        # Validate every original before moving any. The durable phase makes
        # cycles unambiguous: publish gathers all bundles before restore begins.
        for index, pair in enumerate(pairs):
            staged = os.path.join(bundle_dir, str(index))
            fallback = _get_change_path(models_dir, pair, "new" if journal["phase"] == "publish" else "old")
            source = staged if os.path.isdir(staged) else fallback
            if os.path.islink(source) or not os.path.isdir(source) or get_adapter_bundle_sha256(source) != pair["sha256"]:
                raise ValueError("naming original bundle is missing or corrupt")
        if journal["phase"] == "publish":
            for index, pair in enumerate(pairs):
                staged = os.path.join(bundle_dir, str(index))
                if not os.path.exists(staged):
                    _move_bundle(_get_change_path(models_dir, pair, "new"), staged, pair["sha256"])
            journal["phase"] = "restore"
            atomic_json_write(journal, path)
        for index, pair in enumerate(pairs):
            staged = os.path.join(bundle_dir, str(index))
            if os.path.isdir(staged):
                _move_bundle(staged, _get_change_path(models_dir, pair, "old"), pair["sha256"])
        _restore_snapshot(manifest_path, original_manifest, journal["manifest"])
        _restore_snapshot(manifest_path + ".bak", original_backup, journal["backup"])
        if original_aliases is not None:
            _restore_snapshot(alias_path, original_aliases, journal['aliases'])
        if os.path.lexists(history):
            os.remove(history)
            sync_adapter_directory(os.path.dirname(history))
        journal["phase"] = "recovered"
        atomic_json_write(journal, path)
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    sync_adapter_directory(models_dir)
    os.remove(path)
    sync_adapter_directory(models_dir)
    print(f"Naming transaction {journal['phase']}.")
    return True


def apply_adapter_naming_locked(models_dir, manifest_path, new_manifest, renames):
    """All-or-nothing rename with saved metadata; caller holds root/manifest locks."""
    previous = get_resolved_adapter_manifest_rows_locked(
        models_dir, get_voice_manifest(manifest_path))
    get_adapter_id_alias_map(previous)
    by_id = {row['id']: row for row in previous}
    updated = copy.deepcopy(new_manifest)
    new_to_old = {new: old for old, new in renames}
    for row in updated:
        old = new_to_old.get(row['id'])
        if old is not None:
            if old not in by_id:
                raise ValueError(f'Cannot rename unknown adapter ID: {old}')
            history = by_id[old].get('previous_ids', [])
            row['previous_ids'] = [*history, old]
    get_adapter_id_alias_map(updated)
    alias_registry = get_updated_adapter_alias_registry(models_dir, previous, renames)
    return _apply_adapter_changes_locked(models_dir, manifest_path, updated,
        [{"old": old, "new": new} for old, new in renames], alias_registry=alias_registry)


def apply_adapter_deletion_locked(models_dir, manifest_path, new_manifest, adapter_id):
    """Keep the bundle recoverable until manifest removal is committed."""
    if any(row.get("id") == adapter_id for row in new_manifest):
        raise ValueError("deleted adapter is still present in the new manifest")
    return _apply_adapter_changes_locked(models_dir, manifest_path, new_manifest,
        [{"old": adapter_id, "new": adapter_id, "delete": True}])


def apply_rollback_backup_deletion_locked(models_dir, manifest_path, new_manifest, adapter_id, backup_id):
    """Stage only the backup; keep it recoverable until its pointer is durably cleared."""
    entry = next((row for row in new_manifest if row.get('id') == adapter_id), None)
    if entry is None or (entry.get('promotion') or {}).get('backup_id') is not None:
        raise ValueError('rollback backup pointer must be cleared in the new manifest')
    return _apply_adapter_changes_locked(models_dir, manifest_path, new_manifest,
        [{'old': adapter_id, 'new': adapter_id, 'delete': True, 'rollback_backup': backup_id}])


def _apply_adapter_changes_locked(models_dir, manifest_path, new_manifest, changes, alias_registry=None):
    _require_promotion_recovered(models_dir)
    journal_path = os.path.join(models_dir, NAMING_PUBLICATION_JOURNAL)
    if os.path.lexists(journal_path):
        raise ValueError("naming recovery is required first")
    pairs = [dict(change) for change in changes]
    old_paths = {os.path.normcase(_get_change_path(models_dir, pair, "old")) for pair in pairs}
    for pair in pairs:
        _validate_name(pair["old"]); _validate_name(pair["new"])
        source, target = (_get_change_path(models_dir, pair, key) for key in ("old", "new"))
        if not os.path.isdir(source) or os.path.islink(source):
            raise ValueError(f"adapter directory is missing or unsafe: {source}")
        if is_adapter_checkpoint_recovery_pending(source):
            raise ValueError("checkpoint recovery is required before naming")
        if os.path.lexists(target) and os.path.normcase(target) not in old_paths:
            raise ValueError(f"adapter target is occupied: {target}")
        pair["sha256"] = get_adapter_bundle_sha256(source)
    if len({os.path.normcase(p['old']) for p in pairs}) != len(pairs) or len({os.path.normcase(p['new']) for p in pairs}) != len(pairs):
        raise ValueError("duplicate naming rename paths")
    metadata_targets = [('manifest', manifest_path), ('backup', manifest_path + '.bak')]
    if alias_registry is not None:
        metadata_targets.append(('aliases', os.path.join(models_dir, ADAPTER_ID_ALIASES_FILE)))
    for _key, target in metadata_targets:
        if os.path.lexists(target) and (os.path.islink(target) or not os.path.isfile(target)):
            raise ValueError("unsafe naming metadata target")
    workspace = tempfile.mkdtemp(prefix=".naming-", dir=models_dir)
    try:
        os.mkdir(_get_child_path(workspace, "adapters"))
        journal = {"version": 2 if alias_registry is not None else 1, "phase": "collect", "workspace": os.path.basename(workspace),
                   "manifest_path": os.path.abspath(manifest_path), "pairs": pairs}
        snapshots = {}
        for key, target in metadata_targets:
            present = os.path.isfile(target)
            data = b""
            if present:
                with open(target, "rb") as handle:
                    data = handle.read()
            snapshots[key] = data
            save_adapter_publication_bytes(os.path.join(workspace, key + ".before"), data)
            journal[key] = {"present": present, "sha256": hashlib.sha256(data).hexdigest(),
                            "mode": stat.S_IMODE(os.stat(target).st_mode) if present else None}
        history = manifest_path + ".bak." + os.path.basename(workspace)[1:]
        if os.path.lexists(history):
            raise FileExistsError("naming backup archive already exists")
        for pair in pairs:
            sync_adapter_tree(_get_change_path(models_dir, pair, "old"))
        sync_adapter_directory(workspace)
        sync_adapter_directory(models_dir)
        atomic_json_write(journal, journal_path)
        try:
            if journal["backup"]["present"]:
                save_adapter_publication_bytes(history, snapshots["backup"], mode=journal["backup"]["mode"])
            save_adapter_publication_bytes(manifest_path + ".bak", snapshots["manifest"], mode=journal["manifest"]["mode"])
            for index, pair in enumerate(pairs):
                _move_bundle(_get_change_path(models_dir, pair, "old"), os.path.join(workspace, "adapters", str(index)), pair["sha256"])
            journal["phase"] = "publish"
            atomic_json_write(journal, journal_path)
            for index, pair in enumerate(pairs):
                if not pair.get("delete", False):
                    _move_bundle(os.path.join(workspace, "adapters", str(index)), _get_change_path(models_dir, pair, "new"), pair["sha256"])
            save_adapter_publication_bytes(manifest_path, json.dumps(new_manifest, indent=2, ensure_ascii=False).encode("utf-8"), mode=journal["manifest"]["mode"])
            if alias_registry is not None:
                alias_path = os.path.join(models_dir, ADAPTER_ID_ALIASES_FILE)
                mode = journal['aliases']['mode'] if journal['aliases']['present'] else None
                save_adapter_publication_bytes(alias_path, json.dumps(alias_registry, indent=2, ensure_ascii=False).encode('utf-8'), mode=mode)
            journal["phase"] = "committed"
            atomic_json_write(journal, journal_path)
        except BaseException:
            recover_adapter_naming_locked(models_dir, manifest_path)
            raise
        recover_adapter_naming_locked(models_dir, manifest_path)
    finally:
        if not os.path.lexists(journal_path) and os.path.isdir(workspace):
            shutil.rmtree(workspace)
