"""Native Bash archived runner with independent CPU worker exit statuses."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('ARCHIVED_CORPUS_SOURCE', ROOT / 'run_chains/archive/run_random_corpus.sh'))


class ArchivedCorpusFailureTests(unittest.TestCase):
    def run_case(self, codes):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'run_chains/archive'
            archive.mkdir(parents=True)
            shutil.copytree(ROOT / 'run_chains/lib', archive.parent / 'lib')
            chain = archive / 'run_random_corpus.sh'
            shutil.copyfile(SOURCE, chain)
            worker = archive / 'run_with_restart.sh'
            worker.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\n'
                'p=Path(os.environ["CPU_DISPATCHES"])\n'
                'count=len(p.read_text().splitlines()) if p.exists() else 0\n'
                'with p.open("a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                'sys.exit(json.loads(os.environ["CPU_EXIT_CODES"])[count])\n')
            worker.chmod(0o755)
            env = {**os.environ, 'HF_TOKEN': 'cpu-fixture', 'AUDIO_DIR': str(root / 'audio with spaces'),
                   'SOURCE_DIR': str(root / 'sources with spaces'), 'CPU_DISPATCHES': str(root / 'dispatch.jsonl'),
                   'CPU_EXIT_CODES': json.dumps(codes)}
            result = subprocess.run(['bash', str(chain)], env=env, capture_output=True, text=True, timeout=10)
            dispatches = [json.loads(row) for row in (root / 'dispatch.jsonl').read_text().splitlines()]
            return result, dispatches

    def test_failed_workers_still_attempt_later_pairs_and_cannot_print_success(self):
        for codes in ([1, 0, 0], [0, 2, 0], [1, 2, 7]):
            with self.subTest(codes=codes):
                result, dispatches = self.run_case(codes)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertEqual(3, len(dispatches))
                self.assertNotIn('Random corpus test complete', result.stdout)
                self.assertIn('random_corpus FAILED', result.stdout)
                for index, code in enumerate(codes, 1):
                    if code:
                        self.assertIn(f'corpus_{index} = failed:{code}', result.stdout)
                self.assertTrue(all('--limit' in args and args[args.index('--limit') + 1] == '5'
                                    for args in dispatches))

    def test_success_requires_all_three_workers(self):
        result, dispatches = self.run_case([0, 0, 0])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(3, len(dispatches))
        self.assertIn('3/3 stages ok', result.stdout)
        self.assertIn('Random corpus test complete', result.stdout)

    def test_interrupt_preserves_130_and_does_not_dispatch_more_pairs(self):
        result, dispatches = self.run_case([130, 0, 0])
        self.assertEqual(130, result.returncode)
        self.assertEqual(1, len(dispatches))
        self.assertNotIn('Random corpus test complete', result.stdout)
