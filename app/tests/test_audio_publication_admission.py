"""Real PCM and publication/cleanup interleavings without model inference."""
from pathlib import Path
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
import audio_validation as subject
from tts import TTSEngine


class AudioPublicationAdmissionTests(unittest.TestCase):
    def test_cleanup_cannot_unlink_replacement_published_after_its_observation(self):
        for mode in ('atomic_publication','local_wav_write'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);output=root/'render.wav';stage=root/'fresh.wav'
                sf.write(output,np.full(2400,.1),24000);sf.write(stage,np.full(2400,.2),24000)
                fresh=stage.read_bytes();observed=threading.Event();resume=threading.Event();started=threading.Event();published=threading.Event();errors=[]
                unlink=os.remove
                def paused(path):
                    if Path(path)==output:
                        observed.set()
                        if not resume.wait(5):raise AssertionError('cleanup was not released')
                    unlink(path)
                def cleanup():
                    try:subject.remove_stale_audio(output)
                    except BaseException as error:errors.append(error)
                def publish():
                    started.set()
                    try:
                        if mode=='atomic_publication':self.assertTrue(subject.publish_audio_output(stage,output))
                        else:TTSEngine._save_wav(np.full(2400,.2),24000,str(output))
                        published.set()
                    except BaseException as error:errors.append(error)
                cleaner=threading.Thread(target=cleanup);writer=threading.Thread(target=publish)
                with patch.object(subject.os,'remove',side_effect=paused):
                    cleaner.start()
                    try:
                        self.assertTrue(observed.wait(2));writer.start();self.assertTrue(started.wait(2))
                        self.assertFalse(published.wait(.1),'writer published inside stale cleanup admission')
                    finally:
                        resume.set();cleaner.join(5)
                        if writer.ident is not None:writer.join(5)
                self.assertFalse(cleaner.is_alive() or writer.is_alive());self.assertEqual([],errors)
                self.assertTrue(published.is_set());self.assertEqual(fresh,output.read_bytes())
                pcm,rate=sf.read(output);self.assertEqual(24000,rate);np.testing.assert_allclose(pcm,.2,atol=4e-5)
                self.assertEqual(str(output),str(subject.validate_generated_audio(str(output))))

    def test_absent_output_and_broken_symlink_are_cleared_without_following_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'render.wav';subject.remove_stale_audio(path)
            target=root/'absent-target.wav';path.symlink_to(target);subject.remove_stale_audio(path)
            self.assertFalse(os.path.lexists(path));self.assertFalse(target.exists())
            sf.write(target,np.full(2400,.1),24000);before=target.read_bytes();path.symlink_to(target)
            subject.remove_stale_audio(path);self.assertEqual(before,target.read_bytes())

    def test_failed_local_write_cannot_leave_old_audio_and_releases_publication_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'render.wav';sf.write(path,np.full(2400,.1),24000)
            with patch.object(sf,'write',side_effect=OSError('write failed')),self.assertRaisesRegex(OSError,'write failed'):
                TTSEngine._save_wav(np.full(2400,.2),24000,str(path))
            self.assertFalse(path.exists())
            TTSEngine._save_wav(np.full(2400,.2),24000,str(path))
            pcm,_=sf.read(path);np.testing.assert_allclose(pcm,.2,atol=4e-5)

    def test_cancellation_is_rechecked_after_waiting_for_publication_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'render.wav';stage=root/'private.wav';sf.write(stage,np.full(2400,.2),24000)
            cancelled=threading.Event();started=threading.Event();result=[];errors=[]
            def publish():
                started.set()
                try:result.append(subject.publish_audio_output(stage,path,cancelled))
                except BaseException as error:errors.append(error)
            worker=threading.Thread(target=publish)
            with subject.ensure_audio_output(path):
                worker.start();self.assertTrue(started.wait(2));cancelled.set()
            worker.join(5);self.assertFalse(worker.is_alive());self.assertEqual([],errors);self.assertEqual([False],result)
            self.assertFalse(path.exists());self.assertTrue(stage.exists())

    def test_independent_process_publication_waits_for_the_same_kernel_admission(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/'render.wav';stage=root/'private.wav'
            sf.write(output,np.full(2400,.1),24000);sf.write(stage,np.full(2400,.2),24000);old=output.read_bytes();fresh=stage.read_bytes()
            code="from audio_validation import publish_audio_output;import sys;print('ready',flush=True);assert publish_audio_output(sys.argv[1],sys.argv[2]);print('published',flush=True)"
            worker=None
            try:
                with subject.ensure_audio_output(output):
                    worker=subprocess.Popen([sys.executable,'-c',code,str(stage),str(output)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                    self.assertEqual('ready',worker.stdout.readline().strip())
                    with self.assertRaises(subprocess.TimeoutExpired):worker.communicate(timeout=.15)
                    self.assertEqual(old,output.read_bytes());self.assertIsNone(worker.poll())
                stdout,stderr=worker.communicate(timeout=5)
                self.assertEqual(0,worker.returncode,stderr);self.assertIn('published',stdout)
                self.assertEqual(fresh,output.read_bytes());self.assertFalse(stage.exists())
            finally:
                if worker is not None and worker.poll() is None:worker.kill();worker.communicate(timeout=5)

    def test_lock_namespace_is_bounded_alias_stable_and_does_not_pollute_audio_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/'audio';directory.mkdir();path=directory/'render.wav'
            targets={subject.get_audio_lock_target(directory/f'{index}.wav') for index in range(8192)}
            self.assertLessEqual(len(targets),4096)
            self.assertEqual(subject.get_audio_lock_target(path),subject.get_audio_lock_target(directory/'..'/'audio'/'render.wav'))
            TTSEngine._save_wav(np.full(2400,.2),24000,str(path))
            self.assertEqual(['render.wav'],[item.name for item in directory.iterdir()])
            missing=root/'not-created'/'render.wav';subject.remove_stale_audio(missing)
            self.assertFalse(missing.parent.exists())
