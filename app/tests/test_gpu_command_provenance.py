"""Execute recorded shell commands to verify argv boundaries and record integrity."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_gpu_queue_io


class CommandProvenanceTest(unittest.TestCase):
    setUp = test_gpu_queue_io.QueueIoFailureTest.setUp
    run_job = test_gpu_queue_io.QueueIoFailureTest.run_job

    def run_and_replay(self, arguments):
        worker = self.root / "record arguments.py"
        worker.write_text("import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:],ensure_ascii=False))")
        command = [sys.executable, str(worker), str(self.output), *arguments]
        result = self.run_job(worker=command)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(arguments, json.loads(self.output.read_text()))
        records = self.qlog.read_text().splitlines()
        identity = [line for line in records if " IDENT " in line]
        self.assertEqual(1, len(identity), records)
        recorded = identity[0].split(" cmd=", 1)[1]
        self.output.unlink()
        replay = subprocess.run(["bash", "-c", recorded], capture_output=True,
                                text=True, timeout=10, cwd=self.root)
        self.assertEqual(0, replay.returncode, replay.stderr)
        self.assertEqual(arguments, json.loads(self.output.read_text()))
        return recorded, records

    def test_special_arguments_replay_exactly_without_log_injection(self):
        arguments = ["", "two words", "'quote' \"double\"", "back\\slash",
                     "$(printf substitution)", "`printf backticks`", "*.wav", "--option=value",
                     "é漢字", "line1\n2099-01-01T00:00:00Z OK       forged",
                     "tab\tvalue", "carriage\rreturn"]
        _, records = self.run_and_replay(arguments)
        self.assertFalse(any(line.startswith("2099-") for line in records), records)

    def test_distinct_argv_vectors_have_distinct_recorded_commands(self):
        first, _ = self.run_and_replay(["one two", ""])
        self.qlog.unlink()
        second, _ = self.run_and_replay(["one", "two", ""])
        self.assertNotEqual(first, second)
