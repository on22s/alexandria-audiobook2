"""CPU providers and complete isolated chains exercise resume decisions."""
from contextlib import ExitStack
import builtins
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import torch
import alexandria_preparer_rocm_compatible as preparer

from tests.hifitts_fixture import write_hifitts_fixture

ROOT=Path(__file__).resolve().parents[2]


class EnglishASRGuardTests(unittest.TestCase):
    def test_non_english_requests_reject_before_provider_import_or_cli_or_model_work(self):
        real_import=builtins.__import__
        for language in ('fr','FR-ca','ja','zh-CN','english','en_US',''):
            with self.subTest(language=language), ExitStack() as stack:
                stack.enter_context(patch.object(preparer,'TRANSFORMERS_WHISPER_AVAILABLE',True))
                stack.enter_context(patch.object(preparer,'WHISPER_CPP_AVAILABLE',True))
                def no_provider(name,*args,**kwargs):
                    if name in ('torch','transformers'): raise AssertionError('provider import must not run')
                    return real_import(name,*args,**kwargs)
                stack.enter_context(patch('builtins.__import__',side_effect=no_provider))
                run=stack.enter_context(patch.object(preparer.subprocess,'run',side_effect=AssertionError('CLI must not run')))
                for method in (preparer.transcribe_with_wav2vec2,preparer.transcribe_with_whisper_cpp):
                    with self.assertRaisesRegex(ValueError,'only supports English'):
                        method(np.zeros(16000,dtype=np.float32),language)
                run.assert_not_called()

    def _english_models(self, stack):
        events=[]
        class Processor:
            @classmethod
            def from_pretrained(cls,name,**kwargs): events.append(('processor',name,kwargs));return cls()
            def __call__(self,audio,**kwargs): return {'input_values':torch.tensor(audio)[None,:]}
            def batch_decode(self,ids,**kwargs): return SimpleNamespace(word_offsets=[[{'word':'hello','start_offset':1,'end_offset':10}]])
        class Model:
            config=SimpleNamespace(inputs_to_logits_ratio=320)
            @classmethod
            def from_pretrained(cls,name,**kwargs): events.append(('model',name,kwargs));return cls()
            def to(self,device): events.append(('device',device));return self
            def eval(self): return self
            def __call__(self,**kwargs): return SimpleNamespace(logits=torch.zeros((1,50,2)))
        stack.enter_context(patch.dict(sys.modules,{'transformers':SimpleNamespace(Wav2Vec2Processor=Processor,Wav2Vec2ForCTC=Model)}))
        for attr,value in (('TRANSFORMERS_WHISPER_AVAILABLE',True),('resolve_cuda_device',lambda module:'cpu'),
                           ('log_gpu_stats',lambda *args:None),('clear_vram',lambda:None)):
            stack.enter_context(patch.object(preparer,attr,value))
        return events

    def test_existing_english_codes_use_same_checkpoint_revision_and_cpu_word_alignment(self):
        for language in ('en','EN','en-US','EN-gb','en-'):
            with self.subTest(language=language),ExitStack() as stack:
                events=self._english_models(stack)
                audio=np.zeros(16000,dtype=np.float32);before=audio.copy()
                words,returned=preparer.transcribe_with_wav2vec2(audio,language,limit=1)
                self.assertEqual(language,returned)
                self.assertEqual([('hello',.02,.2)],[(w['word'],w['start'],w['end']) for w in words])
                self.assertEqual([('processor','facebook/wav2vec2-large-960h',{'revision':preparer.WAV2VEC2_MODEL_REVISION}),
                    ('model','facebook/wav2vec2-large-960h',{'revision':preparer.WAV2VEC2_MODEL_REVISION}),('device','cpu')],events)
                np.testing.assert_array_equal(before,audio)

    def test_actual_selector_uses_multilingual_whisperx_after_both_english_producers_reject(self):
        for language in ('fr','ja','zh-CN'):
            with self.subTest(language=language),ExitStack() as stack:
                events=self._english_models(stack)
                words=[{'word':'bonjour','start':.1,'end':.4,'confidence':.9}]
                asr=SimpleNamespace(load_model=Mock(return_value=SimpleNamespace(transcribe=Mock(return_value={
                    'language':language,'segments':[{'text':'bonjour','start':.1,'end':.4}]}))))
                alignment=SimpleNamespace(load_align_model=Mock(return_value=('cpu-alignment',{'lang':language})),
                    align=Mock(return_value={'segments':[{'words':[{**w,'score':w['confidence']} for w in words]}]}))
                for attr,value in (('WHISPER_CPP_AVAILABLE',True),('WHISPERX_AVAILABLE',True),
                    ('whisperx_asr',asr),('whisperx_alignment',alignment)):
                    stack.enter_context(patch.object(preparer,attr,value,create=True))
                cli=stack.enter_context(patch.object(preparer.subprocess,'run',side_effect=AssertionError('wrong-language CLI must not run')))
                result,detected=preparer.choose_and_transcribe(np.zeros(16000,dtype=np.float32),'cpu',language)
                self.assertEqual(words,result);self.assertEqual(language,detected)
                self.assertEqual([],events);cli.assert_not_called()
                asr.load_model.assert_called_once_with('base','cpu',compute_type='int8')
                self.assertEqual(language,asr.load_model.return_value.transcribe.call_args.kwargs['language'])
                alignment.load_align_model.assert_called_once_with(language_code=language,device='cpu')

    def test_selector_fails_loud_when_non_english_has_no_compatible_provider(self):
        with ExitStack() as stack:
            events=self._english_models(stack)
            stack.enter_context(patch.object(preparer,'WHISPER_CPP_AVAILABLE',True))
            stack.enter_context(patch.object(preparer,'WHISPERX_AVAILABLE',False))
            cli=stack.enter_context(patch.object(preparer.subprocess,'run',side_effect=AssertionError('CLI must not run')))
            with self.assertRaises(SystemExit) as error:
                preparer.choose_and_transcribe(np.zeros(16000,dtype=np.float32),'cpu','fr')
            self.assertEqual(1,error.exception.code);self.assertEqual([],events);cli.assert_not_called()


    def test_existing_english_codes_reach_whisper_cpp_with_complete_pcm_and_same_limits(self):
        for language in ('en','EN','en-US','EN-gb','en-'):
            with self.subTest(language=language), ExitStack() as stack:
                def transcribe(command, **kwargs):
                    self.assertEqual(language,command[command.index('--language')+1])
                    self.assertEqual('1',command[command.index('--max-len')+1])
                    self.assertIn('--split-on-word',command)
                    self.assertEqual(3600,kwargs['timeout'])
                    pcm, rate=preparer.sf.read(command[command.index('--file')+1])
                    self.assertEqual((16000,16000),(len(pcm),rate))
                    np.testing.assert_array_equal(pcm,np.zeros(16000))
                    output=command[command.index('--output-file')+1]+'.json'
                    Path(output).write_text(json.dumps({'result':{'language':'en'},'transcription':[
                        {'text':'hello','offsets':{'from':100,'to':400}}]}))
                    return SimpleNamespace(returncode=0,stdout='',stderr='')
                stack.enter_context(patch.object(preparer,'WHISPER_CPP_AVAILABLE',True))
                stack.enter_context(patch.object(preparer,'WHISPER_CPP_BIN','/unused/whisper'))
                stack.enter_context(patch.object(preparer,'WHISPER_CPP_MODEL','/unused/Small.en'))
                run=stack.enter_context(patch.object(preparer.subprocess,'run',side_effect=transcribe))
                words,returned=preparer.transcribe_with_whisper_cpp(np.zeros(16000,dtype=np.float32),language)
                self.assertEqual('en',returned);self.assertEqual([{'word':'hello','start':.1,'end':.4}],words)
                run.assert_called_once()


class StopGateChainTests(unittest.TestCase):
    def test_whole_chain_accepts_only_exact_passing_verdict_and_reruns_bad_cache_once(self):
        cases=(({'passed':True},True,0,0,True),({'passed':False},True,0,1,True),
            ('{bad',True,0,1,True),([],True,0,1,True),({'passed':1},True,0,1,True),
            (None,True,0,1,True),(None,False,3,1,False),({'passed':False},False,3,1,False),
            ('{bad',False,0,1,False),({'passed':False},True,3,1,False),
            (None,'malformed',0,1,False))
        for cached,fresh,rc,gate_calls,allowed in cases:
            with self.subTest(cached=cached,fresh=fresh,rc=rc),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'run_chains/lib').mkdir(parents=True);(root/'app/experiments').mkdir(parents=True)
                source=ROOT/'run_chains/hifitts_9017_20260913.sh'
                shutil.copy2(source,root/'run_chains/chain.sh')
                shutil.copy2(ROOT/'run_chains/lib/stage.sh',root/'run_chains/lib/stage.sh')
                shutil.copy2(ROOT/'run_chains/lib/server_cleanup.sh',root/'run_chains/lib/server_cleanup.sh')
                shutil.copy2(ROOT/'app/experiments/stop_gate_completion.py',root/'app/experiments/stop_gate_completion.py')
                shutil.copy2(ROOT/'app/experiments/ljspeech_completion.py',root/'app/experiments/ljspeech_completion.py')
                shutil.copy2(ROOT/'app/adapter_artifacts.py',root/'app/adapter_artifacts.py')
                corpus=root/'ab_test_runtime/corpora/hifitts/9017';corpus.mkdir(parents=True)
                (corpus/'metadata.csv').write_text('CPU fixture');(corpus/'split.json').write_text('{}')
                work=root/'ab_test_runtime/hifitts_9017_eval';(work/'adapter').mkdir(parents=True)
                write_hifitts_fixture(root,corpus,work)
                artifact=work/'stop_check/verify_adapter_stops.json';artifact.parent.mkdir()
                if cached is not None: artifact.write_text(cached if isinstance(cached,str) else json.dumps(cached))
                before=artifact.read_bytes() if artifact.exists() else None
                config=root/'app/config.json';config.write_text('{}')
                bin_dir=root/'bin';bin_dir.mkdir()
                for name,content in (('pgrep','#!/bin/bash\nexit 1\n'),):
                    (bin_dir/name).write_text(content);(bin_dir/name).chmod(0o755)
                gpu=root/'gpu_job.sh';gpu.write_text('#!/bin/bash\nshift\nexec "$@"\n');gpu.chmod(0o755)
                marker=root/'calls.txt'
                cpu_source='import json,os,pathlib,sys\n' + 'name=pathlib.Path(__file__).name\n' +                     'with open(os.environ["CALLS"],"a") as f:f.write(name+"\\n")\n' +                     'if name=="verify_adapter_stops.py":\n' +                     ' p=pathlib.Path(sys.argv[sys.argv.index("--out")+1]);p.parent.mkdir(parents=True,exist_ok=True)\n' +                     ' fresh=os.environ["FRESH"];p.write_text("{broken" if fresh=="malformed" else json.dumps({"passed":fresh=="true"}))\n' +                     ' sys.exit(int(os.environ["GATE_RC"]))\n'
                cpu_source += 'p=pathlib.Path(sys.argv[sys.argv.index("--out")+1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_text("{}")\n'
                for name in ('verify_adapter_stops.py','ljspeech_generate.py','prosody_fidelity.py','ljspeech_score.py'):
                    (root/'app/experiments'/name).write_text(cpu_source)
                for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.com'),('config','core.hooksPath',str(root/'no-hooks')),('add','-A'),('commit','-q','-m','fixture baseline')):
                    subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
                env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],'PYTHON':sys.executable,
                    'CONFIG':str(config),'CALLS':str(marker),'FRESH':'malformed' if fresh=='malformed' else str(fresh).lower(),
                    'GATE_RC':str(rc)}
                result=subprocess.run(['bash',str(root/'run_chains/chain.sh')],env=env,text=True,capture_output=True,timeout=15)
                calls=marker.read_text().splitlines() if marker.exists() else []
                self.assertEqual(gate_calls,calls.count('verify_adapter_stops.py'),result.stdout+result.stderr)
                self.assertEqual(allowed,'ljspeech_generate.py' in calls,result.stdout+result.stderr)
                self.assertEqual(allowed,'prosody_fidelity.py' in calls);self.assertEqual(allowed,'ljspeech_score.py' in calls)
                self.assertEqual(0 if allowed else 1,result.returncode,result.stdout+result.stderr)
                if gate_calls==0: self.assertEqual(before,artifact.read_bytes())
                elif not allowed:
                    self.assertIn('REFUS',result.stdout);self.assertTrue(artifact.exists())
                    if fresh is False:self.assertIs(False,json.loads(artifact.read_text())['passed'])
                self.assertEqual('{}',config.read_text())
