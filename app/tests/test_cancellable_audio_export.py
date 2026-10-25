"""Native encoder cancellation reaps descendants and preserves prior output."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from pydub import AudioSegment
import audio_export
from tts import ExportCancelled


class CancellableAudioExportTests(unittest.TestCase):
    def test_fresh_wav_worker_needs_no_application_or_model_import(self):
        import subprocess
        code = '''
import importlib.abc,sys,tempfile,wave
from pathlib import Path
class Reject(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in ('tts','torch','pydantic'):
   raise ImportError('Forbidden application import: '+fullname)
sys.meta_path.insert(0,Reject())
from audio_export import write_pcm_wav
with tempfile.TemporaryDirectory() as tmp:
 raw=Path(tmp)/'input.pcm';raw.write_bytes(b'\\x01\\x00'*24000)
 output=Path(tmp)/'output.wav'
 if not write_pcm_wav(raw,output,dict(format='wav',options={},sample_width=2,channels=1,frame_rate=24000)):
  raise RuntimeError('Native writer refused')
 with wave.open(str(output),'rb') as stream:
  if stream.readframes(24000) != raw.read_bytes():raise RuntimeError('PCM changed')
'''
        result = subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)


    def test_real_wav_encoding_preserves_pcm_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'chapter.wav'
            segment=AudioSegment.silent(duration=300,frame_rate=24000)
            audio_export.export_audio_segment(segment,path,'wav',lambda:False)
            loaded=AudioSegment.from_wav(path)
            self.assertEqual(segment.raw_data,loaded.raw_data)
            self.assertEqual(segment.frame_rate,loaded.frame_rate)
            self.assertEqual(['chapter.wav'],sorted(p.name for p in Path(tmp).iterdir()))

    @unittest.skipUnless(sys.platform=='linux','native Linux descendant ownership test')
    def test_cancel_during_native_worker_reaps_child_and_preserves_previous_chapter(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'chapter.mp3';path.write_bytes(b'previous complete chapter')
            marker=root/'workers.json';start=audio_export.start_owned_subprocess
            processes=[]
            script="""import subprocess,sys,os,json,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
open(sys.argv[1],'wb').write(b'partial new chapter')
open(sys.argv[2],'w').write(json.dumps([os.getpid(),child.pid]))
time.sleep(60)
"""
            def launch(command,**kwargs):
                process=start([sys.executable,'-c',script,command[3],str(marker)],**kwargs)
                processes.append(process)
                return process
            deadline=time.monotonic()+10
            def cancel():
                if time.monotonic()>deadline:
                    self.fail('worker did not reach cancellation fixture')
                return marker.exists()
            with patch.object(audio_export,'start_owned_subprocess',side_effect=launch):
                with self.assertRaises(ExportCancelled):
                    audio_export.export_audio_segment(AudioSegment.silent(duration=300),path,'mp3',cancel)
            self.assertEqual(b'previous complete chapter',path.read_bytes())
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual([],list(root.glob('.chapter-export-*')))
            for pid in json.loads(marker.read_text()):self.assertFalse(Path('/proc',str(pid)).exists())

    def test_encoder_failure_preserves_previous_output_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'chapter.mp3';path.write_bytes(b'previous audio')
            with self.assertRaisesRegex(RuntimeError,'Audio encoding failed'):
                audio_export.export_audio_segment(AudioSegment.silent(duration=30),path,'invalid-format',lambda:False)
            self.assertEqual(b'previous audio',path.read_bytes())
            self.assertEqual([],list(root.glob('.chapter-export-*')))
            report=root/'encoder-error.json';report.touch()
            modes=[None]
            from synthesis_workspace import is_synthesis_workspace_supported
            if is_synthesis_workspace_supported():
                identities={}
                for name in ('input.pcm','output.mp3','encoder-error.json'):
                    member=root/name;member.touch();info=member.stat();identities[name]=(info.st_dev,info.st_ino)
                modes.append({'format':'mp3','registered_files':identities})
            cases={
                'oversize':json.dumps({'module':'builtins','type':'ValueError','args':['x'*65537]}),
                'deep':'{"module":"builtins","type":"ValueError","args":'+'['*2000+'"nested"'+']'*2000+'}',
                'duplicate':'{"module":"builtins","module":"builtins","type":"ValueError","args":["bad"]}',
                'nonfinite':'{"module":"builtins","type":"ValueError","args":[NaN]}',
                'positive-overflow':'{"module":"builtins","type":"ValueError","args":[1e9999]}',
                'negative-overflow':'{"module":"builtins","type":"ValueError","args":[-1e9999]}',
                'unicode-overflow':json.dumps({'module':'builtins','type':'UnicodeTranslateError','args':['x',10**100,1,'bad']})
            }
            for settings in modes:
                for label,payload in cases.items():
                    with self.subTest(report=label,registered=settings is not None):
                        report.write_text(payload);fallback=RuntimeError('original encoder failure')
                        self.assertIs(fallback,audio_export.get_encoder_exception(report,fallback,settings))
                report.write_text(json.dumps({'module':'builtins','type':'ValueError','args':['ordinary encoder error']}))
                error=audio_export.get_encoder_exception(report,RuntimeError('fallback'),settings)
                self.assertIsInstance(error,ValueError);self.assertEqual(('ordinary encoder error',),error.args)
                report.write_text(json.dumps({'module':'builtins','type':'OSError','args':[10**100,'bad']}))
                error=audio_export.get_encoder_exception(report,RuntimeError('fallback'),settings)
                self.assertIsInstance(error,OSError);self.assertEqual((10**100,'bad'),error.args)
            for invalid in ({},'invalid',[],True):
                fallback=RuntimeError('original encoder failure')
                self.assertIs(fallback,audio_export.get_encoder_exception(report,fallback,invalid))

    def test_real_mp3_encoding_produces_decodable_audio(self):
        from pydub.generators import Sine
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'book';directory.mkdir()
            path=directory/'chapter.mp3'
            report=Path(tmp)/'external-report.log'
            report_name=str(report).replace('\\','\\\\').replace(':','\\:').replace("'","\\'")
            segment=Sine(220,sample_rate=24000).to_audio_segment(duration=600)
            with patch.dict(os.environ,{'FFREPORT':'file='+report_name+':level=48'}):
                audio_export.export_audio_segment(segment,path,'mp3',lambda:False,bitrate='192k')
            with path.open('rb') as stream:loaded=AudioSegment.from_mp3(stream)
            self.assertAlmostEqual(len(segment),len(loaded),delta=2)
            self.assertGreater(loaded.rms,100)
            self.assertEqual(['chapter.mp3'],sorted(p.name for p in directory.iterdir()))
            self.assertFalse(report.exists())

    @unittest.skipUnless(sys.platform=='linux','native Linux descendant ownership test')
    def test_full_and_incremental_chapter_encoder_cancel_preserves_outputs_and_manifest(self):
        from tests.test_chapter_export import _project,_tone
        from project import CHAPTER_EXPORT_DIR
        for changed_only in (False,True):
            with self.subTest(changed_only=changed_only),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);pm,chunks=_project(tmp)
                with patch.object(pm,'load_chunks',return_value=chunks):
                    ok,message=pm.export_chapters(fmt='wav')
                    self.assertTrue(ok,message)
                    output=root/CHAPTER_EXPORT_DIR
                    before={path.name:path.read_bytes() for path in output.iterdir()}
                    _tone(str(root/'voicelines/c1.wav'),.5,hz=800)
                    marker=root/'started';start=audio_export.start_owned_subprocess
                    processes=[]
                    def launch(command,**kwargs):
                        script="import sys,time;open(sys.argv[1],'wb').write(b'partial');open(sys.argv[2],'w').write('started');time.sleep(60)"
                        process=start([sys.executable,'-c',script,command[3],str(marker)],**kwargs)
                        processes.append(process)
                        return process
                    with patch.object(audio_export,'start_owned_subprocess',side_effect=launch):
                        result=pm.export_chapters(fmt='wav',changed_only=changed_only,cancel_check=marker.exists)
                    self.assertEqual((False,'Export cancelled'),result)
                    self.assertEqual(1,len(processes))
                    self.assertIsNotNone(processes[0].poll())
                    self.assertEqual(before,{path.name:path.read_bytes() for path in output.iterdir()})
