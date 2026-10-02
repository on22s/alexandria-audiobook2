"""Exercise report labels through the actual corpus CLI with owned structured artifacts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.corpus_fixture_support import copy_report_driver, WORKER
from tests import test_corpus_dispatch_identity as dispatch_fixture

ROOT = Path(__file__).resolve().parent.parent.parent


class CorpusReportLabelTests(unittest.TestCase):
    def test_aggregate_keeps_complete_timestamp_and_distinct_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = dispatch_fixture.CorpusDispatchIdentityTests()
            root = fixture.create_repo(base)
            output = root / 'output'
            original = {}
            for stamp in ('20260929_120000', '20270929_120000'):
                path = root / 'logs' / ('alexandria_preparer_' + stamp + '.log')
                path.write_text('Annotation complete: 999 total segments (999 new this run)\nTotal audio in dataset : 999.0s\n')
                original[path] = path.read_bytes()
            identities = []
            for _ in range(2):
                result = fixture.invoke(root, output, '--run', base)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                index = json.loads((output / 'corpus_attempts.json').read_text())
                identities.append(index['run_id'])
                self.assertRegex(index['run_id'], r'^\d{8}_\d{6}-.+')
            self.assertEqual(2, len(set(identities)))
            for identity in identities:
                self.assertTrue((output / 'annotation_reports' / identity / 'attempts.json').is_file())
            result = fixture.invoke(root, output, '--aggregate', base)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            report = (output / 'aggregated_report.md').read_text()
            self.assertIn(identities[-1], report)
            self.assertNotIn(identities[0], report)
            self.assertIn('Total segments emitted: **6**', report)
            self.assertIn('Total dataset audio:    **24s**', report)
            for path, data in original.items():
                self.assertEqual(data, path.read_bytes())


class CorpusModeInputTests(unittest.TestCase):
    def test_help_and_aggregate_do_not_discover_or_require_input_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "repository")
            root.mkdir()
            (root / "build_test_corpus.sh").write_bytes((ROOT / "build_test_corpus.sh").read_bytes())
            copy_report_driver(root)
            python = root / "app/env/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            logs = root / "logs"
            logs.mkdir()
            log = logs / "alexandria_preparer_20260930_032000.log"
            log.write_text("Annotation complete: 7 total segments (7 new this run)\n"
                           "Total audio in dataset : 21.0s\n")
            original = log.read_bytes()
            env = {key:value for key,value in os.environ.items()
                   if key not in ("AUDIO_DIR", "SOURCE_DIR")}
            output = root / "output"
            env["OUT_DIR"] = str(output)
            for mode in ("--help", "-h", None, "--aggregate"):
                with self.subTest(mode=mode):
                    args = ["bash", str(root / "build_test_corpus.sh")]
                    if mode is not None:
                        args.append(mode)
                    result = subprocess.run(args, cwd=tmp, env=env, capture_output=True,
                                            text=True, timeout=10)
                    if mode == "--aggregate":
                        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                        self.assertIn("No owned corpus attempt index", result.stderr)
                    else:
                        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                        self.assertIn("--aggregate", result.stdout)
            self.assertEqual(original, log.read_bytes())
            self.assertFalse((output / "pairs.json").exists())
            self.assertFalse((output / "dry_run_report.md").exists())

    def test_pairing_modes_still_reject_each_missing_required_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {key:value for key,value in os.environ.items()
                   if key not in ("AUDIO_DIR", "SOURCE_DIR")}
            for mode in ("--plan", "--dry-run", "--run"):
                for audio in (None, str(Path(tmp, "audio"))):
                    with self.subTest(mode=mode, audio=audio):
                        case_env = dict(env, OUT_DIR=str(Path(tmp, "output")))
                        if audio is not None:
                            case_env["AUDIO_DIR"] = audio
                        result = subprocess.run(["bash", str(ROOT / "build_test_corpus.sh"), mode],
                                                cwd=tmp, env=case_env, capture_output=True,
                                                text=True, timeout=10)
                        self.assertNotEqual(0, result.returncode)
                        self.assertIn("Set " + ("AUDIO_DIR" if audio is None else "SOURCE_DIR"),
                                      result.stderr)
            self.assertFalse(Path(tmp, "output").exists())


class CorpusRunFailureTests(unittest.TestCase):
    def test_failed_pairs_remain_failures_after_later_success_and_aggregation(self):
        for failed in (("alpha",), ("alpha", "beta"), ()):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp, "repository")
                root.mkdir()
                for name in ("build_test_corpus.sh", "alexandria_batch_processor.py"):
                    (root / name).write_bytes((ROOT / name).read_bytes())
                copy_report_driver(root)
                python = root / "app/env/bin/python"
                python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                for name in ("audio", "source", "logs"):
                    (root / name).mkdir()
                for stem in ("alpha", "beta", "gamma"):
                    (root / "audio" / (stem + ".wav")).write_bytes(b"CPU dispatch fixture")
                    (root / "source" / (stem + ".txt")).write_text(stem)
                original_sources = {p:p.read_bytes() for name in ("audio", "source")
                                    for p in (root / name).iterdir()}
                wrapper = root / "run_with_restart.sh"
                wrapper.write_text("#!" + sys.executable + "\n" + WORKER)
                wrapper.chmod(0o755)
                output = root / "output"
                env = dict(os.environ, AUDIO_DIR=str(root / "audio"),
                           SOURCE_DIR=str(root / "source"), OUT_DIR=str(output),
                           FAILED_PAIRS=",".join(failed))
                result = subprocess.run(["bash", str(root / "build_test_corpus.sh"), "--run"],
                                        cwd=tmp, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(1 if failed else 0, result.returncode,
                                 result.stdout + result.stderr)
                dispatches = [json.loads(line) for line in
                              (root / "dispatches.jsonl").read_text().splitlines()]
                self.assertEqual(["alpha", "beta", "gamma"],
                                 [Path(args[args.index('--audio') + 1]).stem for args in dispatches])
                report = (output / "aggregated_report.md").read_text()
                self.assertIn(f"Total segments emitted: **{3 * (3 - len(failed))}**", report)
                for stem in ("alpha", "beta", "gamma"):
                    dispatched = next(args for args in dispatches if Path(args[args.index('--audio') + 1]).stem == stem)
                    dataset_path = Path(dispatched[dispatched.index('--output') + 1])
                    self.assertEqual(output, dataset_path.parent)
                    self.assertEqual(stem not in failed, dataset_path.exists())
                for p, data in original_sources.items():
                    self.assertEqual(data, p.read_bytes())
                if failed:
                    self.assertIn(f"{len(failed)} pair(s) failed", result.stderr)
                    self.assertNotIn("All runs complete", result.stdout)
                    for stem in failed:
                        self.assertIn(stem, result.stderr)
                        self.assertIn("exit 7", result.stderr)

    def test_aggregate_failure_is_not_hidden_by_final_success_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "repository")
            root.mkdir()
            (root / "build_test_corpus.sh").write_bytes((ROOT / "build_test_corpus.sh").read_bytes())
            python = root / "app/env/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            copy_report_driver(root)
            (root / "logs").mkdir()
            env = dict(os.environ, OUT_DIR=str(root / "output"))
            result = subprocess.run(["bash", str(root / "build_test_corpus.sh"), "--aggregate"],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("No owned corpus attempt index", result.stderr)
            self.assertNotIn("Done. Report", result.stdout)
            self.assertFalse((root / "output/aggregated_report.md").exists())
