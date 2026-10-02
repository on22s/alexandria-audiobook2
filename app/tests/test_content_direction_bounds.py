"""Selected whitespace repairs cannot save oversized or unsafe instructions."""
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


class ContentDirectionBoundsTests(unittest.TestCase):
    def apply(self, value):
        rows = [{'speaker':'NARRATOR','text':'Story text.','instruct':' '+value+' '}]
        selection = {'entry_number':1, 'expected_instruct':rows[0]['instruct'], 'new_instruct':value}
        return rows, selection

    def test_helper_rejects_exact_but_oversized_or_unsafe_suggestions(self):
        for value in ('x'*401, 'Calm.\x00', 'Quiet.\x1b[31m', 'Soft.\x7f', 'Soft.\ud800'):
            with self.subTest(value=repr(value)):
                rows, selection = self.apply(value); original = copy.deepcopy((rows, selection))
                with self.assertRaises(ValueError):
                    apply_content_selections(rows, [], [selection])
                self.assertEqual(original, (rows, selection))

    def test_http_refusals_leave_script_and_backups_unchanged(self):
        app = FastAPI(); app.include_router(scripts_library.router)
        with TestClient(app) as client:
            for value in ('x'*401, 'Calm.\x00', 'Quiet.\x1b[31m', 'Soft.\x7f'):
                with self.subTest(value=repr(value)), tempfile.TemporaryDirectory() as tmp:
                    rows, selection = self.apply(value)
                    root = Path(tmp); script = root/'book.json'; raw = json.dumps(rows).encode(); script.write_bytes(raw)
                    with patch.object(scripts_library, 'SCRIPTS_DIR', str(root)):
                        response = client.post('/api/scripts/book/repair/content/apply', json={
                            'expected_sha256':hashlib.sha256(raw).hexdigest(), 'direction_changes':[selection]})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(raw, script.read_bytes())
                    self.assertEqual(['.active_book_transaction.json.lock','book.json','book.json.lock'], sorted(p.name for p in root.iterdir()))

    def test_boundary_unicode_and_whitespace_repairs_preserve_original_backup(self):
        app = FastAPI(); app.include_router(scripts_library.router)
        for value in ('x'*400, '温かく café مُطمئن 👩‍👩‍👧‍👦', 'Calm. Gently.'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp, TestClient(app) as client:
                rows, selection = self.apply(value)
                if value == 'Calm. Gently.':
                    rows[0]['instruct'] = '\tCalm.\r\nGently.\t'
                    selection['expected_instruct'] = rows[0]['instruct']
                raw = json.dumps(rows).encode(); root=Path(tmp); script=root/'book.json'; script.write_bytes(raw)
                with patch.object(scripts_library, 'SCRIPTS_DIR', str(root)):
                    response = client.post('/api/scripts/book/repair/content/apply', json={
                        'expected_sha256':hashlib.sha256(raw).hexdigest(), 'direction_changes':[selection]})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(value, json.loads(script.read_bytes())[0]['instruct'])
                self.assertEqual(raw, (root/response.json()['backup']).read_bytes())
