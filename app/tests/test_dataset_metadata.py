"""Upload and standalone training share strict metadata shape validation."""
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from fastapi import FastAPI
import httpx
import numpy as np
import soundfile as sf
import torch
from routers import lora
import train_lora
from tests.test_upload_event_loop import archive_bytes,wav_bytes


def get_invalid_rows():
    return [json.dumps(value) for value in (
        {'audio':['sample.wav'],'text':'Hello.'},
        {'audio':{'path':'sample.wav'},'text':'Hello.'},
        {'audio':True,'text':'Hello.'},
        {'audio':7,'text':'Hello.'},
        {'audio':'sample.wav'},
        {'audio':'sample.wav','text':None},
        {'audio':'sample.wav','text':['Hello.']},
        {'audio':'sample.wav','text':'Hello.','ref_audio':['sample.wav']},
        ['not an object'],None)] + ['{broken']


class DatasetMetadataUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_mixed_rows_return_400_and_remove_reserved_and_temp_artifacts(self):
        valid=json.dumps({'audio':'sample.wav','text':'Hello.'})
        for malformed in get_invalid_rows():
            with self.subTest(malformed=malformed),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);app=FastAPI();app.include_router(lora.router)
                document=archive_bytes({'metadata.jsonl':valid+'\n'+malformed+'\n','sample.wav':wav_bytes()})
                with patch.object(lora,'LORA_DATASETS_DIR',tmp):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        result=await client.post('/api/lora/upload_dataset',files={'file':('dataset.zip',document,'application/zip')})
                self.assertEqual(400,result.status_code,result.text)
                self.assertIn('line 2',result.json()['detail'])
                self.assertEqual(['.dataset-locks'],sorted(p.name for p in root.iterdir()))

    async def test_empty_all_invalid_and_non_utf8_metadata_are_rejected_and_cleaned(self):
        for raw in (b'', b'\n  \n', b'null\n', b'{broken\n', b'\xff\n'):
            with self.subTest(raw=raw),tempfile.TemporaryDirectory() as tmp:
                app=FastAPI();app.include_router(lora.router)
                document=archive_bytes({'metadata.jsonl':raw,'sample.wav':wav_bytes()})
                with patch.object(lora,'LORA_DATASETS_DIR',tmp):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        result=await client.post('/api/lora/upload_dataset',files={'file':('dataset.zip',document,'application/zip')})
                self.assertEqual(400,result.status_code,result.text)
                self.assertEqual(['.dataset-locks'],sorted(p.name for p in Path(tmp).iterdir()))

    async def test_valid_legacy_rows_and_missing_file_counts_preserve_original_artifacts(self):
        cases=[('audio',False),('audio_filepath',False),('audio',True)]
        for field,all_missing in cases:
            with self.subTest(field=field,all_missing=all_missing),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);app=FastAPI();app.include_router(lora.router)
                rows=[{field:'../escape.wav','text':'Outside.'}]
                if not all_missing:rows.insert(0,{field:'sample.wav','text':'Hello.'})
                raw='\n'.join(json.dumps(row) for row in rows)+'\n';audio=wav_bytes()
                document=archive_bytes({'metadata.jsonl':raw,'sample.wav':audio})
                with patch.object(lora,'LORA_DATASETS_DIR',tmp):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                        result=await client.post('/api/lora/upload_dataset',files={'file':('dataset.zip',document,'application/zip')})
                if all_missing:
                    self.assertEqual(400,result.status_code);self.assertEqual(['.dataset-locks'],sorted(p.name for p in root.iterdir()))
                else:
                    self.assertEqual(200,result.status_code,result.text)
                    self.assertEqual((1,2),(result.json()['sample_count'],result.json()['metadata_count']))
                    self.assertEqual(raw,(root/'dataset/metadata.jsonl').read_text())
                    self.assertEqual(audio,(root/'dataset/sample.wav').read_bytes())
                    self.assertEqual(['.dataset-locks','dataset'],sorted(path.name for path in root.iterdir()))


class DatasetMetadataTrainingTests(unittest.TestCase):
    def test_load_dataset_rejects_every_malformed_row_before_reference_or_tokenizer(self):
        reference=Mock(side_effect=AssertionError('invalid metadata reached audio provider'))
        mel=Mock(side_effect=AssertionError('invalid metadata reached embedding'))
        for malformed in get_invalid_rows():
            with self.subTest(malformed=malformed),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp,'metadata.jsonl');raw=json.dumps({'audio':'sample.wav','text':'Hello.'})+'\n'+malformed+'\n';path.write_text(raw)
                Path(tmp,'sample.wav').write_bytes(wav_bytes())
                with patch.dict(sys.modules,{'librosa':SimpleNamespace(load=reference),
                        'qwen_tts.core.models.modeling_qwen3_tts':SimpleNamespace(mel_spectrogram=mel)}):
                    with self.assertRaisesRegex(ValueError,'line 2'):
                        train_lora.load_dataset(tmp,object(),object(),'cpu',torch.float32,30)
                self.assertEqual(raw,path.read_text());reference.assert_not_called();mel.assert_not_called()

    def test_train_rejects_invalid_metadata_before_loading_base_model(self):
        for malformed in get_invalid_rows():
            with self.subTest(malformed=malformed),tempfile.TemporaryDirectory() as tmp:
                Path(tmp,'metadata.jsonl').write_text(malformed+'\n')
                with patch.object(sys,'argv',['train_lora.py','--data_dir',tmp,'--output_dir',tmp,'--device','cpu']):
                    args=train_lora.parse_args()
                load=Mock(side_effect=AssertionError('invalid metadata loaded Base model'))
                with patch.dict(sys.modules,{'qwen_tts':SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=load))}),patch.object(train_lora,'resolve_device',return_value='cpu'),patch.object(train_lora,'enable_rocm_optimizations'):
                    with self.assertRaisesRegex(ValueError,'line 1'):train_lora.train(args)
                load.assert_not_called()

    def test_real_cpu_preparation_preserves_train_split_legacy_audio_and_containment_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'train').mkdir();(root/'val').mkdir()
            audio=wav_bytes();(root/'train/sample.wav').write_bytes(audio)
            (root/'val/sample.wav').write_bytes(audio)
            train_rows=[{'audio':'train/sample.wav','text':'Train only.'},{'audio':'../escape.wav','text':'Must be skipped.'}]
            raw='\n'.join(json.dumps(row) for row in train_rows)+'\n'
            (root/'train/metadata.jsonl').write_text(raw)
            (root/'metadata.jsonl').write_text(json.dumps({'audio':'val/sample.wav','text':'Held out.'}))
            (root/'val/metadata.jsonl').write_text(json.dumps({'audio':'val/sample.wav','text':'Held out.'}))
            seen=[]
            def read_audio(path,sr,mono):
                seen.append(Path(path));data,rate=sf.read(path,dtype='float32');return data,rate
            hf=SimpleNamespace(speaker_encoder=lambda tensor:torch.ones(1,4),speech_tokenizer=SimpleNamespace(encode=lambda audio,sr:SimpleNamespace(audio_codes=[torch.zeros(2,16,dtype=torch.long)])))
            processor=lambda **kwargs:{'input_ids':torch.tensor([[1,2,3]])}
            with patch.dict(sys.modules,{'librosa':SimpleNamespace(load=read_audio),
                    'qwen_tts.core.models.modeling_qwen3_tts':SimpleNamespace(mel_spectrogram=lambda *args,**kwargs:torch.ones(1,128,4))}):
                samples,reference=train_lora.load_dataset(tmp,hf,processor,'cpu',torch.float32,30)
            self.assertEqual(['Train only.'],[sample['text'] for sample in samples])
            self.assertEqual(str(root/'train/sample.wav'),reference)
            self.assertEqual([root/'train/sample.wav']*2,seen)
            self.assertEqual((2,16),tuple(samples[0]['codec_ids'].shape))
            self.assertEqual(raw,(root/'train/metadata.jsonl').read_text())
            self.assertEqual(audio,(root/'train/sample.wav').read_bytes())
