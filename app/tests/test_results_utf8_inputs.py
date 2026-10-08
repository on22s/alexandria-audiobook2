import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from tests.test_experiment_infra import stage_index_scripts, run_index_script


class ResultsUtf8InputTests(unittest.TestCase):
    def test_actual_collector_keeps_utf8_rows_under_simulated_cp1252_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_index_scripts(tmp)
            artifacts = root / 'ab_test_runtime/experiments'
            audits = root / 'ab_test_runtime/audit'
            artifacts.mkdir(parents=True); audits.mkdir(parents=True)
            artifact = artifacts / 'unicode.json'
            content = json.dumps({'rows': [{'arm': 'base', 'text': 'あ', 'correct': True}]},
                                 ensure_ascii=False).encode('utf-8')
            artifact.write_bytes(content)
            for name in ('artifact_structural_audit.json', 'legacy_attribution_audit.json'):
                (audits / name).write_text('{"artifacts": []}', encoding='utf-8')
            wrapper = '''import builtins,runpy
original=builtins.open
def legacy(file,mode='r',*args,**kwargs):
 if 'b' not in mode and len(args)<2 and kwargs.get('encoding') is None:kwargs['encoding']='cp1252'
 return original(file,mode,*args,**kwargs)
builtins.open=legacy
runpy.run_path('tools/audit/collect_results.py',run_name='__main__')
'''
            for arguments in ([sys.executable, 'tools/audit/collect_results.py'],
                              [sys.executable, '-c', wrapper]):
                result = run_index_script(arguments, cwd=root, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                with (root / 'results_index.csv').open(encoding='utf-8') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(1, len(rows))
                self.assertEqual(('unicode.json', '1', '1', ''),
                                 tuple(rows[0][key] for key in ('artifact', 'n', 'correct', 'note')))
                self.assertEqual(content, artifact.read_bytes())
