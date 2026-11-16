"""Run CI's unit partitions in detached, byte-faithful local snapshots."""
import contextlib
import errno
import hashlib
import json
import math
import os
from pathlib import Path
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


def get_default_workers():
    return 2


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


def run_git(repo, *arguments, environment=None, _locked=False, include_stderr=False):
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
            cwd=repo, env=environment or get_git_environment(), timeout=120)
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
    run_git(repo, 'read-tree', head, environment=environment)
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


def add_snapshot_worktree(repo, snapshot, path):
    run_git(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', '--quiet', '--detach',
            str(path), snapshot['commit'])
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


def remove_snapshot_worktree(repo, path, owned_root, *, _locked=False):
    if not _locked:
        with file_lock(get_common_directory(repo) / 'alexandria-local-unit-git', timeout=120):
            return remove_snapshot_worktree(repo, path, owned_root, _locked=True)
    path, owned_root = Path(path), Path(owned_root).resolve()
    if not path.resolve().is_relative_to(owned_root) or path.resolve() == owned_root:
        raise ValueError('Refusing to remove a worktree outside the owned unit directory')
    for attempt in range(3):
        try:
            registered = {Path(os.fsdecode(record[9:])).resolve() for record in
                          run_git(repo, 'worktree', 'list', '--porcelain', '-z', _locked=True).split(b'\0')
                          if record.startswith(b'worktree ')}
            if path.resolve() in registered:
                run_git(repo, 'worktree', 'remove', '--force', str(path), _locked=True)
            if path.exists():
                if not path.resolve().is_relative_to(owned_root):
                    raise ValueError('Owned worktree changed during cleanup')
                shutil.rmtree(path)
            return
        except (OSError, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(.1)


def is_owner_alive(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid local unit owner PID; refusing stale cleanup')
    if sys.platform == 'win32':
        import ctypes
        from windows_subprocess_job import get_windows_job_api
        api = get_windows_job_api()
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            return True
        try:
            wait = api.WaitForSingleObject
            wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            wait.restype = ctypes.c_uint32
            return wait(handle, 0) != 0
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno != errno.EPERM:
            raise
    return True


def register_run(repo, artifacts, expected_ids, count):
    common = get_common_directory(repo)
    with file_lock(common / 'alexandria-local-unit-git', timeout=120):
        registry = common / 'alexandria-local-unit-runs'
        registry.mkdir(exist_ok=True)
        manifest = registry / (uuid.uuid4().hex + '.json')
        atomic_json_write(dict(schema_version=1, owner_pid=os.getpid(),
                               owner_platform=sys.platform, owner_host=socket.gethostname(),
                               common_directory=str(common), artifact_dir=str(artifacts),
                               expected_ids=expected_ids, shard_count=count,
                               worktrees=[str(artifacts / 'worktrees' / f'shard-{number}')
                                          for number in range(1, count + 1)]),
                          str(manifest), allow_nonatomic_fallback=False)
    return manifest


def cleanup_stale_runs(repo):
    common = get_common_directory(repo)
    recovered = []
    with file_lock(common / 'alexandria-local-unit-git', timeout=120):
        for manifest in sorted((common / 'alexandria-local-unit-runs').glob('*.json')):
            record = json.loads(manifest.read_text(encoding='utf-8'))
            if record.get('schema_version') != 1 or record.get('common_directory') != str(common):
                raise ValueError(f'Invalid local unit recovery manifest: {manifest}')
            if (record['owner_platform'] != sys.platform
                    or record['owner_host'] != socket.gethostname()
                    or is_owner_alive(record['owner_pid'])):
                continue
            artifacts = Path(record['artifact_dir'])
            root = artifacts / 'worktrees'
            count = record['shard_count']
            if (type(count) is not int or count < 1 or not artifacts.is_absolute()
                    or artifacts.resolve() != artifacts or root.resolve() != root
                    or record['worktrees'] != [str(root / f'shard-{number}')
                                               for number in range(1, count + 1)]):
                raise ValueError(f'Unsafe local unit recovery paths: {manifest}')
            for path in record['worktrees']:
                remove_snapshot_worktree(repo, Path(path), root, _locked=True)
            if not run_git(repo, 'worktree', 'prune', '--dry-run', '--expire=now',
                           _locked=True, include_stderr=True).strip():
                run_git(repo, 'worktree', 'prune', _locked=True)
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
                          remaining_worktrees=[], recovered_owner_pid=record['owner_pid'])
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
    artifacts = Path(report_dir).resolve() if report_dir else Path(tempfile.mkdtemp(prefix='alexandria-local-units-'))
    if artifacts.is_relative_to(repo):
        raise ValueError('Unit artifact directory must be outside the source checkout')
    if artifacts.exists() and any(artifacts.iterdir()):
        raise ValueError('Unit artifact directory must be new or empty; refusing stale shard receipts')
    artifacts.mkdir(parents=True, exist_ok=True)
    worktree_root = artifacts / 'worktrees'
    worktree_root.mkdir()
    started, jobs, reports, problems, expected_ids = time.monotonic(), [], [], [], []
    active, worktrees, snapshot, interrupted, fallback = [], [], None, None, None
    manifest = None
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
            assignment = get_test_shards(inventory, durations, count)
            print(f'Local unit execution: workers={workers}, partitions={count}, GPU devices hidden', flush=True)
            protected_common = os.fsdecode(run_git(
                repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).strip()
            if workers:
                manifest = register_run(repo, artifacts, expected_ids, count)
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
                               notice=notice, started=time.monotonic(), closed=False)
                    jobs.append(job)
                    try:
                        if path is not None:
                            worktrees.append(path)
                            add_snapshot_worktree(repo, snapshot, path)
                        job['log'] = log_path.open('wb')
                        job['process'] = start_owned_subprocess(
                            command, cwd=app, env={**get_git_environment(), 'PYTHONPATH': str(app),
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
                            remove_snapshot_worktree(repo, job['worktree'], worktree_root)
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
                    remove_snapshot_worktree(repo, path, worktree_root)
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
                        help='recover only recorded local shard worktrees whose coordinator PID is gone')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    cleanup_stale_runs(args.repo)
    return 0


if __name__ == '__main__':
    sys.exit(main())
