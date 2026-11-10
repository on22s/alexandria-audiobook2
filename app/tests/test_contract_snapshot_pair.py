"""Contract readers never accept a mixed publication; writers recover interrupted pairs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI
import book_state_transaction as transaction
import update_api_contract_snapshots as snapshots
from tests.test_support import intercept_book_publications


def application(title):
    app=FastAPI(title=title)
    @app.get('/'+title)
    def endpoint():return {'title':title}
    return app


class ContractSnapshotPairTests(unittest.TestCase):
    def test_deterministic_pair_and_second_write_failure_preserve_old_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=application('old');new=application('new')
            paths=snapshots.write_snapshots(old,root);original=[p.read_bytes() for p in paths]
            routes=root/'routes.json';failed=[]
            def fail_second_replacement(target):
                if target==routes and not failed:
                    failed.append(target)
                    raise OSError('fixture second replacement failed')
            with intercept_book_publications(transaction,before=fail_second_replacement), \
                    self.assertRaisesRegex(OSError,'second replacement'):
                snapshots.write_snapshots(new,root)
            self.assertEqual([routes],failed)
            self.assertEqual(original,[p.read_bytes() for p in paths])
            self.assertEqual([],snapshots.check_snapshots(old,root))
            self.assertFalse((root/transaction.JOURNAL).exists())
            snapshots.write_snapshots(new,root)
            self.assertEqual([],snapshots.check_snapshots(new,root))
            expected=(json.dumps(new.openapi(),indent=2,sort_keys=True,ensure_ascii=False)+'\n').encode()
            self.assertEqual(expected,paths[0].read_bytes())
            first=[p.read_bytes() for p in paths];snapshots.write_snapshots(new,root)
            self.assertEqual(first,[p.read_bytes() for p in paths])

    def test_native_killed_writer_leaves_pending_pair_rejected_then_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=application('old');paths=snapshots.write_snapshots(old,root)
            original=[p.read_bytes() for p in paths];ready=root/'fixture_ready'
            code='''
import os,time
from pathlib import Path
from fastapi import FastAPI
import book_state_transaction as transaction
import update_api_contract_snapshots as snapshots
from tests.test_support import intercept_book_publications
root=Path(os.environ['FIXTURE_ROOT'])
app=FastAPI(title='new')
@app.get('/new')
def endpoint():return {'title':'new'}
def interrupted(target):
 if Path(target).name=='openapi.json':
  (root/'fixture_ready').write_text('first published')
  while True:time.sleep(.1)
with intercept_book_publications(transaction,after=interrupted):
 snapshots.write_snapshots(app,root)
'''
            child=subprocess.Popen([sys.executable,'-c',code],env=dict(os.environ,FIXTURE_ROOT=tmp),
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+10
                while not ready.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(ready.exists(),'child did not reach first publication')
                child.kill();stdout,stderr=child.communicate(timeout=5)
                self.assertNotEqual(original[0],paths[0].read_bytes())
                self.assertEqual(original[1],paths[1].read_bytes())
                mixed=[p.read_bytes() for p in paths]
                with self.assertRaisesRegex(ValueError,'publication was interrupted'):
                    snapshots.check_snapshots(old,root)
                self.assertEqual(mixed,[p.read_bytes() for p in paths])
                new=application('new');snapshots.write_snapshots(new,root)
                self.assertEqual([],snapshots.check_snapshots(new,root))
                self.assertFalse((root/transaction.JOURNAL).exists())
                self.assertFalse(list(root.glob('.book-switch-*')))
            finally:
                if child.poll() is None:child.kill()
                child.communicate(timeout=5)

    def test_reader_waits_for_pair_publication_and_observes_complete_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);snapshots.write_snapshots(application('old'),root);new=application('new')
            entered=threading.Event();release=threading.Event();reader_started=threading.Event()
            def pause(target):
                if Path(target).name=='openapi.json':
                    entered.set()
                    if not release.wait(3):raise RuntimeError('fixture release timed out')
            def check():
                reader_started.set();return snapshots.check_snapshots(new,root)
            with intercept_book_publications(transaction,after=pause),ThreadPoolExecutor(max_workers=2) as pool:
                writer=pool.submit(snapshots.write_snapshots,new,root)
                try:
                    self.assertTrue(entered.wait(2));reader=pool.submit(check)
                    self.assertTrue(reader_started.wait(2));self.assertFalse(reader.done())
                finally:release.set()
                writer.result(timeout=4);self.assertEqual([],reader.result(timeout=4))
