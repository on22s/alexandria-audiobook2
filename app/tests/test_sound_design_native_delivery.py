"""Qualification of approved candidate PCM against the actual native MP3 encoder.

This exercises private encoding only. Final publication and bounded delivery
staging still need integration before this is a user-facing export path.
"""
from pathlib import Path
import tempfile
import unittest
import core
from tests import test_sound_design_owned_capture as fixtures
from tests.test_export_pcm_plan import clip
from project_scope_admission import ensure_audio_scope_admission
from sound_design_context import capture_sound_design_context
from sound_design_render import save_sound_design_candidate
from sound_design_approval import save_sound_candidate_approval, ensure_approved_sound_candidate
from audio_export import export_pcm_mp3
from export_quality import get_pcm_export_encoding_plan, get_admitted_export_report
from project import MP3_BITRATE


class SoundNativeDeliveryTests(unittest.TestCase):
    setUp = fixtures.OwnedSoundCaptureTests.setUp

    def test_approved_pcm_encodes_with_native_quality_plan_and_stale_approval_refuses(self):
        audio = clip(24000,48000)
        with audio.export(self.fixture.audio,format='wav'): pass
        before = self.fixture.audio.read_bytes()
        results = []
        def run():
            import numpy as np
            import soundfile as sf
            from sound_design_assets import SoundDesignAssetStore
            from sound_design_context import capture_sound_design_anchor
            from sound_design_registry import get_sound_design_registry_edit, ACTIVE_CUES
            sf.write(self.root/'effect.wav',np.full(48000,2**27,dtype=np.int32),24000,subtype='PCM_32')
            asset = SoundDesignAssetStore(self.root).import_asset('effect.wav')
            with ensure_audio_scope_admission(self.root):
                snapshot = capture_sound_design_context(self.root)
                anchor = capture_sound_design_anchor(self.root,snapshot,'synthetic-uid')
                recipe = dict(book=snapshot['book'],selection=anchor['selection'],boundary='start',
                    offset_frames=0,frame_rate=24000,kind='effect',asset=asset,
                    duration=dict(mode='native',frames=None,loop_crossfade_frames=None),
                    mix=dict(gain_db=0,duck_db=0,fade_in_frames=0,fade_out_frames=0))
                self.fixture.write(ACTIVE_CUES,get_sound_design_registry_edit(snapshot['registry'],'effect',recipe))
                context = capture_sound_design_context(self.root)['context_revision']
            candidate = save_sound_design_candidate(self.root,context)
            stage = (self.root/candidate['directory']).name
            options = {'format':'mp3','export_quality':None,'mastering':None}
            approved = save_sound_candidate_approval(self.root,stage,candidate['candidate_revision'],
                                                     options,full_mix_reviewed=True)
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                raw, output = Path(temporary)/'input.pcm', Path(temporary)/'output.mp3'
                with ensure_approved_sound_candidate(self.root,stage,candidate['candidate_revision'],
                        approved['approval_revision'],options) as (document, stream):
                    with raw.open('xb') as destination:
                        while block := stream.read(65536): destination.write(block)
                    self.assertNotEqual(audio.raw_data,raw.read_bytes())
                    self.assertEqual(1,len(document['binding']['cue_versions']))
                    plan = document['binding']['pcm_plan']
                    quality = get_pcm_export_encoding_plan(plan,format_name='mp3')
                    reports = []
                    export_pcm_mp3(raw,output,plan,None,bitrate=MP3_BITRATE,
                                   quality_plan=quality,quality_callback=reports.append)
                    report = get_admitted_export_report(output,quality,reports[-1])
                    self.assertIn(report['status'],('verified','review'))
                    self.assertEqual(quality['frames'],report['expected_frames'])
                    self.assertEqual('mp3',report['codec'])
                    results.append((report['decoded_frames'],report['sample_rate']))
                self.fixture.source.write_text('Changed after approval')
                with self.assertRaisesRegex(ValueError,'stale'):
                    with ensure_approved_sound_candidate(self.root,stage,candidate['candidate_revision'],
                            approved['approval_revision'],options): self.fail('Stale approval admitted')
        core._run_owned_background_task('audio',self.claim,run)
        self.assertEqual(1,len(results))
        self.assertEqual(before,self.fixture.audio.read_bytes())
        self.assertFalse(core.process_state['audio']['running'])
