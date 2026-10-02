"""Reject forged completion markers and prove actual required-stage outcomes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from experiments.respelling_completion import get_respelling_completion_error
from tests import test_chain_measurement_scope as measurement


class RespellingCompletionTests(unittest.TestCase):
    def test_structural_counts_unique_terms_and_requested_identity_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'result.json'
            good={'status':'complete','candidates_considered':2,'results':[{'term':'Alice'},{'term':'Bob','skipped':'unmappable kana'}]}
            cases=[(good,None),({k:v for k,v in good.items() if k!='status'},None),
                (dict(good,status='partial'),'not complete'),(dict(good,candidates_considered=True),'positive count'),
                (dict(good,results=[{'term':'Alice'}]),'row count'),(dict(good,candidates_considered=1,results=[{'term':'Alice'}]),'requested term count'),
                (dict(good,results=[{'term':'Alice'},{'term':'Alice'}]),'duplicate'),
                (dict(good,results=[{'term':'Alice'},{}]),'nonempty term'),
                (dict(good,run_identity={'limit':3}),'identity'),(dict(good,run_identity={'limit':True}),'identity'),
                (dict(good,run_identity={'limit':2}),None)]
            for document,error in cases:
                with self.subTest(document=document):
                    path.write_text(json.dumps(document));before=path.read_bytes()
                    actual=get_respelling_completion_error(path,2)
                    self.assertEqual(None,actual) if error is None else self.assertIn(error,actual)
                    self.assertEqual(before,path.read_bytes())
            for raw in ('[]','null','{broken'):
                path.write_text(raw);self.assertIsNotNone(get_respelling_completion_error(path,2))
            path.unlink();self.assertIsNotNone(get_respelling_completion_error(path,2))


class RequiredSeparatorStageTests(unittest.TestCase):
    setUp=measurement.ChainMeasurementScopeTests.setUp
    _write=measurement.ChainMeasurementScopeTests._write
    _run=measurement.ChainMeasurementScopeTests._run

    def test_invalid_required_dot_arm_fails_summary_but_independent_cpu_stages_continue(self):
        import os
        original=os.environ.get('ANCHOR_CHAIN_SOURCE')
        if original:(self.root/'run_chains/anchor_and_separator_table_20260826.sh').write_text(Path(original).read_text())
        runtime=self.root/'ab_test_runtime'
        cases=[None,{'status':'partial','candidates_considered':1600,'results':[{'term':'x'}]},
            {'status':'complete','candidates_considered':1600,'results':[{'term':'term'+str(i)} for i in range(487)]},
            {'status':'complete','candidates_considered':487,'results':[{'term':'term'+str(i)} for i in range(487)]},
            {'status':'complete','candidates_considered':1600,'results':[{'term':'same'}]*1600}]
        dot=runtime/'experiments/respelling_dot_allrows_n1600.json'
        for document in cases:
            with self.subTest(document_shape='missing' if document is None else (document['status'],document['candidates_considered'],len(document['results']))):
                if document is None:
                    if dot.exists():dot.unlink()
                else:dot.write_text(json.dumps(document))
                result=self._run('anchor_and_separator_table_20260826.sh',expected_status=1)
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertNotIn('START pauses_four_arms',result.stdout)
                self.assertIn('pauses_four_arms = failed:1',result.stdout)
                self.assertIn('OK    indexes',result.stdout)
                self.assertFalse((runtime/'experiments/respelling_pauses_allrows_4arm.json').exists())


class SiblingGuardTests(unittest.TestCase):
    def test_actual_cached_guard_calls_bind_each_chains_requested_count(self):
        import os
        import shlex
        import re
        root=Path(__file__).resolve().parents[2]
        for name,count in (('e_row_arms_20260817.sh',400),('e_row_replication_20260818.sh',1200),('separator_arms_20260818.sh',120)):
            source_path=Path('/tmp/respelling_before_'+name) if os.environ.get('RESPELLING_OLD_GUARDS') else root/'run_chains'/name
            source=source_path.read_text();a=source.index('artifact_complete() {');b=source.index('\n}',a)+2
            call=re.search(r'artifact_complete "\$python" "\$out"[^;\n]*',source).group()
            with self.subTest(chain=name),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'result.json'
                cases=[(count,count,0),(count-1,count-1,1),(count,count-1,1)]
                if name=='e_row_replication_20260818.sh':cases.append((800,800,1))
                for actual,rows,expected in cases:
                    path.write_text(json.dumps({'status':'complete','candidates_considered':actual,'results':[{'term':'term'+str(i)} for i in range(rows)]}))
                    body='set -uo pipefail\nREPO='+shlex.quote(str(root))+'\npython='+shlex.quote(sys.executable)+'\nout='+shlex.quote(str(path))+'\nLIMIT='+str(count)+'\nlimit='+str(count)+'\n'+source[a:b]+'\n'+call+'\n'
                    result=subprocess.run(['bash','-c',body],capture_output=True,text=True,timeout=5)
                    self.assertEqual(expected,result.returncode,result.stdout+result.stderr)
