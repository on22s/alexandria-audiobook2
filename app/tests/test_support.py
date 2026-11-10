from contextlib import contextmanager, nullcontext
import functools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


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


def get_bash_executable():
    """Resolve the same native Bash used by the production source probe."""
    from experiments.manifest import _source_probe_shell
    executable, detail = _source_probe_shell()
    if executable is None:
        raise RuntimeError(detail)
    return executable


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


@contextmanager
def intercept_book_publications(module, before=None, after=None):
    """Intercept each row publication on whichever protocol is in use.

    before(target) runs before that row can change (raise to inject a failed
    publication); after(target) runs once the target holds the staged bytes
    (the v3 publish move returned, or the v4 exchange swapped). Targets are
    Paths. Callbacks that must fire once should guard themselves: the v4
    companion exchange that a rollback makes has the same direction, so after
    a deliberate abort the same target can be seen again.
    """
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
