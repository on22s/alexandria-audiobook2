"""Exercise report labels through the actual corpus CLI with owned structured artifacts."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from experiments import manifest
from tests.corpus_fixture_support import copy_report_driver, WORKER
from tests.test_support import get_bash_executable, write_python_bash_fixture

ROOT = Path(__file__).resolve().parent.parent.parent


class CorpusWorkerLaunchTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('corpus_launch_subject', ROOT / 'corpus_run_report.py')
        self.report = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', [str(ROOT), *sys.path]):
            spec.loader.exec_module(self.report)

    def test_windows_resolves_bash_and_preserves_argument_boundaries(self):
        root = PureWindowsPath('C:/corpus repository')
        audio, source = root / 'audio/book.wav', root / 'source/book.txt'
        output, summary = root / 'out/data.zip', root / 'out/summary.json'
        shell = 'C:/Program Files/Git/bin/bash.exe'
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(manifest, '_source_probe_shell', return_value=(shell, None)) as resolve:
            command = self.report.get_worker_command(root, audio, source, output, summary, 'model.gguf', 'fallback.gguf')
        resolve.assert_called_once_with()
        self.assertEqual([shell, 'C:/corpus repository/run_with_restart.sh',
            '--audio', audio.as_posix(), '--source', source.as_posix(), '--model', 'model.gguf',
            '--output', output.as_posix(), '--summary-output', summary.as_posix(),
            '--chunk-size', '10.0', '--lang', 'en', '--fallback-model', 'fallback.gguf'], command)

    def test_posix_keeps_direct_execution_and_original_argv(self):
        root = PurePosixPath('/corpus repository')
        audio, source = root / 'audio/book.wav', root / 'source/book.txt'
        output, summary = root / 'out/data.zip', root / 'out/summary.json'
        for fallback in (None, 'fallback.gguf'):
            with self.subTest(fallback=fallback), patch.object(sys, 'platform', 'linux'), \
                    patch.object(manifest, '_source_probe_shell') as resolve:
                command = self.report.get_worker_command(root, audio, source, output, summary, 'model.gguf', fallback)
                resolve.assert_not_called()
                expected = [str(root / 'run_with_restart.sh'), '--audio', str(audio), '--source', str(source),
                    '--model', 'model.gguf', '--output', str(output), '--summary-output', str(summary),
                    '--chunk-size', '10.0', '--lang', 'en']
                if fallback:
                    expected += ['--fallback-model', fallback]
                self.assertEqual(expected, command)

    def test_missing_windows_bash_is_recorded_as_failure_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, source = root / 'book.wav', root / 'book.txt'
            audio.write_bytes(b'fixture audio')
            source.write_text('fixture source')
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps([{'audio': str(audio), 'source': str(source), 'output_name': 'data.zip'}]))
            output = root / 'output'
            output.mkdir()
            stderr = io.StringIO()
            with patch.object(sys, 'platform', 'win32'), \
                    patch.object(manifest, '_source_probe_shell', return_value=(None, 'no usable bash found')), \
                    patch.object(self.report.subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as run, \
                    contextlib.redirect_stderr(stderr):
                status = self.report.run_corpus(root, pairs, output, 'model.gguf', None)
            self.assertEqual(1, status)
            run.assert_not_called()
            index = json.loads((output / 'corpus_attempts.json').read_text())
            self.assertIn('Bash is required to launch the corpus worker on Windows: no usable bash found',
                          index['rows'][0]['status'])
            self.assertNotIn('worker_exit', index['rows'][0])
            report = (output / 'aggregated_report.md').read_text()
            self.assertIn('Bash is required', report)
            self.assertIn('1 pair(s) failed; 0 completed; 1 attempted.', report)
            self.assertIn('Bash is required', stderr.getvalue())


def write_python_launcher(path, source):
    write_python_bash_fixture(path, source)
    path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))


def copy_portable_report_driver(root):
    copy_report_driver(root)
    package = root / 'app/experiments'
    package.mkdir(parents=True, exist_ok=True)
    for name in ('__init__.py', 'manifest.py'):
        (package / name).write_bytes((ROOT / 'app/experiments' / name).read_bytes())


def write_interpreter(root):
    python = root / 'app/env/bin/python'
    python.parent.mkdir(parents=True)
    write_python_launcher(python, 'import subprocess, sys\nsys.exit(subprocess.call([sys.executable, *sys.argv[1:]]))\n')


def create_dispatch_repo(base):
    root = base / 'repository'
    root.mkdir()
    for name in ('build_test_corpus.sh', 'alexandria_batch_processor.py'):
        (root / name).write_bytes((ROOT / name).read_bytes())
    copy_portable_report_driver(root)
    write_interpreter(root)
    for directory in ('audio', 'source', 'logs'):
        (root / directory).mkdir()
    (root / 'source/book.txt').write_text('source content')
    for suffix in ('.wav', '.mp3'):
        (root / 'audio' / ('book' + suffix)).write_bytes(('input' + suffix).encode())
    write_python_launcher(root / 'run_with_restart.sh', WORKER)
    return root


def invoke(root, output, mode, cwd):
    return subprocess.run([get_bash_executable(), (root / 'build_test_corpus.sh').as_posix(), mode], cwd=cwd,
        env=dict(os.environ, AUDIO_DIR=(root / 'audio').as_posix(),
                 SOURCE_DIR=(root / 'source').as_posix(), OUT_DIR=output.as_posix()),
        capture_output=True, text=True, timeout=15)


class CorpusReportLabelTests(unittest.TestCase):
    def test_aggregate_keeps_complete_timestamp_and_distinct_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = create_dispatch_repo(base)
            output = root / 'output'
            original = {}
            for stamp in ('20260929_120000', '20270929_120000'):
                path = root / 'logs' / ('alexandria_preparer_' + stamp + '.log')
                path.write_text('Annotation complete: 999 total segments (999 new this run)\nTotal audio in dataset : 999.0s\n')
                original[path] = path.read_bytes()
            identities = []
            for _ in range(2):
                result = invoke(root, output, '--run', base)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                index = json.loads((output / 'corpus_attempts.json').read_text())
                identities.append(index['run_id'])
                self.assertRegex(index['run_id'], r'^\d{8}_\d{6}-.+')
            self.assertEqual(2, len(set(identities)))
            for identity in identities:
                self.assertTrue((output / 'annotation_reports' / identity / 'attempts.json').is_file())
            result = invoke(root, output, '--aggregate', base)
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
            copy_portable_report_driver(root)
            write_interpreter(root)
            logs = root / "logs"
            logs.mkdir()
            log = logs / "alexandria_preparer_20260930_032000.log"
            log.write_text("Annotation complete: 7 total segments (7 new this run)\n"
                           "Total audio in dataset : 21.0s\n")
            original = log.read_bytes()
            env = {key:value for key,value in os.environ.items()
                   if key not in ("AUDIO_DIR", "SOURCE_DIR")}
            output = root / "output"
            env["OUT_DIR"] = output.as_posix()
            for mode in ("--help", "-h", None, "--aggregate"):
                with self.subTest(mode=mode):
                    args = [get_bash_executable(), (root / "build_test_corpus.sh").as_posix()]
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
                for audio in (None, Path(tmp, "audio").as_posix()):
                    with self.subTest(mode=mode, audio=audio):
                        case_env = dict(env, OUT_DIR=Path(tmp, "output").as_posix())
                        if audio is not None:
                            case_env["AUDIO_DIR"] = audio
                        result = subprocess.run([get_bash_executable(), (ROOT / "build_test_corpus.sh").as_posix(), mode],
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
                copy_portable_report_driver(root)
                write_interpreter(root)
                for name in ("audio", "source", "logs"):
                    (root / name).mkdir()
                for stem in ("alpha", "beta", "gamma"):
                    (root / "audio" / (stem + ".wav")).write_bytes(b"CPU dispatch fixture")
                    (root / "source" / (stem + ".txt")).write_text(stem)
                original_sources = {p:p.read_bytes() for name in ("audio", "source")
                                    for p in (root / name).iterdir()}
                wrapper = root / "run_with_restart.sh"
                write_python_launcher(wrapper, WORKER)
                output = root / "output"
                env = dict(os.environ, AUDIO_DIR=(root / "audio").as_posix(),
                           SOURCE_DIR=(root / "source").as_posix(), OUT_DIR=output.as_posix(),
                           FAILED_PAIRS=",".join(failed))
                result = subprocess.run([get_bash_executable(), (root / "build_test_corpus.sh").as_posix(), "--run"],
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
            write_interpreter(root)
            copy_portable_report_driver(root)
            (root / "logs").mkdir()
            env = dict(os.environ, OUT_DIR=(root / "output").as_posix())
            result = subprocess.run([get_bash_executable(), (root / "build_test_corpus.sh").as_posix(), "--aggregate"],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("No owned corpus attempt index", result.stderr)
            self.assertNotIn("Done. Report", result.stdout)
            self.assertFalse((root / "output/aggregated_report.md").exists())
