"""Native CPU tensors and allocation-only meta device; no GPU/model inference."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import torch
import soundfile as sf
import train_lora
from tests.test_teacher_forcing_language import make_fixture
from tests.test_upload_event_loop import wav_bytes


class TrainingDatasetResidencyTests(unittest.TestCase):
    def test_prepared_corpus_stays_on_cpu_even_when_training_target_is_another_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'sample.wav').write_bytes(wav_bytes())
            rows=[{'audio':'sample.wav','text':f'Row {i}.'} for i in range(64)]
            (root/'metadata.jsonl').write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
            before=(root/'metadata.jsonl').read_bytes();audio=(root/'sample.wav').read_bytes()
            codes=torch.arange(32,dtype=torch.long).reshape(2,16)
            texts=torch.arange(10,dtype=torch.long).unsqueeze(0)
            speaker=torch.arange(4,dtype=torch.float32).unsqueeze(0)
            def read_audio(path,sr,mono):
                data,rate=sf.read(path,dtype='float32');return data,rate
            hf=SimpleNamespace(speaker_encoder=lambda tensor:speaker,
                speech_tokenizer=SimpleNamespace(encode=lambda audio,sr:SimpleNamespace(audio_codes=[codes])))
            processor=lambda **kwargs:{'input_ids':texts}
            with patch.dict(sys.modules,{'librosa':SimpleNamespace(load=read_audio),
                    'qwen_tts.core.models.modeling_qwen3_tts':SimpleNamespace(mel_spectrogram=lambda *args,**kwargs:torch.ones(1,128,4))}):
                samples,reference=train_lora.load_dataset(tmp,hf,processor,'meta',torch.float32,30)
            self.assertEqual(64,len(samples));self.assertEqual(str(root/'sample.wav'),reference)
            for index,sample in enumerate(samples):
                self.assertEqual(rows[index]['text'],sample['text'])
                for key,expected in (('codec_ids',codes),('text_ids',texts),('spk_embedding',speaker)):
                    self.assertEqual('cpu',sample[key].device.type)
                    self.assertFalse(sample[key].requires_grad)
                    torch.testing.assert_close(sample[key],expected)
            self.assertTrue(all(sample['spk_embedding'] is samples[0]['spk_embedding'] for sample in samples))
            self.assertEqual(before,(root/'metadata.jsonl').read_bytes());self.assertEqual(audio,(root/'sample.wav').read_bytes())

    def test_teacher_input_transfers_current_sample_without_moving_cached_sources(self):
        sample,model,codec,text=make_fixture()
        groups=model.talker.code_predictor.get_input_embeddings()
        model.talker.code_predictor.get_input_embeddings=lambda:groups
        sample['spk_embedding']=sample['spk_embedding'].double()
        before={key:value.clone() for key,value in sample.items()}
        codec.to('meta');text.to('meta')
        for embedding in groups:embedding.to('meta')
        inputs,labels,codes,prefill=train_lora.build_teacher_forcing_input(sample,model,'meta',torch.float32)
        self.assertEqual(['meta']*3,[tensor.device.type for tensor in (inputs,labels,codes)])
        self.assertEqual(torch.float32,inputs.dtype);self.assertEqual(torch.long,codes.dtype)
        self.assertEqual(3,inputs.shape[1]-prefill)
        for key,value in sample.items():
            self.assertEqual('cpu',value.device.type);torch.testing.assert_close(before[key],value)

    def test_cpu_input_values_labels_and_cache_are_unchanged_across_repeated_steps(self):
        sample,model,codec,text=make_fixture();before={key:value.clone() for key,value in sample.items()}
        groups=model.talker.code_predictor.get_input_embeddings()
        model.talker.code_predictor.get_input_embeddings=lambda:groups
        first=train_lora.build_teacher_forcing_input(sample,model,'cpu',torch.float32)
        second=train_lora.build_teacher_forcing_input(sample,model,'cpu',torch.float32)
        for a,b in zip(first[:3],second[:3]):torch.testing.assert_close(a,b)
        self.assertEqual([1,3,5],first[1][0,first[3]:].tolist())
        for key,value in sample.items():torch.testing.assert_close(before[key],value)
