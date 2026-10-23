"""Real immutable assets and hand-computed sample/frame mixing oracles."""
from contextlib import contextmanager
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from audio_generation_owner import AudioGenerationCancelled
from audio_pcm_producer import PCMSourceStore
from sound_design_assets import SoundDesignAssetStore
from sound_design_cues import get_sound_design_cue_version
import sound_design_mix as mixer


BOOK = dict(book_id='synthetic-book', source_sha256='a' * 64, script_sha256='b' * 64)
SELECTION = dict(chunk_uid='synthetic-chunk', generation_sha256='c' * 64,
                 audio_sha256='d' * 64, audio_revision='e' * 32)


class SoundDesignMixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.book_root, self.staging = self.root / 'book', self.root / 'private'
        self.book_root.mkdir(); self.staging.mkdir()
        self.assets = SoundDesignAssetStore(self.book_root)

    def asset(self, values, *, rate=24000):
        path = self.book_root / 'synthetic.wav'
        sf.write(path, np.asarray(values, dtype=np.int32), rate, subtype='PCM_32')
        original = path.read_bytes()
        facts = self.assets.import_asset(path.name)
        self.assertEqual(original, path.read_bytes())
        return facts

    def cue(self, asset, *, start=0, frames=None, crossfade=None, gain=0, duck=0,
            fade_in=0, fade_out=0, identity='synthetic-cue', rate=24000):
        recipe = dict(book=copy.deepcopy(BOOK), selection=copy.deepcopy(SELECTION),
            boundary='start', offset_frames=start, frame_rate=rate,
            kind='ambience' if crossfade is not None else 'effect', asset=asset,
            duration=dict(mode='native' if frames is None else 'fixed', frames=frames,
                          loop_crossfade_frames=crossfade),
            mix=dict(gain_db=gain, duck_db=duck, fade_in_frames=fade_in, fade_out_frames=fade_out))
        return get_sound_design_cue_version(identity, recipe)

    @contextmanager
    def inputs(self, values, *, width=4, rate=24000):
        values = np.asarray(values)
        channels = 1 if values.ndim == 1 else values.shape[1]
        raw = values.astype('i1' if width == 1 else '<i' + str(width)).tobytes()
        with PCMSourceStore(self.staging, lambda: None) as store, tempfile.NamedTemporaryFile(dir=self.staging) as output:
            source = store.save_pcm((raw[i:i+65536] for i in range(0, len(raw), 65536)),
                                    (channels, rate, width), len(values))
            plan = dict(frames=len(values), frame_rate=rate, channels=channels, sample_width=width,
                        source_frame_ranges=[[0, len(values)]], intentional_pause_frames=[])
            yield source, output, plan, raw

    def write(self, source, output, plan, versions, **options):
        return mixer.write_sound_design_pcm(source, output, versions, BOOK, [SELECTION], plan,
            self.assets, self.staging, check_active=options.pop('check_active', lambda: None), **options)

    def render(self, values, versions, *, width=4, rate=24000, **options):
        with self.inputs(values, width=width, rate=rate) as (source, output, plan, raw):
            report = self.write(source, output, plan, versions, **options)
            self.assertFalse(output.closed)
            output.seek(0); data = output.read()
            self.assertEqual(hashlib.sha256(data).hexdigest(), report['sha256'])
            self.assertEqual(len(data), report['bytes'])
            source.store.stream.seek(source.offset)
            self.assertEqual(raw, source.store.stream.read(len(raw)))
            return data, report

    def test_dry_identity_full_scope_and_submillisecond_scoped_tail_all_widths(self):
        for width in (1, 2, 4):
            limit = 1 << (8 * width - 1)
            values = np.resize(np.array([-limit, -1, 0, 1, limit - 1]), 24001)
            raw = values.astype('i1' if width == 1 else '<i'+str(width)).tobytes()
            for scope in (None, [23999, 24001]):
                with self.subTest(width=width, scope=scope):
                    data, facts = self.render(values, [], width=width, scope=scope, block_frames=7)
                    expected = raw if scope is None else raw[23999*width:]
                    self.assertEqual(expected, data)
                    self.assertEqual(hashlib.sha256(expected).hexdigest(), facts['dry_scope_sha256'])

    def test_impulse_cue_uses_exact_frame_offset_and_preserves_dry_elsewhere(self):
        facts = self.asset([2**28, 0, 0])
        dry = np.arange(24001, dtype=np.int32)
        data, _ = self.render(dry, [self.cue(facts, start=23998)], block_frames=1)
        actual = np.frombuffer(data, dtype='<i4'); expected = dry.copy(); expected[23998] += 2**28
        np.testing.assert_array_equal(actual, expected)

    def test_mono_asset_promotes_to_reviewed_stereo_clock_with_exact_frames(self):
        facts = self.asset([2**28, 2**27, 0], rate=16000)
        data, report = self.render(np.zeros((12, 2), dtype=np.int32), [self.cue(facts, start=3)], block_frames=2)
        actual = np.frombuffer(data, dtype='<i4').reshape(-1, 2)
        # Fresh audioop interpolation: N=3 at16k ->4frames at24k.
        expected = np.zeros((12, 2), dtype=np.int32)
        expected[3:7] = np.array([2**28, 178956970, 89478485, 0])[:, None]
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual((12, 2, 24000), (report['frames'], report['channels'], report['frame_rate']))

    def test_overlap_ducks_only_dry_with_strongest_explicit_attenuation(self):
        silent = self.asset([0] * 6)
        cues = [self.cue(silent, start=2, duck=-6, identity='one'),
                self.cue(silent, start=4, duck=-12, identity='two')]
        dry = np.full(13, 2**28, dtype=np.int32)
        data, _ = self.render(dry, cues, block_frames=3)
        expected = dry.copy(); expected[2:4] = np.rint(2**28 * 10**(-6/20)); expected[4:10] = np.rint(2**28 * 10**(-12/20))
        np.testing.assert_array_equal(np.frombuffer(data, dtype='<i4'), expected)

    def test_fade_endpoints_zero_one_and_touching_ranges(self):
        facts = self.asset([2**28] * 6)
        cases = [(0, 0, [1]*6), (1, 0, [0,1,1,1,1,1]),
                 (0, 1, [1,1,1,1,1,0]), (3, 3, [0,.5,1,1,.5,0])]
        for fade_in, fade_out, weights in cases:
            with self.subTest(fade_in=fade_in, fade_out=fade_out):
                cue = self.cue(facts, start=2, fade_in=fade_in, fade_out=fade_out)
                data, _ = self.render(np.zeros(10, dtype=np.int32), [cue], block_frames=2)
                expected = np.zeros(10, dtype=np.int32); expected[2:8] = np.array(weights) * 2**28
                np.testing.assert_array_equal(np.frombuffer(data, dtype='<i4'), expected)

    def test_loops_match_hand_oracles_at_nonperiod_aligned_cue_start(self):
        scale = 2**26
        facts = self.asset(np.array([0,1,2,3]) * scale)
        cases = [(1, [0,1,2,1.5,1,2,1.5,1]), (2, [0,1,4/3,5/3,4/3,5/3]),
                 (3, [0,.75,11/9,1.5,1.5,1.5])]
        for crossfade, oracle in cases:
            outputs = []
            for block in (1, 2, 3, 4, 5, 8192):
                with self.subTest(crossfade=crossfade, block=block):
                    cue = self.cue(facts, start=5, frames=len(oracle), crossfade=crossfade)
                    data, _ = self.render(np.zeros(17, dtype=np.int32), [cue], block_frames=block)
                    expected = np.zeros(17, dtype=np.int32); expected[5:5+len(oracle)] = np.rint(np.array(oracle)*scale)
                    np.testing.assert_array_equal(np.frombuffer(data, dtype='<i4'), expected)
                    outputs.append(data)
            self.assertEqual(1, len(set(outputs)))

    def test_constant_ambience_multi_cycle_overlap_is_block_invariant(self):
        facts = self.asset([2**27] * 7)
        cue = self.cue(facts, start=3, frames=100, crossfade=6)
        outputs = [self.render(np.zeros(107, dtype=np.int32), [cue], block_frames=b)[0]
                   for b in (1, 5, 7, 13, 8192)]
        expected = np.zeros(107, dtype=np.int32); expected[3:103] = 2**27
        for data in outputs:np.testing.assert_array_equal(np.frombuffer(data, dtype='<i4'), expected)

    def test_true_voice_cap_rejected_before_conversion_but_loose_bound_is_not(self):
        loop = self.asset([2**24] * 10)
        effect = self.asset([0] * 6)
        cues = [self.cue(loop, frames=11, crossfade=4, identity='loop'), self.cue(effect, identity='effect')]
        # Conservative span count is3; actual count never exceeds2.
        self.render(np.zeros(12, dtype=np.int32), cues, max_source_voices=2, block_frames=2)
        cues[1] = self.cue(loop, frames=11, crossfade=4, identity='other-loop')
        with self.inputs(np.zeros(12, dtype=np.int32)) as (source, output, plan, _):
            with patch.object(mixer, 'iter_converted_pcm') as convert:
                with self.assertRaisesRegex(ValueError, 'voice limit'):
                    self.write(source, output, plan, cues, max_source_voices=2)
                convert.assert_not_called(); self.assertEqual(0, output.tell())

    def test_positive_and_negative_saturation_rejected_before_integer_cast(self):
        for value in (2**30, -2**30):
            with self.subTest(value=value):
                facts = self.asset([value] * 4)
                with self.assertRaisesRegex(ValueError, 'clips'):
                    self.render(np.full(8, value, dtype=np.int32), [self.cue(facts, gain=6)])

    def test_exact_integer_endpoints_remain_valid(self):
        for value in (-2**31, 2**31-1):
            with self.subTest(value=value):
                facts = self.asset([value] * 2)
                data, _ = self.render(np.zeros(4, dtype=np.int32), [self.cue(facts)])
                np.testing.assert_array_equal(np.frombuffer(data, dtype='<i4'), [value,value,0,0])

    def test_fixed_crop_and_duplicate_asset_charge_only_required_prefix_once(self):
        facts = self.asset(np.arange(100, dtype=np.int32))
        cues = [self.cue(facts, start=0, frames=2, identity='one'),
                self.cue(facts, start=5, frames=3, identity='two')]
        data, report = self.render(np.zeros(10, dtype=np.int32), cues, max_staged_bytes=40+40+12)
        self.assertEqual(92, report['logical_staged_bytes'])
        np.testing.assert_array_equal(np.frombuffer(data, dtype='<i4'), [0,1,0,0,0,0,1,2,0,0])
        with self.assertRaisesRegex(ValueError, 'staging allowance'):
            self.render(np.zeros(10, dtype=np.int32), cues, max_staged_bytes=91)

    def test_stale_scope_and_invalid_limits_fail_before_conversion_or_output(self):
        facts = self.asset([0] * 4); cue = self.cue(facts, start=2)
        stale = copy.deepcopy(cue['recipe']); stale['book']['source_sha256'] = 'f' * 64
        cases = [([get_sound_design_cue_version('stale', stale)], {}),
                 ([cue], {'scope':[3,8]}), ([cue], {'block_frames':True}),
                 ([cue], {'max_staged_bytes':True}), ([cue], {'timeout':float('nan')}),
                 ([cue], {'timeout':10**1000})]
        for versions, options in cases:
            with self.subTest(options=options), self.inputs(np.zeros(10, dtype=np.int32)) as (source, output, plan, _):
                with patch.object(mixer, 'iter_converted_pcm') as convert:
                    with self.assertRaises(ValueError):self.write(source, output, plan, versions, **options)
                    convert.assert_not_called(); self.assertEqual(0, output.tell())

    def test_conflicting_canonical_descriptors_and_actual_mismatch_fail_closed(self):
        facts = self.asset([0] * 4); changed = {**facts, 'sha256':'f'*64}
        cases = [[self.cue(facts, identity='one'), self.cue(changed, identity='two')], [self.cue(changed)]]
        for cues in cases:
            with self.subTest(cues=len(cues)), self.inputs(np.zeros(10, dtype=np.int32)) as (source, output, plan, _):
                with patch.object(mixer, 'iter_converted_pcm') as convert:
                    with self.assertRaises(ValueError):self.write(source, output, plan, cues)
                    convert.assert_not_called(); self.assertEqual(0, output.tell())

    def test_private_stage_cannot_pollute_asset_inventory_and_existing_output_is_preserved(self):
        facts = self.asset([0] * 4)
        with self.inputs(np.zeros(10, dtype=np.int32)) as (source, output, plan, _):
            with self.assertRaisesRegex(ValueError, 'separate canonical'):
                mixer.write_sound_design_pcm(source, output, [self.cue(facts)], BOOK, [SELECTION], plan,
                    self.assets, self.assets.directory, check_active=lambda: None)
            output.write(b'preserve prior bytes'); output.seek(0)
            with self.assertRaises(ValueError):self.write(source, output, plan, [])
            output.seek(0); self.assertEqual(b'preserve prior bytes', output.read())

    def test_fd_backed_output_and_forged_output_name_are_refused_without_writes(self):
        self.asset([0,1,0])
        with self.inputs(np.arange(10, dtype=np.int32)) as (source, output, plan, _):
            for directory in (self.staging, self.assets.directory):
                path = directory / 'unregistered.pcm'
                with self.subTest(directory=directory), os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600), 'w+b') as unsafe:
                    try:
                        with self.assertRaisesRegex(ValueError, 'named private'):
                            self.write(source, unsafe, plan, [])
                        self.assertEqual(0, unsafe.tell()); self.assertEqual(0, path.stat().st_size)
                    finally:
                        # Close before unlinking for Windows compatibility.
                        unsafe.close(); path.unlink()
            class ForgedName:
                name = output.name
                def __init__(self, stream):self.stream = stream
                def __getattr__(self, key):return getattr(self.stream, key)
            with tempfile.NamedTemporaryFile(dir=self.staging) as different:
                with self.assertRaises(ValueError):self.write(source, ForgedName(different), plan, [])
                self.assertEqual(0, different.tell())

    def test_fictional_geometry_refused_before_exact_voice_preflight(self):
        facts = self.asset([0,1,0])
        fictional = {**facts, 'frames':1024}
        cues = [self.cue(fictional, frames=10240, crossfade=1016, identity=identity)
                for identity in ('one','two')]
        with self.inputs(np.zeros(10240, dtype=np.int32)) as (source, output, plan, _):
            with patch.object(mixer, '_ensure_voice_limit') as preflight, \
                    patch.object(mixer, 'iter_converted_pcm') as convert:
                with self.assertRaisesRegex(ValueError, 'facts changed'):
                    self.write(source, output, plan, cues)
                preflight.assert_not_called(); convert.assert_not_called()
            self.assertEqual(0, output.tell())

    def test_closed_dry_store_and_expired_deadline_produce_no_output(self):
        with self.inputs(np.arange(10, dtype=np.int32)) as (source, output, plan, _):
            with patch.object(mixer.time, 'monotonic', side_effect=[0.,2.]):
                with self.assertRaises(TimeoutError):self.write(source, output, plan, [], timeout=1)
            self.assertEqual(0, output.tell())
            source.store.close()
            with self.assertRaisesRegex(ValueError, 'store is unavailable'):
                self.write(source, output, plan, [])
            self.assertEqual(0, output.tell())

    def test_deep_overlap_stereo_impulse_uses_normalized_cycle_weights(self):
        # At local frame4: raw cycle weights1/4,9/16,1/4; sum17/16.
        # Only the first cycle contains the impulse: normalized contribution4/17.
        amplitude = 17 * 2**20
        facts = self.asset([[0,0]]*4 + [[amplitude,-amplitude]])
        cue = self.cue(facts, start=3, frames=19, crossfade=3)
        expected = None
        for block in (1,2,5,8192):
            with self.subTest(block=block):
                data, _ = self.render(np.zeros((25,2),dtype=np.int32), [cue], block_frames=block)
                samples = np.frombuffer(data,dtype='<i4').reshape(-1,2)
                np.testing.assert_array_equal(samples[7], [4*2**20,-4*2**20])
                if expected is None:expected = data
                self.assertEqual(expected,data)

    def test_multichunk_resampled_stereo_loop_matches_whole_conversion(self):
        import audioop
        values = np.column_stack((np.arange(50001), -np.arange(50001))).astype('<i4')
        facts = self.asset(values, rate=44100)
        whole = audioop.ratecv(values.tobytes(), 4, 2, 44100, 24000, None)[0]
        native = len(whole)//8
        cue = self.cue(facts, frames=native+9, crossfade=3)
        data, _ = self.render(np.zeros((native+9,2),dtype=np.int32), [cue], block_frames=8192)
        # Before the first crossfade, a loop is exactly the single converted asset.
        self.assertEqual(whole[:(native-3)*8], data[:(native-3)*8])
        one, _ = self.render(np.zeros((native+9,2),dtype=np.int32), [cue], block_frames=257)
        self.assertEqual(data,one)

    def test_cancellation_during_conversion_and_render_preserves_originals(self):
        facts = self.asset([2**24] * 100)
        canonical = self.assets.directory / (facts['asset_id']+'.pcm')
        before = canonical.read_bytes(); cue = self.cue(facts)
        for stage in ('convert','render'):
            with self.subTest(stage=stage), self.inputs(np.zeros(120, dtype=np.int32)) as (source, output, plan, raw):
                def cancelled(*args, **kwargs):raise AudioGenerationCancelled('synthetic cancellation')
                target = 'iter_converted_pcm' if stage=='convert' else '_read_frames'
                with patch.object(mixer, target, side_effect=cancelled):
                    with self.assertRaises(AudioGenerationCancelled):self.write(source, output, plan, [cue])
                self.assertFalse(output.closed); self.assertFalse(source.store.stream.closed)
                source.store.stream.seek(0); self.assertEqual(raw, source.store.stream.read())
                self.assertEqual(before, canonical.read_bytes())

    def test_short_writes_and_silent_output_rewrites_cannot_return_success(self):
        class FaultyOutput:
            def __init__(self, stream, silent):self.stream, self.silent = stream, silent
            def __getattr__(self, key):return getattr(self.stream, key)
            def write(self, value):
                if self.silent:return self.stream.write(bytes(len(value)))
                self.stream.write(value[:-1]); return len(value)-1
        for silent in (False, True):
            with self.subTest(silent=silent), self.inputs(np.arange(10, dtype=np.int32)) as (source, output, plan, _):
                with self.assertRaises((ValueError, OSError)):
                    self.write(source, FaultyOutput(output, silent), plan, [], block_frames=3)

    def test_truncated_dry_snapshot_refused_before_output(self):
        with self.inputs(np.arange(10, dtype=np.int32)) as (source, output, plan, _):
            source.store.stream.truncate(4)
            with self.assertRaises(ValueError):self.write(source, output, plan, [])
            self.assertEqual(0, output.tell())

    def test_same_observed_dry_version_does_not_hide_changed_content(self):
        with self.inputs(np.arange(10, dtype=np.int32)) as (source, output, plan, _):
            original = mixer._read_frames
            changed = False
            def read_and_change(selected, *args):
                nonlocal changed
                data = original(selected, *args)
                if selected is source and not changed:
                    changed = True
                    selected.store.stream.seek(0); selected.store.stream.write(b'xxxx'); selected.store.stream.flush()
                return data
            # Simulate a filesystem whose entire observed version remains the same.
            with patch.object(mixer, '_version', return_value=('same observed version',)), \
                    patch.object(mixer, '_read_frames', side_effect=read_and_change):
                with self.assertRaisesRegex(ValueError, 'bytes changed'):
                    self.write(source, output, plan, [], block_frames=2)
            self.assertTrue(changed)

    def test_consumed_asset_bytes_are_hashed_even_after_path_payload_is_restored(self):
        facts = self.asset(np.arange(100, dtype=np.int32)); cue = self.cue(facts, frames=3)
        path = self.assets.directory / (facts['asset_id'] + '.pcm'); original = path.read_bytes()
        convert = mixer.iter_converted_pcm
        def consume_changed_then_restore(*args):
            changed = bytearray(original); changed[0] ^= 1; path.write_bytes(changed)
            try:
                yield from convert(*args)
            finally:
                path.write_bytes(original)
        with self.inputs(np.zeros(10, dtype=np.int32)) as (source, output, plan, _):
            with patch.object(mixer, '_version', return_value=('same observed version',)), \
                    patch('sound_design_assets._version', return_value=('same observed version',)), \
                    patch.object(mixer, 'iter_converted_pcm', side_effect=consume_changed_then_restore):
                with self.assertRaisesRegex(ValueError, 'changed while converting'):
                    self.write(source, output, plan, [cue])
            self.assertEqual(0, output.tell())
            self.assertEqual(original, path.read_bytes())

    def test_late_cancellation_after_render_returns_no_success_and_keeps_borrowed_files(self):
        with self.inputs(np.arange(10, dtype=np.int32)) as (source, output, plan, raw):
            read_hash = mixer.read_bounded_audio_hash
            def cancel_at_readback(stream, *args):
                if stream is output:
                    raise AudioGenerationCancelled('cancel during final verification')
                return read_hash(stream, *args)
            with patch.object(mixer, 'read_bounded_audio_hash', side_effect=cancel_at_readback):
                with self.assertRaises(AudioGenerationCancelled):self.write(source, output, plan, [])
            self.assertFalse(output.closed); self.assertFalse(source.store.stream.closed)
            source.store.stream.seek(0); self.assertEqual(raw, source.store.stream.read())

    def test_unselected_dry_store_bytes_still_count_toward_task_budget(self):
        with self.inputs(np.arange(10, dtype=np.int32)) as (source, output, plan, raw):
            source.store.save_pcm([raw], (1,24000,4),10)
            with self.assertRaisesRegex(ValueError, 'staging allowance'):
                self.write(source, output, plan, [], max_staged_bytes=119)
            self.assertEqual(0, output.tell())

    def test_sub_lsb_negative_overload_is_refused_before_rounding_can_hide_it(self):
        facts = self.asset([-1])
        with self.assertRaisesRegex(ValueError, 'clips'):
            self.render(np.array([-2**31,0],dtype=np.int32), [self.cue(facts, gain=-96)])
