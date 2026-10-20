"""Drift scoring follows serving aliases and the trusted shipped asset root."""

import copy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import tts
import voice_drift


def write_wav(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as handle:
        handle.setparams((1,2,24000,0,'NONE','not compressed'))
        handle.writeframes(b'\x00\x10'*6000)


class DriftReferenceAssetTests(unittest.TestCase):
    def test_native_renamed_adapter_reaches_scoring_without_rewriting_saved_config(self):
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        from tests.test_lora_generation_cache import _write
        from voice_manifest import get_adapter_asset_snapshot
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);models,_=NamingTransactionTests().fixture(root);_write(models/'raw_a')
            old=models/'raw_a'/'ref_sample.wav';reference=old.read_bytes()
            output=root/'voicelines/output.wav';output.parent.mkdir();output.write_bytes(reference)
            self.assertEqual(0,_invoke(models,'--apply'))
            expected,_=get_adapter_asset_snapshot(str(old));calls=[];decoded=[]
            real_decode=voice_drift._decode_to_wav
            def decode(path,*args):
                decoded.append(Path(path))
                if Path(path) == Path(expected):
                    self.assertEqual(reference, Path(path).read_bytes())
                return real_decode(path,*args)
            def score(pairs,python):calls.extend(pairs);return [.8]*len(pairs),None
            chunks=[{'uid':'row','speaker':'A','status':'done','audio_path':'voicelines/output.wav'}]
            for adapter in ('models/raw_a',str(models/'raw_a')):
                with self.subTest(adapter=adapter),patch.object(tts,'_get_runtime_data_dir',return_value=str(root)), \
                     patch.object(voice_drift,'_decode_to_wav',side_effect=decode):
                    config={'A':{'type':'lora','adapter_path':adapter}};before=copy.deepcopy(config)
                    report=voice_drift.check_voice_drift(chunks,config,str(root),sys.executable,.45,
                        resolve_asset_path=tts._resolve_asset_path,score_pairs=score)
                    self.assertIsNone(report['error']);self.assertEqual(.8,report['results'][0]['score'])
                    self.assertEqual(before,config)
            config={'A':{'type':'clone','ref_audio':str(expected)}};before=copy.deepcopy(config)
            with patch.object(tts,'_get_runtime_data_dir',return_value=str(root)), \
                 patch.object(voice_drift,'_decode_to_wav',side_effect=decode):
                report=voice_drift.check_voice_drift(chunks,config,str(root),sys.executable,.45,
                    resolve_asset_path=tts._resolve_asset_path,score_pairs=score)
                self.assertIsNone(report['error']);self.assertEqual(.8,report['results'][0]['score'])
                self.assertEqual(before,config)
            self.assertEqual(3,len(calls));self.assertIn(Path(expected),decoded)
            with tempfile.TemporaryDirectory() as foreign_root:
                foreign=Path(foreign_root)/'reference.wav';write_wav(foreign)
                config={'A':{'type':'clone','ref_audio':str(foreign)}};before=copy.deepcopy(config)
                with patch.object(tts,'_get_runtime_data_dir',return_value=str(root)):
                    report=voice_drift.check_voice_drift(chunks,config,str(root),sys.executable,.45,
                        resolve_asset_path=tts._resolve_asset_path,score_pairs=score)
                self.assertIsNone(report['error']);self.assertIsNone(report['results'][0]['score'])
                self.assertIn('outside project',report['results'][0]['error'])
                self.assertEqual(3,len(calls));self.assertEqual(before,config)

    def test_relocated_builtin_reference_is_scored_but_reference_symlink_escape_is_not(self):
        for escaped in (False,True):
            with self.subTest(escaped=escaped),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);installed=base/'installed';runtime=base/'runtime'
                reference=installed/'builtin_lora/A/ref_sample.wav';write_wav(reference)
                target=runtime/'voicelines/render.wav';write_wav(target)
                if escaped:
                    foreign=installed/'private/ref_sample.wav';foreign.parent.mkdir();shutil.move(reference,foreign)
                    reference.symlink_to(foreign)
                chunks=[{'uid':'row','speaker':'A','status':'done','audio_path':'voicelines/render.wav'}]
                config={'A':{'type':'builtin_lora','adapter_path':'builtin_lora/A'}}
                original=copy.deepcopy((chunks,config));pairs=[]
                def score(batch,python):pairs.extend(batch);return [.8]*len(batch),None
                with patch.object(tts,'__file__',str(installed/'app/tts.py')), \
                     patch.object(tts,'_get_runtime_data_dir',return_value=str(runtime)):
                    report=voice_drift.check_voice_drift(chunks,config,str(runtime),sys.executable,.45,
                        resolve_asset_path=tts._resolve_asset_path,score_pairs=score)
                self.assertIsNone(report['error'])
                if escaped:
                    self.assertEqual([],pairs);self.assertIsNone(report['results'][0]['score'])
                    self.assertIn('outside project',report['results'][0]['error'])
                else:
                    self.assertEqual(1,len(pairs));self.assertEqual(.8,report['results'][0]['score'])
                self.assertEqual(original,(chunks,config))


if __name__=='__main__':unittest.main()
