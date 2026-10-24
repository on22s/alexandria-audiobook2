"""A valid immutable candidate may still be stale for the current book."""
import unittest
from unittest.mock import patch
from tests import test_sound_design_capture as fixtures
from sound_design_render import save_sound_design_candidate
import sound_design_current as current


class CurrentSoundCandidateTests(unittest.TestCase):
    revision = fixtures.SoundCaptureCompositionTests.revision

    def setUp(self):
        fixtures.SoundCaptureCompositionTests.setUp(self)
        admission = patch.object(current, 'ensure_audio_scope_admission',
                                 side_effect=lambda root: self.fixture.admitted())
        admission.start(); self.addCleanup(admission.stop)
        self.result = save_sound_design_candidate(self.root, self.revision())
        self.stage = (self.root / self.result['directory']).name

    def open(self):
        return current.ensure_current_sound_candidate(self.root, self.stage, self.result['candidate_revision'])

    def test_current_candidate_yields_verified_pcm(self):
        with self.open() as (document, stream):
            self.assertEqual(self.result['candidate_revision'], document['candidate_revision'])
            self.assertEqual(self.audio.raw_data, stream.read())

    def test_changed_source_settings_or_selected_take_refuse(self):
        mutations = [lambda: self.fixture.source.write_text('Changed source'),
                     lambda: self.fixture.write('config.json', {'tts': {'pause_same_speaker_ms': 100}}),
                     lambda: self.fixture.audio.write_bytes(self.fixture.audio.read_bytes()[:-2]+b'\x01\x01')]
        paths = [self.fixture.source, self.root/'config.json', self.fixture.audio]
        # Each mutation is isolated by restoring exact bytes; context identity
        # includes file versions, so use a freshly rendered candidate each time.
        for path, mutate in zip(paths, mutations):
            before = path.read_bytes()
            with self.subTest(path=path):
                result = save_sound_design_candidate(self.root, self.revision())
                mutate()
                with self.assertRaises(ValueError):
                    with current.ensure_current_sound_candidate(self.root,
                            (self.root/result['directory']).name,result['candidate_revision']):
                        self.fail('Stale candidate admitted')
                path.write_bytes(before)

    def test_mutation_while_consumer_uses_candidate_refuses_on_exit(self):
        with self.assertRaises(ValueError):
            with self.open(): self.fixture.source.write_text('Changed during use')

    def test_changed_canonical_asset_invalidates_unchanged_candidate(self):
        import numpy as np
        import soundfile as sf
        from sound_design_assets import SoundDesignAssetStore
        from sound_design_context import capture_sound_design_context, capture_sound_design_anchor
        from sound_design_registry import get_sound_design_registry_edit, ACTIVE_CUES
        path = self.root/'effect.wav'
        sf.write(path, np.full(4800, 2**27, dtype=np.int32), 24000, subtype='PCM_32')
        store = SoundDesignAssetStore(self.root); asset = store.import_asset(path.name)
        with self.fixture.admitted():
            context = capture_sound_design_context(self.root)
            anchor = capture_sound_design_anchor(self.root, context, 'synthetic-uid')
        recipe = dict(book=context['book'], selection=anchor['selection'], boundary='start',
            offset_frames=0, frame_rate=24000, kind='effect', asset=asset,
            duration=dict(mode='native', frames=None, loop_crossfade_frames=None),
            mix=dict(gain_db=0,duck_db=0,fade_in_frames=0,fade_out_frames=0))
        self.fixture.write(ACTIVE_CUES, get_sound_design_registry_edit(context['registry'],'effect',recipe))
        self.result = save_sound_design_candidate(self.root, self.revision())
        self.stage = (self.root/self.result['directory']).name
        with self.open(): pass
        canonical = store._path(asset['asset_id'], '.pcm')
        original = canonical.read_bytes(); changed = bytearray(original); changed[-1] ^= 1
        canonical.write_bytes(changed)
        with self.assertRaisesRegex(ValueError,'asset bytes changed'):
            with self.open(): self.fail('Modified asset admitted')
        self.assertEqual(bytes(changed),canonical.read_bytes())
