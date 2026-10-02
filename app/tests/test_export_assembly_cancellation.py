"""Cancellation reaches real silent-edge scans and audiobook assembly."""
import unittest
from unittest.mock import patch
from pydub import AudioSegment
import tts
import project
from tests.test_chapter_export import _project
import tempfile


class ExportAssemblyCancellationTests(unittest.TestCase):
    def test_cancel_interrupts_one_long_silent_edge_scan(self):
        calls=0
        def cancel():
            nonlocal calls
            calls+=1
            return calls==5
        with self.assertRaises(project.ExportCancelled):
            tts.trim_edge_silence(AudioSegment.silent(duration=60000),cancel_check=cancel)
        self.assertEqual(5,calls)

    def test_combine_cancel_does_not_process_all_segments(self):
        segments=[AudioSegment.silent(duration=100)]*10
        trim=tts.trim_edge_silence
        calls=0
        def checked(segment,**kw):
            nonlocal calls
            calls+=1
            return trim(segment,**kw)
        with patch.object(tts,'trim_edge_silence',side_effect=checked):
            with self.assertRaises(project.ExportCancelled):
                tts.combine_audio_with_pauses(segments,['A']*10,cancel_check=lambda:calls>=2)
        self.assertEqual(2,calls)

    def test_uncancelled_combine_and_timeline_audio_and_offsets_are_unchanged(self):
        segments=[AudioSegment.silent(duration=100),AudioSegment.silent(duration=200)]
        baseline=tts.combine_audio_with_pauses(segments,['A','B'],pause_ms=80)
        combined=tts.combine_audio_with_pauses(segments,['A','B'],pause_ms=80,cancel_check=lambda:False)
        self.assertEqual(baseline.raw_data,combined.raw_data)
        rows=[({'speaker':'A'},segments[0]),({'speaker':'B'},segments[1])]
        self.assertEqual([0,180],[entry[2] for entry in tts.compute_timeline(rows,pause_ms=80,cancel_check=lambda:False)])

    def test_chapter_export_cancellation_inside_timeline_returns_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm,chunks=_project(tmp)
            active=False
            timeline=project.compute_timeline
            def interrupted(*args,**kw):
                nonlocal active
                active=True
                return timeline(*args,**kw)
            with patch.object(pm,'load_chunks',return_value=chunks),patch.object(project,'compute_timeline',side_effect=interrupted),patch.object(project,'_export_audio_segment') as encode:
                self.assertEqual((False,'Export cancelled'),pm.export_chapters(fmt='wav',cancel_check=lambda:active))
                encode.assert_not_called()

    def test_full_export_stops_inside_combination_before_processing_whole_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm,chunks=_project(tmp)
            combining=False;visits=0
            combine=project.combine_audio_with_pauses;trim=tts.trim_edge_silence
            def begin(*args,**kwargs):
                nonlocal combining
                combining=True
                return combine(*args,**kwargs)
            def scan(*args,**kwargs):
                nonlocal visits
                if combining:visits+=1
                return trim(*args,**kwargs)
            with patch.object(pm,'load_chunks',return_value=chunks),patch.object(project,'combine_audio_with_pauses',side_effect=begin),patch.object(tts,'trim_edge_silence',side_effect=scan),patch.object(project,'_export_audio_segment') as encode:
                result=pm.export_chapters(fmt='wav',cancel_check=lambda:combining and visits>0)
            self.assertEqual((False,'Export cancelled'),result)
            self.assertEqual(1,visits)
            encode.assert_not_called()
