"""Actual ASR cache CLI/chain with known PCM, metrics, and no inference."""
import copy
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import unittest

from tests import test_asr_completion as fixtures

REPO=Path(__file__).resolve().parents[2]


class JapaneseAsrCacheTests(unittest.TestCase):
    setUpFixture=fixtures.AsrCompletionTests.setUp
    make_document=fixtures.AsrCompletionTests.make_document

    def setUp(self):
        self.setUpFixture()
        folder=self.runtime/'kokoro_ja_asr_eval';folder.mkdir()
        build=folder/'build.json';shutil.copyfile(self.args.build,build)
        self.args.build=str(build);self.args.row_offset=0
        self.args.out=str(self.runtime/'experiments/asr_silero_whisper_ja_confirmation.json')
        self.rows=self.asr.get_asr_rows(self.args)
        self.truth=[self.asr.get_alignment_boundary(row,index*.6,.1) for index,row in enumerate(self.rows)]
        self.document=self.make_document()
        chain=self.root/'run_chains/japanese_asr_confirmation.sh';chain.parent.mkdir()
        source=Path(os.environ.get('JAPANESE_ASR_SOURCE',str(REPO/'run_chains'/chain.name))).read_text()
        chain.write_text(source)
        python=self.root/'app/env/bin/python';python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');python.chmod(0o755)
        (self.root/'app/experiments/kokoro_ja_asr_set.py').write_text('import os,pathlib\npathlib.Path(os.environ["FIXTURE_ROOT"],"built").touch()\n')
        queue=self.root/'gpu_job.sh';queue.write_text('#!/bin/bash\ntouch "$FIXTURE_ROOT/dispatched"\nexit 7\n');queue.chmod(0o755)
        self.chain=chain
        self.env=dict(os.environ,FIXTURE_ROOT=str(self.root),JA_CONFIRM_CLIPS='10',PYTHONPATH=str(REPO/'app'))

    def run_chain(self,clips='10'):
        for marker in ('built','dispatched'):
            p=self.root/marker
            if p.exists():p.unlink()
        return subprocess.run(['bash',str(self.chain)],cwd=self.root.parent,env=dict(self.env,JA_CONFIRM_CLIPS=clips),capture_output=True,text=True,timeout=10)

    def test_valid_real_format_good_and_poor_measurements_skip_without_model_or_generation(self):
        for poor in (False,True):
            for marker in (False,True):
                with self.subTest(poor=poor,status_marker=marker):
                    document=self.make_document(poor)
                    if marker:document['status']='complete';Path(self.args.out).write_text(json.dumps(document))
                    before=Path(self.args.out).read_bytes();result=self.run_chain()
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertIn('already confirmed',result.stdout)
                    self.assertFalse((self.root/'built').exists())
                    self.assertFalse((self.root/'dispatched').exists())
                    self.assertEqual(before,Path(self.args.out).read_bytes())

    def test_changed_clip_request_partial_coverage_or_model_cannot_skip(self):
        original=copy.deepcopy(self.document);original['status']='complete'
        for kind in ('clips50','partial','wrong_scores','model_changed'):
            with self.subTest(kind=kind):
                document=copy.deepcopy(original)
                if kind=='partial':document['results']['silero_whisper_cpp']['processed_ids'].pop()
                if kind=='wrong_scores':document['results']['silero_whisper_cpp']['wer_mean']=.99
                model=self.large_model;old=model.read_bytes()
                try:
                    if kind=='model_changed':model.write_bytes(old+b'changed')
                    Path(self.args.out).write_text(json.dumps(document));before=Path(self.args.out).read_bytes()
                    result=self.run_chain('50' if kind=='clips50' else '10')
                    self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                    self.assertNotIn('already confirmed',result.stdout)
                    self.assertTrue((self.root/'built').exists())
                    self.assertTrue((self.root/'dispatched').exists())
                    self.assertIn('JAPANESE ASR CONFIRMATION FAILED',result.stdout)
                    self.assertEqual(before,Path(self.args.out).read_bytes())
                finally:model.write_bytes(old)
