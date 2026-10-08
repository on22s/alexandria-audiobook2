"""Known immutable snapshots and actual comparison artifacts with CPU-only SDKs."""
import contextlib
import importlib.util
from importlib import metadata
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

REPO=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('cloud_comparison_provenance',REPO/'run_chains/cloud_comparison_provenance.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)


class CloudComparisonModelIdentityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'native Linux owned-hook cleanup')
    def test_source_fsmonitor_timeout_reaps_hook_and_clean_control_still_matches(self):
        import shlex
        import time
        source = self.source_repo()
        pidfile = self.root / 'probe-hook.pid'
        hook = self.root / 'probe-hook'
        code = f'import os,pathlib,time;pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(3)'
        hook.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code) + '\n')
        hook.chmod(0o700)
        subprocess.run(['git', '-C', str(source), 'config', 'core.fsmonitor', str(hook)], check=True)
        started = time.monotonic()
        with patch.object(helper, 'GIT_PROBE_TIMEOUT_SECONDS', .2, create=True):
            with self.assertRaises(subprocess.TimeoutExpired):
                helper.get_comparison_source_commit(source)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(pidfile.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        subprocess.run(['git', '-C', str(source), 'config', '--unset', 'core.fsmonitor'], check=True)
        expected = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(expected, helper.get_comparison_source_commit(source))

    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.commit='a'*40;self.snapshot=self.root/'cache/snapshots'/self.commit;self.snapshot.mkdir(parents=True)
        self.download=unittest.mock.Mock(return_value=str(self.snapshot));self.info=unittest.mock.Mock(return_value=types.SimpleNamespace(sha=self.commit))
        self.hub=types.SimpleNamespace(HfApi=lambda:types.SimpleNamespace(model_info=self.info),snapshot_download=self.download)
        self.modules=patch.dict(sys.modules,{'huggingface_hub':self.hub,'cloud_comparison_provenance':helper});self.modules.start();self.addCleanup(self.modules.stop)

    def test_moving_branch_resolves_once_then_only_committed_snapshot_downloads(self):
        result=helper.ensure_comparison_model_snapshot('Org/Model','main')
        self.assertEqual(self.commit,result['revision']);self.assertEqual(str(self.snapshot),result['snapshot_path'])
        self.info.assert_called_once();self.assertEqual(self.commit,self.download.call_args.kwargs['revision'])

    def test_full_commit_replay_does_not_resolve_moving_branch(self):
        patterns=['weights.bin'];original=list(patterns)
        result=helper.ensure_comparison_model_snapshot('Org/Model',self.commit,patterns)
        self.info.assert_not_called();self.assertEqual(original,patterns)
        self.assertEqual(patterns,self.download.call_args.kwargs['allow_patterns']);self.assertEqual(self.commit,result['revision'])

    def test_invalid_revision_and_wrong_or_missing_snapshot_fail_without_plausible_identity(self):
        for revision in (None,'main','bad',True):
            self.info.return_value=types.SimpleNamespace(sha=revision)
            with self.subTest(revision=revision),self.assertRaises(ValueError):helper.ensure_comparison_model_snapshot('Org/Model')
        self.info.return_value=types.SimpleNamespace(sha=self.commit)
        for path in (self.root/'other/snapshots'/('b'*40),self.root/'not-a-snapshot'/self.commit):
            path.mkdir(parents=True);self.download.return_value=str(path)
            with self.subTest(path=path),self.assertRaises(ValueError):helper.ensure_comparison_model_snapshot('Org/Model')
        self.download.return_value=str(self.root/'absent/snapshots'/self.commit)
        with self.assertRaises(ValueError):helper.ensure_comparison_model_snapshot('Org/Model')

    def test_metadata_missing_or_expected_package_version_mismatch_fails(self):
        with patch.object(metadata,'version',return_value='1.2.3'):
            self.assertEqual({'qwen-tts':'1.2.3'},helper.get_comparison_package_versions(['qwen-tts']))
            with self.assertRaises(ValueError):helper.get_comparison_package_versions(['qwen-tts'],{'qwen-tts':'other'})
        with patch.object(metadata,'version',return_value=None):
            with self.assertRaises(ValueError):helper.get_comparison_package_versions(['qwen-tts'])
        with patch.object(metadata,'version',side_effect=metadata.PackageNotFoundError('missing')):
            with self.assertRaises(metadata.PackageNotFoundError):helper.get_comparison_package_versions(['qwen-tts'])

    def source_repo(self):
        source=self.root/'source';source.mkdir();(source/'source.py').write_text('known source\n')
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.com'),
                     ('config','core.hooksPath',str(self.root/'no-hooks')),('add','source.py'),('commit','-qm','known source')):
            subprocess.run(['git','-C',str(source),*args],check=True,capture_output=True)
        return source

    def test_source_commit_names_real_clean_git_blob_and_dirty_source_is_refused(self):
        source=self.source_repo();commit=helper.get_comparison_source_commit(source)
        self.assertEqual(subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip(),commit)
        (source/'source.py').write_text('changed source\n')
        with self.assertRaises(ValueError):helper.get_comparison_source_commit(source)
        subprocess.run(['git','-C',str(source),'checkout','--','source.py'],check=True,capture_output=True)
        (source/'untracked.py').write_text('unrecorded source\n')
        with self.assertRaises(ValueError):helper.get_comparison_source_commit(source)

    def compare(self, kind, gpu_probe=None):
        scripts=self.root/'run_chains';scripts.mkdir();builddir=self.root/'ab_test_runtime/reference_spread';builddir.mkdir(parents=True)
        (builddir/'build_spread3.json').write_text(json.dumps({'ref_sample':'ref.wav','ref_text':'Reference speech'}))
        (self.root/'ref.wav').write_bytes(b'known reference fixture')
        wrapper=self.root/'gpu_job.sh';wrapper.write_text('#!/bin/sh\n[ "$1" = --check-lock-owner ]\n');wrapper.chmod(0o755)
        source=self.source_repo() if kind=='chatterbox' else self.root
        calls=[]
        def load(path=None,**kwargs):calls.append((path,kwargs));return model
        class Wav:
            shape=(1,2)
            def cpu(self):return self
        model=types.SimpleNamespace(sr=2,generate_voice_clone=lambda **kwargs:([[.1,.2]],2),generate=lambda *args,**kwargs:Wav())
        klass=types.SimpleNamespace(from_pretrained=load,from_local=load)
        def save(path,*args):
            with wave.open(str(path),'wb') as audio:
                audio.setparams((1,2,2,0,'NONE','not compressed'));audio.writeframes(b'\x00\x10'*2)
        fake_torch=types.SimpleNamespace(bfloat16=object(),__version__='fixture',manual_seed=lambda value:None)
        modules={'torch':fake_torch,'soundfile':types.SimpleNamespace(write=save),
                 'qwen_tts':types.SimpleNamespace(Qwen3TTSModel=klass),'torchaudio':types.SimpleNamespace(save=save),
                 'chatterbox':types.ModuleType('chatterbox'),'chatterbox.mtl_tts':types.SimpleNamespace(ChatterboxMultilingualTTS=klass)}
        out=self.root/'out';out.mkdir();env=dict(ROOT=str(self.root),OUT=str(out),SRC=str(source),ALEXANDRIA_GPU_LOCK_HELD='1',
                                               QWEN_MODEL_REVISION=self.commit,CHATTERBOX_MODEL_REVISION=self.commit)
        original=subprocess.check_output
        def capture(argv,*args,**kwargs):
            if argv[0]=='nvidia-smi':
                return gpu_probe(kwargs) if gpu_probe else 'fixture gpu'
            return original(argv,*args,**kwargs)
        with patch.dict(sys.modules,modules),patch.dict(os.environ,env),patch.object(metadata,'version',return_value='1.2.3'), \
             patch.object(subprocess,'check_output',side_effect=capture),contextlib.redirect_stdout(io.StringIO()):
            if kind=='qwen':
                path=Path('/tmp/cloud_qwen_before300.py') if os.environ.get('CLOUD_IDENTITY_BASELINE') else REPO/'run_chains/cloud_tts_qwen_compare_20260824.py'
                script=scripts/'compare.py';script.write_text(path.read_text());runpy.run_path(str(script),run_name='__main__')
            else:
                path=Path('/tmp/cloud_chatterbox_before299.sh') if os.environ.get('CLOUD_IDENTITY_BASELINE') else REPO/'run_chains/cloud_tts_chatterbox_compare_20260824.sh'
                program=path.read_text().split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
                exec(compile(program,str(path),'exec'),{'__name__':'__main__'})
        document=json.loads((out/'result.json').read_text())
        self.assertIn('model_identity',document)
        self.assertEqual(self.commit,document['model_identity']['revision'])
        self.assertEqual(str(self.snapshot),calls[0][0]);self.assertEqual(1,len(calls))
        self.assertEqual({'qwen-tts' if kind=='qwen' else 'chatterbox-tts':'1.2.3'},document['package_versions'])
        self.assertEqual(20260824,document['seed']);self.assertEqual(1,document['audio_seconds'])
        if kind=='chatterbox':self.assertEqual(helper.get_comparison_source_commit(source),document['source_commit'])
        return document

    def test_actual_qwen_result_records_loaded_snapshot_and_package_version(self):self.compare('qwen')
    def test_actual_chatterbox_heredoc_records_loaded_snapshot_source_and_package(self):self.compare('chatterbox')


    def test_qwen_metadata_timeout_publishes_audio_with_explicit_missing_identity(self):
        def probe(options):
            self.assertIn('timeout',options)
            self.assertLessEqual(options['timeout'],5)
            return subprocess.check_output([sys.executable,'-c','import time;time.sleep(60)'],text=True,timeout=.05)
        result=self.compare('qwen',gpu_probe=probe)
        self.assertIsNone(result['gpu'])
        self.assertEqual('unavailable',result['gpu_metadata']['status'])
        self.assertIn('TimeoutExpired',result['gpu_metadata']['error'])
        with wave.open(str(self.root/'out/audiobook_passage.wav')) as audio:
            self.assertEqual(2,audio.getnframes())

    def test_qwen_missing_metadata_executable_publishes_named_failure(self):
        def probe(options):raise FileNotFoundError('synthetic missing metadata executable')
        result=self.compare('qwen',gpu_probe=probe)
        self.assertIsNone(result['gpu'])
        self.assertEqual('unavailable',result['gpu_metadata']['status'])
        self.assertIn('FileNotFoundError',result['gpu_metadata']['error'])

    def test_qwen_empty_metadata_is_not_a_measured_identity(self):
        result=self.compare('qwen',gpu_probe=lambda options:'   ')
        self.assertIsNone(result['gpu'])
        self.assertEqual('unavailable',result['gpu_metadata']['status'])
        self.assertIn('empty',result['gpu_metadata']['error'])
