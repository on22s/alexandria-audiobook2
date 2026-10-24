"""Bounded parent admission, native mastering and publication failure controls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pydub.generators import Sine

import audio_pcm_mux
from audio_pcm_mux import export_bounded_pcm
from export_quality import get_pcm_export_encoding_plan


def settings():
    return dict(frames=48000,frame_rate=24000,channels=1,sample_width=2,
        source_frame_ranges=[[0,48000]],intentional_pause_frames=[])


class BoundedPCMExportTests(unittest.TestCase):
    def test_invalid_geometry_refuses_before_hash_probe_or_loudness(self):
        from contextlib import ExitStack
        geometry=settings()
        plan=get_pcm_export_encoding_plan(geometry,format_name='mp3')
        for change in (dict(frames=True),dict(frame_rate=1,frames=96000),dict(channels=3),
                       dict(sample_width=3),dict(frame_rate='24000')):
            with self.subTest(change=change),ExitStack() as stack:
                mocks=[stack.enter_context(patch(target)) for target in (
                    'audio_pcm_mux.capture_scope_file','audio_pcm_mux.get_ffmpeg_runtime_identity',
                    'audio_mastering.get_loudness_measurements','audio_pcm_mux.run_owned_media_check')]
                with self.assertRaises(ValueError):
                    export_bounded_pcm('/unread/input.pcm','/unread/output.mp3',
                        {**geometry,**change},None,quality_plan=plan,max_output_bytes=1024**2,mastering={})
                for mock in mocks:mock.assert_not_called()

    def test_mastered_native_output_and_hard_quota_keep_previous_delivery(self):
        audio=Sine(440,sample_rate=24000).to_audio_segment(duration=2000).apply_gain(-24)
        geometry=settings()
        for fmt in ('mp3','wav'):
            for allowance in ((1024**2,70000,10000) if fmt=='mp3' else (1024**2,10000)):
                with self.subTest(format=fmt,allowance=allowance),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp).resolve();raw=root/'input.pcm';raw.write_bytes(audio.raw_data)
                    output=root/('output.'+fmt);output.write_bytes(b'prior complete delivery')
                    plan=get_pcm_export_encoding_plan(geometry,format_name=fmt)
                    reports=[]
                    def encode():
                        return export_bounded_pcm(raw,output,geometry,None,quality_plan=plan,
                            max_output_bytes=allowance,mastering={},quality_callback=reports.append)
                    if allowance==10000:
                        with self.assertRaises(ValueError):encode()
                        self.assertEqual(b'prior complete delivery',output.read_bytes())
                    else:
                        report=encode()
                        self.assertEqual('verified',reports[-1]['status'])
                        self.assertLessEqual(output.stat().st_size,allowance)
                        self.assertIn(report['mode'],('linear','dynamic'))
                        self.assertEqual({'loudness':-18,'true_peak':-2},report['settings'])
                    self.assertEqual(audio.raw_data,raw.read_bytes())
                    self.assertEqual({'input.pcm','output.'+fmt},{p.name for p in root.iterdir()})

    def test_late_pcm_edit_and_runtime_change_refuse_before_output_replace(self):
        from audio_encoder_identity import get_ffmpeg_runtime_identity, FFmpegRuntimeIdentity
        audio=Sine(440,sample_rate=24000).to_audio_segment(duration=2000).apply_gain(-24)
        geometry=settings();plan=get_pcm_export_encoding_plan(geometry,format_name='mp3')
        runtime=get_ffmpeg_runtime_identity();changed=runtime.model_dump();changed['version_sha256']='0'*64
        changed=FFmpegRuntimeIdentity.model_validate(changed)
        for fault in ('source','runtime'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();raw=root/'input.pcm';raw.write_bytes(audio.raw_data)
                output=root/'output.mp3';output.write_bytes(b'prior complete delivery')
                worker=audio_pcm_mux.run_owned_media_check
                def late_edit(*args,**kwargs):
                    result=worker(*args,**kwargs)
                    raw.write_bytes(b'\0'*len(audio.raw_data))
                    return result
                with patch.object(audio_pcm_mux,'get_ffmpeg_runtime_identity',side_effect=[runtime,changed] if fault=='runtime' else None,
                                  **({} if fault=='runtime' else {'return_value':runtime})), \
                        patch.object(audio_pcm_mux,'run_owned_media_check',side_effect=late_edit if fault=='source' else worker):
                    with self.assertRaisesRegex(ValueError,'changed'):
                        export_bounded_pcm(raw,output,geometry,None,quality_plan=plan,max_output_bytes=1024**2)
                self.assertEqual(b'prior complete delivery',output.read_bytes())
                self.assertEqual({'input.pcm','output.mp3'},{p.name for p in root.iterdir()})

    def test_mastered_resampling_and_silent_native_sample_drop_use_full_parent_gate(self):
        import subprocess
        from media_process import get_media_process_environment
        from export_quality import ExportQualityError
        audio=Sine(440,sample_rate=30000).to_audio_segment(duration=2000).apply_gain(-24)
        geometry=dict(frames=60000,frame_rate=30000,channels=1,sample_width=2,
            source_frame_ranges=[[0,60000]],intentional_pause_frames=[])
        plan=get_pcm_export_encoding_plan(geometry,format_name='mp3')
        self.assertEqual(32000,plan['sample_rate'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();raw=root/'input.pcm';raw.write_bytes(audio.raw_data)
            output=root/'output.mp3';reports=[]
            def encode():
                return export_bounded_pcm(raw,output,geometry,None,quality_plan=plan,
                    max_output_bytes=1024**2,mastering={},quality_callback=reports.append)
            encode();self.assertEqual('verified',reports[-1]['status']);prior=output.read_bytes()
            worker=audio_pcm_mux.run_owned_media_check
            def drop_samples(command,*args,**kwargs):
                result=worker(command,*args,**kwargs)
                target=Path(command[-1])/'output.mp3'
                # An intentional fault: a native encoder exits successfully,
                # leaving valid MP3 with only half the approved source duration.
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','s16le','-ar','30000',
                    '-ac','1','-i',str(raw),'-t','1','-ar','32000','-c:a','libmp3lame','-b:a','192k',
                    str(target)],check=True,capture_output=True,timeout=10,env=get_media_process_environment())
                return result
            with patch.object(audio_pcm_mux,'run_owned_media_check',side_effect=drop_samples):
                with self.assertRaises(ExportQualityError) as failure:encode()
            self.assertEqual('failed',failure.exception.report['status'])
            self.assertEqual(prior,output.read_bytes())
            self.assertEqual({'input.pcm','output.mp3'},{p.name for p in root.iterdir()})
