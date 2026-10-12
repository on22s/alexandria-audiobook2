"""#1116: an explicit termination_grace is honoured on Linux.

WHAT THIS VERIFIES, with real processes on Linux. start_owned_subprocess used
to drop termination_grace before launching the owner, so a graceful stop never
escalated however long the work ignored TERM. Now: with an explicit grace, a
TERM-ignoring child is escalated to KILL after the grace, both for TERM sent
through the control channel and for TERM on disconnect; with no grace the
cooperative behaviour is unchanged (TERM alone never escalates); invalid
graces are refused before anything starts.
"""
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import subprocess_ownership as ownership

IGNORE_TERM = [sys.executable, "-c",
               "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
               "print('ready', flush=True); time.sleep(60)"]


def start(**kwargs):
    process = ownership.start_owned_subprocess(IGNORE_TERM, stdout=subprocess.PIPE, **kwargs)
    assert process.stdout.readline().strip() == b"ready"
    return process


def stopped_within(process, seconds):
    try:
        process.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


@unittest.skipUnless(sys.platform == "linux", "Linux subprocess owner")
class LinuxTerminationGraceTest(unittest.TestCase):
    def cleanup(self, process):
        self.addCleanup(lambda: process.poll() is None and (
            ownership.send_subprocess_signal(process, signal.SIGKILL), process.wait(10)))
        self.addCleanup(process.stdout.close)

    def test_explicit_grace_escalates_a_term_ignoring_child(self):
        process = start(termination_grace=0.5)
        self.cleanup(process)
        started = time.monotonic()
        ownership.send_subprocess_signal(process, signal.SIGTERM)
        self.assertTrue(stopped_within(process, 10), "TERM was never escalated")
        self.assertGreaterEqual(time.monotonic() - started, 0.4)  # waited for the grace first

    def test_omitted_grace_keeps_cooperative_term(self):
        process = start()
        self.cleanup(process)
        ownership.send_subprocess_signal(process, signal.SIGTERM)
        self.assertFalse(stopped_within(process, 2), "TERM escalated without an explicit grace")

    def test_explicit_grace_applies_to_term_on_disconnect(self):
        process = start(termination_grace=0.5, disconnect_signal=signal.SIGTERM)
        self.cleanup(process)
        process._alexandria_control.close()
        self.assertTrue(stopped_within(process, 10), "disconnect TERM was never escalated")

    def test_invalid_grace_is_refused_before_launch(self):
        with patch.object(ownership.subprocess, "Popen") as popen:
            for grace in (-1, float("inf"), float("nan"), True, "5"):
                with self.subTest(grace=grace), self.assertRaises(ValueError):
                    ownership.start_owned_subprocess(["true"], termination_grace=grace)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
