import ntpath
import shlex
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import benchmark_runner as runner
from benchmark_worker_protocol import get_decoded_worker_payload


class RemotePathPlatformTests(unittest.TestCase):
    def test_remote_staging_and_workers_use_posix_paths_on_windows_controller(self):
        fixture = {"sha256": "abc", "dataset_path": "dataset",
                   "audio_sha256": {"nested/audio.wav": "hash"}}
        settings = {"remote_root": "/remote checkout", "remote_python": "/env/python"}
        for stage in ("training", "dedup", "export"):
            with self.subTest(stage=stage):
                calls = []
                workers = []

                def run(command, **kwargs):
                    calls.append(command)
                    return SimpleNamespace(returncode=0, stderr="", stdout=
                                           "/tmp/alexandria-lora-training.ABCDEF1234\n")

                def worker(command, *args, **kwargs):
                    workers.append(shlex.split(command[2]))
                    return {"status": "passed"}

                with patch.object(runner, "os", SimpleNamespace(path=ntpath)), \
                        patch.object(runner, "run_benchmark_subprocess", side_effect=run), \
                        patch.object(runner.subprocess, "run", side_effect=run), \
                        patch.object(runner, "run_benchmark_worker", side_effect=worker), \
                        patch.object(runner, "_validate_export_fixture"):
                    if stage == "training":
                        runner._run_lora_training_worker(fixture, "thunder", settings,
                                                         r"C:\synthetic", "host")
                    elif stage == "dedup":
                        runner._run_dedup_worker(fixture, "thunder", settings,
                                                r"C:\synthetic", "host")
                    else:
                        runner._run_export_worker("mp3", fixture, "thunder", settings,
                                                 r"C:\synthetic", "host")
                transfers = [command for command in calls if command[0] == "scp"]
                self.assertTrue(transfers)
                for command in transfers:
                    self.assertIn("\\", command[1])
                    self.assertNotIn("\\", command[2])
                self.assertTrue(any(command[2].endswith("/nested/audio.wav")
                                    for command in transfers))
                self.assertEqual("/remote checkout/app/" +
                                 {"training": "lora_training", "dedup": "dedup",
                                  "export": "export"}[stage] + "_benchmark.py", workers[0][1])
                payload = get_decoded_worker_payload(workers[0][-1])
                if stage == "training":
                    self.assertEqual("/remote checkout/app/train_lora.py", payload["train_script"])
                    self.assertEqual("alexandria-lora-training.ABCDEF1234",
                                     payload["fixture"]["dataset_path"])
                elif stage == "dedup":
                    self.assertEqual("/remote checkout/tools/voice_lab/voice_analysis.py",
                                     payload["analysis_script"])
