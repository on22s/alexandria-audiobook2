"""Native capture composition; owner admission is covered separately."""
import unittest
from unittest.mock import patch
import sound_design_capture as capture
from sound_design_context import capture_sound_design_context
from tests import test_sound_design_context as fixtures
from tests.test_export_pcm_plan import clip
from audio_streaming import write_combined_pcm
from tts import iter_timeline, DEFAULT_PAUSE_MS, SAME_SPEAKER_PAUSE_MS, ExportCancelled

class SoundCaptureCompositionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SoundDesignContextTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.stage = self.root / 'private-stage'; self.stage.mkdir()
        self.audio = clip(24000, 4800)
        with self.audio.export(self.fixture.audio, format='wav'): pass
        admission = patch.object(capture, 'ensure_audio_scope_admission',
                                 side_effect=lambda root: self.fixture.admitted())
        admission.start(); self.addCleanup(admission.stop)

    def revision(self):
        with self.fixture.admitted():
            return capture_sound_design_context(self.root)['context_revision']

    def test_real_capture_matches_native_export(self):
        before = self.fixture.audio.read_bytes()
        timeline = list(iter_timeline([(self.fixture.row, self.audio)]))
        expected = self.root / 'expected.pcm'; plans = []
        write_combined_pcm([item[1] for item in timeline], ['NARRATOR'], expected,
                           DEFAULT_PAUSE_MS, SAME_SPEAKER_PAUSE_MS, [None],
                           pcm_plan_callback=plans.append)
        with capture.ensure_sound_design_audio_capture(self.root, self.stage, self.revision()) as result:
            self.assertEqual(plans[0], result['pcm_plan'])
            self.assertEqual(expected.read_bytes(), b''.join(result['sources'].iter_pcm(result['dry'], 65536)))
        self.assertEqual(before, self.fixture.audio.read_bytes())
        self.assertEqual([], list(self.stage.iterdir()))

    def test_bad_pause_and_stale_context_refuse_before_decode(self):
        old = self.revision()
        self.fixture.write('chunks.json', [{**self.fixture.row, 'pause_after': '100'}])
        for revision in (old, self.revision()):
            with self.subTest(revision=revision), patch.object(capture, 'load_sound_design_take',
                    side_effect=AssertionError('Decoded before boundary validation')):
                with self.assertRaises(ValueError):
                    with capture.ensure_sound_design_audio_capture(self.root, self.stage, revision):
                        self.fail('Invalid capture returned')
        self.assertEqual([], list(self.stage.iterdir()))

    def test_changed_take_on_exit_rejects_candidate(self):
        with self.assertRaisesRegex(ValueError, 'take changed'):
            with capture.ensure_sound_design_audio_capture(self.root, self.stage, self.revision()):
                raw = bytearray(self.fixture.audio.read_bytes()); raw[-1] ^= 1
                self.fixture.audio.write_bytes(raw)
        self.assertEqual([], list(self.stage.iterdir()))

    def test_cancel_and_small_stage_preserve_source(self):
        before = self.fixture.audio.read_bytes()
        for kwargs, error in (({'cancel_check': lambda: True}, ExportCancelled),
                              ({'max_staged_bytes': 32}, ValueError)):
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                with capture.ensure_sound_design_audio_capture(self.root, self.stage, self.revision(), **kwargs):
                    self.fail('Invalid capture returned')
            self.assertEqual(before, self.fixture.audio.read_bytes())
            self.assertEqual([], list(self.stage.iterdir()))
