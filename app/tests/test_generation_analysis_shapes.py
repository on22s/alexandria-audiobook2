"""Malformed telemetry is excluded visibly, never counted as plausible recall."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import analyze_generation_attempts as analyzer


class GenerationAnalysisShapeTests(unittest.TestCase):
    def test_native_cli_reports_valid_known_counts_and_skips_each_bad_file(self):
        valid={'chunks':[{'chunk_number':1,'attempts':[{'outcome':'quality_rejected','quality_metrics':{'source_token_recall':.4}}, {'outcome':'accepted'}]}]}
        cases=[({'chunks':[None]},'chunks[0]'),({'chunks':[{'attempts':'invalid'}]},'attempts'),({'chunks':[{'attempts':[None]}]},'attempts[0]'),({'chunks':[{'attempts':[{'quality_metrics':'bad'}]}]},'quality_metrics'),({'chunks':[],'status':'failed','failed_chunk_attempts':[None]},'failed_chunk_attempts[0]')]
        for recall in ('0.4',True,float('nan'),float('inf'),-.1,1.1):
            data=copy.deepcopy(valid);data['chunks'][0]['attempts'][0]['quality_metrics']['source_token_recall']=recall
            cases.append((data,'source_token_recall'))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'valid.generation_quality.json').write_text(json.dumps(valid))
            for i,(data,_) in enumerate(cases):
                (root/f'bad{i}.generation_quality.json').write_text(json.dumps(data))
            before={p.name:p.read_bytes() for p in root.iterdir()}
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(0,analyzer.main(['--scripts-dir',tmp,'--json']))
            report=json.loads(output.getvalue())
            self.assertEqual(1,report['manifest_count'])
            self.assertEqual(1,report['recall_histogram']['0.30-0.60'])
            self.assertEqual(1,sum(report['recall_histogram'].values()))
            self.assertEqual({'rejected':1,'recovered':1},report['recovery_by_band']['0.30-0.60'])
            self.assertEqual(len(cases),len(report['warnings']))
            for i,(_,path) in enumerate(cases):
                warning=next(w for w in report['warnings'] if f'bad{i}.generation_quality.json:' in w)
                self.assertIn(path,warning)
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})

    def test_public_aggregations_refuse_bad_attempts_with_concrete_value_error(self):
        records=[{'accepted':True,'adaptively_split':False,'attempts':[None]}]
        for func in (analyzer.recall_histogram,analyzer.recovery_by_band,analyzer.attempt_count_distribution,analyzer.split_outcomes):
            with self.subTest(function=func.__name__),self.assertRaisesRegex(ValueError,r'records\[0\].attempts\[0\]'):
                func(records)
        with self.assertRaisesRegex(ValueError,r'failed_chunk_attempts\[0\]'):
            analyzer.extract_chunk_records({'chunks':[],'status':'failed','failed_chunk_attempts':[None]})

    def test_nonfinite_or_wrong_type_recall_never_enters_a_band(self):
        for value in (float('nan'),float('inf'),-float('inf'),True,'0.4',None,-.01,1.01,10**1000):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'finite number'):
                analyzer.recall_band(value)
        self.assertEqual('<0.30',analyzer.recall_band(0))
        self.assertEqual('>=0.90',analyzer.recall_band(1))

    def test_missing_optional_legacy_metrics_are_preserved_without_inventing_recall(self):
        manifest={'chunks':[{'attempts':None},{'attempts':[{'outcome':'quality_rejected','quality_metrics':None}]}]}
        original=copy.deepcopy(manifest)
        records=analyzer.extract_chunk_records(manifest)
        self.assertEqual(0,sum(analyzer.recall_histogram(records).values()))
        self.assertEqual(original,manifest)
