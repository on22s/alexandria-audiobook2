"""GPU placement follows the endpoint when on_this_gpu is unset (#2134).

The fallback in core.is_llm_on_this_gpu used to reuse the billing answer
(is_active_llm_remote), which classifies llm_mode="remote" as remote even when
base_url is loopback. That let a script/review task bypass the GPU lock against
audio. The documented contract (function docstring, README, Setup "Auto"
option) says the fallback is the ENDPOINT's locality.

Explicit on_this_gpu overrides remain authoritative - including false for a
localhost tunnel to another machine or a CPU-only server. That is the
deliberate policy from #1193 / PR #1235 and the labelled Setup control; #2119's
request to override an explicit false is parked for product triage, not silently
applied here.
"""
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import core


def _config(mode, base_url, on_this_gpu=None, transport=None):
    profile = {'base_url': base_url, 'model_name': 'fixture-model'}
    if on_this_gpu is not None:
        profile['on_this_gpu'] = on_this_gpu
    if transport is not None:
        profile['transport'] = transport
    return {'llm_mode': mode, 'llm_' + mode: profile}


class RemoteLoopbackPlacementTests(unittest.TestCase):
    def test_remote_loopback_without_flag_is_on_this_gpu(self):
        self.assertTrue(core.is_llm_on_this_gpu(
            _config('remote', 'http://127.0.0.1:4301/v1')))

    def test_remote_loopback_without_flag_serializes_with_audio(self):
        config = _config('remote', 'http://127.0.0.1:4301/v1')
        state = {'script': {'running': False}, 'audio': {'running': True}}
        with patch.object(core, 'load_app_config', return_value=config), \
             patch.object(core, 'process_state', state), \
             patch.object(core, '_task_claims', {}):
            with self.assertRaises(core.HTTPException):
                core.check_global_gpu_lock('script')

    def test_explicit_false_still_overrides_for_tunnels_and_cpu_servers(self):
        self.assertFalse(core.is_llm_on_this_gpu(
            _config('remote', 'http://127.0.0.1:4301/v1', on_this_gpu=False)))

    def test_explicit_true_and_manual_transport(self):
        self.assertTrue(core.is_llm_on_this_gpu(
            _config('remote', 'http://127.0.0.1:4301/v1', on_this_gpu=True)))
        self.assertFalse(core.is_llm_on_this_gpu(
            _config('remote', 'http://127.0.0.1:4301/v1', transport='manual')))

    def test_genuine_remote_endpoint_and_local_loopback_controls(self):
        self.assertFalse(core.is_llm_on_this_gpu(
            _config('remote', 'https://api.example.invalid/v1')))
        self.assertTrue(core.is_llm_on_this_gpu(
            _config('local', 'http://127.0.0.1:1234/v1')))

    def test_local_loopback_explicit_false_is_honoured(self):
        # #2119 asks to force this True; parked. The Setup control labels false
        # "hosted API, CPU-only server, or another machine", #1193/PR #1235
        # depend on honouring it, and the existing suite asserts it.
        self.assertFalse(core.is_llm_on_this_gpu(
            _config('local', 'http://127.0.0.1:1234/v1', on_this_gpu=False)))

    def test_conservative_fallback_on_policy_read_failure(self):
        with patch.object(core, 'load_app_config', side_effect=OSError('no config')), \
             patch.object(core, 'record_failure'):
            self.assertTrue(core.llm_is_on_this_gpu())

    def test_claim_gpu_task_resolves_endpoint_for_remote_loopback(self):
        # needs_local_gpu and the conflict set must agree (issue #2098).
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(core.process_state, {
            'script': {'running': False, 'logs': []}}))
        stack.enter_context(patch.object(core, '_task_claims', {}))
        stack.enter_context(patch.object(core, '_gpu_leases', {}))
        stack.enter_context(patch.object(core.threading, 'Thread', MagicMock()))
        stack.enter_context(patch.object(
            core, 'is_task_running', side_effect=lambda name, states=None: False))
        lease = MagicMock()
        lease_call = stack.enter_context(patch.object(
            core, 'acquire_task_lease', return_value=lease))
        gpu_handle = object()
        gpu_lock = stack.enter_context(patch.object(
            core, 'acquire_gpu_lock', return_value=gpu_handle))
        stack.enter_context(patch.object(core, 'release_gpu_lock'))
        stack.enter_context(patch.object(
            core.project_manager, 'ensure_idle_local_models_released', return_value=True))
        stack.enter_context(patch.object(core, 'record_failure'))
        core.claim_gpu_task('script',
                            llm_config=_config('remote', 'http://127.0.0.1:4301/v1'))
        gpu_lock.assert_called_once_with()
        self.assertIn('audio', lease_call.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
