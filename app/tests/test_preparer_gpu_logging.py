import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tests import test_preparer_run_state as support

preparer = support.preparer


class PreparerGpuLoggingTests(unittest.TestCase):
    def test_success_reports_responsiveness_without_claiming_offload(self):
        for metadata in ({}, {'device': 'cpu'}, {'device': 'cuda'}):
            with self.subTest(metadata=metadata):
                llm = SimpleNamespace(n_gpu_layers=99, metadata=metadata,
                                      create_chat_completion=Mock(return_value={}))
                with patch.object(preparer, 'Llama', return_value=llm) as constructor, self.assertLogs('alexandria', 'DEBUG') as logs:
                    self.assertIs(llm, preparer._load_llm('fixture.gguf'))
                text = '\n'.join(logs.output)
                self.assertNotIn('GPU inference verified', text)
                self.assertNotIn('GPU Layers Loaded', text)
                self.assertNotIn('cuda (via n_gpu_layers=-1)', text)
                self.assertIn('Requested GPU layers: 99', text)
                self.assertIn('Model responsiveness verified', text)
                self.assertIn('Reported model device: ' + metadata.get('device', 'unknown'), text)
                self.assertEqual(99, constructor.call_args.kwargs['n_gpu_layers'])
                llm.create_chat_completion.assert_called_once_with(
                    messages=[{'role': 'user', 'content': 'test'}], max_tokens=1)

    def test_broken_pipe_retry_preserves_the_requested_layers(self):
        llm = SimpleNamespace(n_gpu_layers=99, metadata={}, create_chat_completion=Mock())
        with patch.object(preparer, 'Llama', side_effect=[BrokenPipeError(), llm]) as constructor, self.assertLogs('alexandria', 'DEBUG') as logs:
            self.assertIs(llm, preparer._load_llm('fixture.gguf'))
        self.assertEqual([99, 99], [call.kwargs['n_gpu_layers'] for call in constructor.call_args_list])
        self.assertEqual([True, False], [call.kwargs['verbose'] for call in constructor.call_args_list])
        self.assertNotIn('GPU inference verified', '\n'.join(logs.output))

    def test_failed_warmup_is_not_reported_as_verified(self):
        llm = SimpleNamespace(n_gpu_layers=99, metadata={},
                              create_chat_completion=Mock(side_effect=RuntimeError('warmup failed')))
        with patch.object(preparer, 'Llama', return_value=llm), self.assertLogs('alexandria', 'DEBUG') as logs:
            with self.assertRaisesRegex(RuntimeError, 'warmup failed'):
                preparer._load_llm('fixture.gguf')
        self.assertNotIn('responsiveness verified', '\n'.join(logs.output))

    def test_native_annotation_saves_audio_without_unproven_gpu_claims(self):
        with self.assertLogs('alexandria', 'DEBUG') as logs:
            support.PreparerFallbackContextTests(
                'test_full_and_tail_fallbacks_use_only_preceding_text_and_save_audio'
            ).test_full_and_tail_fallbacks_use_only_preceding_text_and_save_audio()
        text = '\n'.join(logs.output)
        self.assertNotIn('GPU inference confirmed', text)
        self.assertNotIn('Device: GPU (CUDA/ROCm acceleration)', text)
        self.assertNotIn('GPU Layers: All (-1', text)
        self.assertIn('Requested GPU layers: 99', text)
