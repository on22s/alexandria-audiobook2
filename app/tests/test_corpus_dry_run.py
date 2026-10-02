"""Actual shell dry-runs must dispatch ASR, validate reports and expose failures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from tests import test_corpus_dispatch_identity as fixture

ROOT = Path(__file__).resolve().parents[2]


class CorpusDryRunTests(unittest.TestCase):
    def create_repo(self, base):
        root = fixture.CorpusDispatchIdentityTests.create_repo(self, base, "repo's path")
        for name in ('corpus_alignment_prescan.py', 'alexandria_run_manifest.py'):
            (root / name).write_bytes((ROOT / name).read_bytes())
        (root / 'source/book.txt').write_text('matched source content')
        wrapper = root / 'run_with_restart.sh'
        wrapper.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
from alexandria_run_manifest import get_file_identity, write_json_atomic
args=sys.argv[1:]
root=Path(__file__).parent
with (root/'dispatches.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
def value(name):return args[args.index(name)+1]
assert value('--phase')=='asr'
assert '--model' not in args and '--fallback-model' not in args
output=Path(value('--output'))
audio,source=Path(value('--audio')),Path(value('--source'))
report=Path(value('--alignment-report'))
mode=os.environ.get('PRESCAN_FIXTURE_MODE','valid')
if audio.suffix=='.mp3':
    if mode=='failed':sys.exit(7)
    if mode=='missing':sys.exit(0)
quality={'average_ratio':0.93,'sampled':30,'below_60_percent':1,'review_needed':2}
identity={'audio':get_file_identity(audio),'source':get_file_identity(source),
          'options':{'alignment_report':str(report),'output':str(output),'chunk_size':10.0,'lang':'en','limit':int(value('--limit'))}}
if audio.suffix=='.mp3':
    if mode=='stale':identity['audio']['sha256']='0'*64
    if mode=='wrong-settings':identity['options']['alignment_report']='another invocation'
    if mode=='invalid':quality['sampled']=True
    if mode=='undersampled':quality['sampled']=29
    if mode=='nan':quality['average_ratio']=float('nan')
write_json_atomic({'version':True if mode=='bad-version' and audio.suffix=='.mp3' else 1,
                  'scope':'initial_provisional_chunks','identity':identity,'quality':quality},report)
''')
        wrapper.chmod(0o755)
        return root

    def invoke(self, root, output, base, mode='valid'):
        from unittest.mock import patch
        with patch.dict(os.environ, PRESCAN_FIXTURE_MODE=mode):
            return fixture.CorpusDispatchIdentityTests.invoke(self, root, output, '--dry-run', base)

    def test_actual_shell_measures_both_pairs_without_dataset_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.create_repo(base)
            output = root / "output's path"
            result = self.invoke(root, output, base)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue((root / 'dispatches.jsonl').exists(), 'dry-run must actually dispatch ASR')
            commands = [json.loads(line) for line in (root / 'dispatches.jsonl').read_text().splitlines()]
            self.assertEqual(2, len(commands))
            report_paths = [args[args.index('--alignment-report')+1] for args in commands]
            self.assertEqual(2, len(set(report_paths)))
            data = json.loads((output / 'dry_run_report.json').read_text())
            self.assertEqual(['measured', 'measured'], [row['status'] for row in data['rows']])
            self.assertEqual([30, 30], [row['quality']['sampled'] for row in data['rows']])
            text = (output / 'dry_run_report.md').read_text()
            self.assertIn('book.mp3', text)
            self.assertIn('book.wav', text)
            self.assertIn('0.930 | 30 | 1 | 2', text)
            self.assertIn('do not measure whole-book alignment', text)
            self.assertFalse(list(output.glob('*.zip')))
            self.assertFalse((output / 'aggregated_report.md').exists())
            self.assertEqual('matched source content', (root / 'source/book.txt').read_text())
            self.assertEqual({b'input.wav', b'input.mp3'}, {p.read_bytes() for p in (root / 'audio').iterdir()})

    def test_failed_missing_stale_and_invalid_reports_fail_but_attempt_independent_pairs(self):
        for mode in ('failed', 'missing', 'stale', 'wrong-settings', 'invalid', 'nan', 'bad-version', 'undersampled'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root = self.create_repo(base)
                output = root / 'output'
                result = self.invoke(root, output, base, mode)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertTrue((root / 'dispatches.jsonl').exists())
                self.assertEqual(2, len((root / 'dispatches.jsonl').read_text().splitlines()))
                data = json.loads((output / 'dry_run_report.json').read_text())
                failed = next(row for row in data['rows'] if row['audio'].endswith('.mp3'))
                measured = next(row for row in data['rows'] if row['audio'].endswith('.wav'))
                self.assertTrue(failed['status'].startswith('failed: '))
                self.assertNotIn('quality', failed)
                self.assertEqual('measured', measured['status'])
                self.assertIn('failed:', (output / 'dry_run_report.md').read_text())

    def test_prior_reports_cannot_turn_missing_new_report_into_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.create_repo(base)
            output = root / 'output'
            result = self.invoke(root, output, base)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue((output / 'dry_run_report.json').exists())
            original_reports = {p: p.read_bytes() for p in (output / 'alignment_reports').rglob('*.json')}
            result = self.invoke(root, output, base, 'missing')
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            data = json.loads((output / 'dry_run_report.json').read_text())
            self.assertEqual(1, sum('quality' in row for row in data['rows']))
            for path, before in original_reports.items():
                self.assertEqual(before, path.read_bytes())

    def test_no_matched_pairs_refuse_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.create_repo(base)
            (root / 'source/book.txt').unlink()
            result = self.invoke(root, root / 'output', base)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertFalse((root / 'dispatches.jsonl').exists())
            data = json.loads((root / 'output/dry_run_report.json').read_text())
            self.assertTrue(all(row['status']=='no source match' for row in data['rows']))
