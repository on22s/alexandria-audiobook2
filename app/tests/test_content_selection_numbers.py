"""Content selection indices are strict one-based JSON integers at both boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from content_repair import apply_content_selections
from routers import scripts_library

BAD=(True,False,'1',1.0,1.5,None,[],{},0,-1,99)
ROWS=[{'speaker':'NARRATOR','text':'Copyright Publisher','instruct':' Neutral. '},
      {'speaker':'NARRATOR','text':'The story began.','instruct':' Calm. '}]

class ContentSelectionNumberTests(unittest.TestCase):
    def test_invalid_indices_raise_value_error_before_set_arithmetic_or_input_mutation(self):
        for number in BAD:
            for kind in ('remove','direction'):
                with self.subTest(number=number,kind=kind):
                    rows=copy.deepcopy(ROWS);selection=({'entry_number':number,'expected_text':ROWS[0]['text']} if kind=='remove'
                        else {'entry_number':number,'expected_instruct':ROWS[0]['instruct'],'new_instruct':'Neutral.'})
                    before=copy.deepcopy(selection)
                    with self.assertRaises(ValueError):
                        apply_content_selections(rows,[selection] if kind=='remove' else [],[selection] if kind=='direction' else [])
                    self.assertEqual(ROWS,rows);self.assertEqual(before,selection)

    def test_http_invalid_json_types_are_rejected_before_backups_or_script_writes(self):
        app=FastAPI();app.include_router(scripts_library.router)
        with TestClient(app) as client:
            for number in BAD[:-1]:
                for kind in ('remove','direction'):
                    with self.subTest(number=number,kind=kind),tempfile.TemporaryDirectory() as tmp:
                        root=Path(tmp);script=root/'book.json';original=json.dumps(ROWS).encode();script.write_bytes(original)
                        selection=({'entry_number':number,'expected_text':ROWS[0]['text']} if kind=='remove'
                            else {'entry_number':number,'expected_instruct':ROWS[0]['instruct'],'new_instruct':'Neutral.'})
                        body={'expected_sha256':hashlib.sha256(original).hexdigest(),
                            'front_matter_removals':[selection] if kind=='remove' else [],
                            'direction_changes':[selection] if kind=='direction' else []}
                        with patch.object(scripts_library,'SCRIPTS_DIR',str(root)):
                            response=client.post('/api/scripts/book/repair/content/apply',json=body)
                        self.assertEqual(422,response.status_code,response.text)
                        self.assertEqual(original,script.read_bytes())
                        self.assertFalse(any('backup' in p.name or '.bak' in p.name for p in root.iterdir()))

    def test_valid_json_integers_preserve_selected_repair_and_exact_original_backup(self):
        app=FastAPI();app.include_router(scripts_library.router)
        with tempfile.TemporaryDirectory() as tmp,TestClient(app) as client:
            root=Path(tmp);script=root/'book.json';original=json.dumps(ROWS).encode();script.write_bytes(original)
            with patch.object(scripts_library,'SCRIPTS_DIR',str(root)):
                response=client.post('/api/scripts/book/repair/content/apply',json={
                    'expected_sha256':hashlib.sha256(original).hexdigest(),
                    'front_matter_removals':[{'entry_number':1,'expected_text':ROWS[0]['text']}],
                    'direction_changes':[{'entry_number':2,'expected_instruct':ROWS[1]['instruct'],'new_instruct':'Calm.'}]})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual([{**ROWS[1],'instruct':'Calm.'}],json.loads(script.read_bytes()))
            self.assertEqual(original,(root/response.json()['backup']).read_bytes())
