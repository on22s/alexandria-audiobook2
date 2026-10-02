"""Native Git history batching preserves individual artifact origins and CSV bytes."""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments import replay_artifact as replay
from tools.audit import collect_results as collector
from tools.audit import audit_experiment_artifacts as membership
from tests import test_added_commit_determinism as topology


def git(root,*args):
    return subprocess.run(['git',*args],cwd=root,capture_output=True,text=True,check=True).stdout.strip()


if os.environ.get("COLLECTOR_BATCH_BASELINE"):
    spec=importlib.util.spec_from_file_location('collector_baseline',os.environ['COLLECTOR_BATCH_BASELINE'])
    collector=importlib.util.module_from_spec(spec);spec.loader.exec_module(collector)


class ProducerResolutionBatchTests(unittest.TestCase):
    def fixture(self,root,count=120):
        git(root,'init','-q');git(root,'config','user.name','fixture');git(root,'config','user.email','fixture@example.invalid')
        experiments=root/'ab_test_runtime/experiments';experiments.mkdir(parents=True)
        scripts=root/'app/experiments';scripts.mkdir(parents=True)
        (scripts/'worker.py').write_text('# fixture producer')
        paths=[]
        for i in range(count):
            name=f'worker__{i}.json';path=experiments/name
            path.write_text(json.dumps({'meta':{'experiment':'fixture','model':'cpu'},'rows':[{'id':'book:1','arm':'base','correct':True}]}));paths.append(str(path))
        for name in ('space 日本語.json','literal[1]?.json','line\nname.json','unknown.json'):
            path=experiments/name;path.write_text('{"rows":[]}');paths.append(str(path))
        provenance=experiments/'exact.json';provenance.write_text(json.dumps({'provenance':{'script':'worker.py','args':{'limit':3}}}));paths.append(str(provenance))
        git(root,'add','.');git(root,'commit','-qm','first artifacts')
        missing=experiments/'untracked.json';missing.write_text('{}');paths.append(str(missing))
        return experiments,paths

    def test_native_batched_results_equal_each_single_result_with_two_git_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);experiments,paths=self.fixture(root)
            with patch.object(replay,'REPO',str(root)):
                expected={p:replay.resolve_producer(p,'python') for p in paths}
                native_run=subprocess.run
                with patch.object(replay.subprocess,'run',wraps=native_run) as runs,patch.object(replay.glob,'glob',wraps=replay.glob.glob) as searches:
                    actual=replay.resolve_producers(paths+[paths[0]],'python')
                self.assertEqual(expected,actual)
                self.assertEqual(2,runs.call_count)
                self.assertEqual(1,searches.call_count)
                self.assertEqual('provenance',actual[str(experiments/'exact.json')]['tier'])
                self.assertIsNotNone(actual[str(experiments/'exact.json')]['argv'])
                self.assertIsNone(actual[str(experiments/'untracked.json')]['commit'])
                self.assertEqual('none',actual[str(experiments/'unknown.json')]['tier'])
                self.assertIsNotNone(actual[str(experiments/'unknown.json')]['commit'])
                self.assertIsNone(actual[paths[0]]['argv'])

    def test_batch_retains_squash_merge_vantage_independence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo,_main,branch,_merge=topology.build_squash_topology(tmp)
            paths=['ab_test_runtime/experiments/result.json','ab_test_runtime/experiments/none.json']
            # The actual fixture artifact path is exposed in its native tree.
            paths[0]=git(repo,'ls-files','ab_test_runtime/experiments').splitlines()[0]
            with patch.object(replay,'REPO',repo):
                for ref in (branch,'main'):
                    git(repo,'checkout','-q',ref)
                    expected={p:replay.added_commit(p) for p in paths}
                    self.assertEqual(expected,replay.get_added_commits(paths))

    def test_actual_collector_preserves_csv_provenance_with_one_batch_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);experiments,paths=self.fixture(root,count=8)
            paths=[p for p in paths if Path(p).name.startswith('worker__')]
            audit=root/'ab_test_runtime/audit';audit.mkdir()
            for name in ('artifact_structural_audit.json','legacy_attribution_audit.json'):
                (audit/name).write_text('{"artifacts":[]}')
            kwargs={'REPO':str(root),'E':str(experiments),'AUDIT':str(audit)}
            with patch.multiple(collector,**kwargs),patch.object(replay,'REPO',str(root)),patch.object(membership,'indexable_artifacts',return_value=(paths,[])),contextlib.redirect_stdout(io.StringIO()):
                native_run=subprocess.run
                with patch.object(replay.subprocess,'run',wraps=native_run) as runs:
                    self.assertEqual(0,collector.main([]))
                self.assertEqual(1,runs.call_count,'one history query for eight distinct artifacts')
                actual=(root/'results_index.csv').read_bytes()
                # Disable only batching to compare actual per-artifact output bytes.
                with patch.object(replay,'resolve_producers',side_effect=RuntimeError('force per-artifact comparison')),contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(0,collector.main([]))
                self.assertEqual(actual,(root/'results_index.csv').read_bytes())
            rows=list(csv.DictReader(io.StringIO(actual.decode())))
            self.assertEqual(8,len(rows));self.assertTrue(all(r['producer']=='worker.py' and r['resolution_tier']=='git+naming' and r['replayable']=='no' and r['added_commit'] for r in rows))

    def test_failed_batch_history_does_not_assign_plausible_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(replay,'REPO',str(root)),contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual({'missing.json':(None,None)},replay.get_added_commits(['missing.json']))
            self.assertIn('history',errors.getvalue())
