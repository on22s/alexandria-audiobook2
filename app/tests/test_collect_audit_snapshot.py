"""Result classifications and seeds come from one native audit snapshot."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tools.audit.collect_results as collector
import tools.audit.audit_experiment_artifacts as membership
import experiments.replay_artifact as replay


class CollectorSnapshotTests(unittest.TestCase):
    def fixture(self, root):
        audit=root/'ab_test_runtime/audit';audit.mkdir(parents=True)
        experiments=root/'ab_test_runtime/experiments';experiments.mkdir()
        paths=[];records=[]
        for index,family in enumerate(('zeta','alpha','zeta')):
            name=f'fixture{index}.json';path=experiments/name
            path.write_text(json.dumps({'meta':{'experiment':family,'model':'fixture'},'rows':[
                {'id':'book_b:1','arm':'base','correct':True},
                {'id':'book_a:1','arm':'base','correct':False},
                {'id':'book_a:2','arm':'lora','correct':True}]}))
            paths.append(str(path));records.append({'artifact':name,'classification':'supported_structure','seed':index+7})
        (audit/'artifact_structural_audit.json').write_text(json.dumps({'artifacts':records}))
        (audit/'legacy_attribution_audit.json').write_text(json.dumps({'artifacts':[
            {'artifact':'fixture1.json','classification':'legacy_special'}]}))
        return audit,experiments,paths

    def run_collector(self, root, audit, experiments, paths):
        with patch.object(collector,'REPO',str(root)),patch.object(collector,'E',str(experiments)),patch.object(collector,'AUDIT',str(audit)),patch.object(membership,'indexable_artifacts',return_value=(paths,[])),patch.object(replay,'resolve_producer',return_value={'script':'fixture.py','tier':'provenance','argv':['fixture.py'],'commit':'abc12345'}),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0,collector.main([]))
        with (root/'results_index.csv').open() as handle:return list(csv.DictReader(handle))

    def test_audit_replacement_after_close_cannot_mix_status_and_seed_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);audit,experiments,paths=self.fixture(root)
            structural=audit/'artifact_structural_audit.json';native_open=open;reads=[]
            class Snapshot:
                def __init__(self,handle):self.handle=handle
                def read(self):return self.handle.read()
                def __enter__(self):return self.handle
                def __exit__(self,*args):
                    self.handle.close()
                    with native_open(structural,'w') as handle:json.dump({'artifacts':[
                        {'artifact':f'fixture{i}.json','classification':'changed','seed':99} for i in range(3)]},handle)
            def opening(path,*args,**kwargs):
                handle=native_open(path,*args,**kwargs)
                if Path(path)==structural:
                    reads.append(str(path));return Snapshot(handle)
                return handle
            with patch.object(collector,'open',opening,create=True):rows=self.run_collector(root,audit,experiments,paths)
            self.assertEqual(1,len(reads))
            self.assertEqual({'7','8','9'},{r['seed'] for r in rows})
            self.assertEqual({'supported_structure','legacy_special'},{r['evidence_status'] for r in rows})
            self.assertEqual('changed',json.loads(structural.read_text())['artifacts'][0]['classification'])

    def test_native_report_preserves_group_order_scored_rows_and_legacy_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);audit,experiments,paths=self.fixture(root)
            rows=self.run_collector(root,audit,experiments,paths)
            self.assertEqual(9,len(rows));self.assertEqual({'7','8','9'},{r['seed'] for r in rows})
            self.assertTrue(all(r['evidence_status']=='legacy_special' for r in rows if r['artifact']=='fixture1.json'))
            md=(root/'RESULTS_INDEX.md').read_text()
            self.assertLess(md.index('## alpha'),md.index('## zeta'))
            self.assertEqual(2,md.count('## '));self.assertEqual(9,md.count('fixture |'))
