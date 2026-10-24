"""Compare captured dry PCM with the actual existing export writer."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace

from audio_pcm_producer import PCMSourceStore
from audio_streaming import write_combined_pcm
from sound_design_pcm import save_sound_design_dry_pcm
from tests.test_export_pcm_plan import clip
from tts import ExportCancelled


class SoundDesignPCMTests(unittest.TestCase):
    def test_capture_matches_export_bytes_and_exact_plan_for_native_layouts_and_pauses(self):
        for rates in ([24000]*3,[8000,16000,44100]):
            for pauses in ([17,75,None],[-2,2.5,None]):
                with self.subTest(rates=rates,pauses=pauses), tempfile.TemporaryDirectory() as tmp:
                    segments=[clip(rate,2000+i*17,1 if i==0 else 2).set_sample_width(4 if i==2 else 2)
                              for i,rate in enumerate(rates)]
                    rows=[dict(speaker=speaker,pause_after=pause) for speaker,pause in zip(['A','B','A'],pauses)]
                    plans=[];path=Path(tmp)/'expected.pcm'
                    write_combined_pcm(segments,['A','B','A'],path,75.5,2.5,pauses,pcm_plan_callback=plans.append)
                    with PCMSourceStore(tmp,lambda:None) as store:
                        snapshots=[store.save_segment(segment) for segment in segments]
                        dry,plan=save_sound_design_dry_pcm(store,rows,snapshots,tmp,(75.5,2.5))
                        self.assertEqual(plans[0],plan)
                        self.assertEqual(path.read_bytes(),b''.join(store.iter_pcm(dry,65536)))
                        for original,snapshot in zip(segments,snapshots):
                            self.assertEqual(original.raw_data,b''.join(store.iter_pcm(snapshot,65536)))

    def test_bounds_refuse_before_materializing_an_owned_clip(self):
        with tempfile.TemporaryDirectory() as tmp, PCMSourceStore(tmp,lambda:None) as store:
            source=store.save_segment(clip(24000,2000));before=store.byte_count
            for kwargs in ({'max_clip_bytes':1},{'max_staged_bytes':before}):
                with self.subTest(kwargs=kwargs), patch.object(store,'load_segment',side_effect=AssertionError('allocated')):
                    with self.assertRaises(ValueError):
                        save_sound_design_dry_pcm(store,[{'speaker':'A'}],[source],tmp,(0,0),**kwargs)
                self.assertEqual(before,store.byte_count)

    def test_cancellation_and_foreign_ranges_cannot_return_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, PCMSourceStore(tmp,lambda:None) as store:
            source=store.save_segment(clip(24000,2000))
            with self.assertRaises(ExportCancelled):
                save_sound_design_dry_pcm(store,[{'speaker':'A'}],[source],tmp,(0,0),cancel_check=lambda:True)
            with self.assertRaises(ValueError):
                save_sound_design_dry_pcm(store,[{'speaker':'A'}],[replace(source,offset=store.byte_count)],tmp,(0,0))
