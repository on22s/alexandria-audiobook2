"""Native gold build/merge CLI artifacts must carry complete name evidence and provenance."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(os.environ.get('GOLD_MERGE_SOURCE', Path(__file__).resolve().parent.parent / 'gold_set_builder.py'))


class GoldMergeEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / 'fixture.json'
        self.output.write_bytes(b'previous fixture')
        self.batch = self.root / 'batch.json'
        self.filled = self.root / 'filled.json'
        self.source = self.root / 'source.txt'
        self.row = {'id':'book-00001','entry_index':1,'line':'Yes, I agree.',
                    'passage_before':'Emilia waved.', 'passage_after':'The door shut.'}
        self.batch.write_text(json.dumps({'book':'book','rows':[self.row]}))
        self.write_answer('EMILIA INVENTED')
        self.source.write_text('Emilia waved. Roxy Migurdia arrived elsewhere.')

    def write_answer(self, answer):
        self.filled.write_text(json.dumps({'rows':[{'id':self.row['id'], 'ANSWER':answer, 'reasoning':'Evidence.'}]}))

    def run_merge(self, *extra):
        return subprocess.run([sys.executable,str(SCRIPT),'merge','book',str(self.filled),
            '--batches',str(self.batch),'--judged-by','human','--out',str(self.output),*extra],
            text=True,capture_output=True,timeout=10)

    def test_full_multiword_name_is_required_and_rejection_preserves_prior_artifact(self):
        original = [p.read_bytes() for p in (self.batch,self.filled,self.source)]
        result = self.run_merge()
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertIn('invented',result.stdout)
        self.assertEqual(b'previous fixture',self.output.read_bytes())
        self.assertEqual(original,[p.read_bytes() for p in (self.batch,self.filled,self.source)])
        self.batch.write_text(json.dumps({'book':'book','rows':[{**self.row,'passage_before':'Emilia\n  Invented waved.'}]}))
        result = self.run_merge()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual('EMILIA INVENTED',json.loads(self.output.read_text())['entries'][0]['expected_speaker'])

    def test_explicit_source_accepts_whole_book_name_and_preserves_custom_run(self):
        self.write_answer('ROXY MIGURDIA')
        original = [p.read_bytes() for p in (self.batch,self.filled,self.source)]
        result = self.run_merge('--source',str(self.source),'--run','experiment-B')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        fixture = json.loads(self.output.read_text())
        self.assertEqual('experiment-B',fixture['source_run'])
        self.assertEqual('ROXY MIGURDIA',fixture['entries'][0]['expected_speaker'])
        self.assertEqual(original,[p.read_bytes() for p in (self.batch,self.filled,self.source)])
        self.write_answer('ROXY INVENTED')
        before = self.output.read_bytes()
        result = self.run_merge('--source',str(self.source),'--run','experiment-B')
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertEqual(before,self.output.read_bytes())

    def test_custom_build_run_is_inferred_at_merge_and_root_provides_book_evidence(self):
        run_dir = self.root / 'experiment-B' / 'book'
        run_dir.mkdir(parents=True)
        (run_dir/'result.json.threepass_checkpoint.json').write_text(json.dumps({
            'segmented':[{'type':'SPOKEN','text':self.row['line']}],
            'named':[{'speaker':'ROXY MIGURDIA','text':self.row['line']}]}))
        inputs = self.root/'inputs'
        inputs.mkdir()
        (inputs/'book.txt').write_text(self.source.read_text())
        output_dir = self.root/'batches'
        result = subprocess.run([sys.executable,str(SCRIPT),'build','book','--root',str(self.root),
            '--run','experiment-B','--out',str(output_dir),'--count','1'],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.batch = next(output_dir.glob('*.json'))
        batch = json.loads(self.batch.read_text())
        self.assertEqual('experiment-B',batch['source_run'])
        self.row = batch['rows'][0]
        self.write_answer('ROXY MIGURDIA')
        result = self.run_merge('--root',str(self.root))
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual('experiment-B',json.loads(self.output.read_text())['source_run'])

    def test_run_conflicts_and_unreadable_explicit_source_do_not_publish(self):
        for extra,metadata in [(['--run','other'],{'source_run':'experiment-B'}),
                              (['--source',str(self.root/'missing.txt')],{})]:
            with self.subTest(extra=extra):
                self.batch.write_text(json.dumps({'book':'book','rows':[self.row],**metadata}))
                result = self.run_merge(*extra)
                self.assertEqual(2,result.returncode,result.stdout+result.stderr)
                self.assertEqual(b'previous fixture',self.output.read_bytes())
        other = self.root/'other.json'
        other.write_text(json.dumps({'book':'book','source_run':'other','rows':[]}))
        self.batch.write_text(json.dumps({'book':'book','source_run':'experiment-B','rows':[self.row]}))
        result = subprocess.run([sys.executable,str(SCRIPT),'merge','book',str(self.filled),
            '--batches',str(self.batch),str(other),'--judged-by','human','--out',str(self.output)],
            text=True,capture_output=True,timeout=10)
        self.assertEqual(2,result.returncode,result.stdout+result.stderr)
        self.assertEqual(b'previous fixture',self.output.read_bytes())
