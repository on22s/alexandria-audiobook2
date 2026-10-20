import asyncio
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx
import core
from routers import editor
from tests import test_word_timing as fixtures
import word_timing


class WordTimingCPUAdmissionTests(unittest.TestCase):
    def test_word_timing_owns_cpu_slot_during_gpu_work_and_cancellation_until_worker_exits(self):
        fixture = fixtures.WordTimingTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        state = copy.deepcopy(core.process_state)
        for row in state.values():
            row['running'] = False
        arrived, release, finished = threading.Event(), threading.Event(), threading.Event()
        def render(*args):
            try:
                self.assertTrue(core._task_claims['word_timing']['cpu_only'])
                arrived.set()
                if not release.wait(3):
                    raise TimeoutError('timing fixture release missing')
                return fixture.render(*args)
            finally:
                finished.set()
        async def run():
            api = FastAPI(); api.include_router(editor.router)
            token = editor.get_snapshot_digest(editor.get_editor_book_identity(str(fixture.root)))
            body = {'expected_book_token': token, 'expected_uid': 'row', 'expected_audio_revision': 'take1'}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://fixture') as client:
                task = asyncio.create_task(client.post('/api/chunks/0/word_timing', json=body))
                try:
                    self.assertTrue(await asyncio.to_thread(arrived.wait, 1))
                    self.assertFalse(task.done())
                    self.assertEqual(400, (await client.post('/api/chunks/0/word_timing', json=body)).status_code)
                    self.assertTrue((fixture.root / '.task_ownership/task-cpu__word_timing.lock').is_file())
                    state['audio']['running'] = False
                    try:
                        with self.assertRaises(HTTPException) as blocked:
                            with core.ensure_book_switch_allowed():
                                self.fail('CPU word timing allowed a book switch')
                        self.assertEqual(409, blocked.exception.status_code)
                        self.assertIn('word_timing', blocked.exception.detail)
                    finally:
                        state['audio']['running'] = True
                    # The unrelated admitted GPU task still blocks another GPU task.
                    with self.assertRaises(HTTPException):
                        core.claim_gpu_task('voice_design')
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual('started', core._task_claims['word_timing']['phase'])
                    self.assertEqual(200, (await client.post('/api/word_timing/cancel')).status_code)
                finally:
                    release.set()
                    await asyncio.to_thread(finished.wait, 2)
                    if not task.done():
                        await task
            for _ in range(100):
                if 'word_timing' not in core._task_claims:
                    break
                await asyncio.sleep(.01)
            self.assertNotIn('word_timing', core._task_claims)
            self.assertFalse(state['word_timing']['running'])
            self.assertFalse(list(fixture.root.glob('.word-timing-*')))
        with ExitStack() as stack:
            for module, name, value in (
                    (core, 'process_state', state), (editor, 'process_state', state),
                    (core, '_task_claims', {}), (core, '_gpu_leases', {}),
                    (core, 'DATA_DIR', str(fixture.root)),
                    (editor, 'project_manager', fixture.pm),
                    (editor, 'SCRIPT_PATH', str(fixture.root / 'annotated_script.json'))):
                stack.enter_context(patch.object(module, name, value))
            acquire = stack.enter_context(patch.object(core, 'acquire_gpu_lock', side_effect=AssertionError('CPU timing acquired GPU')))
            stack.enter_context(patch('whisper_cpp_runtime.get_whisper_cpp_runtime', return_value={**fixture.runtime, 'available': True}))
            stack.enter_context(patch('editor_word_timing.render_word_timing', side_effect=render))
            state['audio']['running'] = True
            try:
                asyncio.run(run())
                acquire.assert_not_called()
            finally:
                for name, owner in list(core._task_claims.items()):
                    core.release_gpu_task_claim(name, owner['id'])

    def test_real_decoder_and_provider_command_keep_cpu_only_bounded_contract(self):
        fixture = fixtures.WordTimingTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        command = word_timing.run_timing_command
        seen = []
        def run(argv, cancel, *args):
            seen.append(argv)
            if argv[0] == 'ffmpeg':
                return command(argv, cancel, *args)
            self.assertIn('--no-gpu', argv)
            self.assertEqual('2', argv[argv.index('--threads') + 1])
            output = Path(argv[argv.index('--output-file') + 1] + '.json')
            output.write_text(json.dumps({'transcription': [
                {'text': 'Amber', 'offsets': {'from': 100, 'to': 500}},
                {'text': 'arrives', 'offsets': {'from': 700, 'to': 1200}}]}))
        with patch.object(word_timing, 'run_timing_command', side_effect=run):
            result = word_timing.render_word_timing(fixture.audio, fixture.runtime,
                'Amber arrives', lambda: False, fixture.root)
        self.assertEqual(2, len(seen))
        self.assertIn('--no-gpu', seen[1])
        self.assertEqual(2, len(result['spans']))
        self.assertEqual(2, result['duration'])


if __name__ == '__main__':
    unittest.main()
