"""Private full candidate integration with real take decoding and mixer."""
import hashlib
import json
import unittest
from unittest.mock import patch
from tests import test_sound_design_capture as fixtures
import sound_design_render as render
from sound_design_staging import DIRECTORY


class SoundCandidateTests(unittest.TestCase):
    setUp = fixtures.SoundCaptureCompositionTests.setUp
    revision = fixtures.SoundCaptureCompositionTests.revision

    def test_full_candidate_has_exact_pcm_and_bound_manifest(self):
        original = self.fixture.audio.read_bytes()
        result = render.save_sound_design_candidate(self.root, self.revision())
        directory = self.root / result['directory']
        document = json.loads((directory / 'candidate.json').read_bytes())
        pcm = (directory / document['pcm_file']).read_bytes()
        self.assertEqual(result['candidate_revision'], document['candidate_revision'])
        self.assertEqual(hashlib.sha256(pcm).hexdigest(), result['mix']['sha256'])
        self.assertEqual(len(pcm), result['mix']['bytes'])
        self.assertEqual(pcm, self.audio.raw_data)
        self.assertEqual([0, result['mix']['frames']], result['mix']['scope'])
        self.assertEqual(self.revision(), document['binding']['context_revision'])
        self.assertEqual(original, self.fixture.audio.read_bytes())
        self.assertEqual({'candidate.json', 'mixed.pcm'}, {path.name for path in directory.iterdir()})

    def test_changed_take_after_mixing_cannot_leave_candidate(self):
        original = render.write_sound_design_pcm
        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            raw = bytearray(self.fixture.audio.read_bytes()); raw[-1] ^= 1
            self.fixture.audio.write_bytes(raw)
            return result
        with patch.object(render, 'write_sound_design_pcm', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'take changed'):
                render.save_sound_design_candidate(self.root, self.revision())
        self.assertEqual([], list((self.root / DIRECTORY).glob('render-*')))

    def test_retained_candidate_is_charged_before_decode(self):
        render.save_sound_design_candidate(self.root, self.revision())
        with patch.object(render, 'ensure_sound_design_audio_capture', side_effect=AssertionError('decoded')):
            with self.assertRaisesRegex(ValueError, 'allowance'):
                render.save_sound_design_candidate(self.root, self.revision(),
                                                   max_staged_bytes=render.MAX_CANDIDATE_METADATA)

    def test_selected_effect_changes_samples_and_binds_exact_revision(self):
        import numpy as np
        import soundfile as sf
        from sound_design_assets import SoundDesignAssetStore
        from sound_design_context import capture_sound_design_context, capture_sound_design_anchor
        from sound_design_registry import get_sound_design_registry_edit, ACTIVE_CUES
        asset_path = self.root / 'effect.wav'
        sf.write(asset_path, np.full(4800, 2**28, dtype=np.int32), 24000, subtype='PCM_32')
        asset = SoundDesignAssetStore(self.root).import_asset(asset_path.name)
        with self.fixture.admitted():
            context = capture_sound_design_context(self.root)
            anchor = capture_sound_design_anchor(self.root, context, 'synthetic-uid')
        recipe = dict(book=context['book'], selection=anchor['selection'], boundary='start',
                      offset_frames=0, frame_rate=24000, kind='effect', asset=asset,
                      duration=dict(mode='native', frames=None, loop_crossfade_frames=None),
                      mix=dict(gain_db=0, duck_db=0, fade_in_frames=0, fade_out_frames=0))
        registry = get_sound_design_registry_edit(context['registry'], 'effect', recipe)
        self.fixture.write(ACTIVE_CUES, registry)
        result = render.save_sound_design_candidate(self.root, self.revision())
        stage = self.root / result['directory']
        samples = np.frombuffer((stage / 'mixed.pcm').read_bytes(), dtype='<i2')
        # Dry 8192 plus effect 2**28 / 2**31 * 32768 = 4096.
        self.assertTrue(np.all(samples == 12288))
        manifest = json.loads((stage / 'candidate.json').read_bytes())
        self.assertEqual([registry['selected']['effect']], manifest['binding']['cue_versions'])
