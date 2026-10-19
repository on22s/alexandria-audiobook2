"""Real short FFmpeg checks, transactional chapter reuse and report identity."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine
import audio_mastering as mastering
from audio_export import export_audio_segment
from project import ProjectManager, CHAPTER_EXPORT_DIR
from tts import ExportCancelled


class AudioMasteringTests(unittest.TestCase):
    def test_real_wav_and_mp3_measure_final_file_without_mutating_source(self):
        for fmt,channels in [('wav',1),('mp3',2)]:
            with self.subTest(fmt=fmt),tempfile.TemporaryDirectory() as tmp:
                segment=Sine(440,sample_rate=24000).to_audio_segment(duration=5000).apply_gain(-24).set_channels(channels)
                before=segment.raw_data;output=Path(tmp)/('out.'+fmt)
                report=export_audio_segment(segment,output,fmt,lambda:False,mastering={'loudness':-18,'true_peak':-2})
                self.assertEqual(before,segment.raw_data)
                self.assertEqual(mastering.get_audio_sha256(output),report['sha256'])
                self.assertLess(abs(report['after']['integrated_lufs']-(-18)),1)
                self.assertLess(report['after']['true_peak_dbtp'],-1)
                with output.open('rb') as stream:decoded=AudioSegment.from_file(stream,format=fmt)
                self.assertEqual(24000,decoded.frame_rate);self.assertEqual(channels,decoded.channels)
                self.assertEqual(['out.'+fmt],sorted(p.name for p in Path(tmp).iterdir()))
                # Separate FFmpeg meter confirms the report describes the encoded file.
                measured=subprocess.run(['ffmpeg','-hide_banner','-nostats','-i',str(output),'-af','ebur128=peak=true','-f','null','-'],capture_output=True,text=True,check=True).stderr
                import re
                values=re.findall(r'I:\s*(-?[\d.]+) LUFS',measured)
                self.assertTrue(values)
                self.assertAlmostEqual(report['after']['integrated_lufs'],float(values[-1]),delta=.2)

    def test_silence_failure_and_cancellation_preserve_previous_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'out.wav';output.write_bytes(b'previous verified output')
            with self.assertRaisesRegex(ValueError,'finite loudness'):
                export_audio_segment(AudioSegment.silent(duration=5000,frame_rate=24000),output,'wav',lambda:False,mastering={})
            self.assertEqual(b'previous verified output',output.read_bytes())
            with self.assertRaises(ExportCancelled):
                export_audio_segment(Sine(440).to_audio_segment(duration=4000),output,'wav',lambda:True,mastering={})
            self.assertEqual(b'previous verified output',output.read_bytes())
            self.assertEqual(['out.wav'],[p.name for p in Path(tmp).iterdir()])

    def test_rejects_bad_measurements_and_unbounded_targets(self):
        for value in [float('nan'),float('inf'),-100,0]:
            with self.assertRaises(ValueError):mastering.get_mastering_options({'loudness':value})
        for text in ['', '{"input_i":"-inf","input_tp":"-inf","input_lra":"0","input_thresh":"-70","target_offset":"inf"}']:
            with self.assertRaises(ValueError):mastering.get_loudnorm_record(text)
        with self.assertRaises(RuntimeError):
            mastering.run_mastering_ffmpeg(['-i','/file-that-does-not-exist','-f','null','-'])

    def test_chapter_options_invalidate_reuse_and_failed_mastering_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);audio=root/'tone.wav';Sine(330,sample_rate=24000).to_audio_segment(duration=4500).apply_gain(-20).export(audio,format='wav').close()
            pm=ProjectManager(tmp);pm.save_chunks([{'id':0,'uid':'a','speaker':'Hero','text':'Hello.','status':'done','audio_path':'tone.wav'}])
            reports=[]
            ok,msg=pm.export_chapters(fmt='wav',mastering={},mastering_callback=reports.append);self.assertTrue(ok,msg);self.assertEqual(1,len(reports))
            directory=root/CHAPTER_EXPORT_DIR;before={p.name:p.read_bytes() for p in directory.iterdir()}
            self.assertEqual([],pm.preview_chapter_filenames(fmt='wav',changed_only=True,mastering={}))
            self.assertEqual(1,len(pm.preview_chapter_filenames(fmt='wav',changed_only=True)))
            self.assertEqual(1,len(pm.preview_chapter_filenames(fmt='wav',changed_only=True,mastering={'loudness':-20})))
            with patch.object(mastering,'export_mastered_pcm',side_effect=RuntimeError('synthetic mastering failure')):
                with self.assertRaises(RuntimeError):pm.export_chapters(fmt='wav',mastering={'loudness':-20})
            self.assertEqual(before,{p.name:p.read_bytes() for p in directory.iterdir()})

    def test_m4b_preserves_chapters_and_metadata_with_final_encoded_measurements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tone=Sine(220,sample_rate=24000).to_audio_segment(duration=4500).apply_gain(-18)
            tone.export(root/'tone.wav',format='wav').close()
            pm=ProjectManager(tmp);pm.save_chunks([{'id':0,'uid':'a','speaker':'NARRATOR','text':'Chapter 1','status':'done','audio_path':'tone.wav'}])
            reports=[]
            ok,msg=pm.merge_m4b(metadata={'title':'Synthetic title','author':'Synthetic author'},mastering={},mastering_callback=reports.append)
            self.assertTrue(ok,msg);self.assertEqual(1,len(reports))
            result=json.loads(subprocess.run(['ffprobe','-v','error','-show_chapters','-show_format','-of','json',str(root/'audiobook.m4b')],capture_output=True,text=True,check=True).stdout)
            self.assertEqual('Synthetic title',result['format']['tags']['title'])
            self.assertEqual('Synthetic author',result['format']['tags']['artist'])
            self.assertEqual(1,len(result['chapters']))
            self.assertEqual(mastering.get_audio_sha256(root/'audiobook.m4b'),reports[0]['sha256'])
            self.assertEqual([],list(root.glob('.m4b-*')))

    def test_native_cancel_and_timeout_reap_started_owned_processes(self):
        import os
        import sys
        import time
        for mode in ('cancel','timeout'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                marker=Path(tmp)/'started.json';processes=[];start=mastering.start_owned_subprocess
                script="import os,sys,json,time,subprocess;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);open(sys.argv[1],'w').write(json.dumps([os.getpid(),child.pid]));time.sleep(60)"
                def launch(command,**kwargs):
                    process=start([sys.executable,'-c',script,str(marker)],**kwargs);processes.append(process);return process
                with patch.object(mastering,'start_owned_subprocess',side_effect=launch):
                    if mode=='cancel':
                        with self.assertRaises(ExportCancelled):mastering.run_mastering_ffmpeg(['ignored'],marker.exists,timeout=5)
                    else:
                        with self.assertRaises(subprocess.TimeoutExpired):mastering.run_mastering_ffmpeg(['ignored'],timeout=.3)
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(marker.exists())
                if sys.platform=='linux':
                    for pid in json.loads(marker.read_text()):self.assertFalse(Path('/proc',str(pid)).exists())

    def test_report_endpoint_refuses_stale_bytes_books_paths_and_active_tasks(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root=Path(tmp);pm=ProjectManager(tmp);pm.save_chunks([{'id':0,'uid':'a','speaker':'Hero','text':'Hi'}]);output=root/'out.wav';output.write_bytes(b'synthetic output')
            state={name:{'running':False,'mastering_reports':[]} for name in ('audio','m4b_export','chapter_export')}
            with patch.object(editor,'DATA_DIR',tmp),patch.object(editor,'SCRIPT_PATH',str(root/'annotated_script.json')),patch.object(editor,'project_manager',pm),patch.object(editor,'process_state',state):
                token=editor.get_snapshot_digest(editor.get_editor_book_identity(tmp))
                report={'file':'out.wav','sha256':mastering.get_audio_sha256(output),'book_token':token}
                state['audio']['mastering_reports']=[report]
                app=FastAPI();app.include_router(editor.router);client=TestClient(app)
                def get():return client.get('/api/export_mastering',params={'expected_book_token':token})
                self.assertEqual([report],get().json()['reports'])
                state['audio']['running']=True;self.assertEqual([],get().json()['reports']);state['audio']['running']=False
                output.write_bytes(b'changed');self.assertEqual([],get().json()['reports'])
                output.unlink();self.assertEqual([],get().json()['reports'])
                target=Path(outside)/'private.wav';target.write_bytes(b'outside');output.symlink_to(target)
                report['sha256']=mastering.get_audio_sha256(target);self.assertEqual([],get().json()['reports'])
                report['file']='../outside.wav';self.assertEqual([],get().json()['reports'])
                self.assertEqual(409,client.get('/api/export_mastering',params={'expected_book_token':'f'*64}).status_code)
                self.assertEqual(b'outside',target.read_bytes())
