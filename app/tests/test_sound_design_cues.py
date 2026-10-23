"""Cue identity, staleness and real PCM frame controls; no synthesis or GPU."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydub import AudioSegment

from audio_export_plan import get_combined_pcm_plan
from audio_pcm_producer import get_pcm_source_shape, iter_converted_pcm
from audio_streaming import write_combined_pcm
from sound_design_cues import (get_sound_design_cue_plan, get_sound_design_cue_version,
                               validate_sound_design_cue_version, MAX_CUES)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


BOOK = dict(book_id='saved-book-a', source_sha256=digest('source'), script_sha256=digest('script'))
SELECTION = dict(chunk_uid='chunk-a', generation_sha256=digest('same spoken words'),
                 audio_sha256=digest('selected take'), audio_revision='a' * 32)


def recipe():
    return dict(book=copy.deepcopy(BOOK), selection=copy.deepcopy(SELECTION), boundary='start',
                offset_frames=0, frame_rate=24000, kind='effect',
                asset=dict(asset_id='explicit-take-two', sha256=digest('asset two'), frames=2400,
                           frame_rate=24000, channels=1, sample_width=2),
                duration=dict(mode='native', frames=None, loop_crossfade_frames=None),
                mix=dict(gain_db=-12, duck_db=-6, fade_in_frames=0, fade_out_frames=0))


def pcm_plan(frames=24001):
    return dict(frames=frames, frame_rate=24000, channels=1, sample_width=2,
                source_frame_ranges=[[0, frames]], intentional_pause_frames=[])


class SoundDesignCueTests(unittest.TestCase):
    def project(self, value=None, **kwargs):
        cue = get_sound_design_cue_version('cue-a', recipe() if value is None else value)
        return get_sound_design_cue_plan([cue], kwargs.pop('book', BOOK),
            kwargs.pop('dialogue', [SELECTION]), kwargs.pop('plan', pcm_plan()), **kwargs)

    def test_revision_detaches_inputs_and_explicit_parent_changes_version(self):
        value = recipe(); original = copy.deepcopy(value)
        first = get_sound_design_cue_version('cue-a', value)
        value['mix']['gain_db'] = -20
        self.assertEqual(original, first['recipe'])
        second = get_sound_design_cue_version('cue-a', value, first['version'])
        self.assertEqual(first['version'], second['parent_version'])
        self.assertNotEqual(first['version'], second['version'])
        validate_sound_design_cue_version(first); validate_sound_design_cue_version(second)

    def test_forged_self_consistent_digest_cannot_bypass_semantic_validation(self):
        cue = get_sound_design_cue_version('cue-a', recipe()); cue['recipe']['offset_frames'] = True
        seed = {key: value for key, value in cue.items() if key != 'version'}
        cue['version'] = hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True,
                                                  separators=(',', ':')).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'offset'):
            validate_sound_design_cue_version(cue)

    def test_modified_recipe_and_unknown_keys_are_rejected(self):
        cue = get_sound_design_cue_version('cue-a', recipe()); cue['recipe']['mix']['gain_db'] = -2
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            validate_sound_design_cue_version(cue)
        cue['unexpected'] = 1
        with self.assertRaisesRegex(ValueError, 'structure'):
            validate_sound_design_cue_version(cue)

    def test_invalid_numbers_unicode_and_nested_structures_fail_before_projection(self):
        cases = [('offset_frames', True), ('offset_frames', 1.5), ('frame_rate', 0),
                 ('frame_rate', 192001), ('kind', []), ('boundary', {'start': 1})]
        for key, bad in cases:
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                value = recipe(); value[key] = bad; self.project(value)
        for bad in (True, float('nan'), float('inf'), -(10**10000)):
            with self.subTest(kind=type(bad)), self.assertRaises(ValueError):
                value = recipe(); value['mix']['gain_db'] = bad; self.project(value)
        for key in ('book_id',):
            with self.assertRaises(ValueError):
                value = recipe(); value['book'][key] = '\ud800'; self.project(value)
        for bad in (2, True, '', '0' * 129, '\ud800'):
            with self.subTest(revision=bad), self.assertRaises(ValueError):
                value = recipe(); value['selection']['audio_revision'] = bad; self.project(value)

    def test_native_end_anchor_preserves_sub_millisecond_final_sample(self):
        value = recipe(); value.update(boundary='end', offset_frames=-1); value['asset']['frames'] = 1
        result = self.project(value)
        self.assertTrue(result['can_project'])
        self.assertEqual((24000, 24001), (result['cues'][0]['start_frame'], result['cues'][0]['end_frame']))

    def test_projection_matches_real_mixed_rate_pcm_writer_with_fractional_gaps(self):
        segments = [AudioSegment(data=b'\x00\x20' * frames, frame_rate=rate, sample_width=2, channels=1)
                    for rate, frames in ((8000, 1001), (24000, 2401), (16000, 1703))]
        dialogue = [dict(SELECTION, chunk_uid=f'uid-{i}', audio_sha256=hashlib.sha256(s.raw_data).hexdigest())
                    for i, s in enumerate(segments)]
        plan = get_combined_pcm_plan([get_pcm_source_shape(s) for s in segments], [17, 23])
        before = copy.deepcopy((dialogue, plan)); dry = [s.raw_data for s in segments]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dry.pcm'
            geometry = write_combined_pcm(segments, ['A'] * 3, path, 17, 23,
                                          pause_overrides=[17, 23, None])
            original = path.read_bytes()
            value = recipe(); value['selection'] = dialogue[2]; value['asset']['frames'] = 1
            result = self.project(value, dialogue=dialogue, plan=plan)
            self.assertEqual(plan['source_frame_ranges'][2][0], result['cues'][0]['start_frame'])
            self.assertEqual(len(original) // 2, result['frames'])
            self.assertEqual(geometry['frames'], result['frames'])
            self.assertEqual(original, path.read_bytes())
        self.assertEqual(before, (dialogue, plan)); self.assertEqual(dry, [s.raw_data for s in segments])

    def test_selected_asset_native_length_not_an_unselected_candidate(self):
        first = self.project(); value = recipe(); value['asset'].update(asset_id='explicit-take-one', frames=1200)
        second = self.project(value)
        self.assertEqual(2400, first['cues'][0]['end_frame']); self.assertEqual(1200, second['cues'][0]['end_frame'])
        self.assertNotEqual(first['cues'][0]['version'], second['cues'][0]['version'])

    def test_same_source_in_another_saved_book_and_changed_script_are_stale(self):
        for changes in ({'book_id': 'saved-book-b'}, {'script_sha256': digest('edited')},
                        {'source_sha256': digest('replaced')}):
            with self.subTest(changes=changes):
                result = self.project(book=dict(BOOK, **changes))
                self.assertFalse(result['can_project']); self.assertEqual('stale', result['cues'][0]['status'])
                self.assertIsNone(result['cues'][0]['start_frame'])

    def test_missing_uid_never_relocates_to_same_text_or_same_audio(self):
        result = self.project(dialogue=[dict(SELECTION, chunk_uid='different-occurrence')])
        self.assertEqual('dialogue_selection_changed', result['cues'][0]['reason'])

    def test_take_generation_and_legacy_content_changes_each_invalidate_anchor(self):
        for changes in ({'audio_revision': 'b' * 32}, {'audio_sha256': digest('replacement')},
                        {'generation_sha256': digest('edited text')}):
            with self.subTest(changes=changes):
                self.assertFalse(self.project(dialogue=[dict(SELECTION, **changes)])['can_project'])
        value = recipe(); value['selection']['audio_revision'] = None
        legacy = dict(SELECTION, audio_revision=None)
        self.assertTrue(self.project(value, dialogue=[legacy])['can_project'])
        self.assertFalse(self.project(value, dialogue=[dict(legacy, audio_sha256=digest('new'))])['can_project'])

    def test_legacy_opaque_revision_is_preserved_and_must_match_exactly(self):
        value = recipe(); value['selection']['audio_revision'] = 'revision1'
        selection = dict(SELECTION, audio_revision='revision1')
        self.assertTrue(self.project(value, dialogue=[selection])['can_project'])
        changed = self.project(value, dialogue=[dict(selection, audio_revision='revision2')])
        self.assertEqual('stale', changed['cues'][0]['status'])

    def test_actual_selected_asset_conversion_matches_projected_frames_and_pcm(self):
        discriminating_cases = 0
        for source_rate in (8000, 11025, 16000, 24000, 44100, 48000):
            for rate in (8000, 24000, 48000):
                for frames in (1, 2, 97, 1001):
                    for channels, target_channels in ((1, 1), (1, 2), (2, 2)):
                        with self.subTest(source_rate=source_rate, rate=rate, frames=frames,
                                          channels=channels, target_channels=target_channels):
                            raw = (b'\x11\x20' * channels) * frames
                            segment = AudioSegment(data=raw, sample_width=2,
                                                   channels=channels, frame_rate=source_rate)
                            actual = segment.set_channels(target_channels).set_frame_rate(rate)
                            converted = b''.join(iter_converted_pcm([raw], (channels, source_rate, 2),
                                                (target_channels, rate, 2), lambda: None))
                            self.assertEqual(actual.raw_data, converted)
                            expected = len(converted) // (2 * target_channels)
                            value = recipe(); value['frame_rate'] = rate
                            value['asset'].update(frames=frames, frame_rate=source_rate, channels=channels,
                                                  sha256=hashlib.sha256(raw).hexdigest())
                            plan = pcm_plan(rate * 3); plan.update(frame_rate=rate, channels=target_channels)
                            result = self.project(value, plan=plan)
                            self.assertTrue(result['can_project'])
                            self.assertEqual(expected, result['cues'][0]['end_frame'])
                            discriminating_cases += expected != frames
        # A planner that simply copies declared source frames fails these controls.
        self.assertEqual(129, discriminating_cases)

    def test_projection_revision_binds_preceding_geometry_order_scope_and_layout(self):
        cue = get_sound_design_cue_version('cue-a', recipe())
        other = dict(SELECTION, chunk_uid='chunk-b')
        dialogue = [other, SELECTION]
        plan = pcm_plan(48002); plan['source_frame_ranges'] = [[0, 24001], [24001, 48002]]
        def project(rows=dialogue, geometry=plan, scope=None):
            return get_sound_design_cue_plan([cue], BOOK, rows, geometry, scope=scope)
        first = project()
        self.assertEqual(first['projection_revision'], project()['projection_revision'])
        changed = copy.deepcopy(plan); changed['source_frame_ranges'][1][0] += 1
        changed['intentional_pause_frames'] = [[24001, 24002]]
        longer_pause = project(geometry=changed)
        self.assertEqual(first['cues'][0]['version'], longer_pause['cues'][0]['version'])
        self.assertEqual(first['cues'][0]['start_frame'] + 1, longer_pause['cues'][0]['start_frame'])
        variants = [longer_pause, project(rows=[SELECTION, other]), project(scope=[1000, 48002])]
        changed = copy.deepcopy(plan); changed['sample_width'] = 4
        variants.append(project(geometry=changed))
        for variant in variants:
            self.assertNotEqual(first['projection_revision'], variant['projection_revision'])

    def test_invalid_recipe_refuses_before_copy_or_encoding(self):
        value = recipe(); value['asset']['frames'] = True
        with patch('sound_design_cues.copy.deepcopy', side_effect=AssertionError('copied invalid data')), \
                patch('sound_design_cues._get_version_bytes', side_effect=AssertionError('encoded invalid data')):
            with self.assertRaisesRegex(ValueError, 'Asset frames'):
                get_sound_design_cue_version('cue-a', value)

    def test_changed_output_clock_requires_review_instead_of_rescaling_authored_offsets(self):
        plan = pcm_plan(); plan['frame_rate'] = 48000
        self.assertEqual('output_clock_changed', self.project(plan=plan)['cues'][0]['reason'])

    def test_both_incoming_and_outgoing_scope_crossings_require_review(self):
        value = recipe(); value['offset_frames'] = 4000; value['asset']['frames'] = 8000
        for scope in ([10000, 20000], [0, 10000]):
            with self.subTest(scope=scope):
                row = self.project(value, scope=scope)['cues'][0]
                self.assertEqual('crosses_scope_boundary', row['reason']); self.assertIsNone(row['start_frame'])
        result = self.project(value, scope=[12000, 20000])
        self.assertTrue(result['can_project']); self.assertEqual('outside_scope', result['cues'][0]['status'])

    def test_cue_outside_timeline_is_not_silently_truncated(self):
        for offset in (-1, 24000):
            value = recipe(); value['offset_frames'] = offset
            self.assertEqual('outside_dialogue_timeline', self.project(value)['cues'][0]['reason'])

    def test_extension_requires_explicit_ambience_loop_and_valid_crossfade(self):
        value = recipe(); value['duration'].update(mode='fixed', frames=5000)
        self.assertEqual('explicit_loop_required', self.project(value)['cues'][0]['reason'])
        value['duration']['loop_crossfade_frames'] = 100
        with self.assertRaisesRegex(ValueError, 'ambience'):
            self.project(value)
        value['kind'] = 'ambience'
        self.assertTrue(self.project(value)['can_project'])
        value['duration']['loop_crossfade_frames'] = 2400
        self.assertEqual('invalid_loop_extension', self.project(value)['cues'][0]['reason'])
        value['duration'].update(frames=1200, loop_crossfade_frames=100)
        self.assertEqual('invalid_loop_extension', self.project(value)['cues'][0]['reason'])

    def test_fades_and_channel_downmix_require_explicit_valid_geometry(self):
        value = recipe(); value['mix'].update(fade_in_frames=2000, fade_out_frames=1000)
        self.assertEqual('fades_exceed_duration', self.project(value)['cues'][0]['reason'])
        value = recipe(); value['asset']['channels'] = 2
        self.assertEqual('asset_channels_require_review', self.project(value)['cues'][0]['reason'])

    def test_duplicate_source_or_cue_ids_and_bad_geometry_are_rejected(self):
        cue = get_sound_design_cue_version('cue-a', recipe())
        with self.assertRaisesRegex(ValueError, 'one version'):
            get_sound_design_cue_plan([cue, cue], BOOK, [SELECTION], pcm_plan())
        with self.assertRaisesRegex(ValueError, 'unique'):
            get_sound_design_cue_plan([cue], BOOK, [SELECTION, SELECTION], pcm_plan())
        for ranges in ([], [[0, True]], [[0, 24002]], [[-1, 24001]]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                plan = pcm_plan(); plan['source_frame_ranges'] = ranges; self.project(plan=plan)
        with self.assertRaisesRegex(ValueError, 'count'):
            get_sound_design_cue_plan([cue] * (MAX_CUES + 1), BOOK, [SELECTION], pcm_plan())

    def test_empty_trimmed_source_cannot_authorize_a_cue(self):
        plan = pcm_plan(); plan['source_frame_ranges'] = [[100, 100]]
        self.assertEqual('empty_dialogue_source', self.project(plan=plan)['cues'][0]['reason'])

    def test_cancellation_at_final_projection_check_never_returns_success(self):
        calls = []
        def check():
            calls.append(1)
            if len(calls) == 4:
                raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.project(check_active=check)
        self.assertEqual(4, len(calls))


if __name__ == '__main__':
    unittest.main()
