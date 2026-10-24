"""Actual WAV audition frames and late-mutation/cancellation fences."""
import io
import unittest
import wave
from unittest.mock import patch
from tests import test_sound_design_capture as fixtures
from sound_design_render import save_sound_design_candidate
from tts import ExportCancelled
import sound_design_candidate_audition as audition


class SoundAuditionTests(unittest.TestCase):
    setUp = fixtures.SoundCaptureCompositionTests.setUp
    revision = fixtures.SoundCaptureCompositionTests.revision

    def candidate(self):
        result = save_sound_design_candidate(self.root, self.revision())
        return self.root / result['directory'], result['candidate_revision']

    def test_wav_exact_windows_including_final_frame_and_unsigned_eight_bit(self):
        for width in (1, 2, 4):
            with self.subTest(width=width):
                audio = self.audio.set_sample_width(width)
                with audio.export(self.fixture.audio, format='wav'): pass
                stage, revision = self.candidate()
                for start, end in ((0,1),(101,317),(4799,4800)):
                    result = audition.read_sound_candidate_audition(self.root, stage.name, revision, start, end)
                    with wave.open(io.BytesIO(result['wav'])) as wav:
                        self.assertEqual((1,width,24000,end-start),
                            (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes()))
                        raw = wav.readframes(end-start)
                    expected = audio.raw_data[start*width:end*width]
                    if width == 1: expected = bytes((value + 128) % 256 for value in expected)
                    self.assertEqual(expected, raw)
                    self.assertEqual([start,end], result['scope'])
                    self.assertEqual(revision, result['candidate_revision'])

    def test_invalid_ranges_and_byte_duration_limits_refuse_before_output(self):
        stage, revision = self.candidate()
        for start,end in ((False,1),(0,0),(-1,1),(0,4801)):
            with self.subTest(start=start,end=end), self.assertRaises(ValueError):
                audition.read_sound_candidate_audition(self.root, stage.name, revision, start,end)
        for constant,value in (('MAX_AUDITION_BYTES',45),('MAX_AUDITION_SECONDS',0)):
            with patch.object(audition,constant,value), patch.object(audition,'write_producer_wav',
                    side_effect=AssertionError('WAV allocated')), self.assertRaises(ValueError):
                audition.read_sound_candidate_audition(self.root,stage.name,revision,0,10)

    def test_mutation_during_wav_creation_never_returns_excerpt(self):
        stage, revision = self.candidate(); write = audition.write_producer_wav
        def mutate(*args, **kwargs):
            write(*args, **kwargs)
            path = stage/'mixed.pcm'; raw = bytearray(path.read_bytes())
            raw[-1] ^= 1
            path.write_bytes(raw)
        with patch.object(audition,'write_producer_wav',side_effect=mutate):
            with self.assertRaisesRegex(ValueError,'PCM changed'):
                audition.read_sound_candidate_audition(self.root,stage.name,revision,0,10)

    def test_cancellation_preserves_candidate(self):
        stage, revision = self.candidate(); before = (stage/'mixed.pcm').read_bytes()
        with self.assertRaises(ExportCancelled):
            audition.read_sound_candidate_audition(self.root,stage.name,revision,0,10,cancel_check=lambda:True)
        self.assertEqual(before,(stage/'mixed.pcm').read_bytes())
