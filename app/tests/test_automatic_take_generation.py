"""Owned Editor single/parallel/native routes exercise automatic retention."""
import asyncio
import copy
import unittest

from fastapi import BackgroundTasks
from tests import test_audio_generation_cancellation as cancellation_cases
from audio_take_history import AudioTakeHistory
from book_state_transaction import ensure_book_state
from take_retention import KEY, get_take_history_policy, set_take_history_policy
from routers import editor


class AutomaticTakeGenerationTests(unittest.TestCase):
    fixture = cancellation_cases.AudioGenerationCancellationTests.fixture
    write_audio = cancellation_cases.AudioGenerationCancellationTests.write_audio
    engine = cancellation_cases.AudioGenerationCancellationTests.engine
    start = cancellation_cases.AudioGenerationCancellationTests.start

    def exercise(self, cancel):
        for mode in ('single', 'parallel', 'native'):
            # Parallel rendering itself owns the active journal; setting its
            # admission field beforehand would impersonate an existing run.
            for journal in ((True,) if mode == 'parallel' else (False, True)):
                with self.subTest(journal=journal, mode=mode, cancel=cancel), \
                        self.fixture(2) as (root, manager, before, state):
                    originals = {row['uid']:(root/row['audio_path']).read_bytes() for row in before}
                    with ensure_book_state(root):
                        set_take_history_policy(manager, True, get_take_history_policy(root)['revision'])
                    if journal:
                        active = manager._get_status_journal_locked(); active.ensure_writable()
                        if mode != 'parallel':
                            manager._active_journal_identity = copy.deepcopy(active.book_identity)
                    def provider(paths):
                        if journal: self.assertIsNotNone(manager._active_journal_identity)
                        for path in paths: self.write_audio(path)
                        if cancel: self.assertEqual(editor._apply_audio_cancel(), {'status':'cancelling'})
                        return True
                    self.engine(manager, provider)
                    async def render():
                        tasks = BackgroundTasks()
                        await self.start(mode, tasks, [0] if mode == 'single' else [0, 1])
                        await tasks()
                    asyncio.run(render())
                    after = manager.load_chunks()
                    records = AudioTakeHistory(root).list()['records']
                    self.assertGreaterEqual(len(records), 1)
                    for record in records:
                        self.assertEqual(record['state'], 'ready')
                        self.assertEqual((root/f"take_history/{record['version']}.wav").read_bytes(), originals[record['uid']])
                        source = next(row for row in before if row['uid'] == record['uid'])
                        self.assertEqual(record['text'], source['text'])
                        self.assertEqual(record['audio_revision'], source['audio_revision'])
                    rendered = 1 if mode == 'single' else 2
                    for index, row in enumerate(after):
                        if cancel or index >= rendered:
                            public = {key:value for key,value in row.items() if key != KEY}
                            self.assertEqual(public, before[index])
                            self.assertEqual((root/row['audio_path']).read_bytes(), originals[row['uid']])
                        else:
                            self.assertEqual(row['status'], 'done'); self.assertIsNone(row[KEY])
                            self.assertNotEqual(row['audio_revision'], before[index]['audio_revision'])
                            self.assertNotEqual((root/row['audio_path']).read_bytes(), originals[row['uid']])
                    self.assertFalse(state['audio']['running'])

    def test_success_archives_prior_bytes_in_all_owned_render_modes(self):
        self.exercise(False)

    def test_cancel_keeps_selected_audio_and_verified_archive_holds(self):
        self.exercise(True)
