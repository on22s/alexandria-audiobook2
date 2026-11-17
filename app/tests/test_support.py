"""Shared synthetic fixtures and portable capability gates.

can_symlink() caches real file and directory link attempts once per process;
@skip_unless_symlink skips only when that probe fails, with its explicit reason.
can_fork() checks multiprocessing's available start methods, not the OS name.
get_fixture_process_context() defaults to spawn: targets must be module-level,
picklable callables with explicit arguments, not inherited globals or closures.
Use require_fork=True only for fixtures whose semantics need inherited state;
it raises SkipTest when fork is unavailable. Consumers opt in separately.
"""

from contextlib import ExitStack, contextmanager, nullcontext
import functools
from http.server import BaseHTTPRequestHandler
import inspect
import json
import locale
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


logger = logging.getLogger(__name__)


def get_isolated_git_environment(extra=None):
    """Keep fixture Git commands out of the caller's repository and index.

    Preserve HOME and global Git settings: portability fixtures exercise those
    settings explicitly. Only inherited repository/config injection is removed;
    fixtures needing neutral global settings can supply GIT_CONFIG_GLOBAL.
    """
    environment = os.environ.copy()
    if extra:
        environment.update(extra)
    excluded = {
        'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
        'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
        'GIT_PREFIX', 'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES',
        'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
    }
    for name in list(environment):
        normalized = name.upper()
        if normalized in excluded or normalized.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')):
            del environment[name]
    environment['GIT_CONFIG_NOSYSTEM'] = '1'
    return environment


def isolate_git_environment(test_case=None):
    """Scope fixture Git readers, or enter until a TestCase finishes cleanup.

    Apply deliberate adversarial Git overrides inside this scope. HOME and
    global settings follow get_isolated_git_environment's preservation policy.
    Optional TestCase ownership also supports Python without enterContext().
    """
    @contextmanager
    def scope():
        with patch.dict(os.environ, get_isolated_git_environment(), clear=True):
            yield

    context = scope()
    if test_case is not None:
        cleanup = ExitStack()
        test_case.addCleanup(cleanup.close)
        cleanup.enter_context(context)
    return context


@functools.lru_cache(maxsize=1)
def _probe_symlink():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        file_target = root / 'file'
        file_target.touch()
        directory_target = root / 'directory'
        directory_target.mkdir()
        try:
            os.symlink(file_target, root / 'file-link', target_is_directory=False)
            os.symlink(directory_target, root / 'directory-link', target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as error:
            winerror = getattr(error, 'winerror', None)
            if winerror == 1314:
                detail = 'WinError 1314; enable Developer Mode or run elevated'
            elif winerror is not None:
                detail = f'WinError {winerror}'
            elif getattr(error, 'errno', None) is not None:
                detail = f'errno {error.errno}'
            else:
                detail = type(error).__name__
            return False, f'symlink creation not permitted ({detail})'
    return True, ''


def can_symlink():
    """Return the cached result of real file and directory symlink attempts."""
    return _probe_symlink()[0]


def skip_unless_symlink(test):
    """Decorate a test or TestCase with the cached capability and skip reason."""
    supported, reason = _probe_symlink()
    return unittest.skipUnless(supported, reason)(test)


def can_fork():
    """Return whether multiprocessing offers the fork start method."""
    import multiprocessing
    return 'fork' in multiprocessing.get_all_start_methods()


def get_fixture_process_context(*, require_fork=False):
    """Use portable spawn unless inherited-state semantics require fork."""
    import multiprocessing
    if require_fork and not can_fork():
        raise unittest.SkipTest('fork start method is unavailable; use a spawn-safe fixture')
    return multiprocessing.get_context('fork' if require_fork else 'spawn')


class FramedHttpFixtureHandler(BaseHTTPRequestHandler):
    """Let the client consume a framed response before ending its connection.

    Immediate close-delimited responses intermittently reset on native Windows.
    A full length and keep-alive avoid racing client reads against server close.
    Only a reset while awaiting the next request after a complete response is
    normal client teardown; resets during request/response handling still raise.
    """
    protocol_version = 'HTTP/1.1'

    def send_fixture_response(self, status, payload=b'', headers=()):
        self.send_response(status)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'keep-alive')
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        self._fixture_response_complete = True

    def handle_one_request(self):
        completed = getattr(self, '_fixture_response_complete', False)
        self._fixture_response_complete = False
        self.raw_requestline = b''
        try:
            super().handle_one_request()
        except ConnectionResetError:
            if not completed or self.raw_requestline:
                raise
            self.close_connection = True


def assume_ample_free_disk(testcase):
    """Report 1 TiB free to every unpatched disk probe for this test.

    Tests of other behaviour must not fail on a host whose temp disk is low
    (a full /tmp RAM disk failed 15 of them). Tests of the disk floor itself
    still patch shutil.disk_usage locally, which takes precedence.
    """
    import shutil
    from types import SimpleNamespace
    free = 1024 ** 4
    probe = patch.object(shutil, 'disk_usage', return_value=SimpleNamespace(total=2 * free, used=free, free=free))
    probe.start()
    testcase.addCleanup(probe.stop)


@functools.lru_cache(maxsize=1)
def get_bash_executable():
    """Prefer Git-for-Windows usr/bin Bash; reject WSL during fallback probing."""
    from experiments.manifest import _source_probe_shell
    if os.name == 'nt':
        git = shutil.which('git')
        if git:
            probe = subprocess.run([git, '--exec-path'], capture_output=True,
                                   text=True, timeout=10, env=get_isolated_git_environment())
            roots = list(Path(git).parents)
            if probe.returncode == 0:
                roots = list(Path(probe.stdout.strip()).parents) + roots
            for root in roots:
                for relative in ('usr/bin/bash.exe', 'bin/bash.exe'):
                    candidate = root / relative
                    uname = candidate.parent / 'uname.exe'
                    if not candidate.is_file() or not uname.is_file():
                        continue
                    result = subprocess.run([str(candidate), '-c', '"$1" -s', 'fixture',
                                             get_bash_path(uname)], capture_output=True,
                                            text=True, timeout=10)
                    if result.returncode == 0 and result.stdout.upper().startswith(('MINGW', 'MSYS')):
                        return str(candidate)
    executable, detail = _source_probe_shell()
    if executable is None:
        raise RuntimeError(detail)
    return executable


def get_bash_path(path):
    """Return a shell-facing path without exposing native backslash escapes."""
    value = os.fspath(path)
    if os.name != 'nt':
        return value
    value = value.replace('\\', '/')
    if len(value) >= 3 and value[1:3] == ':/':
        return '/' + value[0].lower() + value[2:]
    return value


def write_bash_fixture(path, source):
    """Write LF shell text or a Python payload with a space-safe Bash launcher."""
    path = Path(path)
    first, _, body = source.partition('\n')
    if first.startswith('#!') and 'python' in first.lower():
        return write_python_bash_fixture(path, body)
    path.write_text(source, encoding='utf-8', newline='\n')
    path.chmod(0o755)
    return path


def copy_bash_fixture(source, destination):
    """Copy shell fixtures as LF, retaining exact bytes for non-shell inputs."""
    if Path(source).suffix == '.sh':
        return write_bash_fixture(destination, Path(source).read_text(encoding='utf-8'))
    return shutil.copyfile(source, destination)


def run_bash_fixture(arguments, **kwargs):
    """Run native Bash with isolated Git and host-Python command fixtures.

    Shell snippets must quote get_bash_path() values themselves. Positional
    arguments are transported as individual argv entries, never shell text.
    Custom environment values retain their caller-selected host/shell contract.
    """
    supplied_environment = kwargs.pop('env', None)
    environment = get_isolated_git_environment(supplied_environment)
    if supplied_environment is not None:
        environment = {name: value for name, value in environment.items()
                       if name in supplied_environment or name == 'GIT_CONFIG_NOSYSTEM'}
    with tempfile.TemporaryDirectory(prefix='bash transport ') as directory:
        if os.name == 'nt':
            for name in ('python', 'python3'):
                write_python_bash_launcher(Path(directory) / name)
            shell = Path(get_bash_executable())
            path = environment.get('PATH', '')
            inherited = os.environ.get('PATH', '')
            prefix = path[:-len(inherited)].rstrip(os.pathsep) if inherited and path.endswith(inherited) else ''
            paths = prefix.split(os.pathsep) if prefix else []
            paths += [directory, str(shell.parent), str(shell.parent.parent / 'usr/bin')]
            paths.extend((inherited if prefix else path).split(os.pathsep))
            environment['PATH'] = ':'.join(get_bash_path(path) for path in paths if path)
        command = [get_bash_executable()]
        if os.name == 'nt':
            command += ['-o', 'igncr']
        for index, argument in enumerate(arguments):
            value = os.fspath(argument)
            snippet = index == 1 and arguments[0] == '-c'
            native_absolute = len(value) >= 3 and value[1] == ':' and value[2] in '/\\'
            if not snippet and (isinstance(argument, os.PathLike) or native_absolute
                                or (index == 0 and value != '-c')):
                value = get_bash_path(value)
            command.append(value)
        if os.name == 'nt':
            quoted = []
            for value in command:
                argument = subprocess.list2cmdline([value])
                if not argument.startswith('"'):
                    trailing = len(value) - len(value.rstrip('\\'))
                    argument = '"' + argument + '\\' * trailing + '"'
                quoted.append(argument)
            command = ' '.join(quoted)
        timeout = kwargs.pop('timeout', None)
        check = kwargs.pop('check', False)
        capture = kwargs.pop('capture_output', False)
        if capture and ('stdout' in kwargs or 'stderr' in kwargs):
            raise ValueError('stdout and stderr arguments may not be used with capture_output.')
        outputs = {}
        if capture:
            # Capture into files, never pipes: subprocess.run's reader threads
            # block on a pipe that a surviving grandchild keeps open, so its
            # timeout never returns on Windows.
            encoding = kwargs.get('encoding') or locale.getpreferredencoding(False)
            for name in ('stdout', 'stderr'):
                handle = tempfile.NamedTemporaryFile(prefix='bash transport ', delete=False)
                outputs[name] = (Path(handle.name), handle)
                kwargs[name] = handle
        try:
            process = subprocess.Popen(command, env=environment, **kwargs)
        finally:
            for _, handle in outputs.values():
                handle.close()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_bash_fixture_tree(process)
            remove_bash_fixture_outputs(outputs)
            raise
        captured = {}
        text = kwargs.get('text') or kwargs.get('universal_newlines')
        for name, (path, _) in outputs.items():
            captured[name] = (path.read_text(encoding=encoding, errors=kwargs.get('errors'))
                              if text else path.read_bytes())
        remove_bash_fixture_outputs(outputs)
        completed = subprocess.CompletedProcess(command, process.returncode,
                                                captured.get('stdout'), captured.get('stderr'))
        if check:
            completed.check_returncode()
        return completed


def remove_bash_fixture_outputs(outputs):
    """Remove captured output files, retrying while a closing child holds one."""
    for path, _ in outputs.values():
        for attempt in range(10):
            try:
                path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if attempt == 9:
                    logger.warning('could not remove fixture output %s: %s', path, error)
                else:
                    time.sleep(0.1)


def stop_bash_fixture_tree(process):
    """Kill a timed-out fixture's tree so no descendant outlives the test."""
    if os.name == 'nt':
        from subprocess_ownership import terminate_windows_process_tree
        try:
            terminate_windows_process_tree(process, force=True, timeout=10)
        except OSError:
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def snapshot_fixture_files(root, *, lock_files=()):
    """Snapshot data bytes and exact identities of known held lock sidecars.

    Windows byte-range leases prohibit reading their lock byte. Never treat an
    arbitrary unreadable file as a lock: callers name the actual sidecars.
    """
    root = Path(root)
    result = {}
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        if name in lock_files:
            info = path.stat()
            result[name] = ('lock', info.st_dev, info.st_ino, info.st_size)
        else:
            try:
                result[name] = path.read_bytes()
            except OSError as error:
                raise OSError(f"Could not snapshot fixture data file {name}") from error
    return result



def _run_adapter_fixture_process(ready, target, args):
    # Spawn starts a fresh interpreter. Warm the actual structural-validator
    # dependencies before starting the transaction's existing hang deadline.
    import peft
    import safetensors
    ready.set()
    target(*args)


def start_adapter_fixture_process(target, *args, startup_timeout=60):
    """Start a real spawned adapter worker; bound imports separately from work.

    Full-suite discovery followed by a cold Windows spawn can spend over ten
    seconds importing PEFT/Transformers. Callers still join with their original
    operation timeout after this handshake. Import failure and startup timeout
    are failures, never skips or successful transaction evidence.
    """
    import multiprocessing
    import time
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    process = context.Process(target=_run_adapter_fixture_process,
                              args=(ready, target, args))
    process.start()
    deadline = time.monotonic() + startup_timeout
    while not ready.wait(min(0.1, max(0, deadline - time.monotonic()))):
        alive = process.is_alive()
        if not alive or time.monotonic() >= deadline:
            if alive:
                process.kill()
            process.join(5)
            reason = (f'timed out initializing after {startup_timeout}s' if alive
                      else 'exited before initialization')
            raise AssertionError(f'Adapter fixture {reason} (exitcode={process.exitcode})')
    return process


def write_python_bash_launcher(path, *arguments):
    """Launch the host Python without MSYS guessing Unicode or metacharacter paths."""
    import shlex
    path = Path(path)
    prefix = '#!/bin/sh\n'
    if os.name == 'nt':
        prefix += ('for argument do\n'
                   '  case "$argument" in /[a-zA-Z]/*) argument=$(cygpath -m -- "$argument") || exit;; esac\n'
                   '  shift\n  set -- "$@" "$argument"\ndone\n'
                   'export MSYS2_ARG_CONV_EXCL="*"\n')
    command = ' '.join(shlex.quote(Path(argument).as_posix())
                       for argument in (sys.executable, *arguments))
    path.write_text(prefix + 'exec ' + command + ' "$@"\n', encoding='utf-8', newline='\n')
    path.chmod(0o755)
    return path


def write_python_bash_fixture(path, source):
    """Write a Python payload with a launcher explicitly executed by native Bash."""
    path = Path(path)
    payload = path.with_name(path.name + '.py')
    payload.write_text(source, encoding='utf-8')
    return write_python_bash_launcher(path, payload)


def write_python_cli_fixture(path, source):
    """Return a real host-executable Python fixture, including Windows CMD.

    Keep the payload separate from its launcher so interpreter paths containing
    spaces work without relying on POSIX shebang support in CreateProcess.
    Use only trusted synthetic arguments: CMD shell metacharacters are not
    escaped by this test helper. Callers must wait for a running fixture before
    replacing its payload.
    """
    path = Path(path)
    if os.name != 'nt':
        return write_python_bash_fixture(path, source)
    payload = path.with_name(path.name + '.py')
    payload.write_text(source, encoding='utf-8')
    launcher = path.with_name(path.name + '.cmd')
    # CMD decodes batch contents using its console code page, even when
    # Python is in UTF-8 mode. Select UTF-8 before reading a Unicode path.
    launcher.write_text('@setlocal\n'
                        '@for /f "tokens=2 delims=:" %%c in (\'chcp\') do @set "_AA2_FIXTURE_CP=%%c"\n'
                        '@chcp 65001 >nul\n@"' + sys.executable + '" "' + str(payload) + '" %*\n'
                        '@set "_AA2_FIXTURE_EXIT=%errorlevel%"\n'
                        '@chcp %_AA2_FIXTURE_CP% >nul\n'
                        '@exit /b %_AA2_FIXTURE_EXIT%\n', encoding='utf-8')
    return launcher


def get_missing_linux_queue_tools():
    """Name what this host lacks of the GPU queue's Linux tooling.

    gpu_job.sh and run_chains/ rely on flock, bash 4 (declare -A, exec {fd})
    and /proc; macOS has none of them. Linux CI has all three, and the release
    verifier refuses skips, so these tests can only skip off Linux.
    """
    missing = [] if shutil.which('flock') else ['flock']
    if not os.path.isdir('/proc/self'):
        missing.append('/proc')
    try:
        newer = subprocess.run(['bash', '-c', '((BASH_VERSINFO[0] >= 4))'], capture_output=True,
                               timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        newer = False
    if not newer:
        missing.append('bash 4')
    return missing


_MISSING_LINUX_QUEUE_TOOLS = get_missing_linux_queue_tools()
requires_linux_queue_tools = unittest.skipIf(
    bool(_MISSING_LINUX_QUEUE_TOOLS),
    "needs the GPU queue's Linux tooling; missing " + ', '.join(_MISSING_LINUX_QUEUE_TOOLS))


@contextmanager
def use_private_cpu_fixture_lock():
    """Select a real private lease for model-free CPU fixtures.

    Real GPU/model work must retain the normal shared lease. These CPU fixtures
    acquire the unchanged native lock on their own path, without claiming to
    inherit the caller's ownership.
    """
    with tempfile.TemporaryDirectory(prefix='aa2-cpu-fixture-lock-') as directory:
        lock = Path(directory) / 'gpu.lock'
        with patch.dict(os.environ, {'GPU_LOCK': str(lock), 'ALEXANDRIA_GPU_LOCK_HELD': '0'}):
            yield lock


@contextmanager
def apply_test_module_providers(providers):
    """Restore only replaced provider keys, retaining unrelated lazy imports."""
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in providers}
    sys.modules.update(providers)
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _Upload:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


def app_python_files(app_dir):
    """-> our own .py files under app/, never the virtualenv's.

    `app/env` is a virtualenv inside the directory being walked, so a bare
    rglob returns several thousand site-packages modules. CI and worktrees have
    no venv, so a sweep written that way passes there and fails only in the
    live checkout - which is exactly where the suite most needs to run, because
    that is the tree the GPU queue runs from.

    Two other sweeps already excluded it by hand, each with its own spelling.
    This is the one place that answers "which .py files are ours".
    """
    import pathlib
    app_dir = pathlib.Path(app_dir)
    roots = {p.parent for p in app_dir.rglob("pyvenv.cfg")}
    out = []
    for path in sorted(app_dir.rglob("*.py")):
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            continue
        if any(root in path.parents for root in roots):
            continue
        out.append(path)
    return out


def write_test_adapter(path, value=1.0):
    """Write a structurally real CPU LoRA artifact, without model inference."""
    import json
    from pathlib import Path
    import numpy as np
    from peft import LoraConfig
    from safetensors.numpy import save_file
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    LoraConfig(r=2, lora_alpha=4, target_modules=["q_proj"]).save_pretrained(path)
    save_file({"base_model.model.layer.q_proj.lora_A.weight": np.full((2, 3), value, dtype=np.float32),
               "base_model.model.layer.q_proj.lora_B.weight": np.full((4, 2), value, dtype=np.float32)},
              str(path / "adapter_model.safetensors"))
    (path / "training_meta.json").write_text(json.dumps({"best_loss":2.5, "final_loss":2.5, "num_samples":1}))


def write_test_serving_adapter(path, value=1.0):
    """Write all four real serving files, with known synthetic reference PCM."""
    import json
    from pathlib import Path
    import numpy as np
    import soundfile as sf
    from lora_evidence import get_file_sha256
    write_test_adapter(path, value)
    path = Path(path)
    sf.write(path / "ref_sample.wav", np.full(8000, value * .05), 16000)
    (path / "training_meta.json").write_text(json.dumps({
        "ref_sample_text": "Known synthetic reference.",
        "checkpoint_sha256": get_file_sha256(path / "adapter_model.safetensors")}))


class SyntheticEvaluationEngine:
    """Settings contract for bounded PCM callbacks, without a model backend."""
    def __init__(self, config=None):
        from lora_evidence import get_tts_model_settings
        self._settings = get_tts_model_settings(config or {})

    def get_model_settings(self):
        return dict(self._settings)

    def get_lora_serving_identity(self):
        return {"policy_version": 1, "device": "cpu", "dtype": "float32", "merge_mode": "merged"}


def assert_file_lock_released(path):
    """Verify ownership ended by taking the same lock immediately."""
    from utils import file_lock
    with file_lock(path, timeout=0):
        pass


def assert_directory_payload_names(case, directory, expected, *, lock_targets):
    """Permit only specified persistent lock inodes, proving each is released."""
    from pathlib import Path
    from utils import file_lock
    targets = {Path(str(target) + '.lock'): target for target in lock_targets}
    names = []
    for path in Path(directory).iterdir():
        if path not in targets:
            names.append(path.name)
            continue
        case.assertIn(path.read_bytes(), (b'', b'\0'))
        with file_lock(targets[path], timeout=0):
            pass
    case.assertEqual(sorted(expected), sorted(names))


PARENT_CHANGED_MESSAGE = 'Book artifact parent changed; recovery evidence was preserved'
MOVE_PARENT_CHANGED_MESSAGE = 'Book move parent changed; recovery evidence was preserved'
ORIGINAL_MISSING_MESSAGE = 'Active-book original artifact is missing or changed'
ROOT_CHANGED_MESSAGE = 'Book move root changed; recovery evidence was preserved'
ROOT_ADMISSION_MESSAGE = 'Book root changed during admission; alias scope no longer matches'
WORKSPACE_CHANGED_MESSAGE = 'Book transaction workspace changed; recovery evidence was preserved'


def assert_book_journal_fenced(case, root, *, content_check=None, expected_message, expected_cause_type=None):
    """Refuse recovery twice on the same frozen publishing journal.

    content_check, if given, is executed before the first refusal and
    after EACH refusal, so callers keep their per-iteration byte
    assertions at the exact observed state. expected_message is the
    complete ValueError message for the specific recovery refusal.
    expected_cause_type, if given, requires the refusal's explicit
    __cause__ to be that type on each refusal (e.g. OSError for the
    open-failure conversion, distinguishing it from a bare identity
    mismatch with the same message). Callers without a cause contract
    leave it None.
    """
    import json
    import book_state_transaction as books
    journal = root / books.JOURNAL
    case.assertTrue(journal.exists(), 'journal evidence must be preserved')
    with journal.open('r', encoding='utf-8') as handle:
        case.assertEqual('publishing', json.load(handle)['phase'])
    if content_check is not None:
        content_check()
    frozen = journal.read_bytes()
    for _ in range(2):
        with case.assertRaises(ValueError) as raised:
            with books.ensure_book_state(root):
                case.fail('Book journal recovery was admitted')
        case.assertEqual(expected_message, str(raised.exception))
        if expected_cause_type is not None:
            case.assertIsInstance(raised.exception.__cause__, expected_cause_type)
        case.assertEqual(frozen, journal.read_bytes())
        if content_check is not None:
            content_check()
    return frozen


def get_book_publication_target(source, destination, *, exchange=False):
    """-> the target a book publication primitive is replacing, else None.

    v3 replaces an existing target with two renames:
    _move(target, workspace/old-i), then _move(workspace/new-i, target).
    v4 (#1660) replaces it with one primitive: _exchange(target,
    workspace/new-i) for a replacement, _exchange(slot, target) for a
    creation, and _exchange(target, workspace/old-i) for a removal. Both
    protocols stage a row as new-<index> inside a .book-switch-* workspace,
    so the direction of the call names the same target; tests must intercept
    that primitive, not one rename of a protocol. A v3 backup move
    (target -> old-i) is not a publication, so only an exchange counts the
    old-i direction.
    """
    source, destination = Path(source), Path(destination)
    in_workspace = lambda path: path.parent.name.startswith('.book-switch-')
    if destination.name.startswith('new-') and in_workspace(destination):
        return source            # v4 replacement exchange: first name is the target
    if source.name.startswith('new-') and in_workspace(source):
        return destination       # v3 publication move or v4 creation exchange
    if exchange and destination.name.startswith('old-') and in_workspace(destination):
        return source            # v4 removal exchange: first name is the target
    return None


def _require_callback_arity(callback, count, label):
    """Fail at context entry when a hook cannot take its documented arguments.

    A wrong-arity callback otherwise raises TypeError at the first mutation,
    which a test that only checks "some exception" can mistake for the error it
    meant to inject (#1660 review).
    """
    if callback is None:
        return
    try:
        inspect.signature(callback).bind(*([None] * count))
    except (TypeError, ValueError) as error:
        raise TypeError(
            f'{label} must accept {count} positional argument(s); '
            f'got {callback!r}: {error}') from error


@contextmanager
def intercept_book_publications(module, before=None, after=None):
    """Intercept each row publication on whichever protocol is in use.

    before(target) runs before that row's target changes (raise to inject a
    failed publication). For a v4 creation the target already holds our
    64-byte placeholder at that point, not nothing; after(target) runs once the target holds the staged bytes
    (the v3 publish move returned, or the v4 exchange swapped). Targets are
    Paths. Callbacks that must fire once should guard themselves: the v4
    companion exchange that a rollback makes has the same direction, so after
    a deliberate abort the same target can be seen again.
    """
    _require_callback_arity(before, 1, 'intercept_book_publications before')
    _require_callback_arity(after, 1, 'intercept_book_publications after')
    original_move, original_exchange = module._move, module._exchange
    def wrap(original, exchange):
        def publication(source, destination, **kwargs):
            target = get_book_publication_target(source, destination, exchange=exchange)
            if target is not None and before is not None:
                before(target)
            result = original(source, destination, **kwargs)
            if target is not None and after is not None:
                after(target)
            return result
        return publication
    with patch.object(module, '_move', side_effect=wrap(original_move, False)), \
            patch.object(module, '_exchange', side_effect=wrap(original_exchange, True)):
        yield


# Every post-journal mutation a book publication makes. A v3 row is a backup
# move plus a publish move; a v4 row is one exchange, a creation adds one
# no-overwrite placeholder publish, and a removal adds one proven delete. The
# placeholder publish calls _move internally, so a creation counts as TWO
# ordinals here (_create_book_placeholder_at, then its _move) -- count them
# when asserting an ordinal in a transaction that creates a file. Placeholders are staged in the workspace
# before the journal, like v3 staging, so their create is not among them.
BOOK_MUTATION_SEAMS = ('_move', '_exchange', '_create_book_placeholder_at',
                       '_delete_book_placeholder_at')


@contextmanager
def book_publication_mutations(module, *, before=None, after=None):
    """Intercept every post-journal name mutation a publication makes.

    before(ordinal, name, args) runs before that mutation (raise to inject a
    failed mutation at that exact point); after(ordinal, name, args) runs once
    it is done (raise or exit to emulate process death there). The yielded
    dict holds the running `count`, so a test can pin how many points the
    protocol has and sweep them all instead of naming one protocol's ordinals.
    """
    _require_callback_arity(before, 3, 'book_publication_mutations before')
    _require_callback_arity(after, 3, 'book_publication_mutations after')
    originals = {name: getattr(module, name) for name in BOOK_MUTATION_SEAMS}
    state = {'count': 0}
    def wrap(name, original):
        def call(*args, **kwargs):
            state['count'] += 1
            ordinal = state['count']
            if before is not None:
                before(ordinal, name, args)
            result = original(*args, **kwargs)
            if after is not None:
                after(ordinal, name, args)
            return result
        return call
    with ExitStack() as stack:
        for name, original in originals.items():
            stack.enter_context(patch.object(module, name, side_effect=wrap(name, original)))
        yield state


@functools.lru_cache(maxsize=1)
def exchange_protocol_available():
    """-> whether this host and its temp filesystem can run protocol v4.

    Windows has no exchange at all. Elsewhere the real capability probe over
    a private temp directory answers for the temp trees the tests build; an
    unsupported filesystem leaves only the legacy protocol to exercise.
    """
    import source_upload_publication as publication
    if os.name == 'nt' or os.open not in os.supports_dir_fd:
        return False
    with tempfile.TemporaryDirectory() as temporary:
        descriptor = os.open(temporary, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            return publication.probe_exchange_support(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def legacy_book_publication_protocol():
    """Pin the capability probe off so a replacement-only transaction is v3."""
    import source_upload_publication as publication
    with patch.object(publication, 'probe_exchange_support', return_value=False):
        yield


def book_protocol_cases(v3_only=False):
    """-> (name, context) for each protocol this host can run.

    Every behavioural promise runs under the legacy protocol and, where the
    exchange exists, under v4 as well. v3_only is for tests that assert a
    mechanism v4 deliberately does not have (old-N slots, the v3 move
    sequence, v3 journal fields).
    """
    yield 'v3', legacy_book_publication_protocol()
    if not v3_only and exchange_protocol_available():
        yield 'v4', nullcontext()


class BookProtocolCases:
    """Run each behavioural test under both book publication protocols.

    Behavioural promises (an abort restores exact bytes, a reader never sees
    a half switch, a parent/root swap is refused with evidence, foreign data
    survives, recovery is idempotent) must hold on both. A test that asserts
    a v3 mechanism names itself in v3_only_tests with the reason; only those
    pin the probe off, so the pin the reviewer complained about is now the
    documented exception rather than the default.
    """

    # test name -> why this test can only run on the v3 `_move` protocol
    v3_only_tests = {}

    def _callTestMethod(self, method):
        name = getattr(method, '__name__', '')
        for protocol, context in book_protocol_cases(name in self.v3_only_tests):
            self._book_protocol = protocol
            with self.subTest(protocol=protocol), context:
                super()._callTestMethod(method)


def prepare_book_worker(protocol):
    """Pin a worker script to the protocol its parent test selected."""
    import source_upload_publication as publication
    if protocol == 'v3':
        publication.probe_exchange_support = lambda _fd: False


def get_book_original_slot(root, index=0):
    """-> the workspace path holding a pending journal row's original.

    v3 saves it as old-<index>. v4 rotates the slots: a replacement's or
    creation's exchange leaves the original at new-<index>, a removal's at
    old-<index> (#1660 step 4).
    """
    import book_state_transaction as books
    document = json.loads((Path(root) / books.JOURNAL).read_text(encoding='utf-8'))
    row = document['entries'][index]
    if document.get('version') == 4 and row.get('new_sha256') is not None:
        slot = f'new-{index}'
    else:
        slot = f'old-{index}'
    return Path(root) / document['workspace'] / slot


def get_book_journal_version(root):
    """-> the pending journal's protocol version (4 is one exchange per row)."""
    import book_state_transaction as books
    return json.loads((Path(root) / books.JOURNAL).read_text(encoding='utf-8'))['version']


def create_test_api_server(app):
    """Create an embedded ASGI server without closing other tests' log handlers."""
    import uvicorn
    return uvicorn.Server(uvicorn.Config(app, loop='asyncio', lifespan='off',
                                        log_level='error', log_config=None))


def get_quarantined_path(case, root, path):
    """-> where auto-quarantine (#1660) moved `path`, a name inside a switch's
    workspace, after asserting exactly one completed quarantine holds the
    journal's exact bytes and that neither the journal nor the workspace is
    left in the book root."""
    import hashlib
    import json
    from pathlib import Path
    import book_state_transaction as books
    root, path = Path(root), Path(path)
    workspace_name, *rest = path.relative_to(root).parts
    quarantines = [entry for entry in root.iterdir() if entry.name.startswith(books.QUARANTINE_PREFIX)]
    case.assertEqual(1, len(quarantines), quarantines)
    receipt = json.loads((quarantines[0] / books.QUARANTINE_RECEIPT).read_text(encoding='utf-8'))
    case.assertEqual(('completed', workspace_name), (receipt['state'], receipt['workspace']['name']))
    raw = (quarantines[0] / books.QUARANTINE_JOURNAL).read_bytes()
    case.assertEqual(receipt['journal']['sha256'], hashlib.sha256(raw).hexdigest())
    case.assertFalse((root / books.JOURNAL).exists())
    case.assertFalse((root / workspace_name).exists())
    return quarantines[0].joinpath(books.QUARANTINE_WORKSPACE, *rest)


def create_test_symlink_or_skip(link, target, target_is_directory=False):
    try:
        Path(link).symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == 'nt' and getattr(exc, 'winerror', None) == 1314:
            raise unittest.SkipTest('Windows symlink creation requires privileges (WinError 1314)') from exc
        raise


class FixtureCapabilityTests(unittest.TestCase):
    def setUp(self):
        _probe_symlink.cache_clear()
        self.addCleanup(_probe_symlink.cache_clear)

    def test_symlink_errors_are_cached_with_explicit_skip_reasons(self):
        privilege_error = OSError('privilege not held')
        privilege_error.winerror = 1314
        for error, detail in (
                (OSError(13, 'permission denied'), 'errno 13'),
                (privilege_error, 'WinError 1314; enable Developer Mode or run elevated'),
                (NotImplementedError(), 'NotImplementedError'),
                (AttributeError(), 'AttributeError')):
            with self.subTest(error=type(error).__name__, detail=detail):
                _probe_symlink.cache_clear()
                with patch.object(os, 'symlink', side_effect=error) as symlink:
                    self.assertFalse(can_symlink())
                    self.assertFalse(can_symlink())
                    decorated = skip_unless_symlink(lambda: self.fail('unsupported body ran'))
                    with self.assertRaises(unittest.SkipTest) as skipped:
                        decorated()
                    self.assertEqual(str(skipped.exception),
                                     f'symlink creation not permitted ({detail})')
                    symlink.assert_called_once()

    def test_missing_symlink_attribute_is_unsupported(self):
        with patch.object(os, 'symlink'):
            del os.symlink
            self.assertFalse(can_symlink())
            self.assertIn('AttributeError', _probe_symlink()[1])

    def test_supported_symlink_probe_is_cached_and_runs_body(self):
        with patch.object(os, 'symlink') as symlink:
            self.assertTrue(can_symlink())
            self.assertTrue(can_symlink())
            self.assertEqual(skip_unless_symlink(lambda: 'ran')(), 'ran')
            self.assertEqual(symlink.call_count, 2)
            self.assertEqual([call.kwargs['target_is_directory']
                              for call in symlink.call_args_list], [False, True])

    def test_directory_symlink_denial_is_not_hidden_by_file_support(self):
        with patch.object(os, 'symlink', side_effect=[None, OSError(13, 'denied')]) as symlink:
            self.assertFalse(can_symlink())
            self.assertFalse(can_symlink())
            self.assertEqual(symlink.call_count, 2)

    def test_unexpected_probe_failure_is_not_a_skip(self):
        with patch.object(os, 'symlink', side_effect=RuntimeError('unexpected')):
            with self.assertRaisesRegex(RuntimeError, 'unexpected'):
                can_symlink()

    def test_temp_setup_failure_is_not_a_capability_skip(self):
        with patch.object(tempfile, 'TemporaryDirectory', side_effect=OSError('no temp space')):
            with self.assertRaisesRegex(OSError, 'no temp space'):
                can_symlink()

    def test_temp_cleanup_failure_is_not_a_capability_skip(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.object(tempfile, 'TemporaryDirectory', return_value=directory), \
                patch.object(directory, 'cleanup', side_effect=OSError('temp cleanup denied')):
            with self.assertRaisesRegex(OSError, 'temp cleanup denied'):
                can_symlink()
            self.assertEqual(_probe_symlink.cache_info().currsize, 0)

    def test_real_symlink_probe_cleans_its_temporary_directory(self):
        with patch.object(os, 'symlink', wraps=os.symlink) as symlink:
            supported = can_symlink()
            self.assertIsInstance(supported, bool)
            self.assertEqual(_probe_symlink()[1] == '', supported)
            for call in symlink.call_args_list:
                self.assertFalse(Path(call.args[1]).parent.exists())

    def test_fork_requirement_skips_but_spawn_remains_available(self):
        import multiprocessing
        with patch.object(multiprocessing, 'get_all_start_methods', return_value=['spawn']):
            self.assertFalse(can_fork())
            self.assertEqual(get_fixture_process_context().get_start_method(), 'spawn')
            with self.assertRaisesRegex(unittest.SkipTest, 'fork start method is unavailable'):
                get_fixture_process_context(require_fork=True)

    def test_available_fork_context_is_selected_explicitly(self):
        import multiprocessing
        with patch.object(multiprocessing, 'get_all_start_methods', return_value=['fork', 'spawn']), \
                patch.object(multiprocessing, 'get_context') as get_context:
            self.assertTrue(can_fork())
            self.assertIs(get_fixture_process_context(require_fork=True), get_context.return_value)
            get_context.assert_called_once_with('fork')

    def test_spawn_context_runs_a_real_picklable_worker(self):
        context = get_fixture_process_context()
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_report_fixture_process, args=(sender,))
        try:
            process.start()
            sender.close()
            process.join(timeout=30)
            self.assertFalse(process.is_alive(), 'spawn fixture worker timed out')
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(receiver.poll(5), 'spawn fixture worker sent no result')
            child_pid, method = receiver.recv()
            self.assertNotEqual(child_pid, os.getpid())
            self.assertEqual(method, 'spawn')
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=30)
            receiver.close()
            sender.close()
            process.close()


def _report_fixture_process(sender):
    import multiprocessing
    try:
        sender.send((os.getpid(), multiprocessing.get_start_method()))
    finally:
        sender.close()
