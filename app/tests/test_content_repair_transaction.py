"""Real content-repair HTTP requests are pinned to the whole preview under one file lock."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import scripts_library

ROWS=[{'speaker':'NARRATOR','text':'Copyright Publisher','instruct':' Neutral. '},
      {'speaker':'NARRATOR','text':'The story began.','instruct':' Calm. '}]

def direction(number):
    return {'entry_number':number,'expected_instruct':ROWS[number-1]['instruct'],
            'new_instruct':ROWS[number-1]['instruct'].strip()}

class ContentRepairTransactionTests(unittest.TestCase):
    def test_whole_script_token_rejects_changed_text_speaker_or_unselected_entry(self):
        app=FastAPI();app.include_router(scripts_library.router)
        with TestClient(app) as client:
            for change in ('text','speaker','unselected'):
                with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);script=root/'book.json';script.write_text(json.dumps(ROWS))
                    with patch.object(scripts_library,'SCRIPTS_DIR',str(root)):
                        preview=client.get('/api/scripts/book/repair/content/preview').json()
                        updated=json.loads(json.dumps(ROWS))
                        if change=='unselected':updated[1]['text']='A newer story began.'
                        else:updated[0][change]='Changed after preview'
                        current=json.dumps(updated).encode();script.write_bytes(current)
                        response=client.post('/api/scripts/book/repair/content/apply',json={
                            'expected_sha256':preview['sha256'],'direction_changes':[direction(1)]})
                    self.assertEqual(409,response.status_code,response.text)
                    self.assertIn('Script changed after preview',response.text)
                    self.assertEqual(current,script.read_bytes())
                    self.assertEqual(['.active_book_transaction.json.lock','book.json','book.json.lock'],sorted(p.name for p in root.iterdir()))

    def test_two_real_http_workers_serialize_token_check_backup_and_publication(self):
        app=FastAPI();app.include_router(scripts_library.router)
        entered=threading.Event();attempted=threading.Event();second_started=threading.Event();release=threading.Event()
        count=0;guard=threading.Lock();real_lock=scripts_library.file_lock
        real_apply=scripts_library.apply_content_selections
        @contextmanager
        def observed_lock(*args,**kwargs):
            nonlocal count
            with guard:
                count+=1;n=count
            if n==2:attempted.set()
            with real_lock(*args,**kwargs):yield
        def paused_apply(entries,removals,directions):
            entered.set()
            if not release.wait(3):raise RuntimeError('CPU fixture release timed out')
            return real_apply(entries,removals,directions)
        with tempfile.TemporaryDirectory() as tmp,TestClient(app) as first,TestClient(app) as second:
            root=Path(tmp);script=root/'book.json';original=json.dumps(ROWS).encode();script.write_bytes(original)
            body={'expected_sha256':hashlib.sha256(original).hexdigest()}
            with patch.object(scripts_library,'SCRIPTS_DIR',str(root)), \
                 patch.object(scripts_library,'file_lock',side_effect=observed_lock), \
                 patch.object(scripts_library,'apply_content_selections',side_effect=paused_apply),ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(first.post,'/api/scripts/book/repair/content/apply',json={**body,'direction_changes':[direction(1)]})
                try:
                    self.assertTrue(entered.wait(2),'first request never entered repair')
                    def second_request():
                        second_started.set()
                        return second.post('/api/scripts/book/repair/content/apply',json={**body,'direction_changes':[direction(2)]})
                    b=pool.submit(second_request)
                    self.assertTrue(second_started.wait(2),'second request worker never started')
                    self.assertFalse(b.done(),'second request escaped held repair lock')
                finally:release.set()
                first_result=a.result(timeout=4);second_result=b.result(timeout=4)
            self.assertTrue(attempted.is_set(),'second request never reached inner file lock after release')
            self.assertEqual(200,first_result.status_code,first_result.text)
            self.assertEqual(409,second_result.status_code,second_result.text)
            self.assertIn('Script changed after preview',second_result.text)
            self.assertEqual([{**ROWS[0],'instruct':'Neutral.'},ROWS[1]],json.loads(script.read_bytes()))
            self.assertEqual(original,(root/first_result.json()['backup']).read_bytes())
            self.assertEqual(sorted(['.active_book_transaction.json.lock','book.json','book.json.lock',first_result.json()['backup']]),sorted(p.name for p in root.iterdir()))
