"""Real PCM windows keep channels/frames and preserve old output on failure."""
import contextlib
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import audio_validation as av
from audio_generation_owner import ensure_audio_generation_owner
from utils import file_lock
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from audio_pcm_producer import PCMSourceStore, iter_raw_pcm, PCM_BLOCK_BYTES
from synthesis_windows import plan_synthesis_windows
from synthesis_audio import assemble_synthesis_windows, SynthesisJoinPolicy
from tts import ExportCancelled


class SynthesisAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = plan_synthesis_windows('One. Two.', max_chars=5)
        self.output = self.root/'joined.wav'

    def source(self, index, data, rate=24000, subtype='PCM_16'):
        path = self.root/f'window-{index}.wav'
        sf.write(path, np.asarray(data), rate, subtype=subtype)
        return index, path

    def test_preserved_frames_mixed_rates_mono_to_stereo_without_pad_or_flatten(self):
        first = self.source(0, np.full(1600, .1), 16000)
        stereo = np.column_stack((np.full(2400, .3), np.full(2400, -.2)))
        second = self.source(1, stereo)
        receipt = assemble_synthesis_windows(self.plan, [second, first], self.output)
        values, rate = sf.read(self.output, always_2d=True)
        self.assertEqual(24000, rate)
        self.assertEqual((4799, 2), values.shape)
        self.assertEqual(4799, receipt['output_frames'])
        np.testing.assert_allclose(values[:2399, 0], values[:2399, 1], atol=0)
        self.assertGreater(float(np.mean(values[2399:, 0])), .29)
        self.assertLess(float(np.mean(values[2399:, 1])), -.19)
        self.assertEqual('PCM_32', sf.info(self.output).subtype)

    def test_adaptive_gap_tops_up_and_trims_actual_quiet_frames_with_a_cap(self):
        first = self.source(0, np.r_[np.full(4800, .2), np.zeros(2400)])
        second = self.source(1, np.r_[np.zeros(1200), np.full(4800, .2)])
        for target, cap, expected, added, removed in ((200, 0, 14400, 1200, 0),
                                                   (50, 100, 10800, 0, 2400),
                                                   (50, 1, 13152, 0, 48)):
            with self.subTest(target=target, cap=cap):
                receipt = assemble_synthesis_windows(self.plan, [first, second], self.output,
                    policy=SynthesisJoinPolicy(sentence_gap_ms=target, max_edge_trim_ms=cap))
                self.assertEqual(expected, sf.info(self.output).frames)
                seam = receipt['seams'][0]
                self.assertEqual(added, seam['added_silence_frames'])
                self.assertEqual(removed, seam['left_quiet_trim'] + seam['right_quiet_trim'])
                self.assertTrue(receipt['outer_edges_preserved'])

    def test_continuation_crossfade_is_bounded_convex_and_exactly_accounted(self):
        plan = plan_synthesis_windows('One two.', max_chars=4)
        first = self.source(0, np.full(2400, .2)); second = self.source(1, np.full(2400, -.2))
        receipt = assemble_synthesis_windows(plan, [first, second], self.output,
            policy=SynthesisJoinPolicy(continuation_crossfade_ms=10))
        values, _ = sf.read(self.output)
        self.assertEqual(4560, len(values)); self.assertEqual(240, receipt['seams'][0]['crossfade_frames'])
        self.assertLessEqual(float(np.max(np.abs(values))), .201)
        self.assertLess(float(np.max(np.abs(np.diff(values)))), .002)

    def test_quiet_utterance_is_not_removed_and_outer_edges_remain_unchanged(self):
        first = self.source(0, np.r_[np.zeros(100), np.full(2400, .001), np.zeros(100)])
        second = self.source(1, np.r_[np.zeros(100), np.full(2400, .001), np.zeros(100)])
        receipt = assemble_synthesis_windows(self.plan, [first, second], self.output,
            policy=SynthesisJoinPolicy(sentence_gap_ms=0, max_edge_trim_ms=2000))
        values, _ = sf.read(self.output)
        self.assertEqual(5200, len(values))
        self.assertEqual(0, receipt['seams'][0]['left_quiet_trim'])
        self.assertEqual(0, receipt['seams'][0]['right_quiet_trim'])
        np.testing.assert_equal(values[:100], 0); np.testing.assert_equal(values[-100:], 0)

    def test_fades_have_correct_ends_and_do_not_change_the_frame_count(self):
        first = self.source(0, np.full(2400, .2)); second = self.source(1, np.full(2400, .2))
        receipt = assemble_synthesis_windows(self.plan, [first, second], self.output,
            policy=SynthesisJoinPolicy(fade_ms=5))
        values, _ = sf.read(self.output)
        self.assertEqual(4800, len(values)); self.assertEqual(4800, receipt['output_frames'])
        self.assertGreater(values[0], .19); self.assertGreater(values[-1], .19)
        self.assertEqual(0, values[2399]); self.assertEqual(0, values[2400])
        self.assertGreater(values[2400 + 120], .19)

    def test_missing_duplicate_reused_and_bad_plan_results_preserve_prior_bytes(self):
        first = self.source(0, np.full(2400, .2)); second = self.source(1, np.full(2400, .2))
        self.output.write_bytes(b'prior approved output')
        bad = [[first], [first, first], [first, (1, first[1])], [(True, first[1]), second]]
        for results in bad:
            with self.subTest(results=results), self.assertRaises(ValueError):
                assemble_synthesis_windows(self.plan, results, self.output)
            self.assertEqual(b'prior approved output', self.output.read_bytes())
        wrong = replace(self.plan, windows=tuple(reversed(self.plan.windows)))
        with self.assertRaises(ValueError):
            assemble_synthesis_windows(wrong, [first, second], self.output)
        self.assertEqual(b'prior approved output', self.output.read_bytes())

    def test_decode_range_failure_and_partial_pcm_write_preserve_prior_and_cleanup(self):
        first = self.source(0, np.full(2400, 1.1), subtype='FLOAT')
        second = self.source(1, np.full(2400, .2))
        self.output.write_bytes(b'prior')
        with self.assertRaisesRegex(RuntimeError, 'normalized PCM range'):
            assemble_synthesis_windows(self.plan, [first, second], self.output)
        self.assertEqual(b'prior', self.output.read_bytes())
        first = self.source(0, np.full(2400, .2))
        with patch('audio_pcm_producer.PCMSourceStore.save_pcm', side_effect=OSError('short disk write')):
            with self.assertRaisesRegex(OSError, 'short disk write'):
                assemble_synthesis_windows(self.plan, [first, second], self.output)
        self.assertEqual(b'prior', self.output.read_bytes())
        self.assertFalse(list(self.root.glob('.synthesis-join-*')))

    def test_cancellation_inside_final_publication_lock_preserves_prior_output(self):
        import audio_validation
        first = self.source(0, np.full(2400, .2)); second = self.source(1, np.full(2400, .2))
        self.output.write_bytes(b'prior'); cancelled = False
        original = audio_validation.ensure_audio_output
        @contextlib.contextmanager
        def cancel_at_publication(path):
            nonlocal cancelled
            with original(path):
                cancelled = True
                yield
        with patch.object(audio_validation, 'ensure_audio_output', cancel_at_publication):
            with self.assertRaises(ExportCancelled):
                assemble_synthesis_windows(self.plan, [first, second], self.output, cancel_check=lambda: cancelled)
        self.assertEqual(b'prior', self.output.read_bytes())
        self.assertFalse(list(self.root.glob('.synthesis-join-*')))

    def test_pcm_snapshot_rejects_bad_counts_and_recovers_high_water_without_changing_prior(self):
        layout = (1, 24000, 2)
        with PCMSourceStore(self.root, lambda: None) as store:
            prior = store.save_pcm([b'\x01\x00' * 2], layout, 2)
            for pieces, frames in (([b'\x00'], 1), ([b'\x00\x00'], 2),
                                   ([b'\x00\x00' * 3], 2), ([b'\x00' * (PCM_BLOCK_BYTES + 2)], 32769)):
                with self.subTest(frames=frames), self.assertRaises(ValueError):
                    store.save_pcm(pieces, layout, frames)
                self.assertEqual(4, store.byte_count)
                self.assertEqual(b'\x01\x00' * 2, b''.join(iter_raw_pcm(prior)))
            fresh = store.save_pcm([b'\x02\x00' * 2], layout, 2)
            self.assertEqual(8, store.byte_count)
            self.assertEqual(b'\x02\x00' * 2, b''.join(iter_raw_pcm(fresh)))

    def test_lazy_same_store_copy_keeps_both_committed_ranges_intact(self):
        with PCMSourceStore(self.root, lambda: None) as store:
            values = b'\x01\x00\x02\x00'
            prior = store.save_pcm([values], (1, 24000, 2), 2)
            copied = store.save_pcm(iter_raw_pcm(prior, 2), (1, 24000, 2), 2)
            self.assertEqual(8, store.byte_count)
            self.assertEqual(values, b''.join(iter_raw_pcm(prior)))
            self.assertEqual(values, b''.join(iter_raw_pcm(copied)))

    def test_unsupported_layout_is_rejected_before_any_full_pcm_validation(self):
        first = self.source(0, np.full((2400, 16), .2))
        second = self.source(1, np.full(2400, .2))
        self.output.write_bytes(b'prior')
        with patch('audio_validation.validate_generated_audio') as validate:
            with self.assertRaisesRegex(ValueError, 'mono/stereo'):
                assemble_synthesis_windows(self.plan, [first, second], self.output)
            validate.assert_not_called()
        self.assertEqual(b'prior', self.output.read_bytes())

    def test_header_and_total_promoted_size_are_admitted_before_full_pcm_validation(self):
        from types import SimpleNamespace
        first = self.source(0, np.full(2400, .2)); second = self.source(1, np.full(2400, .2))
        self.output.write_bytes(b'prior')
        for frames in (1200000000, 600000000):
            with self.subTest(frames=frames), patch('soundfile.info', return_value=SimpleNamespace(
                    channels=1, samplerate=24000, frames=frames)), \
                    patch('audio_validation.validate_generated_audio') as validate:
                with self.assertRaisesRegex(ValueError, 'RIFF'):
                    assemble_synthesis_windows(self.plan, [first, second], self.output)
                validate.assert_not_called()
            self.assertEqual(b'prior', self.output.read_bytes())

    def test_cancellation_inside_initial_and_final_validation_stops_before_publication(self):
        original = sf.SoundFile
        first = self.source(0, np.full(2400, .2)); second = self.source(1, np.full(2400, .2))
        for phase in ('initial', 'final'):
            with self.subTest(phase=phase):
                self.output.write_bytes(b'prior'); cancelled = False; reads = []
                class Reader:
                    def __init__(inner, file, *args, **kwargs):
                        inner.source = Path(file); inner.reader = original(file, *args, **kwargs)
                    def __getattr__(inner, name): return getattr(inner.reader, name)
                    def __enter__(inner): return inner
                    def __exit__(inner, *args): return inner.reader.__exit__(*args)
                    def read(inner, *args, **kwargs):
                        nonlocal cancelled
                        values = inner.reader.read(*args, **kwargs)
                        target = inner.source == first[1] if phase == 'initial' else inner.source.name == 'complete.wav'
                        if target and len(values):
                            reads.append(len(values)); cancelled = True
                        return values
                with patch('soundfile.SoundFile', Reader), self.assertRaises(ExportCancelled):
                    assemble_synthesis_windows(self.plan, [first, second], self.output, cancel_check=lambda: cancelled)
                self.assertEqual(1, len(reads)); self.assertEqual(b'prior', self.output.read_bytes())
                self.assertFalse(list(self.root.glob('.synthesis-join-*')))

    def test_owned_publication_admits_before_real_colliding_file_lock_without_owner_reentry(self):
        root = self.root
        output = root / 'private-parent.wav'
        output.write_bytes(b'prior-a')
        target = av.get_audio_lock_target(output)
        other = next(root / f'uid-slot-{i}.wav' for i in range(100000)
                     if av.get_audio_lock_target(root / f'uid-slot-{i}.wav') == target)
        other.write_bytes(b'prior-b')
        staged = root / 'b.stage'
        staged.write_bytes(b'completed-b')
        sources = []
        for index in range(2):
            path = root / f'window-{index}.wav'
            sf.write(path, np.full(2400, .2), 24000)
            sources.append((index, path))

        mutex = threading.Lock()
        ready = threading.Event()
        held = threading.Event()
        local = threading.local()
        events = []
        errors = []

        @contextlib.contextmanager
        def owner(*, allow_cancelled=False):
            # This closure is entered only at assembler final admission in this
            # fixture; the checker below models separate owner status checks.
            ready.set()
            assert held.wait(5)
            with mutex:
                local.admitted = True
                try:
                    yield
                finally:
                    local.admitted = False

        def checker():
            assert not getattr(local, 'admitted', False), (
                'locking predicate invoked under nonreentrant owner admission')
            with mutex:
                return False

        def attachment():
            try:
                assert ready.wait(5)
                with mutex:
                    held.set()
                    assert av.publish_audio_output(staged, other)
                    events.append('other_attachment_published')
            except BaseException as error:
                errors.append(str(error))

        @contextlib.contextmanager
        def short_lock(path):
            with file_lock(path, timeout=1):
                yield

        worker = threading.Thread(target=attachment)
        worker.start()
        try:
            with patch.object(av, 'file_lock', short_lock), ensure_audio_generation_owner(owner):
                receipt = assemble_synthesis_windows(
                    plan_synthesis_windows('One. Two.', max_chars=5),
                    sources, output, cancel_check=checker)
                events.append('window_parent_published')
        finally:
            worker.join(8)
        assert not worker.is_alive()
        assert not errors, errors
        assert sf.info(output).frames == 4800
        assert events == ['other_attachment_published', 'window_parent_published']
        assert other.read_bytes() == b'completed-b'
