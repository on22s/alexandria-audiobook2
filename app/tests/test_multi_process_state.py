import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core


class MultiProcessStateTests(unittest.TestCase):
    def setUp(self):
        self.key = "_test_multi_process"
        self.processes = [SimpleNamespace(poll=lambda: None),
                          SimpleNamespace(poll=lambda: None)]
        core.process_state[self.key] = {
            "running": True, "cancel": False, "paused": False, "logs": [],
            "process": self.processes[-1], "processes": self.processes,
        }

    def tearDown(self):
        core.process_state.pop(self.key, None)

    def test_batch_cancel_signals_every_process(self):
        with patch.object(core, "_send_signal_tree") as signal_tree:
            core._batch_cancel_helper(self.key)
        self.assertTrue(core.process_state[self.key]["cancel"])
        self.assertEqual(self.processes, [call.args[0] for call in signal_tree.call_args_list])

    def test_pause_and_resume_signal_every_process(self):
        with patch.object(core, "_posix_signal") as signal_process:
            core._pause_task(self.key, "idle", "starting", "Batch")
            core._resume_task(self.key, "idle", "Batch")
        self.assertEqual(4, signal_process.call_count)
        self.assertFalse(core.process_state[self.key]["paused"])


if __name__ == "__main__":
    unittest.main()


class LlmOffGpuLockTests(unittest.TestCase):
    """LLM-only tasks stop holding the GPU lock against audio work when the
    active LLM profile is not on this machine's GPU; on it, nothing changes."""

    def setUp(self):
        import core
        self.core = core
        for name in ("script", "audio", "review", "lora_training"):
            core.process_state[name]["running"] = False

    def tearDown(self):
        for name in ("script", "audio", "review", "lora_training"):
            self.core.process_state[name]["running"] = False

    def test_on_gpu_keeps_the_full_lock(self):
        from unittest.mock import patch
        with patch.object(self.core, "llm_is_on_this_gpu", return_value=True):
            self.core.process_state["audio"]["running"] = True
            with self.assertRaises(self.core.HTTPException):
                self.core.check_global_gpu_lock("script")

    def test_off_gpu_lets_script_and_audio_overlap_but_not_two_llm_tasks(self):
        from unittest.mock import patch
        with patch.object(self.core, "llm_is_on_this_gpu", return_value=False):
            self.core.process_state["audio"]["running"] = True
            self.core.check_global_gpu_lock("script")            # allowed now
            self.core.process_state["audio"]["running"] = False
            self.core.process_state["script"]["running"] = True
            self.core.check_global_gpu_lock("audio")             # and the other way round
            self.core.check_global_gpu_lock("lora_training")
            with self.assertRaises(self.core.HTTPException):
                self.core.check_global_gpu_lock("review")        # two LLM tasks still serialise

    def test_profile_flag_and_endpoint_decide(self):
        from unittest.mock import patch
        cases = [
            ({"llm_mode": "local", "llm": {"base_url": "http://localhost:1234/v1"}}, True),
            ({"llm_mode": "local", "llm": {"base_url": "http://localhost:1234/v1", "on_this_gpu": False}}, False),
            ({"llm_mode": "remote", "llm": {"base_url": "https://api.deepseek.com/v1"}}, False),
            ({"llm_mode": "remote", "llm": {"base_url": "https://api.deepseek.com/v1", "on_this_gpu": True}}, True),
        ]
        for config, expected in cases:
            with patch.object(self.core, "load_app_config", return_value=config):
                self.assertEqual(expected, self.core.llm_is_on_this_gpu(), config)
        with patch.object(self.core, "load_app_config", side_effect=OSError("no config")):
            self.assertTrue(self.core.llm_is_on_this_gpu())
