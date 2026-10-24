"""Actual AAC from captured PCM preserves chapter clock, tags and old output."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pydub.generators import Sine
from audio_m4b_pcm import export_pcm_m4b
from export_quality import get_pcm_export_encoding_plan
from m4b_metadata import get_m4b_tags


class M4BPCMTests(unittest.TestCase):
    def test_native_and_mastered_full_output_and_quota_rejection(self):
        audio=Sine(440,sample_rate=24000).to_audio_segment(duration=3000).apply_gain(-24)
        settings=dict(frames=72000,frame_rate=24000,channels=1,sample_width=2,
                      source_frame_ranges=[[0,24000],[24000,72000]],intentional_pause_frames=[])
        quality=get_pcm_export_encoding_plan(settings,format_name='m4b',
            chapters=[('Chapter = one',0,1),('雪 Chapter two',1,3)],chapter_time_base=1,
            expected_tags=get_m4b_tags({'title':'Synthetic = title; #1','author':'Author'}))
        for mastering in (None,{}):
            for allowance in (1024**2,1000):
                with self.subTest(mastering=mastering,allowance=allowance), tempfile.TemporaryDirectory() as tmp:
                    raw=Path(tmp).resolve()/'input.pcm';raw.write_bytes(audio.raw_data)
                    output=Path(tmp).resolve()/'output.m4b';output.write_bytes(b'prior complete delivery')
                    reports=[]
                    def encode():
                        export_pcm_m4b(raw,output,settings,None,quality_plan=quality,
                            max_output_bytes=allowance,mastering=mastering,quality_callback=reports.append)
                    if allowance == 1000:
                        with self.assertRaises((ValueError,RuntimeError)): encode()
                        self.assertEqual(b'prior complete delivery',output.read_bytes())
                    else:
                        encode()
                        self.assertEqual('verified',reports[-1]['status'])
                        self.assertEqual(2,reports[-1]['chapters_checked'])
                        self.assertEqual(6,reports[-1]['tags_checked'])
                    self.assertEqual(audio.raw_data,raw.read_bytes())
                    self.assertEqual({'input.pcm','output.m4b'},{p.name for p in Path(tmp).resolve().iterdir()})

    def test_unsafe_inputs_refuse_before_loudness_or_worker(self):
        settings=dict(frames=8,frame_rate=24000,channels=1,sample_width=2,
                      source_frame_ranges=[[0,8]],intentional_pause_frames=[])
        quality=get_pcm_export_encoding_plan(settings,format_name='m4b',
            chapters=[('Synthetic',0,8/24000)],chapter_time_base=1,
            expected_tags=get_m4b_tags({'title':'Synthetic'}))
        for fault in ('symlink','channel_limit','rate_limit','unsafe_name','wrong_size'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();raw=root/'input.pcm';raw.write_bytes(b'\0'*16)
                geometry=dict(settings)
                if fault=='symlink':
                    target=root/'external.pcm';raw.rename(target)
                    try:raw.symlink_to(target)
                    except OSError as error:
                        self.skipTest('Host cannot create symlink: '+str(error))
                elif fault=='channel_limit':
                    geometry['channels']=3;raw.write_bytes(b'\0'*48)
                elif fault=='rate_limit':geometry['frame_rate']=192001
                elif fault=='unsafe_name':
                    alternate=root/'unsafe name.pcm';raw.rename(alternate);raw=alternate
                else:raw.write_bytes(b'\0'*15)
                output=root/'output.m4b';output.write_bytes(b'prior complete delivery')
                with patch('audio_m4b_pcm.get_loudness_measurements') as measure, \
                        patch('audio_m4b_pcm.run_owned_media_check') as worker:
                    with self.assertRaises(ValueError):
                        export_pcm_m4b(raw,output,geometry,None,mastering={},quality_plan=quality,
                            max_output_bytes=1024**2)
                    measure.assert_not_called();worker.assert_not_called()
                self.assertEqual(b'prior complete delivery',output.read_bytes())
                self.assertFalse(list(root.glob('.pcm-encode-*')))

    def test_native_runtime_change_after_encoding_preserves_previous_delivery(self):
        from audio_encoder_identity import get_ffmpeg_runtime_identity,get_ffmpeg_runtime_digest,FFmpegRuntimeIdentity
        runtime=get_ffmpeg_runtime_identity()
        altered=runtime.model_dump();altered['version_sha256']='0'*64
        changed=FFmpegRuntimeIdentity.model_validate(altered)
        audio=Sine(440,sample_rate=24000).to_audio_segment(duration=2000).apply_gain(-24)
        settings=dict(frames=48000,frame_rate=24000,channels=1,sample_width=2,
            source_frame_ranges=[[0,48000]],intentional_pause_frames=[])
        quality=get_pcm_export_encoding_plan(settings,format_name='m4b',
            chapters=[('Synthetic',0,2)],chapter_time_base=1,expected_tags=get_m4b_tags({'title':'Synthetic'}))
        with tempfile.TemporaryDirectory() as tmp:
            raw=Path(tmp).resolve()/'input.pcm';raw.write_bytes(audio.raw_data)
            output=Path(tmp).resolve()/'output.m4b';output.write_bytes(b'previous complete delivery')
            with patch('audio_m4b_pcm.get_ffmpeg_runtime_identity',side_effect=[runtime,changed]):
                with self.assertRaisesRegex(ValueError,'encoder profile changed during'):
                    export_pcm_m4b(raw,output,settings,None,quality_plan=quality,max_output_bytes=1024**2,
                        encoder_runtime=get_ffmpeg_runtime_digest(runtime))
            self.assertEqual(b'previous complete delivery',output.read_bytes())
            self.assertEqual({'input.pcm','output.m4b'},{p.name for p in Path(tmp).resolve().iterdir()})

    def test_large_moov_cover_faststart_reopen_preserves_exact_cover(self):
        import hashlib
        import io
        import random
        from PIL import Image
        audio=Sine(440,sample_rate=24000).to_audio_segment(duration=2000).apply_gain(-24)
        geometry=dict(frames=48000,frame_rate=24000,channels=1,sample_width=2,
            source_frame_ranges=[[0,48000]],intentional_pause_frames=[])
        plan=get_pcm_export_encoding_plan(geometry,format_name='m4b',
            chapters=[('Own synthetic chapter',0,2)],chapter_time_base=1,
            expected_tags=get_m4b_tags({'title':'Own generated cover'}))
        image=Image.frombytes('RGB',(768,512),random.Random(1444).randbytes(768*512*3))
        buffer=io.BytesIO();image.save(buffer,format='PNG');cover_bytes=buffer.getvalue()
        self.assertGreater(len(cover_bytes),1024**2)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();raw=root/'input.pcm';raw.write_bytes(audio.raw_data)
            cover=root/'cover.image';cover.write_bytes(cover_bytes);output=root/'output.m4b';reports=[]
            export_pcm_m4b(raw,output,geometry,None,quality_plan=plan,max_output_bytes=4*1024**2,
                cover_path=cover,cover_sha256=hashlib.sha256(cover_bytes).hexdigest(),quality_callback=reports.append)
            self.assertEqual('verified',reports[-1]['status'])
            atoms=[]
            with output.open('rb') as stream:
                while header:=stream.read(8):
                    self.assertEqual(8,len(header));size=int.from_bytes(header[:4],'big');name=header[4:]
                    if size==1:
                        extended=stream.read(8);self.assertEqual(8,len(extended))
                        size=int.from_bytes(extended,'big');header+=extended
                    self.assertGreaterEqual(size,len(header));atoms.append((name,size));stream.seek(size-len(header),1)
            self.assertGreater(dict(atoms)[b'moov'],1024**2)
            self.assertLess([name for name,_ in atoms].index(b'moov'),[name for name,_ in atoms].index(b'mdat'))
            self.assertEqual({'input.pcm','cover.image','output.m4b'},{p.name for p in root.iterdir()})
