"""Preflight checks for the standalone batch preparer wrapper."""

import sys
import os
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import alexandria_batch_processor as batch


class BatchProcessorPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root / "model.gguf"
        self.model.touch()

    def test_audio_directory_is_not_processed(self):
        audio_dir = self.root / "book.wav"
        audio_dir.mkdir()
        processor = batch.BatchProcessor(str(self.model))
        self.assertEqual([], processor.validate_files([str(audio_dir)]))
        self.assertEqual("Audio file not found", processor.results["skipped"][0]["reason"])

    def test_folder_discovery_ignores_audio_named_directory(self):
        audio_dir = self.root / "book.wav"
        audio_dir.mkdir()
        argv = ["batch", "--folder", str(self.root), "--model", str(self.model)]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as caught:
            batch.main()
        self.assertEqual(1, caught.exception.code)

    def test_invalid_numeric_arguments_stop_before_processing(self):
        audio = self.root / "book.wav"
        audio.touch()
        for option, value in (("--chunk-size", "0"), ("--chunk-size", "nan"),
                              ("--source-threshold", "nan"),
                              ("--source-threshold", "1.1")):
            with self.subTest(option=option, value=value):
                argv = ["batch", str(audio), "--model", str(self.model), option, value]
                with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as caught:
                    batch.main()
                self.assertEqual(2, caught.exception.code)

    def test_batch_main_holds_gpu_lease_through_run(self):
        from experiments.gpu_guard import acquire_gpu_lock, gpu_is_busy
        audio = self.root / 'book.wav'
        audio.touch()
        lock = str(self.root / 'gpu.lock')
        observed = []
        def run(_processor, _files):
            observed.append(gpu_is_busy(lock))
            return True
        argv = ['batch', str(audio), '--model', str(self.model)]
        with patch.object(sys, 'argv', argv), \
             patch.object(batch, 'acquire_gpu_lock', side_effect=lambda: acquire_gpu_lock(lock)), \
             patch.object(batch.BatchProcessor, 'run', run):
            with self.assertRaises(SystemExit) as result:
                batch.main()
        self.assertEqual(0, result.exception.code)
        self.assertEqual([True], observed)
        self.assertFalse(gpu_is_busy(lock))

    def test_source_directory_is_rejected(self):
        audio = self.root / "book.wav"
        audio.touch()
        argv = ["batch", str(audio), "--model", str(self.model), "--source", str(self.root)]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as caught:
            batch.main()
        self.assertEqual(2, caught.exception.code)

    def test_fuzzy_source_tie_requires_explicit_choice(self):
        sources = self.root / "sources"
        sources.mkdir()
        (sources / "zeta book.epub").touch()
        (sources / "alpha book.epub").touch()
        # Both candidates overlap one of the two audio tokens equally.
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            batch._find_source_for("alpha zeta.wav", str(sources))

    def test_source_catalog_is_scanned_once_and_match_reused(self):
        sources = self.root / "sources"
        sources.mkdir()
        audio_files = []
        for name in ("alpha", "beta"):
            (sources / f"{name}.epub").touch()
            audio = self.root / f"{name}.wav"
            audio.touch()
            audio_files.append(str(audio))
        processor = batch.BatchProcessor(str(self.model), source_folder=str(sources))
        original_scandir = os.scandir
        scans = []

        def record_scan(path):
            if str(path) == str(sources):
                scans.append(path)
            return original_scandir(path)

        with patch.object(batch.os, "scandir", side_effect=record_scan), \
             patch.object(batch.subprocess, "Popen", side_effect=OSError("stop")) as popen:
            self.assertEqual(audio_files, processor.validate_files(audio_files))
            processor.process_file(audio_files[0], 1, 2)
        self.assertEqual(1, len(scans))
        cmd = popen.call_args.args[0]
        self.assertEqual(str(sources / "alpha.epub"), cmd[cmd.index("--source") + 1])

    def test_preparer_path_does_not_depend_on_current_directory(self):
        audio = self.root / "book.wav"
        audio.touch()
        processor = batch.BatchProcessor(str(self.model))
        with patch.object(batch.subprocess, "Popen", side_effect=OSError("stop")) as popen:
            processor.process_file(str(audio), 1, 1)
        cmd = popen.call_args.args[0]
        self.assertEqual(str(Path(batch.__file__).resolve().with_name(
            "alexandria_preparer_rocm_compatible.py")), cmd[2])
        self.assertTrue(Path(cmd[2]).is_file())

    def test_unresponsive_child_is_reaped_after_interrupt(self):
        audio = self.root / "book.wav"
        audio.touch()
        processor = batch.BatchProcessor(str(self.model))

        class InterruptedOutput:
            def __iter__(self):
                raise KeyboardInterrupt

        class Child:
            stdout = InterruptedOutput()
            def __init__(self):
                self.wait_calls = []
                self.killed = False
            def poll(self):
                return None
            def terminate(self):
                pass
            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                if timeout is not None:
                    raise batch.subprocess.TimeoutExpired("preparer", timeout)
            def kill(self):
                self.killed = True

        child = Child()
        with patch.object(batch.subprocess, "Popen", return_value=child):
            with self.assertRaises(KeyboardInterrupt):
                processor.process_file(str(audio), 1, 1)
        self.assertTrue(child.killed)
        self.assertEqual([10, None], child.wait_calls)

    def test_receipt_write_failure_keeps_previous_receipt(self):
        receipt = self.root / "batch_results.json"
        receipt.write_text('{"previous": true}', encoding="utf-8")
        with patch.object(batch.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                batch.save_batch_receipt(str(receipt), {"replacement": True})
        self.assertEqual('{"previous": true}', receipt.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.root.glob(".batch_results_*")))

    def test_batch_summary_writes_a_complete_final_receipt(self):
        processor = batch.BatchProcessor(str(self.model))
        processor.results["skipped"].append({"file": "book.wav", "reason": "Already processed"})
        previous_directory = os.getcwd()
        try:
            os.chdir(self.root)
            with patch.object(batch, "log_gpu_stats"):
                processor.print_summary()
            receipts = list(self.root.glob("batch_results_*.json"))
            self.assertEqual(1, len(receipts))
            document = json.loads(receipts[0].read_text())
            self.assertEqual(processor.results, document["results"])
            self.assertEqual(processor.total_time, document["total_time_seconds"])
            self.assertEqual([], list(self.root.glob(".batch_results_*")))
        finally:
            os.chdir(previous_directory)

    def test_same_stem_inputs_have_distinct_stable_output_names(self):
        first = self.root / "first" / "book.wav"
        second = self.root / "second" / "book.mp3"
        first.parent.mkdir()
        second.parent.mkdir()
        self.assertNotEqual(batch.get_output_name(first), batch.get_output_name(second))
        self.assertEqual(batch.get_output_name(first), batch.get_output_name(str(first)))

    def test_invalid_zip_is_reprocessed_but_complete_zip_is_skipped(self):
        audio = self.root / "book.wav"
        audio.touch()
        output = self.root / "dataset.zip"
        processor = batch.BatchProcessor(str(self.model))
        with patch.object(batch, "get_output_name", return_value=str(output)):
            output.write_bytes(b"")
            self.assertEqual([str(audio)], processor.validate_files([str(audio)]))
            output.write_bytes(b"broken zip")
            self.assertEqual([str(audio)], processor.validate_files([str(audio)]))
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("metadata.jsonl", '{"audio_filepath":"train/clip.wav"}\n')
                archive.writestr("train/clip.wav", b"RIFF-sample")
            self.assertEqual([str(audio)], processor.validate_files([str(audio)]))
            batch.save_batch_receipt(str(output) + ".complete.json", {
                "source": batch.get_source_identity(str(audio)), "volumes": [str(output)]})
            self.assertEqual([], processor.validate_files([str(audio)]))
            output.write_bytes(b"broken after completion")
            self.assertEqual([str(audio)], processor.validate_files([str(audio)]))
        self.assertEqual("Already processed (use --force to reprocess)",
                         processor.results["skipped"][-1]["reason"])

    def test_multivolume_success_is_recorded_and_source_change_reprocesses(self):
        audio = self.root / "book.wav"
        audio.touch()
        output = self.root / "dataset.zip"
        volume = self.root / "dataset_Speaker_vol01.zip"
        processor = batch.BatchProcessor(str(self.model))

        class Child:
            stdout = []
            returncode = 0
            def poll(self):
                return 0
            def wait(self):
                return 0

        def write_volume(*args, **kwargs):
            with zipfile.ZipFile(volume, "w") as archive:
                archive.writestr("metadata.jsonl", '{"audio_filepath":"train/clip.wav"}\n')
                archive.writestr("train/clip.wav", b"RIFF-sample")
            return Child()

        with patch.object(batch, "get_output_name", return_value=str(output)), \
             patch.object(batch.subprocess, "Popen", side_effect=write_volume):
            processor.process_file(str(audio), 1, 1)
            self.assertEqual([str(volume)], processor.results["succeeded"][0]["outputs"])
            self.assertEqual([], processor.validate_files([str(audio)]))
            stat = audio.stat()
            os.utime(audio, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            self.assertEqual([str(audio)], processor.validate_files([str(audio)]))

    def test_failed_rerun_invalidates_previous_completion_marker(self):
        audio = self.root / "book.wav"
        audio.touch()
        output = self.root / "dataset.zip"
        marker = Path(str(output) + ".complete.json")
        batch.save_batch_receipt(str(marker), {
            "source": batch.get_source_identity(str(audio)), "volumes": [str(output)]})
        processor = batch.BatchProcessor(str(self.model), force=True)
        with patch.object(batch, "get_output_name", return_value=str(output)), \
             patch.object(batch.subprocess, "Popen", side_effect=OSError("launch failed")):
            processor.process_file(str(audio), 1, 1)
        self.assertFalse(marker.exists())
        self.assertEqual("launch failed", processor.results["failed"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
