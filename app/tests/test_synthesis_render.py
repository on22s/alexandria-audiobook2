"""Private parent orchestration preserves exact prepared spans and call-local receipts."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
import soundfile as sf
from synthesis_render import (ensure_synthesis_receipts, get_synthesis_window_plan,
                              render_synthesis_windows)
from synthesis_settings import get_synthesis_window_settings
from tts import ExportCancelled


class SynthesisRenderTests(unittest.TestCase):
    def policy(self, chars=5):
        return get_synthesis_window_settings({'enabled': True, 'budgets': {'local.custom': {'max_chars': chars}}})

    def write(self, path, channels=1):
        sf.write(path, np.full(2400 if channels == 1 else (2400, channels), .2), 24000)

    def test_exact_spans_one_parent_receipt_and_nested_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = self.policy(); text = 'One. Two.'
            plan = get_synthesis_window_plan(text, policy, ['local.custom']); seen = []
            def render(window, path):
                seen.append(window)
                self.assertIsNone(get_synthesis_window_plan(text, policy, ['local.custom']))
                self.write(path); return True
            output = Path(tmp)/'parent.wav'
            with ensure_synthesis_receipts() as receipts:
                result = render_synthesis_windows(plan, render, output, policy)
            self.assertEqual(text, ''.join(seen)); self.assertEqual(2, len(seen))
            self.assertEqual(4800, sf.info(output).frames); self.assertEqual([result], receipts)
            self.assertEqual([0, 1], [item['index'] for item in result['windows']])
            self.assertEqual(plan.text_sha256, result['prepared_text_sha256'])
            self.assertFalse(list(Path(tmp).glob('.synthesis-render-*')))
            self.assertIsNotNone(get_synthesis_window_plan(text, policy, ['local.custom']))

    def test_default_unset_unsplit_and_smallest_explicit_member_budget(self):
        self.assertIsNone(get_synthesis_window_plan('One. Two.', None, ['local.custom']))
        self.assertIsNone(get_synthesis_window_plan('One.', self.policy(), ['local.custom']))
        self.assertIsNone(get_synthesis_window_plan('One. Two.', self.policy(), ['local.clone']))
        policy = get_synthesis_window_settings({'enabled': True, 'budgets': {
            'local.ensemble': {'max_chars': 20}, 'local.custom': {'max_chars': 5, 'max_words': 1},
            'local.clone': {'max_chars': 9, 'max_words': 2}}})
        plan = get_synthesis_window_plan('One. Two.', policy, ['local.ensemble', 'local.custom', 'local.clone'])
        self.assertEqual((5, 1, 2), (plan.max_chars, plan.max_words, len(plan.windows)))

    def test_failure_cancel_and_invalid_layout_stop_before_later_calls_and_keep_prior(self):
        for outcome in ('false', 'cancel', 'false_cancel', 'exception_cancel', 'wide'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)/'parent.wav'; output.write_bytes(b'prior')
                policy = self.policy(); plan = get_synthesis_window_plan('One. Two.', policy, ['local.custom'])
                cancelled = False; calls = []
                def render(text, path):
                    nonlocal cancelled
                    calls.append(text); self.write(path, 16 if outcome == 'wide' else 1)
                    if outcome in ('cancel', 'false_cancel', 'exception_cancel'): cancelled = True
                    if outcome == 'exception_cancel': raise RuntimeError('provider aborted')
                    return outcome not in ('false', 'false_cancel')
                with ensure_synthesis_receipts() as receipts:
                    with self.assertRaises(ExportCancelled if 'cancel' in outcome else (RuntimeError, ValueError)):
                        render_synthesis_windows(plan, render, output, policy, lambda: cancelled)
                self.assertEqual(1, len(calls)); self.assertEqual([], receipts)
                self.assertEqual(b'prior', output.read_bytes()); self.assertFalse(list(Path(tmp).glob('.synthesis-render-*')))
                self.assertIsNotNone(get_synthesis_window_plan('One. Two.', policy, ['local.custom']))

    def test_bad_captured_plan_is_refused_before_provider_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = self.policy(); plan = get_synthesis_window_plan('One. Two.', policy, ['local.custom'])
            calls = []
            for bad in (replace(plan, windows=tuple(reversed(plan.windows))), replace(plan, max_chars=True),
                        replace(plan, windows=list(plan.windows)), replace(plan, text_sha256='changed')):
                with self.subTest(plan=bad), self.assertRaises(ValueError):
                    render_synthesis_windows(bad, lambda *args: calls.append(args), Path(tmp)/'parent.wav', policy)
            self.assertEqual([], calls); self.assertFalse(list(Path(tmp).iterdir()))

    def test_nested_collectors_and_parallel_parent_receipts_do_not_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = []; errors = []; barrier = threading.Barrier(2)
            def run(number, text):
                try:
                    policy = self.policy(); plan = get_synthesis_window_plan(text, policy, ['local.custom'])
                    def render(window, path):
                        self.write(path); return True
                    with ensure_synthesis_receipts() as outer:
                        barrier.wait(timeout=3)
                        with ensure_synthesis_receipts() as inner:
                            receipt = render_synthesis_windows(plan, render, Path(tmp)/f'{number}.wav', policy)
                        self.assertEqual([], outer); self.assertEqual([receipt], inner)
                        results.append(receipt['prepared_text_sha256'])
                except BaseException as error: errors.append(error)
            workers = [threading.Thread(target=run, args=(0, 'One. Two.')),
                       threading.Thread(target=run, args=(1, 'Red. Blue.'))]
            for worker in workers: worker.start()
            for worker in workers: worker.join(10)
            self.assertFalse(any(worker.is_alive() for worker in workers)); self.assertEqual([], errors)
            self.assertEqual(2, len(set(results)))

    def test_accumulated_promoted_size_stops_before_decoding_or_requesting_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'parent.wav'; output.write_bytes(b'prior')
            policy = self.policy(); plan = get_synthesis_window_plan('One. Two. Red.', policy, ['local.custom'])
            calls = []
            def render(text, path):
                calls.append(text); Path(path).write_bytes(b'private'); return True
            headers = [SimpleNamespace(channels=channels, samplerate=24000, frames=300000000)
                       for channels in (1, 2)]
            with patch('synthesis_render.get_synthesis_audio_info', side_effect=headers), \
                    patch('synthesis_render.validate_synthesis_audio') as validate, \
                    patch('audio_mastering.get_audio_sha256', return_value='fixture-sha'), \
                    self.assertRaises((ValueError, OverflowError)):
                render_synthesis_windows(plan, render, output, policy)
            self.assertEqual(2, len(calls)); self.assertEqual(1, validate.call_count)
            self.assertEqual(b'prior', output.read_bytes()); self.assertFalse(list(Path(tmp).glob('.synthesis-render-*')))
