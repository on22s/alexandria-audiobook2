"""Actual CLI selection/artifacts with known terms; native chain argv and status."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from experiments import measure_respellings as current
from experiments import generation

REPO=Path(__file__).resolve().parents[2]
measure=current
CHAIN=REPO/'run_chains/queued_20260818_findings.sh'
if os.environ.get('RESPELLING_243_BASELINE'):
    spec=importlib.util.spec_from_file_location('original_sample243','/tmp/measure_respellings_before243.py')
    measure=importlib.util.module_from_spec(spec);spec.loader.exec_module(measure)
    CHAIN=Path('/tmp/queued_findings_before243.sh')


class RespellingSampleOffsetTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        app=self.root/'app';app.mkdir();(app/'config.json').write_text('{}')
        self.app=app;self.candidates=self.root/'candidates.json'
        rows=[{'term':f'Term{i:03}','kana':'ケケ','books':500-i,'series':1,'verdict':'ja'} for i in range(450)]
        rows.insert(0,{'term':'Foreign','kana':'ケケ','books':999,'series':1,'verdict':'en'})
        rows.insert(0,{'term':'OtherMora','kana':'ク','books':998,'series':1,'verdict':'ja'})
        self.candidates.write_text(json.dumps({'candidates':rows}))
        self.addCleanup(setattr,measure,'_ARGS',measure._ARGS)
        self.addCleanup(setattr,measure,'SEPARATOR',measure.SEPARATOR)

    def run_sample(self, name, offset=None, only_failed=None):
        out=self.root/(name+'.json');args=['measure','--candidates',str(self.candidates),
            '--min-books','5','--only-e-row','--e-spelling','ay','--limit','200',
            '--work',str(self.root/name),'--out',str(out)]
        if offset is not None:args+=['--offset',str(offset)]
        if only_failed is not None:args+=['--only-failed',str(only_failed)]
        engine=unittest.mock.Mock(return_value=object())
        def render(*args):Path(args[-1]).write_bytes(b'fixture render output')
        with patch.object(sys,'argv',args),patch.object(measure,'APP',str(self.app)), \
             patch.dict(sys.modules,{'tts':types.SimpleNamespace(TTSEngine=engine)}), \
             patch.object(generation,'render',side_effect=render) as renders, \
             patch.object(measure,'transcribe',return_value='ケケ'),contextlib.redirect_stdout(io.StringIO()):
            measure.main()
        return json.loads(out.read_text()),engine.call_count,renders.call_count

    def test_actual_queued_second_sample_contains_next200_distinct_terms(self):
        source=CHAIN.read_text();stage=source[source.index('run_stage e_row_second_sample'):]
        match=re.search(r'--offset (\d+)',stage)
        offset=int(match.group(1)) if match else None
        first,_,_=self.run_sample('first')
        second,_,_=self.run_sample('second',offset)
        a={row['term'] for row in first['results']};b={row['term'] for row in second['results']}
        self.assertEqual({f'Term{i:03}' for i in range(200)},a)
        self.assertEqual({f'Term{i:03}' for i in range(200,400)},b)
        self.assertFalse(a&b);self.assertEqual(200,second['candidates_considered'])
        self.assertEqual('complete',second['status']);self.assertEqual(200,second['run_identity']['offset'])

    def test_resume_same_offset_does_not_rerender_and_different_offset_refuses(self):
        first,_,renders=self.run_sample('second',200);self.assertEqual(400,renders)
        resumed,_,renders=self.run_sample('second',200);self.assertEqual(0,renders);self.assertEqual(first['results'],resumed['results'])
        before=(self.root/'second.json').read_bytes()
        with self.assertRaisesRegex(SystemExit,'different respelling arm'):self.run_sample('second',0)
        self.assertEqual(before,(self.root/'second.json').read_bytes())

    def test_offset_applies_after_failed_filter_and_sort(self):
        prior=self.root/'prior.json'
        prior.write_text(json.dumps({'results':[{'term':f'Term{i:03}','plain_recovers_word':False,'helps':False} for i in range(100,450)]}))
        doc,_,_=self.run_sample('failed',10,prior)
        self.assertEqual([f'Term{i:03}' for i in range(110,310)],[r['term'] for r in doc['results']])

    def test_negative_and_out_of_pool_offset_refuse_before_model_construction(self):
        for offset in (-1,450):
            with self.subTest(offset=offset),self.assertRaises(SystemExit):self.run_sample('bad'+str(offset),offset)
            self.assertFalse((self.root/('bad'+str(offset)+'.json')).exists())

    def test_actual_cli_reports_resume_baseline_and_only_new_completed_terms(self):
        import gpu_progress
        with patch.object(gpu_progress, 'record_gpu_progress') as report:
            self.run_sample('progress', 200)
        self.assertEqual(('respelling terms', 0, 200), report.call_args_list[0].args)
        self.assertEqual(('respelling terms', 200, 200), report.call_args_list[-1].args)
        self.assertEqual(201, report.call_count)
        with patch.object(gpu_progress, 'record_gpu_progress') as report:
            self.run_sample('progress', 200)
        self.assertEqual([unittest.mock.call('respelling terms', 200, 200)], report.call_args_list)

    def test_native_queue_footer_preserves_worker_failure(self):
        source=CHAIN.read_text();tail=source[source.index('run_stage e_row_second_sample'):]
        worker=self.root/'worker';worker.write_text('#!/bin/sh\nexit 4\n');worker.chmod(0o755)
        wrapper=self.root/'gpu_job.sh';wrapper.write_text('#!/bin/sh\nshift\nexec "$@"\n');wrapper.chmod(0o755)
        code='set -uo pipefail\nREPO='+shlex.quote(str(self.root))+'\nruntime="$REPO/runtime"\npython='+shlex.quote(str(worker))+'\nSTAGE_LOG_DIR="$REPO/logs"\nsource '+shlex.quote(str(REPO/'run_chains/lib/stage.sh'))+'\n'+tail
        result=subprocess.run(['bash','-c',code],capture_output=True,text=True,timeout=5)
        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn('e_row_second_sample = failed:4',result.stdout)
