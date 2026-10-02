"""Public alias corrections govern native batch dedupe."""
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
import review_script
from routers import script


class BatchAliasRegistryTests(unittest.TestCase):
    def test_corrected_and_removed_aliases_govern_single_and_bidirectional_batches(self):
        for aliases in ({},{'BOB':'ROBERT'}):
            for bidirectional in (False,True):
                with self.subTest(aliases=aliases,bidirectional=bidirectional),tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
                    root=Path(tmp);library=root/'scripts';library.mkdir();public=root/'character_aliases.json'
                    legacy=library/'.series_aliases.json';legacy.write_text('{"BOB":"ALICE"}');old=legacy.read_bytes()
                    book=library/'book.json';book.write_text(json.dumps([{'speaker':name,'text':name+' speaks.'} for name in ('BOB','ALICE','ROBERT')]))
                    states=copy.deepcopy(core.process_state)
                    for state in states.values():state['running']=False
                    paths=[]
                    def worker(command,*args,**kwargs):
                        if '--aliases-file' in command:
                            paths.append(command[command.index('--aliases-file')+1])
                            return 0,[]
                        path=command[command.index('--alias-registry')+1];paths.append(path)
                        entries=json.loads(book.read_text())
                        mapping,renamed,changes=review_script.dedupe_speakers(None,'CPU stand-in',entries,registry_path=path)
                        for index,key,value in changes:entries[index][key]=value
                        script.atomic_json_write(entries,str(book))
                        return 0,['Review complete: 3 -> 3 entries','Total changes: '+str(renamed)]
                    for owner,name,value in ((script,'process_state',states),(core,'process_state',states),
                                             (core,'_task_claims',{}),(core,'_gpu_leases',{}),
                                             (script,'SCRIPTS_DIR',str(library)),(script,'CHARACTER_ALIASES_PATH',str(public)),
                                             (script,'REPORTS_DIR',str(root/'reports')),(script,'ROOT_DIR',tmp)):
                        stack.enter_context(patch.object(owner,name,value))
                    stack.enter_context(patch.object(core,'acquire_gpu_lock',return_value=None))
                    stack.enter_context(patch.object(core,'llm_is_on_this_gpu',return_value=True))
                    stack.enter_context(patch.object(script,'_init_task_log',return_value=str(root/'task.log')))
                    stack.enter_context(patch.object(script,'_stream_subprocess_to_logs',side_effect=worker))
                    stack.enter_context(patch.object(script,'_insert_llm_summary',side_effect=lambda lines,*a,**k:lines))
                    stack.enter_context(patch.object(review_script,'call_llm_for_object',return_value={}))
                    app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(script.router)
                    with TestClient(app) as client:
                        self.assertEqual(200,client.post('/api/character_aliases',json=aliases).status_code)
                        response=client.post('/api/review_script/batch/start',json={'script_names':['book'],
                                             'dedupe_speakers':True,'find_nicknames':bidirectional,'bidirectional':bidirectional})
                        self.assertEqual(200,response.status_code,response.text)
                        self.assertEqual(aliases,client.get('/api/character_aliases').json())
                    self.assertTrue(paths);self.assertTrue(all(path==str(public) for path in paths))
                    self.assertEqual('ROBERT' if aliases else 'BOB',json.loads(book.read_text())[0]['speaker'])
                    self.assertEqual(old,legacy.read_bytes());self.assertFalse(core.is_task_running('batch_review'))
