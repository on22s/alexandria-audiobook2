"""Decoder recovery preserves generated codes and fails loudly at a bounded floor."""
import gc
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

import torch
import tts


class CodecOomRecoveryTests(unittest.TestCase):
    def test_normal_decode_retains_exact_default_arguments_and_result(self):
        result = object()
        original = Mock(return_value=result)
        codes = torch.zeros(1,16,120,dtype=torch.long)
        with patch.object(torch.cuda, 'empty_cache') as clear:
            self.assertIs(result, tts.get_memory_bounded_codec_decode(original)(codes))
        original.assert_called_once_with(codes, chunk_size=300, left_context_size=25)
        clear.assert_not_called()

    def test_retry_uses_same_codes_and_overlap_and_keeps_complete_output(self):
        codes = torch.arange(120).reshape(1,1,120)
        attempts = []
        def decode(received, chunk_size, left_context_size):
            self.assertIs(codes, received)
            self.assertEqual(17, left_context_size)
            attempts.append(chunk_size)
            if chunk_size > 30:
                raise torch.OutOfMemoryError('CUDA out of memory')
            return torch.cat([received[..., start:start+chunk_size] for start in range(0,120,chunk_size)],dim=-1)
        with patch.object(torch.cuda, 'empty_cache') as clear:
            actual = tts.get_memory_bounded_codec_decode(decode)(codes,left_context_size=17)
        self.assertTrue(torch.equal(codes,actual))
        self.assertEqual([300,60,30],attempts)
        self.assertEqual(2,clear.call_count)

    def test_exhausted_oom_is_bounded_and_not_a_success(self):
        original = Mock(side_effect=torch.OutOfMemoryError('HIP out of memory'))
        with patch.object(torch.cuda,'empty_cache') as clear:
            with self.assertRaises(torch.OutOfMemoryError):
                tts.get_memory_bounded_codec_decode(original)(torch.zeros(1,16,120))
        self.assertEqual([300,60,30,25],[call.kwargs['chunk_size'] for call in original.call_args_list])
        self.assertEqual(3,clear.call_count)

    def test_non_memory_cuda_errors_and_explicit_small_chunks_are_not_retried(self):
        for error in (RuntimeError('CUDA error: device-side assert triggered'),ValueError('malformed codes')):
            original=Mock(side_effect=error)
            with patch.object(torch.cuda,'empty_cache') as clear, self.assertRaises(type(error)):
                tts.get_memory_bounded_codec_decode(original)(torch.zeros(1,16,120))
            original.assert_called_once()
            clear.assert_not_called()
        original=Mock(side_effect=torch.OutOfMemoryError('CUDA out of memory'))
        with self.assertRaises(torch.OutOfMemoryError):
            tts.get_memory_bounded_codec_decode(original)(torch.zeros(1,16,120),chunk_size=12)
        original.assert_called_once()

    def test_failed_activation_is_released_before_retry(self):
        refs=[]
        def decode(codes,chunk_size,left_context_size):
            if chunk_size == 300:
                activation=torch.ones(50)
                refs.append(weakref.ref(activation))
                raise torch.OutOfMemoryError('CUDA out of memory')
            gc.collect()
            self.assertIsNone(refs[0]())
            return codes
        codes=torch.zeros(1,16,120)
        with patch.object(torch.cuda,'empty_cache'):
            self.assertIs(codes,tts.get_memory_bounded_codec_decode(decode)(codes))

    def test_shared_model_loader_installs_retry_after_cache_or_download_load(self):
        for cached,failed_cache in ((None,False),('/cached',False),('/cached',True)):
            with self.subTest(cached=cached,failed_cache=failed_cache):
                original=Mock(side_effect=[torch.OutOfMemoryError('CUDA out of memory'),'complete'])
                decoder=SimpleNamespace(chunked_decode=original)
                model=SimpleNamespace(model=SimpleNamespace(speech_tokenizer=SimpleNamespace(model=SimpleNamespace(config=SimpleNamespace(model_type='qwen3_tts_tokenizer_12hz'),decoder=decoder))))
                factory=SimpleNamespace(from_pretrained=Mock(side_effect=[ValueError('bad cache'),model] if failed_cache else [model]))
                with patch.object(tts.TTSEngine,'_resolve_local_model_path',return_value=cached),patch.object(torch.cuda,'empty_cache'):
                    loaded=tts.TTSEngine._load_model(factory,'Qwen/fixture',{'dtype':torch.bfloat16})
                    self.assertIs(model,loaded)
                    self.assertEqual('complete',decoder.chunked_decode(torch.zeros(1,16,120)))
                self.assertEqual(2,original.call_count)

    def test_other_codec_types_are_untouched(self):
        original=Mock()
        decoder=SimpleNamespace(chunked_decode=original)
        model=SimpleNamespace(model=SimpleNamespace(speech_tokenizer=SimpleNamespace(model=SimpleNamespace(config=SimpleNamespace(model_type='other'),decoder=decoder))))
        factory=SimpleNamespace(from_pretrained=Mock(return_value=model))
        with patch.object(tts.TTSEngine,'_resolve_local_model_path',return_value=None):
            self.assertIs(model,tts.TTSEngine._load_model(factory,'other',{}))
        self.assertIs(original,decoder.chunked_decode)
