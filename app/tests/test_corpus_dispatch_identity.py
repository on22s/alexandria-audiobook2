"""Run the actual corpus shell with controlled CPU preparer dispatch."""
import os
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from tests.corpus_fixture_support import copy_report_driver, WORKER

ROOT = Path(__file__).resolve().parents[2]


class CorpusDispatchIdentityTests(unittest.TestCase):
    def create_repo(self, base, name='repository'):
        root = base / name
        root.mkdir()
        subject = Path(os.environ.get('CORPUS_PATHS_BASELINE_FILE', str(ROOT / 'build_test_corpus.sh')))
        (root / 'build_test_corpus.sh').write_bytes(subject.read_bytes())
        (root / 'alexandria_batch_processor.py').write_bytes((ROOT / 'alexandria_batch_processor.py').read_bytes())
        copy_report_driver(root)
        python = root / 'app/env/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        for directory in ('audio', 'source', 'logs'):
            (root / directory).mkdir()
        (root / 'source/book.txt').write_text('source content')
        for suffix in ('.wav', '.mp3'):
            (root / 'audio' / ('book' + suffix)).write_bytes(('input' + suffix).encode())
        wrapper = root / 'run_with_restart.sh'
        wrapper.write_text('#!' + sys.executable + '\n' + WORKER)
        wrapper.chmod(0o755)
        return root

    def invoke(self, root, output, mode, cwd):
        return subprocess.run(['bash', str(root / 'build_test_corpus.sh'), mode], cwd=cwd,
            env=dict(os.environ, AUDIO_DIR=str(root / 'audio'), SOURCE_DIR=str(root / 'source'), OUT_DIR=str(output)),
            capture_output=True, text=True, timeout=15)

    def assert_distinct_outputs(self, root, output):
        self.assertTrue((root / 'dispatches.jsonl').exists(), 'matched pairs must be dispatched')
        records = [json.loads(line) for line in (root / 'dispatches.jsonl').read_text().splitlines()]
        self.assertEqual(2, len(records))
        outputs = [Path(args[args.index('--output')+1]) for args in records]
        self.assertEqual(2, len(set(outputs)), 'same-stem inputs must not overwrite the same output')
        for args, path in zip(records, outputs):
            audio = Path(args[args.index('--audio')+1])
            self.assertEqual(output, path.parent)
            self.assertEqual(b'dataset from '+audio.name.encode(), path.read_bytes())
            self.assertEqual(root / 'source/book.txt', Path(args[args.index('--source')+1]))
        self.assertEqual({b'input.wav', b'input.mp3'}, {p.read_bytes() for p in (root / 'audio').iterdir()})
        self.assertEqual('source content', (root / 'source/book.txt').read_text())
        return outputs

    def test_same_stem_audio_dispatches_distinct_datasets_and_repeats_stably(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.create_repo(base)
            output = root / 'output'
            result = self.invoke(root, output, '--run', base)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            first = self.assert_distinct_outputs(root, output)
            (root / 'dispatches.jsonl').unlink()
            result = self.invoke(root, output, '--run', base)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(first, self.assert_distinct_outputs(root, output))

    def test_apostrophes_in_repository_and_output_survive_plan_run_and_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.create_repo(base, "repo's literal $() [path]")
            output = root / "output's literal $() [path]"
            for mode in ('--plan', '--run', '--aggregate'):
                with self.subTest(mode=mode):
                    result = self.invoke(root, output, mode, base)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertNotIn('SyntaxError', result.stderr)
            self.assert_distinct_outputs(root, output)
            pairs = json.loads((output / 'pairs.json').read_text())
            self.assertEqual({str(p) for p in (root / 'audio').iterdir()}, {p['audio'] for p in pairs})
            self.assertIn('Total segments emitted: **6**', (output / 'aggregated_report.md').read_text())

    def test_python_source_in_repository_path_is_inert_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            payload = "repo');__import__('pathlib').Path('EXECUTED').write_text('owned');#"
            root = self.create_repo(base, payload)
            (root / 'logs/alexandria_preparer_fixture.log').write_text('Annotation complete: 3 total segments (3 new this run)\n')
            output = root / 'output'
            result = self.invoke(root, output, '--run', base)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            result = self.invoke(root, output, '--aggregate', base)
            self.assertFalse((base / 'EXECUTED').exists(), 'paths must not execute Python source')
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn('Total segments emitted: **6**', (output / 'aggregated_report.md').read_text())
