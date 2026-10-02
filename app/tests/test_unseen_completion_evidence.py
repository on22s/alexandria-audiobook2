"""Run the actual Bash completion function against partial and bound artifacts."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.unseen_fixture_support import COMPLETION_FIXTURE_CODE

ROOT=Path(__file__).resolve().parents[2]


class UnseenCompletionEvidenceTests(unittest.TestCase):
    def check(self,output,source):
        chain=Path(os.environ.get('UNSEEN_COMPLETION_CHAIN',str(ROOT/'run_chains/unseen_books_20260819b.sh'))).read_text()
        start=chain.index('book_complete() {');end=chain.index('\n}',start)+2
        script='PY="$1"\n'+chain[start:end]+'\nbook_complete "$2" "$3"\n'
        return subprocess.run(['bash','-c',script,'fixture',sys.executable,str(output),str(source)],
            cwd=ROOT/'app',capture_output=True,text=True,timeout=10)

    def test_partial_51_of_110_entries_without_completion_is_not_skipped(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.txt';output=root/'book.json'
            source.write_text('full source')
            output.write_text(json.dumps([{'text':str(i)} for i in range(51)]))
            result=self.check(output,source)
            self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)

    def test_complete_short_book_is_reused_but_modified_input_or_output_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.txt';output=root/'book.json';source.write_text('full source')
            namespace={};exec(COMPLETION_FIXTURE_CODE,namespace)
            namespace['save_complete_fixture'](output,source,[{'text':'short complete book'}])
            result=self.check(output,source)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            output.write_text('[{"text":"partial replacement"}]')
            self.assertNotEqual(0,self.check(output,source).returncode)
            namespace['save_complete_fixture'](output,source,[{'text':'short complete book'}])
            source.write_text('changed input')
            self.assertNotEqual(0,self.check(output,source).returncode)
