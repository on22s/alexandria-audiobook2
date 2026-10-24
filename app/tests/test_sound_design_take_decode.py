"""Native decode compatibility and resource controls for selected takes."""
from pathlib import Path
import tempfile
import unittest
from pydub import AudioSegment
from sound_design_take_decode import load_sound_design_take
from tests.test_export_pcm_plan import clip
from tts import ExportCancelled


class SoundTakeDecodeTests(unittest.TestCase):
    def test_native_formats_match_existing_editor_decode(self):
        for fmt in ('wav','mp3','flac','ogg'):
            for channels in (1,2):
                with self.subTest(fmt=fmt,channels=channels),tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp)/('take.'+fmt)
                    audio=clip(24000,4800,channels)
                    with audio.export(path,format=fmt):pass
                    kwargs={'format':fmt} if fmt in ('wav','mp3','flac') else {}
                    if fmt in ('mp3','flac'):kwargs['codec']=fmt
                    with path.open('rb') as source:expected=AudioSegment.from_file(source,**kwargs)
                    actual=load_sound_design_take(Path(tmp),path.name)
                    self.assertEqual((expected.channels,expected.sample_width,expected.frame_rate),
                                     (actual.channels,actual.sample_width,actual.frame_rate))
                    self.assertEqual(expected.raw_data,actual.raw_data)

    def test_small_allowance_and_cancellation_refuse_complete_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'take.wav'
            with clip(24000,4800).export(path,format='wav'):pass
            before=path.read_bytes()
            with self.assertRaises(ValueError):load_sound_design_take(Path(tmp),path.name,max_pcm_bytes=32)
            with self.assertRaises(ExportCancelled):load_sound_design_take(Path(tmp),path.name,cancel_check=lambda:True)
            self.assertEqual(before,path.read_bytes())

    def test_integer_widths_match_editor_and_nonfinite_float_is_rejected(self):
        import numpy as np
        import soundfile as sf
        for subtype in ('PCM_U8','PCM_16','PCM_24','PCM_32','FLOAT'):
            with self.subTest(subtype=subtype),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'take.wav'
                values=np.array([-.75,-.25,0,.25,.75]*100,dtype=np.float64)
                sf.write(path,values,24000,subtype=subtype)
                with path.open('rb') as source:expected=AudioSegment.from_file(source,format='wav')
                actual=load_sound_design_take(Path(tmp),path.name)
                self.assertEqual(expected.raw_data,actual.raw_data)
                self.assertEqual(expected.sample_width,actual.sample_width)
        for value in (float('nan'),float('inf'),float('-inf')):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'invalid.wav';sf.write(path,[0,value,0],24000,subtype='FLOAT')
                before=path.read_bytes()
                with self.assertRaisesRegex(ValueError,'non-finite'):
                    load_sound_design_take(Path(tmp),path.name)
                self.assertEqual(before,path.read_bytes())
