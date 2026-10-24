"""Real retained files, failed render cleanup, and shared quota contention."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import sound_design_staging as staging


class SoundRenderStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_retained_candidates_reduce_next_allowance_and_are_not_deleted(self):
        with staging.ensure_sound_render_stage(self.root, max_bytes=20) as (first, available):
            self.assertEqual(20, available)
            (first / 'candidate.pcm').write_bytes(b'a' * 12)
        with staging.ensure_sound_render_stage(self.root, max_bytes=20) as (second, available):
            self.assertEqual(8, available)
            (second / 'candidate.pcm').write_bytes(b'b' * 8)
        with self.assertRaisesRegex(ValueError, 'full'):
            with staging.ensure_sound_render_stage(self.root, max_bytes=20):
                self.fail('Unbounded render admitted')
        self.assertEqual(b'a' * 12, (first / 'candidate.pcm').read_bytes())
        self.assertEqual(b'b' * 8, (second / 'candidate.pcm').read_bytes())

    def test_failure_cleans_only_owned_nested_stage(self):
        with staging.ensure_sound_render_stage(self.root) as (retained, _):
            (retained / 'keep').write_bytes(b'recoverable')
        with self.assertRaisesRegex(RuntimeError, 'render failed'):
            with staging.ensure_sound_render_stage(self.root) as (failed, _):
                (failed / 'nested').mkdir()
                (failed / 'nested' / 'partial.pcm').write_bytes(b'partial')
                raise RuntimeError('render failed')
        self.assertFalse(failed.exists())
        self.assertEqual(b'recoverable', (retained / 'keep').read_bytes())

    def test_over_budget_output_cannot_be_retained(self):
        with self.assertRaisesRegex(ValueError, 'reserved allowance'):
            with staging.ensure_sound_render_stage(self.root, max_bytes=2) as (stage, _):
                (stage / 'candidate').write_bytes(b'123')
        self.assertFalse(stage.exists())

    def test_unsafe_old_stages_refuse_without_touching_target(self):
        outside = self.root / 'source'; outside.write_bytes(b'original')
        directory = self.root / staging.DIRECTORY; directory.mkdir()
        stage = directory / 'render-retained'; stage.mkdir()
        link = stage / 'alias'
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            with staging.ensure_sound_render_stage(self.root): self.fail('Alias admitted')
        self.assertTrue(link.is_symlink())
        self.assertEqual(b'original', outside.read_bytes())

    def test_scan_and_stage_count_are_bounded(self):
        directory = self.root / staging.DIRECTORY; directory.mkdir()
        for index in range(staging.MAX_STAGES):
            (directory / ('render-' + str(index))).mkdir()
        with self.assertRaisesRegex(ValueError, 'stages are full'):
            with staging.ensure_sound_render_stage(self.root): self.fail('Extra stage admitted')
        stage = directory / 'render-0'
        for index in range(3): (stage / str(index)).touch()
        with patch.object(staging, 'MAX_ENTRIES', 2), self.assertRaisesRegex(ValueError, 'Too many'):
            staging.get_sound_render_stage_files(stage, self.root)

    def test_second_writer_observes_first_committed_size(self):
        entered = threading.Event(); started = threading.Event(); results = []; failures = []
        def worker():
            started.set()
            try:
                with staging.ensure_sound_render_stage(self.root, max_bytes=20) as (_, available):
                    results.append(available); entered.set()
            except BaseException as error: failures.append(error)
        with staging.ensure_sound_render_stage(self.root, max_bytes=20) as (stage, _):
            thread = threading.Thread(target=worker); thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(entered.wait(.15))
            (stage / 'candidate').write_bytes(b'x' * 9)
        thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual([], failures)
        self.assertEqual([11], results)

    def test_process_death_retains_charged_candidate_and_releases_lease(self):
        import subprocess
        import sys
        script = """import os, sys
from sound_design_staging import ensure_sound_render_stage
with ensure_sound_render_stage(sys.argv[1], max_bytes=20) as (stage, available):
    (stage / 'partial.pcm').write_bytes(b'x' * 7)
    os._exit(23)
"""
        result = subprocess.run([sys.executable, '-c', script, str(self.root)],
                                capture_output=True, timeout=10)
        self.assertEqual(23, result.returncode, result.stderr.decode())
        with staging.ensure_sound_render_stage(self.root, max_bytes=20) as (_, available):
            self.assertEqual(13, available)
        retained = list((self.root / staging.DIRECTORY).glob('render-*/partial.pcm'))
        self.assertEqual(1, len(retained))
        self.assertEqual(b'x' * 7, retained[0].read_bytes())

    def test_cancellation_after_render_cleans_only_current_candidate(self):
        cancelled = [False]
        from audio_generation_owner import AudioGenerationCancelled
        with self.assertRaises(AudioGenerationCancelled):
            with staging.ensure_sound_render_stage(self.root, cancel_check=lambda: cancelled[0]) as (stage, _):
                (stage / 'candidate').write_bytes(b'partial')
                cancelled[0] = True
        self.assertFalse(stage.exists())

    def test_full_store_can_be_inspected_without_admitting_new_render(self):
        with staging.ensure_sound_render_stage(self.root,max_bytes=4) as (stage,_):
            (stage/'candidate').write_bytes(b'full')
        with staging.ensure_sound_render_quota(self.root,max_bytes=4) as (directory,available,count):
            self.assertEqual(self.root/staging.DIRECTORY,directory)
            self.assertEqual((0,1),(available,count))
        with self.assertRaisesRegex(ValueError,'full'):
            with staging.ensure_sound_render_stage(self.root,max_bytes=4):
                self.fail('Full quota admitted a render')
        self.assertEqual(b'full',(stage/'candidate').read_bytes())
