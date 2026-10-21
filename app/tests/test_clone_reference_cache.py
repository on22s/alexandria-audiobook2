"""Actual retained buffers, eviction and mutable prompt boundaries."""
from dataclasses import dataclass
import gc
import unittest
from unittest.mock import patch
import weakref

import torch

from clone_reference_cache import ReferenceLRU, copy_clone_prompt


@dataclass
class Prompt:
    ref_code: object
    ref_spk_embedding: object
    x_vector_only_mode: bool = False
    icl_mode: bool = True
    ref_text: str = 'Synthetic reference'


class ReferenceCachePrimitiveTests(unittest.TestCase):
    def test_count_lru_and_independent_byte_limit_evict_payloads(self):
        cache = ReferenceLRU(2, 4096)
        values = [object() for _ in range(3)]
        for number in (0, 1):
            self.assertTrue(cache.store((number,), values[number], 100))
        self.assertIs(values[0], cache.lookup((0,)))
        self.assertTrue(cache.store((2,), values[2], 100))
        self.assertIsNone(cache.lookup((1,)))
        self.assertIs(values[0], cache.lookup((0,)))
        self.assertIs(values[2], cache.lookup((2,)))
        self.assertEqual(1, cache.snapshot()['evictions'])
        cache = ReferenceLRU(8, 1000)
        self.assertTrue(cache.store(('first',), values[0], 600))
        self.assertTrue(cache.store(('second',), values[1], 600))
        self.assertIsNone(cache.lookup(('first',)))
        self.assertIs(values[1], cache.lookup(('second',)))
        self.assertEqual(1, cache.snapshot()['entries'])
        self.assertLessEqual(cache.snapshot()['charged_bytes'], 1000)

    def test_oversized_entry_preserves_existing_and_clear_releases_ownership(self):
        cache = ReferenceLRU(2, 4096)
        item = Prompt(None, None)
        reference = weakref.ref(item)
        cache.store(('retained',), item, 100)
        del item
        gc.collect()
        self.assertIsNotNone(reference())
        before = cache.snapshot()['charged_bytes']
        self.assertFalse(cache.store(('oversized',), object(), 4097))
        self.assertEqual(before, cache.snapshot()['charged_bytes'])
        self.assertEqual(1, cache.snapshot()['bypasses'])
        cache.clear()
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual((0, 0, 1), tuple(cache.snapshot()[name]
                         for name in ('entries', 'charged_bytes', 'cleared_entries')))

    def test_public_prompt_snapshot_and_delivery_copy_isolate_tensor_and_wrapper_mutation(self):
        original = [Prompt(torch.arange(12).reshape(3, 4), torch.arange(8, dtype=torch.float32))]
        stored, charge = copy_clone_prompt(original)
        delivered, _ = copy_clone_prompt(stored)
        original[0].ref_code.fill_(99)
        original[0].ref_text = 'Changed source wrapper'
        delivered[0].ref_spk_embedding.zero_()
        delivered.clear()
        self.assertTrue(torch.equal(stored[0].ref_code, torch.arange(12).reshape(3, 4)))
        self.assertTrue(torch.equal(stored[0].ref_spk_embedding, torch.arange(8, dtype=torch.float32)))
        self.assertEqual('Synthetic reference', stored[0].ref_text)
        self.assertGreater(charge, 12 * 8 + 8 * 4)
        self.assertIsNone(stored[0].ref_spk_embedding.grad_fn)
        self.assertEqual('cpu', stored[0].ref_code.device.type)

    def test_small_noncontiguous_view_does_not_retain_large_backing_storage(self):
        backing = torch.arange(100000, dtype=torch.float32, requires_grad=True)
        original = [Prompt(None, backing[::10000])]
        stored, charge = copy_clone_prompt(original)
        value = stored[0].ref_spk_embedding
        self.assertTrue(value.is_contiguous())
        self.assertEqual(10 * 4, value.untyped_storage().nbytes())
        self.assertLess(charge, 10000)
        self.assertFalse(value.requires_grad)
        self.assertTrue(torch.equal(value, backing[::10000]))

    def test_unknown_hidden_sparse_and_over_budget_prompts_bypass_before_copy(self):
        normal = Prompt(None, torch.zeros(20))
        hidden = Prompt(None, torch.zeros(20))
        hidden.unaccounted = bytearray(10000)
        sparse = Prompt(None, torch.eye(2).to_sparse())
        for prompt in ([object()], [hidden], [sparse], [normal] * 17):
            self.assertIsNone(copy_clone_prompt(prompt))
        with patch.object(torch.Tensor, 'to', side_effect=AssertionError('oversized copy allocated')):
            self.assertIsNone(copy_clone_prompt([normal], max_bytes=1))

    def test_quantized_metadata_storage_and_aliases_are_not_admitted(self):
        value = torch.quantize_per_channel(torch.ones(10000, 1), torch.ones(10000),
                    torch.zeros(10000, dtype=torch.int64), 0, torch.qint8)
        self.assertEqual(torch.strided, value.layout)
        self.assertGreater(value.q_per_channel_scales().untyped_storage().nbytes(),
                           value.numel() * value.element_size())
        self.assertIsNone(copy_clone_prompt([Prompt(None, value)], max_bytes=30000))

    def test_meta_nested_and_complex_tensors_bypass_without_cpu_copy(self):
        nested = torch.nested.nested_tensor([torch.zeros(2), torch.zeros(3)])
        for value in (torch.empty(4, device='meta'), nested, torch.ones(4, dtype=torch.complex64)):
            with patch.object(torch.Tensor, 'to', side_effect=AssertionError('unsupported tensor copied')):
                self.assertIsNone(copy_clone_prompt([Prompt(None, value)]))

    def test_snapshot_preserves_prepared_state_without_reapplying_constructor_transforms(self):
        @dataclass
        class TransformingPrompt(Prompt):
            def __post_init__(self):
                self.ref_spk_embedding = self.ref_spk_embedding.repeat(2)
        original = TransformingPrompt(None, torch.arange(3, dtype=torch.float32))
        self.assertEqual(6, original.ref_spk_embedding.numel())
        stored, _ = copy_clone_prompt([original])
        delivered, _ = copy_clone_prompt(stored)
        for item in (stored[0], delivered[0]):
            self.assertIs(type(original), type(item))
            self.assertTrue(torch.equal(original.ref_spk_embedding, item.ref_spk_embedding))
            self.assertEqual(6 * 4, item.ref_spk_embedding.untyped_storage().nbytes())

    def test_inference_mode_prompt_can_be_copied_and_used_outside_its_creation_scope(self):
        with torch.inference_mode():
            original = [Prompt(torch.arange(8).reshape(2, 4), torch.arange(4, dtype=torch.bfloat16))]
        self.assertTrue(original[0].ref_spk_embedding.is_inference())
        stored, _ = copy_clone_prompt(original)
        delivered, _ = copy_clone_prompt(stored)
        self.assertEqual(torch.bfloat16, delivered[0].ref_spk_embedding.dtype)
        self.assertTrue(torch.equal(original[0].ref_code, delivered[0].ref_code))
        delivered[0].ref_spk_embedding.zero_()
        self.assertTrue(torch.equal(original[0].ref_spk_embedding, stored[0].ref_spk_embedding))
