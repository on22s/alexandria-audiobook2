"""Creating native review reports must not start an optional LLM explanation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import core
from routers import script

class ReportCreationTests(unittest.TestCase):
    def test_both_report_writers_publish_without_calling_llm(self):
        stats=core._new_review_totals();stats.update(entries_before=2,entries_after=2,total_changes=0)
        state={'tasks':[{'name':'book','status':'done','stats_fwd':stats}], 'totals_fwd':stats}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(core,'REPORTS_DIR',directory), patch.object(script,'REPORTS_DIR',directory), \
             patch.object(core,'_llm_summarize_report',return_value='Optional provider response') as llm:
            single=core._write_single_review_report(stats)
            batch=script._write_batch_review_report(state,['book'],False,False)
            self.assertTrue(single);self.assertTrue(batch)
            llm.assert_not_called()
            for path in (single,batch):
                body=Path(path).read_text()
                self.assertIn('reported 0 change(s)',body)
                self.assertNotIn('Optional provider response',body)

class ReportPublicationTests(unittest.TestCase):
    def setUp(self):
        from review_report import save_review_report
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'report.md'
        self.summary='The report recorded 2 changes.'
        self.body='# Report\n\n## In Plain English\n\n'+self.summary+'\n\n## Counts\n\n2 changes\n\n## Warnings\n\nRead the examples.\n'
        save_review_report(self.path,self.body,self.summary,False)

    def test_replacement_preserves_all_other_report_text_and_allows_explicit_retry(self):
        from review_report import get_review_report_info,apply_review_report_explanation
        original=get_review_report_info(self.path)
        candidate='Inspect <quoted> examples, including --> and café.'
        apply_review_report_explanation(self.path,original['sha256'],candidate)
        changed=get_review_report_info(self.path)
        from review_report import _METADATA_MARKER
        encoded=self.path.read_text().rpartition(_METADATA_MARKER)[2][:-5]
        self.assertNotIn('-->',encoded);self.assertNotIn('<',encoded)
        self.assertEqual(self.body.replace(self.summary,candidate,1),changed['content'])
        self.assertEqual(candidate,changed['summary'])
        apply_review_report_explanation(self.path,changed['sha256'],'A second explicit explanation.')
        self.assertIn('A second explicit explanation.',get_review_report_info(self.path)['content'])

    def test_changed_incomplete_and_unsupported_metadata_refuse_without_writes(self):
        import json
        from review_report import get_review_report_info,apply_review_report_explanation,save_review_report,_METADATA_MARKER
        original=get_review_report_info(self.path)
        raw=self.path.read_bytes()
        for mode in ('changed','replaced','incomplete','version_bool','version_float','version_unknown','completion_tamper'):
            self.path.write_bytes(raw)
            expected_digest=original['sha256']
            if mode=='changed':self.path.write_text(self.path.read_text().replace('\n2 changes\n','\n3 changes\n'))
            elif mode=='replaced':save_review_report(self.path,self.body.replace('2','3'),self.summary.replace('2','3'),False)
            elif mode=='incomplete':
                save_review_report(self.path,self.body,self.summary,True)
                expected_digest=get_review_report_info(self.path)['sha256']
            else:
                text=self.path.read_text();body,_,encoded=text.rpartition(_METADATA_MARKER)
                meta=json.loads(encoded[:-5])
                if mode=='completion_tamper':meta['incomplete']=True
                else:meta['version']={'version_bool':True,'version_float':1.0,'version_unknown':2}[mode]
                self.path.write_text(body+_METADATA_MARKER+json.dumps(meta)+' -->\n')
            before=self.path.read_bytes()
            with self.subTest(mode=mode),self.assertRaises(ValueError):apply_review_report_explanation(self.path,expected_digest,'New summary')
            self.assertEqual(before,self.path.read_bytes())

    def test_failed_atomic_publication_retains_old_report_and_removes_staging(self):
        from review_report import get_review_report_info,apply_review_report_explanation
        original=get_review_report_info(self.path);before=self.path.read_bytes()
        with patch('review_report.os.replace',side_effect=OSError('fixture publication failure')):
            with self.assertRaises(OSError):apply_review_report_explanation(self.path,original['sha256'],'New summary')
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual([],list(self.path.parent.glob('.review_report_*')))

class ReportExplanationApiTests(unittest.TestCase):
    def setUp(self):
        import copy
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        from review_report import save_review_report
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.path=self.root/'review_fixture.md'
        self.body='# Report\n\n## In Plain English\n\nThe review recorded 2 changes.\n\n## Counts\n\n2 changes\n'
        save_review_report(self.path,self.body,'The review recorded 2 changes.',False)
        self.editor=editor;self.state=copy.deepcopy(core.process_state)
        for row in self.state.values():row['running']=False
        for target,name,value in [(core,'DATA_DIR',str(self.root)),(core,'process_state',self.state),(editor,'process_state',self.state),(editor,'REPORTS_DIR',str(self.root)),(core,'_task_claims',{}),(core,'_gpu_leases',{}),(core,'acquire_gpu_lock',lambda *a,**k:None),(core,'llm_is_on_this_gpu',lambda:True)]:
            context=patch.object(target,name,value);context.start();self.addCleanup(context.stop)
        app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(editor.router)
        self.client=TestClient(app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.addCleanup(self.release_claims)

    def release_claims(self):
        for name,owner in list(core._task_claims.items()):core.release_gpu_task_claim(name,owner['id'])

    def test_explicit_action_publishes_and_releases_claim(self):
        with patch.object(self.editor,'_llm_summarize_report',return_value='Inspect the two recorded changes.') as provider:
            response=self.client.post('/api/reports/review_fixture.md/explain')
        self.assertEqual(200,response.status_code,response.text)
        provider.assert_called_once_with(self.body)
        self.assertIn('Inspect the two recorded changes.',self.path.read_text())
        self.assertIn('## Counts\n\n2 changes',self.path.read_text())
        self.assertEqual('done',self.state['report_explanation']['status'])
        self.assertFalse(self.state['report_explanation']['running'])
        self.assertEqual({},core._task_claims)

    def test_queued_cancel_is_run_bound_and_never_calls_provider(self):
        import asyncio
        from fastapi import BackgroundTasks
        async def run():
            tasks=BackgroundTasks()
            response=await self.editor.explain_report('review_fixture.md',tasks)
            self.assertTrue(self.state['report_explanation']['running'])
            owner=core._task_claims['report_explanation']['id']
            self.assertEqual(owner,response['run_id'])
            wrong=self.client.post('/api/reports/explanation/cancel',json={'run_id':'other'})
            self.assertEqual(409,wrong.status_code)
            self.assertFalse(self.state['report_explanation']['cancel'])
            cancelled=self.client.post('/api/reports/explanation/cancel',json={'run_id':owner})
            self.assertEqual(200,cancelled.status_code,cancelled.text)
            self.assertEqual(owner,core._task_claims['report_explanation']['id'])
            await tasks()
        before=self.path.read_bytes()
        with patch.object(self.editor,'_llm_summarize_report') as provider:
            asyncio.run(run())
            provider.assert_not_called()
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual('cancelled',self.state['report_explanation']['status'])
        self.assertEqual({},core._task_claims)

    def test_failed_unsupported_cancelled_or_stale_explanation_preserves_report(self):
        from review_report import save_review_report
        for mode in ('unavailable','unsupported','exception','cancelled','stale'):
            save_review_report(self.path,self.body,'The review recorded 2 changes.',False);before=self.path.read_bytes()
            def provider(_):
                if mode=='exception':raise RuntimeError('fixture model failure')
                if mode=='cancelled':self.state['report_explanation']['cancel']=True
                if mode=='stale':self.path.write_text('Externally edited report')
                return None if mode=='unavailable' else ('Everything looks great.' if mode=='unsupported' else 'Inspect recorded changes.')
            with self.subTest(mode=mode),patch.object(self.editor,'_llm_summarize_report',side_effect=provider):
                response=self.client.post('/api/reports/review_fixture.md/explain')
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual(b'Externally edited report' if mode=='stale' else before,self.path.read_bytes())
                self.assertFalse(self.state['report_explanation']['running'])
                self.assertEqual({},core._task_claims)

    def test_incomplete_or_busy_refuses_before_provider_or_new_claim(self):
        from review_report import save_review_report
        save_review_report(self.path,self.body,'The review recorded 2 changes.',True)
        with patch.object(self.editor,'_llm_summarize_report') as provider:
            response=self.client.post('/api/reports/review_fixture.md/explain')
            self.assertEqual(409,response.status_code,response.text)
            self.assertEqual({},core._task_claims)
            save_review_report(self.path,self.body,'The review recorded 2 changes.',False)
            core.claim_gpu_task('audio')
            response=self.client.post('/api/reports/review_fixture.md/explain')
            self.assertEqual(400,response.status_code,response.text)
            self.assertNotIn('report_explanation',core._task_claims)
            provider.assert_not_called()
