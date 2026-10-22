import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.run_dialogue_continuity import get_continuity_output_path, run_owned_continuity_pair


class ContinuityControllerTests(unittest.TestCase):
    def test_output_cannot_dirty_the_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for path in (root, root / 'new-experiment'):
                with self.assertRaisesRegex(ValueError, 'outside'):
                    get_continuity_output_path(root, path)
            outside = root.parent / (root.name + '-outside')
            self.assertEqual(outside, get_continuity_output_path(root, outside))
            self.assertFalse((root / 'new-experiment').exists())

    def test_cleanup_failure_exits_owned_process_without_releasing_early(self):
        script = '''
from pathlib import Path
import sys, threading
from types import SimpleNamespace
from experiments.run_dialogue_continuity import run_owned_continuity_pair
root=Path(sys.argv[1])
def cleanup():
    if sys.argv[2]=='exception': raise RuntimeError('Fixture cleanup error')
    return False
def start(name,claim,callback):
    def work():
        try: callback()
        finally: (root/'released-too-early').write_text('bad')
    worker=threading.Thread(target=work);worker.start();return worker
core=SimpleNamespace(project_manager=SimpleNamespace(get_engine=lambda:None,
    ensure_idle_local_models_released=cleanup),claim_gpu_task=lambda name:'fixture',
    start_claimed_task_thread=start)
run_owned_continuity_pair(core,b'',{},0,{},root/'pair')
'''
        for failure in ('false', 'exception'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', script, directory, failure],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
                self.assertEqual(70, result.returncode, result.stderr.decode())
                self.assertFalse((Path(directory) / 'released-too-early').exists())
                record = json.loads((Path(directory) / 'cleanup-failure.json').read_text())
                self.assertEqual('model_cleanup_failed', record['reason'])

    def test_worker_cleanup_precedes_claim_release_on_success_and_failure(self):
        for failure in (False, True):
            events = []
            def start(name, claim, callback):
                def work():
                    try:
                        callback()
                    finally:
                        events.append('release')
                worker = threading.Thread(target=work); worker.start()
                return worker
            def render(*args):
                events.append('render')
                if failure:
                    raise RuntimeError('Fixture render failure')
                return {'uid': 'fixture'}
            manager = SimpleNamespace(get_engine=lambda: object(),
                ensure_idle_local_models_released=lambda: events.append('cleanup') or True)
            core = SimpleNamespace(project_manager=manager,
                claim_gpu_task=lambda name: events.append('claim') or 'fixture',
                start_claimed_task_thread=start)
            with self.subTest(failure=failure), patch(
                    'experiments.run_dialogue_continuity.save_continuity_render_pair', render):
                if failure:
                    with self.assertRaisesRegex(RuntimeError, 'Fixture'):
                        run_owned_continuity_pair(core, b'', {}, 0, {}, 'unused')
                else:
                    self.assertEqual({'uid': 'fixture'}, run_owned_continuity_pair(
                        core, b'', {}, 0, {}, 'unused'))
            self.assertEqual(['claim', 'render', 'cleanup', 'release'], events)
