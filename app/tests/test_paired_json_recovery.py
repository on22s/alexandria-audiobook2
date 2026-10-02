import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import utils


class PairedJsonRecoveryTests(unittest.TestCase):
    def test_failed_second_replace_restores_prior_files_or_absence_and_cleans_own_artifacts(self):
        for existing in (False,True):
            with self.subTest(existing=existing),tempfile.TemporaryDirectory() as tmp:
                first,second=Path(tmp,'first.json'),Path(tmp,'second.json')
                if existing:first.write_bytes(b'{"old":1}')
                second.write_bytes(b'{"old":2}')
                historical=Path(str(first)+'.pair-backup');historical.write_bytes(b'older recovery')
                replace=os.replace
                def publish(source,target):
                    if Path(target)==second and Path(source).name.startswith('.pair-'):
                        raise PermissionError('second publish blocked')
                    return replace(source,target)
                with utils.file_lock(first),utils.file_lock(second),patch.object(utils.os,'replace',side_effect=publish):
                    with self.assertRaisesRegex(PermissionError,'second publish blocked'):
                        utils.atomic_json_write_pair({'new':1},str(first),{'new':2},str(second))
                self.assertEqual(existing,first.exists())
                if existing:self.assertEqual(b'{"old":1}',first.read_bytes())
                self.assertEqual(b'{"old":2}',second.read_bytes())
                self.assertEqual(b'older recovery',historical.read_bytes())
                self.assertEqual([],list(Path(tmp).glob('.pair-*')))

    def test_failed_rollback_retains_original_bytes_and_identifies_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            first,second=Path(tmp,'first.json'),Path(tmp,'second.json')
            first.write_bytes(b'{"old":1}');second.write_bytes(b'{"old":2}')
            replace=os.replace
            def publish(source,target):
                if Path(target)==second:raise PermissionError('second publish blocked')
                if Path(source).name.startswith('.pair-backup-'):raise PermissionError('rollback blocked')
                return replace(source,target)
            with utils.file_lock(first),utils.file_lock(second),patch.object(utils.os,'replace',side_effect=publish):
                with self.assertRaisesRegex(RuntimeError,'Paired JSON rollback failed') as error:
                    utils.atomic_json_write_pair({'new':1},str(first),{'new':2},str(second))
            backups=list(Path(tmp).glob('.pair-backup-*'))
            self.assertEqual(1,len(backups))
            self.assertEqual(b'{"old":1}',backups[0].read_bytes())
            self.assertIn(str(backups[0]),str(error.exception))
            self.assertEqual(b'{"old":2}',second.read_bytes())

    def test_serialization_failure_leaks_no_stage_and_never_changes_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            first,second=Path(tmp,'first.json'),Path(tmp,'second.json')
            first.write_bytes(b'{"old":1}');second.write_bytes(b'{"old":2}')
            with self.assertRaises(TypeError):
                utils.atomic_json_write_pair({'new':1},str(first),{'bad':object()},str(second))
            self.assertEqual(b'{"old":1}',first.read_bytes())
            self.assertEqual(b'{"old":2}',second.read_bytes())
            self.assertEqual([],list(Path(tmp).glob('.pair-*')))

    def test_interrupt_between_replacements_restores_pair_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            first,second=Path(tmp,'first.json'),Path(tmp,'second.json')
            first.write_bytes(b'{"old":1}');second.write_bytes(b'{"old":2}')
            replace=os.replace
            def publish(source,target):
                if Path(target)==second:raise KeyboardInterrupt()
                return replace(source,target)
            with patch.object(utils.os,'replace',side_effect=publish):
                with self.assertRaises(KeyboardInterrupt):
                    utils.atomic_json_write_pair({'new':1},str(first),{'new':2},str(second))
            self.assertEqual(b'{"old":1}',first.read_bytes())
            self.assertEqual(b'{"old":2}',second.read_bytes())
            self.assertEqual([],list(Path(tmp).glob('.pair-*')))

    def test_actual_voice_suggestion_caller_waits_for_native_owner_of_both_locks(self):
        import contextlib
        import subprocess
        import sys
        import threading
        from routers import voices
        with tempfile.TemporaryDirectory() as tmp:
            first,second=Path(tmp,'config.json'),Path(tmp,'library.json')
            first.write_text('{}');second.write_text('{"casts":{"series":{"members":{}}},"shared":{},"favorites":[]}')
            before=(first.read_bytes(),second.read_bytes())
            child=subprocess.Popen([sys.executable,'-c',
                "import sys;from utils import file_lock\n"
                "with file_lock(sys.argv[1]),file_lock(sys.argv[2]):\n"
                " print('READY',flush=True);sys.stdin.readline()\n",str(second),str(first)],
                env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            entered=threading.Event(); results=[]; errors=[]
            @contextlib.contextmanager
            def lock(path):
                entered.set()
                with utils.file_lock(path):yield
            def apply():
                try:results.append(voices._apply_voice_suggestions({},'series'))
                except BaseException as error:errors.append(error)
            thread=threading.Thread(target=apply)
            try:
                self.assertEqual('READY',child.stdout.readline().strip())
                with patch.object(voices,'VOICE_CONFIG_PATH',str(first)), \
                     patch.object(voices,'VOICE_LIBRARY_PATH',str(second)), \
                     patch.object(voices,'file_lock',lock), \
                     patch.object(voices,'_load_voice_library',side_effect=lambda:json.loads(second.read_text())), \
                     patch.object(voices,'_build_lora_candidates',return_value=[]), \
                     patch.object(voices,'_script_line_counts',return_value={}), \
                     patch.object(voices,'get_active_book_id',return_value='fixture'):
                    thread.start();self.assertTrue(entered.wait(5));self.assertTrue(thread.is_alive())
                    self.assertEqual(before,(first.read_bytes(),second.read_bytes()))
                    child.communicate('release\n',timeout=5);self.assertEqual(0,child.returncode)
                    thread.join(timeout=5);self.assertFalse(thread.is_alive());self.assertEqual([],errors)
                self.assertEqual(0,results[0]['count'])
                self.assertEqual({},json.loads(first.read_bytes()))
                self.assertIn('series',json.loads(second.read_bytes())['casts'])
                self.assertEqual([],list(Path(tmp).glob('.pair-*')))
            finally:
                if child.poll() is None:child.kill();child.communicate(timeout=5)
                if thread.is_alive():thread.join(timeout=10)
