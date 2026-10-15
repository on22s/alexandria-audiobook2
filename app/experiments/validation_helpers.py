"""Developer-run ownership evidence and bounded static POSIX file bindings.

These helpers do not dispatch a workload, retire a process, or qualify a run.
"""
import os
from pathlib import Path
import stat

from experiments.provenance import file_sha256


MAX_RUNTIME_FILES = 13
MAX_ALIASES = 1
IDENTITY_KEYS = {'device', 'inode', 'file_type'}
ALIAS_KEYS = {'alias', 'target', 'readlink', 'alias_identity',
              'target_identity', 'sha256'}


def start_registered_process(custody, processes, start_native, command, **kwargs):
    """Register before dispatch; retain the same holder until caller proves retirement.

    Both lists are intentionally shared live registries. The caller owns native
    cleanup and must retain unknown constructor outcomes, including a None handle.
    """
    pending = {'process': None, 'thread': None}
    custody.append(pending)
    process = start_native(command, **kwargs)
    pending['process'] = process
    processes.append(process)
    return process


def get_process_holder(custody, process):
    """Find one existing identity-bound holder without registering another one."""
    if process is None:
        raise ValueError('Unknown process cannot identify a custody holder')
    result = None
    for holder in custody:
        if holder['process'] is process:
            if result is not None:
                raise ValueError('Duplicate process custody holders')
            result = holder
    if result is None:
        raise ValueError('Process has no existing custody holder')
    return result


def is_retirement_evidence_valid(receipt, worker_count):
    """Check returned worker-then-server records, not operating-system retirement.

    The caller must separately verify source, clocks, caps, workload outcome and
    native ownership proofs. An integer server signal exit is legitimate here.
    """
    if (type(receipt) is not dict or type(worker_count) is not int
            or worker_count < 0 or receipt.get('custody_retained_PIDs') != []):
        return False
    owned = receipt.get('owned')
    dispatched = receipt.get('cleanup_dispatch_PIDs')
    expected = worker_count + 1
    if (type(owned) is not list or len(owned) != expected
            or any(type(record) is not dict
                   or type(record.get('pid')) is not int or record['pid'] <= 0
                   or type(record.get('returncode')) is not int
                   or record.get('native_retired') is not True for record in owned)
            or any(record['returncode'] != 0 for record in owned[:-1])
            or len({record['pid'] for record in owned}) != expected
            or type(dispatched) is not list or len(dispatched) != expected
            or any(type(pid) is not int or pid <= 0 for pid in dispatched)
            or len(set(dispatched)) != expected
            or set(dispatched) != {record['pid'] for record in owned}):
        return False
    return True


def get_identity(value):
    return {'device': value.st_dev, 'inode': value.st_ino,
            'file_type': stat.S_IFMT(value.st_mode)}


def is_digest(value):
    return (type(value) is str and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def is_path(value):
    return (type(value) is str and value.startswith('/') and '\x00' not in value
            and str(Path(value)) == value and os.path.normpath(value) == value)


def ensure_identity(value, kind):
    if (type(value) is not dict or set(value) != IDENTITY_KEYS
            or any(type(value[k]) is not int or value[k] < 0 for k in IDENTITY_KEYS)
            or value['inode'] <= 0 or value['file_type'] != kind):
        raise ValueError('Malformed runtime file identity')


def ensure_runtime_binding(runtime, files, ledger):
    """Validate literal runtime keys before hashing; inputs are never mutated."""
    if (type(runtime) is not dict or not 1 <= len(runtime) <= MAX_RUNTIME_FILES
            or type(files) is not dict or type(ledger) is not dict
            or set(ledger) != {'schema', 'aliases'} or type(ledger['schema']) is not int
            or ledger['schema'] != 1 or type(ledger['aliases']) is not list
            or len(ledger['aliases']) > MAX_ALIASES):
        raise ValueError('Malformed bounded runtime binding')
    for path, digest in runtime.items():
        if not is_path(path) or not is_digest(digest) or files.get(path) != digest:
            raise ValueError('Literal runtime key/hash is not closed by the frozen map')
    seen = set()
    for record in ledger['aliases']:
        if type(record) is not dict or set(record) != ALIAS_KEYS:
            raise ValueError('Malformed runtime alias record')
        alias, target, text = record['alias'], record['target'], record['readlink']
        if (not is_path(alias) or not is_path(target) or alias == target or alias in seen
                or alias not in runtime or type(text) is not str or not text
                or '\x00' in text or os.path.isabs(text)
                or os.path.normpath(str(Path(alias).parent / text)) != target
                or not is_digest(record['sha256']) or runtime[alias] != record['sha256']
                or files.get(target) != record['sha256']):
            raise ValueError('Runtime alias/target is not exactly bound')
        ensure_identity(record['alias_identity'], stat.S_IFLNK)
        ensure_identity(record['target_identity'], stat.S_IFREG)
        seen.add(alias)
    return True


def get_binding_snapshot(runtime, ledger):
    """Read declared POSIX paths; reject extra links, chains and replaced objects."""
    aliases = {record['alias']: record for record in ledger['aliases']}
    paths = sorted(set(runtime) | {record['target'] for record in ledger['aliases']})
    snapshot = {}
    for path in paths:
        value = Path(path).lstat()
        identity = get_identity(value)
        record = aliases.get(path)
        if record is not None:
            if identity != record['alias_identity'] or os.readlink(path) != record['readlink']:
                raise ValueError('Runtime alias identity/readlink changed')
            target = Path(record['target'])
            if str(Path(path).resolve(strict=True)) != record['target']:
                raise ValueError('Runtime alias resolves to a different target')
            if get_identity(target.lstat()) != record['target_identity']:
                raise ValueError('Runtime target identity/type changed')
        elif not stat.S_ISREG(value.st_mode):
            raise ValueError('Undeclared link or nonregular runtime input')
        snapshot[path] = identity
    for record in ledger['aliases']:
        if snapshot[record['target']] != record['target_identity']:
            raise ValueError('Runtime target is not the bound ordinary inode')
    return snapshot


def get_provenance_observation(runtime, files, ledger, get_sha=file_sha256,
                               runtime_only=False):
    """Hash once per literal key, requiring both alias and physical target content.

    Before/after identities are observations, not atomic snapshots or proof of
    dynamic-loader resolution. Nonruntime source-map hashing stays caller-scoped.
    """
    ensure_runtime_binding(runtime, files, ledger)
    if type(runtime_only) is not bool:
        raise ValueError('Unknown provenance observation scope')
    before = get_binding_snapshot(runtime, ledger)
    selected = sorted(before) if runtime_only else list(files)
    observed = {}
    for path in selected:
        digest = get_sha(path)
        if not is_digest(digest):
            raise ValueError('Unknown runtime/source content digest')
        observed[path] = digest
    after = get_binding_snapshot(runtime, ledger)
    if before != after:
        raise ValueError('Runtime identity changed during provenance observation')
    required = dict(runtime)
    required.update({record['target']: record['sha256'] for record in ledger['aliases']})
    return {'observed_sha256': observed,
            'runtime_unchanged': all(observed.get(p) == h for p, h in required.items()),
            'runtime_binding_observed': True}


def get_alias_ledger(runtime, alias):
    """Read one prospectively declared alias; never discover an allowlist."""
    if alias not in runtime or not is_path(alias):
        raise ValueError('Declared alias is not a literal runtime input')
    path = Path(alias)
    text = os.readlink(path)
    target = str(path.resolve(strict=True))
    ledger = {'schema': 1, 'aliases': [{'alias': alias, 'target': target, 'readlink': text,
        'alias_identity': get_identity(path.lstat()),
        'target_identity': get_identity(Path(target).lstat()), 'sha256': runtime[alias]}]}
    files = dict(runtime)
    files[target] = runtime[alias]
    ensure_runtime_binding(runtime, files, ledger)
    get_binding_snapshot(runtime, ledger)
    return ledger
