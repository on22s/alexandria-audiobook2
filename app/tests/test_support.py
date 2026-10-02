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
