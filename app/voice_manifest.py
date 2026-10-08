"""Strict, dependency-free reads for standalone Voice Lab manifest writers."""
import copy
import json
import os
import re


def get_voice_manifest(path):
    """Read a list of object rows without filtering or changing legacy fields."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("manifest must contain a JSON list")
    for index, row in enumerate(data, 1):
        if not isinstance(row, dict):
            raise ValueError(f"manifest row {index} must be a JSON object")
    return data


def validate_adapter_name(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or "/" in value or "\\" in value or "\0" in value or os.path.basename(value) != value):
        raise ValueError(f"unsafe adapter name: {value!r}")


def get_adapter_id_alias_map(manifest):
    """Resolve historical IDs directly to one current ID, without alias chains."""
    if not isinstance(manifest, list):
        raise ValueError('manifest must contain a JSON list')
    mapping = {}
    for row in manifest:
        if not isinstance(row, dict):
            raise ValueError('adapter manifest row must be an object')
        current = row.get('id')
        validate_adapter_name(current)
        aliases = row.get('previous_ids', [])
        if not isinstance(aliases, list):
            raise ValueError('previous_ids must be a list of adapter IDs')
        for name in [current, *aliases]:
            validate_adapter_name(name)
            key = os.path.normcase(name)
            if key in mapping:
                raise ValueError(f'duplicate adapter ID or alias: {name}')
            mapping[key] = current
    return mapping


def get_resolved_adapter_id(adapter_id, manifest):
    validate_adapter_name(adapter_id)
    return get_adapter_id_alias_map(manifest).get(os.path.normcase(adapter_id), adapter_id)


def get_adapter_id_map_locked(models_dir):
    """Read the single alias source; caller holds publication admission."""
    if os.path.lexists(os.path.join(models_dir, ADAPTER_ID_ALIASES_FILE)):
        return get_adapter_alias_registry(models_dir)
    manifest_path = os.path.join(models_dir, 'manifest.json')
    if os.path.isfile(manifest_path):
        return get_adapter_id_alias_map([row for row in get_voice_manifest(manifest_path) if 'id' in row])
    return {}


def get_resolved_adapter_path_locked(adapter_path):
    """Read the canonical path; caller holds root admission and fences publication."""
    root = os.path.dirname(os.path.abspath(adapter_path))
    name = os.path.basename(adapter_path)
    validate_adapter_name(name)
    resolved = get_adapter_id_map_locked(root).get(os.path.normcase(name), name)
    return os.path.join(root, resolved)


def get_adapter_identity_ids_locked(models_dir, adapter_id):
    """Return current and historical storage IDs for one voice without mutation."""
    validate_adapter_name(adapter_id)
    mapping = get_adapter_id_map_locked(models_dir)
    current = mapping.get(os.path.normcase(adapter_id), adapter_id)
    return [current, *sorted(old for old, target in mapping.items()
                            if os.path.normcase(target) == os.path.normcase(current)
                            and os.path.normcase(old) != os.path.normcase(current))]


def get_resolved_adapter_path(adapter_path):
    """Resolve persisted paths; serving must resolve again inside snapshot admission."""
    from utils import file_lock
    from adapter_publication import get_adapter_publication_recovery_command
    root = os.path.dirname(os.path.abspath(adapter_path))
    if not os.path.isdir(root):
        return adapter_path
    with file_lock(os.path.join(root, 'manifest.json')):
        pending = get_adapter_publication_recovery_command(root)
        if pending:
            raise ValueError(f'adapter publication recovery is required: run {pending}')
        return get_resolved_adapter_path_locked(adapter_path)


def get_adapter_identity_read_recovery_command(models_dir):
    """Only naming/promotion can change ID mapping; checkpoint status stays readable."""
    from adapter_publication import get_adapter_publication_owner, get_adapter_publication_recovery_command
    return (get_adapter_publication_recovery_command(models_dir)
            if get_adapter_publication_owner(models_dir) else None)


def get_resolved_adapter_manifest_rows_locked(models_dir, manifest):
    """Copy metadata onto canonical IDs; caller holds root admission."""
    mapping = get_adapter_id_map_locked(models_dir)
    rows = copy.deepcopy(manifest)
    for row in rows:
        if 'id' not in row:
            continue
        old = row['id']
        validate_adapter_name(old)
        current = mapping.get(os.path.normcase(old), old)
        history = row.get('previous_ids', [])
        if not isinstance(history, list):
            raise ValueError('previous_ids must be a list of adapter IDs')
        for previous in history:
            validate_adapter_name(previous)
            if os.path.normcase(mapping.get(os.path.normcase(previous), previous)) != os.path.normcase(current):
                raise ValueError('Conflicting historical adapter ID belongs to a different adapter')
        row['id'] = current
        aliases = {name for name, target in mapping.items()
                   if os.path.normcase(target) == os.path.normcase(current)
                   and os.path.normcase(name) != os.path.normcase(current)}
        if aliases:
            historical_keys = {os.path.normcase(name) for name in history}
            row['previous_ids'] = [*history, *sorted(
                name for name in aliases if os.path.normcase(name) not in historical_keys)]
    get_adapter_id_alias_map([row for row in rows if 'id' in row])
    return rows


def get_adapter_manifest_rows(models_dir, manifest_path, loader):
    from adapter_naming_transaction import lock_adapter_naming
    with lock_adapter_naming(models_dir, manifest_path):
        pending = get_adapter_identity_read_recovery_command(models_dir)
        if pending:
            raise ValueError(f'adapter publication recovery is required: run {pending}')
        return get_resolved_adapter_manifest_rows_locked(models_dir, loader(manifest_path))


def get_resolved_adapter_id_mapping(models_dir, adapter_ids):
    """Read one admitted alias map for persisted identity collections."""
    from utils import file_lock
    adapter_ids = list(adapter_ids)
    for name in adapter_ids:
        validate_adapter_name(name)
    if not os.path.isdir(models_dir):
        return {name: name for name in adapter_ids}
    with file_lock(os.path.join(models_dir, 'manifest.json')):
        pending = get_adapter_identity_read_recovery_command(models_dir)
        if pending:
            raise ValueError(f'adapter publication recovery is required: run {pending}')
        mapping = get_adapter_id_map_locked(models_dir)
        return {name: mapping.get(os.path.normcase(name), name) for name in adapter_ids}


def get_resolved_adapter_ids(models_dir, adapter_ids):
    return set(get_resolved_adapter_id_mapping(models_dir, adapter_ids).values())


ADAPTER_ID_ALIASES_FILE = 'adapter_id_aliases.json'


def get_validated_adapter_alias_registry(data):
    if (not isinstance(data, dict) or type(data.get('version')) is not int
            or data['version'] != 1 or not isinstance(data.get('aliases'), dict)):
        raise ValueError('invalid adapter ID alias registry')
    groups = {}
    for old, current in data['aliases'].items():
        validate_adapter_name(old)
        validate_adapter_name(current)
        groups.setdefault(current, []).append(old)
    get_adapter_id_alias_map([{'id': current, 'previous_ids': aliases}
                             for current, aliases in groups.items()])
    return {os.path.normcase(old): current for old, current in data['aliases'].items()}


def get_adapter_alias_registry(models_dir):
    path = os.path.join(models_dir, ADAPTER_ID_ALIASES_FILE)
    if not os.path.lexists(path):
        return {}
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError('unsafe adapter ID alias registry')
    with open(path, encoding='utf-8') as handle:
        return get_validated_adapter_alias_registry(json.load(handle))


def get_reserved_adapter_ids(mapping):
    """Historical spellings and their targets both remain reserved identities."""
    return {os.path.normcase(name) for name in [*mapping, *mapping.values()]}


def get_updated_adapter_alias_registry(models_dir, previous, renames):
    aliases = get_adapter_alias_registry(models_dir)
    if not os.path.exists(os.path.join(models_dir, ADAPTER_ID_ALIASES_FILE)):
        for mapping in (get_adapter_id_map_locked(models_dir), get_adapter_id_alias_map(previous)):
            for old, current in mapping.items():
                if os.path.normcase(old) == os.path.normcase(current):
                    continue
                if old in aliases and os.path.normcase(aliases[old]) != os.path.normcase(current):
                    raise ValueError('Conflicting historical adapter identity across manifests')
                aliases[old] = current
    replacements = {os.path.normcase(old): new for old, new in renames}
    reserved = get_reserved_adapter_ids(aliases)
    for old, new in renames:
        if (os.path.normcase(old) in aliases or os.path.normcase(new) in aliases
                or (os.path.normcase(new) in reserved and os.path.normcase(new) != os.path.normcase(old))):
            raise ValueError('naming would reuse a historical adapter ID')
    updated = {old: replacements.get(os.path.normcase(current), current) for old, current in aliases.items()}
    updated.update({os.path.normcase(old): new for old, new in renames if old != new})
    get_validated_adapter_alias_registry({'version': 1, 'aliases': updated})
    return {'version': 1, 'aliases': updated}


def get_adapter_asset_snapshot(asset_path):
    """Capture old direct/nested adapter assets; ordinary voice files stay unchanged."""
    from pathlib import Path
    from adapter_checkpoint_transaction import ensure_adapter_checkpoint
    from adapter_publication import get_adapter_publication_owner
    absolute = Path(os.path.abspath(asset_path))
    family = next((parent for parent in absolute.parents
                   if os.path.lexists(parent / ADAPTER_ID_ALIASES_FILE)
                   or get_adapter_publication_owner(str(parent))), None)
    if family is None and len(absolute.parents) > 1:
        direct = absolute.parents[1]
        if (direct / 'manifest.json').is_file():
            family = direct
    if family is not None:
        relative = absolute.relative_to(family)
        if len(relative.parts) < 2:
            raise ValueError('Adapter reference must name an asset inside a bundle')
        with ensure_adapter_checkpoint(family / relative.parts[0]) as current:
            resolved = current.joinpath(*relative.parts[1:])
            if (os.path.commonpath([str(current), os.path.realpath(resolved)]) != str(current)
                    or resolved.is_symlink() or not resolved.is_file()):
                raise ValueError('Adapter reference must be a regular file inside its bundle')
            return str(resolved), resolved.read_bytes()
    with open(asset_path, 'rb') as handle:
        return asset_path, handle.read()


def validate_adapter_training_output(adapter_path):
    """Refuse historical output IDs before setup or saving; current retraining stays valid."""
    from utils import file_lock
    from adapter_publication import (get_adapter_publication_recovery_command,
                                     get_adapter_checkpoint_generation_journal)
    path = os.path.abspath(adapter_path)
    root, name = os.path.dirname(path), os.path.basename(path)
    validate_adapter_name(name)
    if not os.path.isdir(root):
        return
    with file_lock(os.path.join(root, 'manifest.json')):
        pending = get_adapter_publication_recovery_command(
            root, ignore_checkpoint_journal=get_adapter_checkpoint_generation_journal(path))
        if pending:
            raise ValueError(f'adapter publication recovery is required: run {pending}')
        aliases = {old: current for old, current in get_adapter_id_map_locked(root).items()
                   if os.path.normcase(old) != os.path.normcase(current)}
        key = os.path.normcase(name)
        if key in aliases or (key in get_reserved_adapter_ids(aliases) and not os.path.isdir(path)):
            raise ValueError('Training output would reuse a historical adapter identity; choose a new output ID')


def validate_adapter_registration_id_locked(models_dir, adapter_id, manifest):
    """New registrations cannot take any existing or historical identity."""
    validate_adapter_name(adapter_id)
    reserved = get_reserved_adapter_ids(get_adapter_id_map_locked(models_dir))
    reserved.update(get_reserved_adapter_ids(get_adapter_id_alias_map([row for row in manifest if 'id' in row])))
    if os.path.normcase(adapter_id) in reserved:
        raise ValueError('Adapter registration would reuse an existing or historical identity')


def is_adapter_named(entry):
    """Raw batch IDs optionally append their training timestamp to the dataset stem."""
    dataset_id = entry.get('dataset_id')
    if not dataset_id:
        return True
    adapter_id = entry.get('id')
    if adapter_id == dataset_id:
        return False
    return not (isinstance(adapter_id, str) and re.fullmatch(
        re.escape(str(dataset_id)) + r'_[0-9]{10}', adapter_id))
