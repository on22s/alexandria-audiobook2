"""Corpus reports require bounded ASR and the requested full sample count."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from tests import test_corpus_dry_run

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('corpus_prefix_subject', ROOT / 'corpus_alignment_prescan.py')
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)

class CorpusPrefixTests(unittest.TestCase):
    def test_real_shell_dispatch_bounds_each_asr_request(self):
        helper = test_corpus_dry_run.CorpusDryRunTests()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = helper.create_repo(base)
            helper.invoke(root, root / 'output', base)
            commands = [json.loads(line) for line in (root / 'dispatches.jsonl').read_text().splitlines()]
            self.assertEqual(2, len(commands))
            for args in commands:
                self.assertIn('--limit', args)
                self.assertEqual('30', args[args.index('--limit') + 1])

    def test_receipt_refuses_short_samples_and_unbounded_identity(self):
        from alexandria_run_manifest import get_file_identity
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, source, output, report = [root / name for name in ('audio.wav', 'source.txt', 'output', 'report.json')]
            audio.write_bytes(b'audio fixture')
            source.write_text('source fixture')
            document = {'version': 1, 'scope': 'initial_provisional_chunks',
                'identity': {'audio': get_file_identity(audio), 'source': get_file_identity(source),
                    'options': {'alignment_report': str(report), 'output': str(output), 'chunk_size': 10.0, 'lang': 'en', 'limit': 30}},
                'quality': {'sampled': 30, 'average_ratio': .9, 'below_60_percent': 0, 'review_needed': 0}}
            report.write_text(json.dumps(document))
            self.assertEqual(30, corpus.get_alignment_report_quality(report, audio, source, output)['sampled'])
            for count in (1, 12, 29, 31):
                document['quality']['sampled'] = count
                report.write_text(json.dumps(document))
                with self.subTest(count=count), self.assertRaisesRegex(ValueError, '30'):
                    corpus.get_alignment_report_quality(report, audio, source, output)
            document['quality']['sampled'] = 30
            for limit in (None, 29, True, False, 30.0):
                document['identity']['options']['limit'] = limit
                report.write_text(json.dumps(document))
                with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'settings'):
                    corpus.get_alignment_report_quality(report, audio, source, output)
