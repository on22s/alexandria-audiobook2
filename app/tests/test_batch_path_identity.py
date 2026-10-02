"""Batch input identity and worker-derived report provenance."""
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import script


class BatchPathIdentityTests(unittest.TestCase):
    def test_batch_routes_reject_transformed_names_and_directory_before_claim(self):
        names=('../secret.txt','folder/book.txt','book.txt')
        for route in ('preflight','start'):
            for name in names:
                with self.subTest(route=route,name=name),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp)
                    (root/'_secret.txt').write_text('Wrong transformed source.')
                    (root/'folder_book.txt').write_text('Wrong transformed source.')
                    (root/'book.txt').mkdir()
                    app=FastAPI();app.include_router(script.router)
                    with patch.object(script,'UPLOADS_DIR',tmp), \
                         patch.object(script,'three_pass_refusal',return_value=None), \
                         patch.object(script,'build_batch_script_preflight',return_value={'workers':1}), \
                         patch.object(script,'schedule_claimed_background_task') as claim, \
                         TestClient(app,raise_server_exceptions=False) as client:
                        response=client.post('/api/generate_script/batch/'+route,json={'tasks':[{'filename':name}]})
                    self.assertEqual(400,response.status_code,response.text);claim.assert_not_called()

    def test_existing_regular_unicode_and_space_names_and_epub_fallback_resolve_exactly(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(script,'UPLOADS_DIR',tmp):
            root=Path(tmp)
            for name in ('book.txt','日本語 book.md','converted.txt'):(root/name).write_text('Source text.')
            for name in ('book.txt','日本語 book.md'):
                self.assertEqual(str(root/name),script._resolve_batch_script_input(name))
            self.assertEqual(str(root/'converted.txt'),script._resolve_batch_script_input('converted.epub'))
            (root/'broken.txt').mkdir()
            for name in ('missing.txt','broken.epub','',r'folder\book.txt','book\x00.txt'):
                with self.subTest(name=name),self.assertRaises(ValueError):script._resolve_batch_script_input(name)

    def test_report_sources_match_actual_workers_and_exclude_skipped_names(self):
        for bidirectional in (False,True):
            with self.subTest(bidirectional=bidirectional),tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
                root=Path(tmp);library=root/'scripts';reports=root/'reports';library.mkdir()
                source=library/'_target.json';original=b'[{"speaker":"ALICE","text":"Keep all of this."}]';source.write_bytes(original)
                states=copy.deepcopy(core.process_state)
                for state in states.values():state['running']=False
                paths=[]
                def worker(command,*args,**kwargs):
                    paths.append(command[command.index('--input')+1])
                    return 0,['Review complete: 1 -> 1 entries','Total changes: 0']
                for owner,name,value in ((script,'process_state',states),(core,'process_state',states),
                                         (core,'_task_claims',{}),(core,'_gpu_leases',{}),
                                         (script,'SCRIPTS_DIR',str(library)),(script,'REPORTS_DIR',str(reports)),
                                         (script,'ROOT_DIR',tmp),(script,'CHARACTER_ALIASES_PATH',str(root/'aliases.json'))):
                    stack.enter_context(patch.object(owner,name,value))
                stack.enter_context(patch.object(core,'acquire_gpu_lock',return_value=None))
                stack.enter_context(patch.object(core,'llm_is_on_this_gpu',return_value=True))
                stack.enter_context(patch.object(script,'_init_task_log',return_value=str(root/'task.log')))
                stack.enter_context(patch.object(script,'_stream_subprocess_to_logs',side_effect=worker))
                stack.enter_context(patch.object(script,'_insert_llm_summary',side_effect=lambda lines,*a,**k:lines))
                app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(script.router)
                with TestClient(app) as client:
                    response=client.post('/api/review_script/batch/start',json={'script_names':['../target','missing'],
                                         'find_nicknames':bidirectional,'dedupe_speakers':bidirectional,
                                         'bidirectional':bidirectional})
                self.assertEqual(200,response.status_code,response.text);self.assertTrue(paths)
                self.assertTrue(all(path==str(source) for path in paths))
                artifacts=states['batch_review']['artifacts'];self.assertEqual(1,len(artifacts))
                self.assertEqual([str(source)],artifacts[0]['source_paths'])
                self.assertEqual(original,source.read_bytes());self.assertTrue(Path(artifacts[0]['artifact_path']).is_file())
                self.assertFalse(core.is_task_running('batch_review'));self.assertEqual({},core._task_claims)
