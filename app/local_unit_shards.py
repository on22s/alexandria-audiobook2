"""Run CI's unit partitions in detached, byte-faithful local snapshots."""
import contextlib
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

from check_shard_reports import get_shard_report_errors
from diagnostics import get_redacted_credentials
from subprocess_ownership import (get_owned_exit_result, run_owned_capture,
                                 start_owned_subprocess, stop_owned_subprocess)
from unit_test_sharding import get_module_durations, get_test_shards
from utils import atomic_json_write, file_lock


LOCAL_SHARDS = 6
DISK_RESERVE_BYTES = 1024 ** 3
COUNT_FIELDS = ('tests_ran', 'tests_completed', 'failures', 'errors', 'skips',
                'expected_failures', 'unexpected_successes')
CPU_DEVICE_MASK = dict(CUDA_VISIBLE_DEVICES='', HIP_VISIBLE_DEVICES='', ROCR_VISIBLE_DEVICES='')
OWNER_TOKEN_FILE = 'alexandria-local-unit-owner'


def get_default_workers():
    return 2


def get_unit_test_dependency_errors():
    manifest = Path(__file__).with_name('requirements-test.txt')
    errors = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        requirement = line.partition('#')[0].strip()
        if not requirement:
            continue
        name, separator, expected = requirement.partition('==')
        if not separator or not name or not expected:
            raise ValueError(f'Unit test dependency must be version-pinned: {requirement}')
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = 'missing'
        if installed != expected:
            errors.append(f'{requirement} (installed: {installed})')
    return errors


def get_git_environment():
    removed = {'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
               'GIT_PREFIX', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
               'GIT_NAMESPACE'}
    if sys.platform == 'win32':
        from windows_subprocess_owner import _inherited_windows_environment
        environment = {key.upper(): value for key, value in _inherited_windows_environment().items()}
    else:
        environment = dict(os.environ)
    return {**{key: value for key, value in environment.items() if key not in removed},
            'GIT_OPTIONAL_LOCKS': '0'}


def get_common_directory(repo):
    return Path(os.fsdecode(run_git(
        repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).strip()).resolve()


def run_git(repo, *arguments, environment=None, _locked=False, include_stderr=False, stdin=None):
    arguments = list(map(str, arguments))
    command_index = 0
    while command_index < len(arguments) and arguments[command_index] == '-c':
        command_index += 2
    command = arguments[command_index] if command_index < len(arguments) else None
    lock = contextlib.nullcontext()
    if command == 'worktree' and not _locked:
        lock = file_lock(get_common_directory(repo) / 'alexandria-local-unit-git', timeout=120)
    with lock:
        result = run_owned_capture(
            ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
             '-c', 'core.longpaths=true', *arguments],
            cwd=repo, env=environment or get_git_environment(), timeout=120, stdin=stdin)
    if result.returncode:
        detail = get_redacted_credentials(os.fsdecode(result.stderr)).strip()[-2000:]
        raise RuntimeError(f'git {arguments[0]} failed: {detail}')
    return result.stdout + result.stderr if include_stderr else result.stdout


def get_source_paths(repo):
    return sorted({os.fsdecode(name) for name in run_git(
        repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
                   if name})


def capture_source_files(repo, names, algorithm, *, overlay=None, entries=None):
    manifest = {}
    for name in names:
        path = repo / name
        try:
            before = path.lstat()
        except FileNotFoundError:
            continue
        version = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                info.st_mtime_ns, info.st_ctime_ns)
        if stat.S_ISLNK(before.st_mode):
            content = os.fsencode(os.readlink(path))
            digest = hashlib.new(algorithm, f'blob {len(content)}\0'.encode() + content).hexdigest()
            kind, size = 'symlink', len(content)
        elif stat.S_ISREG(before.st_mode):
            if not path.resolve().is_relative_to(repo):
                raise ValueError(f'Snapshot source escapes the checkout: {name}')
            kind, size = 'file', before.st_size
            hasher = hashlib.new(algorithm, f'blob {size}\0'.encode())
            target = None
            try:
                if overlay is not None and entries[name][1] != entries[name][2]:
                    destination = overlay / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    target = destination.open('wb')
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        hasher.update(block)
                        if target is not None:
                            target.write(block)
                digest = hasher.hexdigest()
            finally:
                if target is not None:
                    target.close()
            if overlay is not None and (overlay / name).is_file():
                shutil.copystat(path, overlay / name)
        else:
            raise ValueError(f'Snapshot does not support submodules or special files: {name}')
        if version(before) != version(path.lstat()):
            raise ValueError(f'Snapshot source changed during capture: {name}')
        manifest[name] = dict(kind=kind, size=size, digest=digest,
                              mode=stat.S_IMODE(before.st_mode), version=version(before))
    return manifest


def create_working_tree_snapshot(repo, directory):
    repo, directory = Path(repo).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if run_git(repo, 'ls-files', '--unmerged', '-z'):
        raise ValueError('Cannot snapshot an index with unresolved conflicts')
    head = run_git(repo, 'rev-parse', 'HEAD').strip().decode('ascii')
    index_path = Path(os.fsdecode(run_git(repo, 'rev-parse', '--path-format=absolute',
                                         '--git-path', 'index')).strip())
    index_bytes = index_path.read_bytes()
    algorithm = run_git(repo, 'rev-parse', '--show-object-format').strip().decode('ascii')
    names = get_source_paths(repo)
    before = capture_source_files(repo, names, algorithm)
    alternate = directory / 'index'
    environment = {**get_git_environment(), 'GIT_INDEX_FILE': str(alternate)}
    entries_path = directory / 'index-entries'
    entries_path.write_bytes(run_git(repo, 'ls-files', '--stage', '-z'))
    run_git(repo, 'read-tree', '--empty', environment=environment)
    with entries_path.open('rb') as stream:
        run_git(repo, 'update-index', '-z', '--index-info', environment=environment, stdin=stream)
    run_git(repo, 'add', '-A', '--', '.', environment=environment)
    tree = run_git(repo, 'write-tree', environment=environment).strip().decode('ascii')
    entries = {}
    for record in run_git(repo, 'ls-files', '--stage', '-z', environment=environment).split(b'\0'):
        if not record:
            continue
        header, name = record.split(b'\t', 1)
        mode, identifier, stage = header.split()
        name = os.fsdecode(name)
        if mode == b'160000' or stage != b'0' or name not in before:
            raise ValueError('Snapshot does not support submodules, sparse or unmerged indexes')
        entries[name] = (mode.decode(), identifier.decode(), before[name]['digest'])
    if set(entries) != set(before):
        raise ValueError('Snapshot index does not include every current source file')
    overlay = directory / 'overlay'
    overlay.mkdir()
    after = capture_source_files(repo, names, algorithm, overlay=overlay, entries=entries)
    if (after != before or get_source_paths(repo) != names or index_path.read_bytes() != index_bytes
            or run_git(repo, 'rev-parse', 'HEAD').strip().decode('ascii') != head):
        raise ValueError('Working tree, index or HEAD changed while taking the unit snapshot; retry when stable')
    identity = dict(GIT_AUTHOR_NAME='Alexandria readiness snapshot',
                    GIT_AUTHOR_EMAIL='readiness@example.invalid',
                    GIT_COMMITTER_NAME='Alexandria readiness snapshot',
                    GIT_COMMITTER_EMAIL='readiness@example.invalid')
    commit = run_git(repo, '-c', 'commit.gpgsign=false', 'commit-tree', tree, '-p', head,
                     '-m', 'Temporary local readiness snapshot',
                     environment={**environment, **identity}).strip().decode('ascii')
    return dict(commit=commit, parent=head, algorithm=algorithm, files=before,
                overlay=str(overlay), checkout_bytes=sum(item['size'] for item in before.values()))


def get_artifact_root():
    return Path(tempfile.gettempdir()).resolve() / 'alexandria-local-units'


def get_directory_identity(path):
    validate_path_without_links(path)
    info = Path(path).stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'Ownership identity requires a directory: {path}')
    if sys.platform == 'win32':
        from windows_export_staging_directory import _api, _Handle
        api = _api()
        handle = _Handle(api, path)
        try:
            native = handle.information()
            if not native.attributes & 0x10 or native.attributes & 0x400:
                raise ValueError(f'Unsafe ownership directory: {path}')
            return dict(kind='windows-file-id', device=native.volume,
                        inode=(native.index_high << 32) | native.index_low,
                        created=(native.created.high << 32) | native.created.low)
        finally:
            handle.close()
    if hasattr(info, 'st_birthtime'):
        return dict(kind='posix-birthtime', device=info.st_dev, inode=info.st_ino,
                    created=getattr(info, 'st_birthtime_ns', int(info.st_birthtime * 10 ** 9)))
    if sys.platform.startswith('linux'):
        import ctypes
        import struct
        api = ctypes.CDLL(None, use_errno=True)
        operation = getattr(api, 'statx', None)
        if operation is not None:
            operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                  ctypes.c_uint, ctypes.c_void_p]
            operation.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(256)
            if operation(-100, os.fsencode(path), 0x100, 0xFFF, buffer) != 0:
                raise OSError(ctypes.get_errno(), 'Cannot read directory birth identity')
            mask = struct.unpack_from('=I', buffer, 0)[0]
            if mask & 0x800:
                seconds, nanoseconds = struct.unpack_from('=qI', buffer, 80)
                inode = struct.unpack_from('=Q', buffer, 32)[0]
                major, minor = struct.unpack_from('=II', buffer, 136)
                return dict(kind='posix-birthtime', device=os.makedev(major, minor),
                            inode=inode, created=seconds * 10 ** 9 + nanoseconds)
    return dict(kind='posix-ctime', device=info.st_dev, inode=info.st_ino, created=info.st_ctime_ns)


def save_artifact_ownership(artifacts, token):
    marker = artifacts / OWNER_TOKEN_FILE
    validate_path_without_links(marker)
    with marker.open('x', encoding='ascii') as stream:
        ownership = dict(token=token, directory_identity=get_directory_identity(artifacts))
        json.dump(ownership, stream)
    return ownership


def is_artifact_owner(artifacts, ownership, token):
    try:
        marker = artifacts / OWNER_TOKEN_FILE
        validate_path_without_links(marker)
        return (isinstance(ownership, dict) and ownership.get('token') == token
                and get_directory_identity(artifacts) == ownership.get('directory_identity')
                and json.loads(marker.read_text(encoding='ascii')) == ownership)
    except (OSError, ValueError):
        return False


def validate_path_without_links(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError(f'Unsafe local unit recovery paths: {path}')
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Unsafe local unit recovery paths: link or junction at {part}')


def get_registered_worktrees(repo, *, _locked=False):
    records = {}
    for block in run_git(repo, 'worktree', 'list', '--porcelain', '-z', _locked=_locked).split(bytes([0, 0])):
        fields = block.split(bytes([0]))
        if not fields[0].startswith(b'worktree '):
            continue
        path = Path(os.fsdecode(fields[0][9:]))
        records[path] = {os.fsdecode(key): os.fsdecode(value) for key, separator, value in
                         (field.partition(b' ') for field in fields[1:] if field)}
    return records


def validate_artifact_directory(repo, artifacts, *, _locked=False):
    artifacts, root = Path(artifacts), get_artifact_root()
    validate_path_without_links(root)
    validate_path_without_links(artifacts)
    common = get_common_directory(repo)
    if (not artifacts.is_relative_to(root) or artifacts == root
            or artifacts.is_relative_to(common) or common.is_relative_to(artifacts)
            or any(os.path.lexists(parent / '.git') for parent in (artifacts, *artifacts.parents)
                   if parent.is_relative_to(root))
            or any(artifacts.is_relative_to(path) for path in get_registered_worktrees(repo, _locked=_locked))):
        raise ValueError(f'Unsafe local unit recovery paths: artifacts must be below {root} and outside repositories')


def get_worktree_admin(repo, path, snapshot_commit, *, _locked=False):
    common = get_common_directory(repo)
    validate_path_without_links(path)
    registration = get_registered_worktrees(repo, _locked=_locked).get(path)
    if (registration is None or 'detached' not in registration
            or registration.get('HEAD') != snapshot_commit):
        raise ValueError(f'Worktree ownership mismatch: detached snapshot registration changed at {path}')
    gitfile = path / '.git'
    validate_path_without_links(gitfile)
    if not gitfile.is_file():
        raise ValueError(f'Worktree ownership mismatch: missing gitfile at {path}')
    content = gitfile.read_text(encoding='utf-8').strip()
    if not content.startswith('gitdir: '):
        raise ValueError(f'Worktree ownership mismatch: invalid gitfile at {path}')
    admin = Path(content[8:])
    if not admin.is_absolute():
        admin = path / admin
    validate_path_without_links(admin)
    validate_path_without_links(admin / 'gitdir')
    validate_path_without_links(admin / 'HEAD')
    if (admin.parent != common / 'worktrees'
            or Path((admin / 'gitdir').read_text(encoding='utf-8').strip()) != gitfile):
        raise ValueError(f'Worktree ownership mismatch: admin entry changed at {path}')
    return admin


def validate_worktree_ownership(repo, path, ownership, *, _locked=False):
    common = get_common_directory(repo)
    validate_path_without_links(path)
    if not isinstance(ownership, dict) or ownership.get('path') != str(path):
        raise ValueError(f'Worktree ownership mismatch: no recorded identity for {path}')
    name = ownership.get('admin_name')
    if name is not None and (not isinstance(name, str) or name in ('', '.', '..')
                             or '/' in name or chr(92) in name):
        raise ValueError(f'Worktree ownership mismatch: unsafe admin name for {path}')
    registered = get_registered_worktrees(repo, _locked=_locked)
    if path not in registered:
        if (os.path.lexists(path) or name is not None and os.path.lexists(common / 'worktrees' / name)):
            raise ValueError(f'Worktree ownership mismatch: unregistered path or reused admin at {path}; leaving intact')
        return False
    admin = get_worktree_admin(repo, path, ownership.get('snapshot_commit'), _locked=_locked)
    token_file = admin / OWNER_TOKEN_FILE
    validate_path_without_links(token_file)
    token = ownership.get('token')
    if (admin.name != name or not isinstance(token, str) or not token
            or not token_file.is_file() or token_file.read_text(encoding='ascii') != token):
        raise ValueError(f'Worktree ownership mismatch: admin name or token changed at {path}; leaving intact')
    if (get_directory_identity(admin) != ownership.get('admin_identity')
            or get_directory_identity(path) != ownership.get('worktree_identity')):
        raise ValueError(f'Worktree ownership mismatch: directory identity changed at {path}; leaving intact')
    return True


def add_snapshot_worktree(repo, snapshot, path, *, ownership=None, manifest=None):
    path = Path(path)
    with file_lock(get_common_directory(repo) / 'alexandria-local-unit-git', timeout=120):
        validate_path_without_links(path)
        if os.path.lexists(path) or path in get_registered_worktrees(repo, _locked=True):
            raise ValueError(f'Refusing occupied shard worktree path: {path}')
        if ownership is None:
            ownership = dict(path=str(path), admin_name=None, snapshot_commit=snapshot['commit'],
                             token=uuid.uuid4().hex + ':' + secrets.token_hex(32))
        if ownership.get('path') != str(path) or ownership.get('snapshot_commit') != snapshot['commit']:
            raise ValueError('Shard worktree creation identity does not match the snapshot')
        run_git(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', '--quiet', '--detach',
                '--no-checkout', str(path), snapshot['commit'], _locked=True)
        admin = get_worktree_admin(repo, path, snapshot['commit'], _locked=True)
        with (admin / OWNER_TOKEN_FILE).open('x', encoding='ascii') as stream:
            stream.write(ownership['token'])
        ownership['admin_name'] = admin.name
        ownership['admin_identity'] = get_directory_identity(admin)
        ownership['worktree_identity'] = get_directory_identity(path)
        if manifest is not None:
            record = json.loads(manifest.read_text(encoding='utf-8'))
            matches = [entry for entry in record['ownership'] if entry['path'] == str(path)]
            if len(matches) != 1 or matches[0]['token'] != ownership['token']:
                raise ValueError('Shard worktree manifest identity changed during creation')
            matches[0].update(ownership)
            atomic_json_write(record, str(manifest), allow_nonatomic_fallback=False)
        run_git(path, '-c', 'core.autocrlf=false', 'reset', '--hard', '--quiet', snapshot['commit'])
    for source in Path(snapshot['overlay']).rglob('*'):
        if source.is_file():
            shutil.copy2(source, path / source.relative_to(snapshot['overlay']))
    for name, expected in snapshot['files'].items():
        target = path / name
        if expected['kind'] == 'file' and stat.S_IMODE(target.stat().st_mode) != expected['mode']:
            target.chmod(expected['mode'])
    actual = capture_source_files(path.resolve(), list(snapshot['files']), snapshot['algorithm'])
    for name, expected in snapshot['files'].items():
        current = actual.get(name, {})
        if any(current.get(key) != expected[key] for key in ('kind', 'size', 'digest', 'mode')):
            raise ValueError(f'Git checkout/filter did not faithfully materialize snapshot file: {name}')
    # Checkout disabled automatic EOL conversion, then restored the source bytes.
    # Refresh this shard's index under its normal attributes/configuration so Git
    # does not report clean CRLF overlays as modified merely from cached sizes.
    run_git(path, 'add', '-A', '--', '.')
    if run_git(path, 'write-tree').strip() != run_git(repo, 'rev-parse', snapshot['commit'] + '^{tree}').strip():
        raise ValueError('Refreshing the snapshot index changed its captured tree')
    return ownership


def remove_snapshot_worktree(repo, path, owned_root, *, ownership=None, _locked=False):
    if not _locked:
        with file_lock(get_common_directory(repo) / 'alexandria-local-unit-git', timeout=120):
            return remove_snapshot_worktree(repo, path, owned_root, ownership=ownership, _locked=True)
    path, owned_root = Path(path), Path(owned_root).resolve()
    validate_path_without_links(path)
    if not path.is_relative_to(owned_root) or path == owned_root:
        raise ValueError('Refusing to remove a worktree outside the owned unit directory')
    for attempt in range(3):
        try:
            if validate_worktree_ownership(repo, path, ownership, _locked=True):
                run_git(repo, 'worktree', 'remove', '--force', str(path), _locked=True)
            return
        except (OSError, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(.1)


def get_process_start_time(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid local unit owner PID; refusing stale cleanup')
    if sys.platform == 'win32':
        import ctypes
        from windows_subprocess_job import get_windows_job_api
        api = get_windows_job_api()
        handle = api.OpenProcess(0x101000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            wait = api.WaitForSingleObject
            wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            wait.restype = ctypes.c_uint32
            status = wait(handle, 0)
            if status == 0:
                return None
            if status != 258:
                raise ctypes.WinError(ctypes.get_last_error())
            get_times = api.GetProcessTimes
            get_times.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(ctypes.c_uint64)] * 4)]
            get_times.restype = ctypes.c_int
            times = [ctypes.c_uint64() for counter in range(4)]
            if not get_times(handle, *(ctypes.byref(value) for value in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            return 'windows-filetime:' + str(times[0].value)
        finally:
            api.CloseHandle(handle)
    if sys.platform.startswith('linux'):
        try:
            fields = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
        except FileNotFoundError:
            return None
        return None if fields[0] == 'Z' else 'linux-starttime:' + fields[19]
    result = run_owned_capture(['ps', '-p', str(pid), '-o', 'lstart='], timeout=5)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode or not result.stdout.strip():
        raise OSError('Process creation time unavailable; refusing stale cleanup')
    return 'ps-lstart:' + os.fsdecode(result.stdout).strip()


def is_owner_alive(pid, start_time):
    if not isinstance(start_time, str) or not start_time:
        raise ValueError('Missing local unit owner creation time; refusing stale cleanup')
    try:
        actual = get_process_start_time(pid)
    except OSError:
        return True
    return actual is not None and actual == start_time


def register_run(repo, artifacts, expected_ids, count, *, snapshot_commit=None):
    common = get_common_directory(repo)
    with file_lock(common / 'alexandria-local-unit-git', timeout=120):
        validate_artifact_directory(repo, artifacts, _locked=True)
        start_time = get_process_start_time(os.getpid())
        if start_time is None:
            raise OSError('Cannot record coordinator process creation time')
        registry = common / 'alexandria-local-unit-runs'
        validate_path_without_links(registry)
        registry.mkdir(exist_ok=True)
        run_id = uuid.uuid4().hex
        token = run_id + ':' + secrets.token_hex(32)
        manifest = registry / (run_id + '.json')
        snapshot_commit = snapshot_commit or run_git(repo, 'rev-parse', 'HEAD').strip().decode('ascii')
        paths = [str(artifacts / 'worktrees' / f'shard-{number}') for number in range(1, count + 1)]
        artifact_ownership = save_artifact_ownership(artifacts, token)
        atomic_json_write(dict(schema_version=2, run_id=run_id, token=token,
                               owner_pid=os.getpid(), owner_start_time=start_time,
                               owner_platform=sys.platform, owner_host=socket.gethostname(),
                               common_directory=str(common), artifact_dir=str(artifacts),
                               artifact_ownership=artifact_ownership,
                               expected_ids=expected_ids, shard_count=count, snapshot_commit=snapshot_commit,
                               worktrees=paths, ownership=[dict(path=path, admin_name=None, token=token,
                                   snapshot_commit=snapshot_commit) for path in paths]),
                          str(manifest), allow_nonatomic_fallback=False)
    return manifest


def cleanup_stale_runs(repo):
    common = get_common_directory(repo)
    recovered = []
    with file_lock(common / 'alexandria-local-unit-git', timeout=120):
        registry = common / 'alexandria-local-unit-runs'
        validate_path_without_links(registry)
        for manifest in sorted(registry.glob('*.json')):
            validate_path_without_links(manifest)
            record = json.loads(manifest.read_text(encoding='utf-8'))
            if (record.get('schema_version') != 2 or record.get('common_directory') != str(common)
                    or record.get('run_id') != manifest.stem):
                raise ValueError(f'Invalid local unit recovery manifest: {manifest}')
            if (record['owner_platform'] != sys.platform
                    or record['owner_host'] != socket.gethostname()):
                continue
            artifacts = Path(record['artifact_dir'])
            validate_artifact_directory(repo, artifacts, _locked=True)
            root = artifacts / 'worktrees'
            validate_path_without_links(root)
            count = record['shard_count']
            if (type(count) is not int or count < 1 or not artifacts.is_absolute()
                    or artifacts.resolve() != artifacts or root.resolve() != root
                    or record['worktrees'] != [str(root / f'shard-{number}')
                                               for number in range(1, count + 1)]
                    or not isinstance(record.get('ownership'), list) or len(record['ownership']) != count
                    or not isinstance(record.get('token'), str)
                    or len(record['token']) != 97 or not record['token'].startswith(manifest.stem + ':')):
                raise ValueError(f'Unsafe local unit recovery paths: {manifest}')
            if is_owner_alive(record['owner_pid'], record.get('owner_start_time')):
                continue
            for path, ownership in zip(record['worktrees'], record['ownership']):
                if (not isinstance(ownership, dict) or ownership.get('path') != path
                        or ownership.get('token') != record['token']
                        or ownership.get('snapshot_commit') != record['snapshot_commit']):
                    raise ValueError(f'Unsafe local unit recovery paths: inconsistent identity in {manifest}')
                validate_worktree_ownership(repo, Path(path), ownership, _locked=True)
            for number in range(1, count + 1):
                validate_path_without_links(artifacts / f'shard-{number}')
            for path, ownership in zip(record['worktrees'], record['ownership']):
                remove_snapshot_worktree(repo, Path(path), root, ownership=ownership, _locked=True)
            original_artifacts = artifacts
            if not is_artifact_owner(artifacts, record.get('artifact_ownership'), record['token']):
                artifacts = Path(tempfile.mkdtemp(prefix='recovered-', dir=get_artifact_root()))
                validate_artifact_directory(repo, artifacts, _locked=True)
                save_artifact_ownership(artifacts, record['token'])
            reports = []
            for number, path in enumerate(record['worktrees'], 1):
                directory = artifacts / f'shard-{number}'
                directory.mkdir(parents=True, exist_ok=True)
                log_path = directory / 'output.log'
                if not log_path.exists():
                    log_path.write_text('Coordinator exited before capturing shard output.\n', encoding='utf-8')
                report = get_worker_report(dict(
                    receipt=directory / 'tests.json', directory=directory, spec=f'{number}/{count}',
                    worktree=Path(path), log_path=log_path, status='crashed',
                    process=SimpleNamespace(returncode=None)))
                reports.append(report)
            result = aggregate_unit_reports(reports, record['expected_ids'], count)
            result.update(status='failed', counts_complete=False, artifact_dir=str(artifacts),
                          original_artifact_dir=str(original_artifacts),
                          remaining_worktrees=[], recovered_owner_pid=record['owner_pid'])
            if artifacts != original_artifacts:
                result['problems'].append('Original artifact ownership not verified; recovery reports redirected')
            result['problems'].append('Coordinator exited without finalizing; recovered recorded worktrees')
            atomic_json_write(result, str(artifacts / 'aggregate.json'), allow_nonatomic_fallback=False)
            manifest.unlink()
            recovered.append(str(artifacts))
            print(f'Recovered stale local unit run: {artifacts}; partial counts retained in aggregate.json', flush=True)
    return recovered


def aggregate_unit_reports(reports, expected_ids, shard_count):
    totals = {key: 0 for key in COUNT_FIELDS}
    problems, envelopes, identifiers = [], [], []
    complete = len(reports) == shard_count
    for report in reports:
        valid = True
        if type(report.get('schema_version')) is not int or report['schema_version'] != 1:
            problems.append(f"{report.get('shard')}: unsupported shard report schema")
            valid = False
        for key in COUNT_FIELDS:
            value = report.get(key)
            if type(value) is not int or value < 0:
                problems.append(f"{report.get('shard')}: invalid {key}: {value!r}")
                valid = False
            else:
                totals[key] += value
        ids = report.get('test_ids')
        if isinstance(ids, list) and all(isinstance(identifier, str) and identifier for identifier in ids):
            identifiers.extend(ids)
        done = (report.get('complete') is True and valid and isinstance(ids, list)
                and all(isinstance(identifier, str) and identifier for identifier in ids)
                and len(ids) == report.get('tests_ran')
                and report.get('tests_completed') == report.get('tests_ran'))
        complete = complete and done
        if not done:
            problems.append(f"{report.get('shard')}: counts are incomplete")
        passed = (done and report.get('status') == 'passed'
                  and type(report.get('exit_code')) is int and report['exit_code'] == 0
                  and not any(report.get(key) for key in
                              ('failures', 'errors', 'skips', 'unexpected_successes')))
        if not passed:
            problems.append(f"{report.get('shard')}: shard did not pass ({report.get('status')})")
        envelopes.append(dict(shard=report.get('shard'), status='passed' if passed else 'failed',
                              gates=[dict(name='unit_tests', status='passed' if passed else 'failed',
                                          result=dict(tests_ran=report.get('tests_ran'), test_ids=ids))]))
    problems.extend(get_shard_report_errors(envelopes, shard_count, len(expected_ids), expected_ids))
    if not expected_ids or len(set(expected_ids)) != len(expected_ids):
        problems.append('Expected unit inventory must be nonempty and unique')
    complete = (complete and len(identifiers) == len(expected_ids)
                and set(identifiers) == set(expected_ids))
    return dict(schema_version=1, status='failed' if problems else 'passed',
                **totals, counts_complete=complete, test_ids=identifiers,
                problems=problems, shards=reports)


def get_worker_report(job):
    try:
        receipt = json.loads(job['receipt'].read_text(encoding='utf-8'))
        if not isinstance(receipt, dict):
            raise ValueError('Non-object unit receipt')
    except (OSError, ValueError):
        receipt = {}
    report = {key: receipt.get(key) for key in COUNT_FIELDS}
    report.update(schema_version=1, shard=job['spec'], test_ids=receipt.get('test_ids', []),
                  complete=receipt.get('complete') is True,
                  status=job.get('status') or (receipt.get('status', 'crashed')
                                               if receipt.get('complete') is True else 'crashed'),
                  exit_code=job.get('root_exit', job['process'].returncode),
                  log_path=str(job['log_path']), receipt_path=str(job['receipt']),
                  worktree=str(job['worktree']) if job['worktree'] else None,
                  active_test=receipt.get('active_test'),
                  failure_ids=receipt.get('failure_ids', []), error_ids=receipt.get('error_ids', []))
    report['owner_exit_code'] = job['process'].returncode
    if job['process'].returncode != 0 and report['exit_code'] == 0:
        report['status'] = 'crashed'
    if receipt.get('shard') != (job['spec'] if job['worktree'] else None):
        report.update(status='crashed', complete=False, receipt_error='shard marker missing or mismatched')
    if type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1:
        report.update(status='crashed', complete=False, receipt_error='unsupported receipt schema')
    atomic_json_write(report, str(job['directory'] / 'shard.json'), allow_nonatomic_fallback=False)
    return report


def run_local_unit_tests(repo, *, workers, shard_count=LOCAL_SHARDS, report_dir=None,
                         timeout=None, serial_on_low_disk=False, minimum_tests=1):
    if type(workers) is not int or workers < 0 or type(shard_count) is not int or shard_count < 1:
        raise ValueError('Worker count must be nonnegative and shard count positive')
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Unit shard timeout must be positive')
    if type(minimum_tests) is not int or minimum_tests < 1:
        raise ValueError('Unit discovery floor must be positive')
    repo = Path(repo).resolve()
    dependency_errors = get_unit_test_dependency_errors()
    if dependency_errors:
        raise RuntimeError('Unit test dependencies unavailable: ' + ', '.join(dependency_errors)
                           + f'. Install with: "{sys.executable}" -m pip install -r '
                           + '"' + str(Path(__file__).with_name('requirements-test.txt')) + '"')
    artifacts = Path(os.path.abspath(report_dir)) if report_dir else get_artifact_root() / uuid.uuid4().hex
    validate_artifact_directory(repo, artifacts)
    if artifacts.exists() and any(artifacts.iterdir()):
        raise ValueError('Unit artifact directory must be new or empty; refusing stale shard receipts')
    artifacts.mkdir(parents=True, exist_ok=True)
    worktree_root = artifacts / 'worktrees'
    worktree_root.mkdir()
    started, jobs, reports, problems, expected_ids = time.monotonic(), [], [], [], []
    active, worktrees, snapshot, interrupted, fallback = [], [], None, None, None
    manifest = None
    ownerships = {}
    count = shard_count if workers else 1
    workers = min(workers, count)
    print(f'Local unit artifacts: {artifacts}', flush=True)
    with tempfile.TemporaryDirectory(prefix='snapshot-', dir=artifacts) as temporary:
        try:
            cleanup_stale_runs(repo)
            if workers:
                names = get_source_paths(repo)
                source_bytes = sum((repo / name).lstat().st_size for name in names
                                   if os.path.lexists(repo / name))
                required = (2 * workers + 1) * source_bytes + DISK_RESERVE_BYTES
                free = shutil.disk_usage(artifacts).free
                object_directory = Path(os.fsdecode(run_git(
                    repo, 'rev-parse', '--path-format=absolute', '--git-path', 'objects')).strip())
                object_free = shutil.disk_usage(object_directory).free
                if free < required or object_free < source_bytes + DISK_RESERVE_BYTES:
                    message = (f'Insufficient disk for local unit snapshots: need {required} free bytes '
                               f'at {artifacts}, have {free}; source object-store volume has {object_free}. '
                               'Use --unit-workers 0 or explicitly --serial-on-low-disk.')
                    if not serial_on_low_disk:
                        raise OSError(message)
                    workers, count, fallback = 0, 1, message
                    print(message + ' Falling back to serial as requested.', flush=True)
                else:
                    snapshot = create_working_tree_snapshot(repo, Path(temporary))
                    atomic_json_write(snapshot, str(artifacts / 'snapshot.json'), allow_nonatomic_fallback=False)
            if snapshot is None:
                inventory = json.loads((repo / 'app/tests/unit_test_inventory.json').read_text(encoding='utf-8'))
                durations = get_module_durations(repo / 'app/tests/module_durations.json')
            else:
                def frozen_file(name):
                    overlay = Path(snapshot['overlay']) / name
                    return overlay.read_bytes() if overlay.exists() else run_git(repo, 'show', snapshot['commit'] + ':' + name)
                inventory = json.loads(frozen_file('app/tests/unit_test_inventory.json'))
                weights = Path(temporary) / 'weights.json'
                weights.write_bytes(frozen_file('app/tests/module_durations.json'))
                durations = get_module_durations(weights)
            if (not isinstance(inventory, dict) or any(not isinstance(name, str) or not isinstance(ids, list)
                    or any(not isinstance(identifier, str) or not identifier for identifier in ids)
                    for name, ids in inventory.items())):
                raise ValueError('Unit inventory must map module names to lists of test identities')
            expected_ids = [identifier for ids in inventory.values() for identifier in ids]
            if not expected_ids or len(set(expected_ids)) != len(expected_ids):
                raise ValueError('Unit inventory must contain nonempty, unique test identities')
            if timeout is None:
                timeout = 3600 if workers else 7200
            assignment = get_test_shards([name for name, ids in inventory.items() if ids], durations, count)
            print(f'Local unit execution: workers={workers}, partitions={count}, GPU devices hidden', flush=True)
            protected_common = os.fsdecode(run_git(
                repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).strip()
            if workers:
                manifest = register_run(repo, artifacts, expected_ids, count, snapshot_commit=snapshot['commit'])
                ownerships = {entry['path']: entry for entry in
                              json.loads(manifest.read_text(encoding='utf-8'))['ownership']}
            pending = list(range(1, count + 1))
            while pending or active:
                while pending and len(active) < max(1, workers):
                    number = pending.pop(0)
                    directory = artifacts / f'shard-{number}'
                    directory.mkdir()
                    path = worktree_root / f'shard-{number}' if workers else None
                    app = (path or repo) / 'app'
                    receipt_path, log_path, notice = (directory / name for name in
                                                       ('tests.json', 'output.log', 'root-exit.json'))
                    spec = f'{number}/{count}'
                    command = [sys.executable, '-m', 'ci_env', 'discover', '-s', '.', '-p', 'test_*.py', '-v',
                               '--test-report', str(receipt_path)]
                    if workers:
                        command.extend(['--shard', spec])
                    job = dict(process=None, directory=directory, receipt=receipt_path,
                               spec=spec, worktree=path, log_path=log_path, log=None,
                               notice=notice, started=time.monotonic(), closed=False,
                               ownership=ownerships.get(str(path)))
                    jobs.append(job)
                    try:
                        if path is not None:
                            worktrees.append(path)
                            add_snapshot_worktree(repo, snapshot, path, ownership=job['ownership'], manifest=manifest)
                        job['log'] = log_path.open('wb')
                        job['process'] = start_owned_subprocess(
                            command, cwd=app, env={**get_git_environment(), 'PYTHONPATH': str(app),
                                                  'PATH': str(Path(sys.executable).parent) + os.pathsep
                                                  + os.environ.get('PATH', ''),
                                                  'AA2_LOCAL_UNIT_COMMON_DIR': protected_common,
                                                  **CPU_DEVICE_MASK},
                            stdout=job['log'], stderr=subprocess.STDOUT, exit_notice_path=notice,
                            start_new_session=os.name == 'posix', termination_grace=5,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
                    except BaseException as error:
                        if job['log'] is not None:
                            job['log'].close()
                        message = f'{type(error).__name__}: {get_redacted_credentials(str(error))[:1000]}'
                        log_path.write_text(message + '\n', encoding='utf-8')
                        setup = dict(schema_version=1, shard=spec, status='crashed', complete=False,
                                     **{key: 0 for key in COUNT_FIELDS}, test_ids=[], exit_code=None,
                                     log_path=str(log_path), worktree=str(path) if path else None,
                                     setup_error=message)
                        reports.append(setup)
                        atomic_json_write(setup, str(directory / 'shard.json'), allow_nonatomic_fallback=False)
                        raise
                    job['started'] = time.monotonic()
                    active.append(job)
                    print(f'Unit shard {spec} started; log: {log_path}', flush=True)
                for job in list(active):
                    root_exit = get_owned_exit_result(job['notice'])
                    if root_exit is not None:
                        job['root_exit'] = root_exit
                    expired = time.monotonic() - job['started'] >= timeout
                    if expired or (root_exit is not None and root_exit != 0):
                        if expired:
                            job['status'] = 'timed_out'
                        stop_owned_subprocess(job['process'], timeout=5, force_after_grace=sys.platform == 'win32')
                    if job['process'].poll() is not None:
                        control = getattr(job['process'], '_alexandria_control', None)
                        if control is not None:
                            control.close()
                        job['log'].close()
                        report = get_worker_report(job)
                        wanted = {identifier for name in assignment[int(job['spec'].split('/')[0])]
                                  for identifier in inventory[name]}
                        ids = report['test_ids']
                        if report['complete'] and (not isinstance(ids, list)
                                or any(not isinstance(identifier, str) for identifier in ids)
                                or set(ids) != wanted):
                            report['status'] = 'failed'
                            problems.append(f"{job['spec']}: executed identities differ from the CI assignment")
                            atomic_json_write(report, str(job['directory'] / 'shard.json'),
                                              allow_nonatomic_fallback=False)
                        reports.append(report)
                        job['closed'] = True
                        active.remove(job)
                        if job['worktree'] is not None:
                            remove_snapshot_worktree(repo, job['worktree'], worktree_root, ownership=job['ownership'])
                            worktrees.remove(job['worktree'])
                        print(f"Unit shard {job['spec']}: {report['status']}; Ran {report['tests_ran']}; "
                              f"failures={report['failures']}, errors={report['errors']}, skips={report['skips']}", flush=True)
                if active:
                    time.sleep(.05)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                interrupted = error
            message = f'{type(error).__name__}: {get_redacted_credentials(str(error))[:1000]}'
            problems.append(message)
            print('Local unit infrastructure failure: ' + message, file=sys.stderr, flush=True)
        finally:
            for job in jobs:
                if job['closed'] or job['process'] is None:
                    continue
                job['status'] = 'interrupted' if interrupted else 'crashed'
                try:
                    stop_owned_subprocess(job['process'], interrupt=bool(interrupted), timeout=5,
                                          force_after_grace=sys.platform == 'win32')
                except BaseException as error:
                    problems.append(f"{job['spec']}: process cleanup failed: {type(error).__name__}")
                finally:
                    try:
                        control = getattr(job['process'], '_alexandria_control', None)
                        if control is not None:
                            control.close()
                        job['process'].wait()
                        job['log'].close()
                        reports.append(get_worker_report(job))
                    except BaseException as error:
                        problems.append(f"{job['spec']}: owner close/report failed: {type(error).__name__}")
            for path in list(worktrees):
                try:
                    remove_snapshot_worktree(repo, path, worktree_root, ownership=ownerships.get(str(path)))
                    worktrees.remove(path)
                except BaseException as error:
                    problems.append(f'Worktree cleanup failed for {path}: {type(error).__name__}: {error}')
    result = aggregate_unit_reports(reports, expected_ids, count)
    result['problems'].extend(problems)
    if result['tests_ran'] < minimum_tests:
        result['problems'].append(f'Unit discovery is under the {minimum_tests} floor')
    result.update(status='failed' if result['problems'] else result['status'],
                  workers=workers, artifact_dir=str(artifacts),
                  duration_seconds=round(time.monotonic() - started, 3),
                  snapshot_commit=snapshot['commit'] if snapshot else None,
                  timeout_seconds=timeout,
                  gpu_device_mask=dict(CPU_DEVICE_MASK),
                  remaining_worktrees=list(map(str, worktrees)))
    if fallback:
        result['fallback_reason'] = fallback
    atomic_json_write(result, str(artifacts / 'aggregate.json'), allow_nonatomic_fallback=False)
    if manifest is not None and not worktrees:
        with file_lock(get_common_directory(repo) / 'alexandria-local-unit-git', timeout=120):
            manifest.unlink()
    print(f"Ran {result['tests_ran']} tests in {result['duration_seconds']:.3f}s; "
          f"failures={result['failures']}, errors={result['errors']}, skips={result['skips']}; "
          f"unexpected_successes={result['unexpected_successes']}; "
          f"counts_complete={result['counts_complete']}; {result['status']}; report: {artifacts / 'aggregate.json'}", flush=True)
    if interrupted is not None:
        interrupted.result = result
        raise interrupted
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup-stale', action='store_true', required=True,
                        help='recover identity-verified local shards whose coordinator PID/creation time is gone')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    cleanup_stale_runs(args.repo)
    return 0


if __name__ == '__main__':
    sys.exit(main())
