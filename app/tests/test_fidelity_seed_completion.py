"""Hand-known fidelity coverage, real PCM/ZIPs and native no-model cache reuse."""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import zipfile

from experiments import library_voice_fidelity as fidelity

REPO=Path(__file__).resolve().parents[2]


def pcm_bytes():
    stream=io.BytesIO()
    with wave.open(stream,'wb') as audio:
        audio.setparams((1,2,24000,0,'NONE','not compressed'));audio.writeframes(b'\x00\x10'*240)
    return stream.getvalue()


class FidelitySeedCompletionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)/'fixture with spaces'
        self.models=self.root/'lora_models';self.zips=self.root/'zips';self.work=self.root/'work'
        self.models.mkdir(parents=True);self.zips.mkdir()
        for name,dataset in (('voice_a','dataset_a'),('voice_b','dataset_b')):
            directory=self.models/name;directory.mkdir()
            (directory/'training_meta.json').write_text(json.dumps({'ref_sample_audio':'sources/'+dataset+'/ref.wav'}))
            with zipfile.ZipFile(self.zips/(dataset+'.zip'),'w') as archive:
                archive.writestr('val/metadata.jsonl','\n'.join(json.dumps({'audio_filepath':f'val/clip_{i}.wav','text':f'Known speech {i}'}) for i in range(2)))
                for i in range(2):archive.writestr(f'val/clip_{i}.wav',pcm_bytes())
        self.document=self.make_document(20260906,self.work)
        self.out=self.root/'result.json';self.save()

    def make_document(self, seed, work):
        results=[]
        for name,dataset in (('voice_a','dataset_a'),('voice_b','dataset_b')):
            directory=work/name;directory.mkdir(parents=True,exist_ok=True)
            rows=[]
            for i,score in enumerate((.2,.6)):
                human=directory/f'clip_{i}.wav';generated=directory/f'gen_{i}.wav'
                human.write_bytes(pcm_bytes());generated.write_bytes(pcm_bytes())
                rows.append({'human_wav':os.path.relpath(human,self.root),'gen_wav':os.path.relpath(generated,self.root),'ecapa':score})
            results.append({'adapter':name,'dataset':dataset,'rows':rows,'ecapa':.4})
        return {'seed':seed,'lines_per_adapter':20,'ecapa_error':None,'results':results}

    def save(self, document=None):self.out.write_text(json.dumps(self.document if document is None else document))

    def load(self):
        with patch.object(fidelity,'REPO',str(self.root)):
            return fidelity.get_completed_library_fidelity(self.out,self.models,self.zips,self.work,20,20260906)

    def test_complete_known_scope_and_low_scores_are_reusable_read_only(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.document,self.load())
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_wrong_seed_line_count_partial_or_duplicate_adapters_are_rejected(self):
        changes=[lambda d:d.update(seed=99),lambda d:d.update(seed=True),
                 lambda d:d.update(lines_per_adapter=19),lambda d:d.update(ecapa_error='failed'),
                 lambda d:d['results'].pop(),lambda d:d['results'].append(copy.deepcopy(d['results'][0])),
                 lambda d:d['results'][0].update(dataset='other')]
        for change in changes:
            document=copy.deepcopy(self.document);change(document);self.save(document)
            with self.subTest(document=document),self.assertRaises(ValueError):self.load()

    def test_partial_json_and_wrong_root_are_not_complete(self):
        for text in ('{"seed":20260906','[]','{"seed":20260906,"lines_per_adapter":20}'):
            self.out.write_text(text)
            with self.subTest(text=text),self.assertRaises(ValueError):self.load()

    def test_errors_missing_duplicate_clip_wrong_work_and_invalid_scores_are_rejected(self):
        changes=[lambda r:r.update(error='source zip not found'),lambda r:r['rows'].pop(),
                 lambda r:r['rows'][1].update(human_wav=r['rows'][0]['human_wav']),
                 lambda r:r['rows'][0].update(error='generation failed'),
                 lambda r:r['rows'][0].update(ecapa=None),lambda r:r['rows'][0].update(ecapa=True),
                 lambda r:r['rows'][0].update(ecapa=float('nan')),lambda r:r['rows'][0].update(ecapa=float('inf')),
                 lambda r:r['rows'][0].update(ecapa=1.1),lambda r:r.update(ecapa=.9),
                 lambda r:r['rows'][0].update(gen_wav='other-seed/gen_0.wav')]
        for change in changes:
            document=copy.deepcopy(self.document);change(document['results'][0]);self.save(document)
            with self.subTest(document=document),self.assertRaises(ValueError):self.load()

    def test_missing_and_truncated_audio_are_rejected(self):
        path=self.work/'voice_a/gen_0.wav';original=path.read_bytes()
        for payload in (b'',original[:48],b'not audio'):
            path.write_bytes(payload)
            with self.subTest(payload=payload),self.assertRaises(ValueError):self.load()
        path.unlink()
        with self.assertRaises(ValueError):self.load()

    def test_missing_source_zip_is_prerequisite_failure_not_a_rerun(self):
        (self.zips/'dataset_a.zip').unlink()
        with self.assertRaisesRegex(RuntimeError,'source ZIP not found'):self.load()

    def test_clip_selection_is_shared_with_actual_extraction(self):
        archive=self.zips/'dataset_a.zip'
        with zipfile.ZipFile(archive,'a') as handle:
            handle.writestr('val/extra.wav',pcm_bytes())
        expected=fidelity.get_fidelity_expected_clips(self.models,self.zips,1)
        extracted=fidelity.extract_val(archive,str(self.root/'extracted'),1)
        self.assertEqual(expected['voice_a'][1],[Path(path).name for path,text in extracted])
        self.assertEqual('Known speech 0',extracted[0][1])
        self.assertEqual(pcm_bytes(),Path(extracted[0][0]).read_bytes())

    def test_missing_audio_dependency_refuses_instead_of_requesting_gpu_rerun(self):
        import builtins
        original=builtins.__import__
        def missing(name,*args,**kwargs):
            if name=='soundfile':raise ModuleNotFoundError('soundfile unavailable')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=missing):
            with self.assertRaisesRegex(RuntimeError,'audio dependency'):
                self.load()

    def prepare_native_chain(self):
        (self.root/'run_chains').mkdir();(self.root/'app/experiments').mkdir(parents=True)
        (self.root/'app/env/bin').mkdir(parents=True);(self.root/'ab_test_runtime/logs').mkdir(parents=True)
        shutil.copyfile(REPO/'app/experiments/library_voice_fidelity.py',self.root/'app/experiments/library_voice_fidelity.py')
        shutil.copyfile(REPO/'run_chains/overnight_fidelity_replication_20260829.sh',self.root/'run_chains/overnight_fidelity_replication_20260829.sh')
        launcher=self.root/'app/env/bin/python'
        launcher.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' \"$@\"\n');launcher.chmod(0o755)
        templates=self.root/'templates';templates.mkdir()
        experiments=self.root/'ab_test_runtime/experiments';experiments.mkdir()
        for seed in range(20260906,20260914):
            name=f'library_fidelity_seed_{seed}_n20'
            document=self.make_document(seed,self.root/'ab_test_runtime'/name)
            text=json.dumps(document);(templates/(name+'.json')).write_text(text);(experiments/(name+'.json')).write_text(text)
        worker=self.root/'gpu_job.sh'
        worker.write_text('#!'+sys.executable+'\n'+r'''
import json,os,pathlib,sys
args=sys.argv[2:];root=pathlib.Path.cwd()
with open(root/'dispatches','a') as stream:stream.write(json.dumps(sys.argv[1:])+'\n')
seed=int(args[args.index('--seed')+1]);out=root/args[args.index('--out')+1]
if os.environ.get('FAIL_WORKER')=='1':sys.exit(7)
text=(root/'templates'/('library_fidelity_seed_'+str(seed)+'_n20.json')).read_text()
out.write_text('{"partial":true}' if os.environ.get('PARTIAL_WORKER')=='1' else text)
''');worker.chmod(0o755)
        self.env=dict(os.environ,ALEXANDRIA_VOICE_ZIPS=str(self.zips),
                      PYTHONPATH=str(REPO/'app'),WAIT_FOR_PID='')

    def run_native(self):
        return subprocess.run(['bash',str(self.root/'run_chains/overnight_fidelity_replication_20260829.sh')],
                              env=self.env,cwd=self.root,capture_output=True,text=True,timeout=30)

    def test_native_complete_seeds_skip_without_models_or_config(self):
        self.prepare_native_chain();result=self.run_native()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(8,result.stdout.count('validated complete'))
        self.assertFalse((self.root/'dispatches').exists())
        self.assertFalse((self.root/'app/config.json').exists())

    def test_native_partial_seed_is_repaired_once_then_second_run_changes_nothing(self):
        self.prepare_native_chain();out=self.root/'ab_test_runtime/experiments/library_fidelity_seed_20260906_n20.json'
        out.write_text('{"seed":20260906')
        result=self.run_native();self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        dispatches=(self.root/'dispatches').read_bytes();args=json.loads(dispatches)
        self.assertEqual('library_fidelity_seed_20260906_n20',args[0])
        self.assertEqual('20260906',args[args.index('--seed')+1]);self.assertEqual('20',args[args.index('--lines')+1])
        before=out.read_bytes();again=self.run_native()
        self.assertEqual(0,again.returncode,again.stdout+again.stderr)
        self.assertEqual(dispatches,(self.root/'dispatches').read_bytes());self.assertEqual(before,out.read_bytes())

    def test_native_missing_source_refuses_instead_of_dispatching(self):
        self.prepare_native_chain();(self.zips/'dataset_a.zip').unlink()
        result=self.run_native();self.assertEqual(2,result.returncode,result.stdout+result.stderr)
        self.assertFalse((self.root/'dispatches').exists())

    def test_native_successful_worker_with_partial_output_is_not_published_complete(self):
        self.prepare_native_chain();out=self.root/'ab_test_runtime/experiments/library_fidelity_seed_20260906_n20.json'
        out.write_text('{}');self.env['PARTIAL_WORKER']='1'
        result=self.run_native();self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertNotIn('COMPLETE overnight',result.stdout)
        self.assertEqual(1,len((self.root/'dispatches').read_text().splitlines()))

    def test_native_worker_failure_preserves_nonzero_status(self):
        self.prepare_native_chain();(self.root/'ab_test_runtime/experiments/library_fidelity_seed_20260906_n20.json').write_text('{}')
        self.env['FAIL_WORKER']='1';result=self.run_native()
        self.assertEqual(7,result.returncode,result.stdout+result.stderr)
        self.assertNotIn('COMPLETE overnight',result.stdout)
