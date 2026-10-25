from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
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


def create_test_api_server(app):
    """Create an embedded ASGI server without closing other tests' log handlers."""
    import uvicorn
    return uvicorn.Server(uvicorn.Config(app, loop='asyncio', lifespan='off',
                                        log_level='error', log_config=None))
