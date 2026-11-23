"""Child writer used by test_review6_crash: die at a cast/state publication."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

root = Path(sys.argv[1])
boundary = sys.argv[2]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi import BackgroundTasks  # noqa: E402
from routers import script  # noqa: E402
import core  # noqa: E402

script.DATA_DIR = str(root)
script.SCRIPT_PATH = str(root / 'annotated_script.json')
script.CONFIG_PATH = str(root / 'config.json')
states = {'script': {'running': False, 'logs': [], 'cancel': False}}
core.process_state = states
script.process_state = states
core._task_claims = {}
core._gpu_leases = {}

original = script.apply_book_state_locked


def publish(*args, **kwargs):
    if boundary == 'before':
        os._exit(95)
    import book_state_transaction as books
    from tests.test_support import intercept_book_publications

    def dead(_target):
        os._exit(96)

    with intercept_book_publications(books, after=dead):
        return original(*args, **kwargs)


patches = [
    patch.object(script, 'apply_book_state_locked', side_effect=publish),
    patch.object(script, 'check_global_gpu_lock'),
    patch.object(script, 'get_active_reasoning_effort', return_value=None),
    patch.object(script, 'reserve_background_task', return_value='b' * 32),
    patch.object(core, 'process_state', states),
    patch.object(script, 'process_state', states),
    patch.object(core, '_task_claims', {'script': {'id': 'b' * 32, 'phase': 'pending'}}),
    patch.object(core, '_gpu_leases', {}),
    patch.object(core, 'acquire_gpu_lock', return_value=None),
]
for item in patches:
    item.start()

script.start_script_generation(BackgroundTasks(), str(root / 'book.txt'),
                               script.GenerateScriptRequest(start_over=True))
os._exit(3)
