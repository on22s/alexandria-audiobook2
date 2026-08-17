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
