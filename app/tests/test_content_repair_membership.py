"""Only current preview candidates and exact lossless direction suggestions can be applied."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from content_repair import apply_content_selections,build_content_review
from routers import scripts_library

ROWS=[{'speaker':'NARRATOR','text':'Copyright Publisher','instruct':' Neutral. '},
      {'speaker':'NARRATOR','text':'The story began.','instruct':' Calm. '},
      {'speaker':'ANNA','text':'The story continued.','instruct':'Clear.'}]
ROWS += [{'speaker':'NARRATOR','text':'Ordinary story text.','instruct':'Clear.'} for _ in range(57)]
ROWS += [{'speaker':'NARRATOR','text':'Copyright appears in the story beyond the preview limit.','instruct':'Clear.'}]

def removal(number):return {'entry_number':number,'expected_text':ROWS[number-1]['text']}
def direction(number,new):return {'entry_number':number,'expected_instruct':ROWS[number-1]['instruct'],'new_instruct':new}

INVALID=[([removal(2)],[]),([removal(61)],[]),([],[direction(3,'Different.')]),
         ([],[direction(2,'Replace actual delivery.')]),([], [direction(2,'  Calm. ')]),
         ([removal(1),removal(2)],[]),([removal(1),removal(1)],[]),
         ([removal(1)],[direction(1,'Neutral.')]),([],[direction(2,'Calm.'),direction(2,'Calm.')])]

class ContentRepairMembershipTests(unittest.TestCase):
    def test_helper_refuses_nonpreview_rows_arbitrary_directions_and_duplicate_or_conflicting_selections(self):
        for removals,directions in INVALID:
            with self.subTest(removals=removals,directions=directions):
                rows=copy.deepcopy(ROWS);before=copy.deepcopy((removals,directions))
                with self.assertRaises(ValueError):apply_content_selections(rows,removals,directions)
                self.assertEqual(ROWS,rows);self.assertEqual(before,(removals,directions))

    def test_http_refusals_preserve_exact_script_without_creating_backup(self):
        app=FastAPI();app.include_router(scripts_library.router)
        with TestClient(app) as client:
            for removals,directions in INVALID:
                with self.subTest(removals=removals,directions=directions),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);script=root/'book.json';original=json.dumps(ROWS).encode();script.write_bytes(original)
                    with patch.object(scripts_library,'SCRIPTS_DIR',str(root)):
                        response=client.post('/api/scripts/book/repair/content/apply',json={
                            'expected_sha256':hashlib.sha256(original).hexdigest(),
                            'front_matter_removals':removals,'direction_changes':directions})
                    self.assertEqual(409,response.status_code,response.text)
                    self.assertEqual(original,script.read_bytes());self.assertEqual(['.active_book_transaction.json.lock','book.json','book.json.lock'],sorted(p.name for p in root.iterdir()))

    def test_exact_preview_selections_apply_and_preserve_unselected_rows_and_original_backup(self):
        app=FastAPI();app.include_router(scripts_library.router)
        with tempfile.TemporaryDirectory() as tmp,TestClient(app) as client:
            root=Path(tmp);script=root/'book.json';original=json.dumps(ROWS).encode();script.write_bytes(original)
            with patch.object(scripts_library,'SCRIPTS_DIR',str(root)):
                preview=client.get('/api/scripts/book/repair/content/preview').json()
                self.assertEqual([1],[r['entry_number'] for r in preview['front_matter']])
                self.assertEqual([1,2],[r['entry_number'] for r in preview['direction_normalizations']])
                response=client.post('/api/scripts/book/repair/content/apply',json={
                    'expected_sha256':preview['sha256'],'front_matter_removals':[removal(1)],
                    'direction_changes':[direction(2,preview['direction_normalizations'][1]['suggested'])]})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual([{**ROWS[1],'instruct':'Calm.'}]+ROWS[2:],json.loads(script.read_bytes()))
            self.assertEqual(original,(root/response.json()['backup']).read_bytes())
