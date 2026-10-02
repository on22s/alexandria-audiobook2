import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_script import normalize_text
from text_diff import word_diff


class LargeBookDiffTests(unittest.TestCase):
    def assert_reconstructs(self, source, entries, result):
        source_words=normalize_text(source).split(); recovered=[];cursor=0
        for hunk in result['hunks']:
            start=hunk['source_pos']
            self.assertGreaterEqual(start,cursor)
            recovered.extend(source_words[cursor:start])
            old=hunk['source_words'].split()
            self.assertEqual(old,source_words[start:start+len(old)])
            recovered.extend(hunk['script_words'].split());cursor=start+len(old)
        recovered.extend(source_words[cursor:])
        self.assertEqual(normalize_text(' '.join(entry['text'] for entry in entries)).split(),recovered)

    def test_empty_and_boundary_edits_keep_context_and_entry_ownership(self):
        cases = [('', [{'text':'new words'}], (0,2,0)),
                 ('old words',[],(2,0,0)),
                 ('one two', [{'text':'first one'},{'text':'two last'}],(0,2,0)),
                 ('one old two', [{'text':'one new two'}],(0,0,1))]
        for source,entries,counts in cases:
            with self.subTest(source=source):
                result=word_diff(source,entries)
                self.assert_reconstructs(source,entries,result)
                self.assertEqual(counts,tuple(result['totals'][k] for k in ('deleted','inserted','replaced')))
                for hunk in result['hunks']:
                    self.assertEqual(bool(entries),hunk['entry_index'] is not None)
                    self.assertGreaterEqual(hunk['chunk'],1)
        result=word_diff('one two',[{'text':'first one'},{'text':'two last'}])
        self.assertEqual([0,1],[h['entry_index'] for h in result['hunks']])

    def test_repeated_words_with_multiple_distant_edits_are_precise(self):
        original=['red','blue','green','gold','white']*200
        changed=original.copy();changed[703]='scarlet';changed.insert(502,'extra');del changed[201]
        source=' '.join(original);entries=[{'text':' '.join(changed[i:i+100])} for i in range(0,len(changed),100)]
        result=word_diff(source,entries)
        self.assert_reconstructs(source,entries,result)
        self.assertEqual((1,1,1),tuple(result['totals'][k] for k in ('deleted','inserted','replaced')))
        self.assertEqual({'delete','insert','replace'},{h['kind'] for h in result['hunks']})
        self.assertEqual('extra',next(h for h in result['hunks'] if h['kind']=='insert')['script_words'])
        self.assertEqual('scarlet',next(h for h in result['hunks'] if h['kind']=='replace')['script_words'])

    def test_actual_http_route_reports_exact_edit_and_owner(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import script
        words=['alpha','beta','gamma']*500
        changed=words.copy();changed[901]='delta'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.txt';active=root/'active.json'
            source.write_text(' '.join(words))
            active.write_text(json.dumps([{'text':' '.join(changed[:750])},{'text':' '.join(changed[750:])}]))
            (root/'state.json').write_text(json.dumps({'input_file_path':str(source)}))
            before=(source.read_bytes(),active.read_bytes())
            app=FastAPI();app.include_router(script.router)
            with patch.object(script,'SCRIPT_PATH',str(active)),patch.object(script,'DATA_DIR',str(root)),TestClient(app) as client:
                response=client.get('/api/annotated_script/diff')
            self.assertEqual(200,response.status_code,response.text)
            result=response.json();self.assertEqual(1,len(result['hunks']))
            hunk=result['hunks'][0]
            self.assertEqual(('replace','beta','delta',1),
                             tuple(hunk[key] for key in ('kind','source_words','script_words','entry_index')))
            self.assertEqual(1,result['totals']['replaced'])
            self.assertEqual(before,(source.read_bytes(),active.read_bytes()))

    def test_actual_native_100000_word_diff_keeps_three_edits_within_deadline(self):
        program = '''import json,time
from text_diff import word_diff
source=['red','blue','green','gold','white']*20000
changed=source.copy();changed[75003]='scarlet';changed.insert(50002,'extra');del changed[20001]
entries=[{'text':' '.join(changed[i:i+500])} for i in range(0,len(changed),500)]
started=time.monotonic();result=word_diff(' '.join(source),entries)
print(json.dumps({'elapsed':time.monotonic()-started,'result':result}))
'''
        completed=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,completed.returncode,completed.stderr)
        measured=json.loads(completed.stdout);result=measured['result']
        self.assertEqual((100000,100000),tuple(result['totals'][k] for k in ('source_words','script_words')))
        self.assertEqual((1,1,1),tuple(result['totals'][k] for k in ('deleted','inserted','replaced')))
        self.assertEqual(3,len(result['hunks']))
        self.assertLess(measured['elapsed'],5)
        self.assertEqual({100,150},{h['entry_index'] for h in result['hunks'] if h['kind']!='delete'})
