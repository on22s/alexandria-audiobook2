"""Real disk recovery, source evidence and request-body regressions."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import find_nicknames
import generate_script as gs
import llm_provider as lp
import script_repair
import source_repair_paths as sr
import three_pass_generate as tp
from tests.test_three_pass_generate import _client_returning, _load_orchestration_fixture_cast

class ScriptRecoveryTests(unittest.TestCase):
    def test_publication_failure_preserves_prior_bytes(self):
        for existing in (False, True):
            for fail_at in (1, 2, 3):
                with self.subTest(existing=existing, fail_at=fail_at), tempfile.TemporaryDirectory() as tmp:
                    output, report = Path(tmp,'output.txt'), Path(tmp,'report.json')
                    if existing: output.write_bytes(b'prior\r\noutput')
                    save, calls = sr.save_repair_text, []
                    def fail(path,text):
                        calls.append(path)
                        if len(calls)==fail_at: raise OSError('injected publication failure')
                        return save(path,text)
                    with patch.object(sr,'save_repair_text',side_effect=fail), self.assertRaisesRegex(OSError,'publication'):
                        sr.save_source_repair_result(report,{},output,'fixed')
                    self.assertEqual(existing,output.exists())
                    if existing: self.assertEqual(b'prior\r\noutput',output.read_bytes())
                    if report.exists(): self.assertFalse(json.loads(report.read_text())['applied'])
                    self.assertEqual([],list(Path(tmp).glob('.*.tmp')))
                    sr.save_source_repair_result(report,{},output,'fixed')
                    self.assertEqual('fixed',output.read_text())
                    self.assertTrue(json.loads(report.read_text())['applied'])

    def test_failed_rollback_keeps_recoverable_prior_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, report = Path(tmp, 'output.txt'), Path(tmp, 'report.json')
            output.write_bytes(b'prior bytes')
            save, replace, calls = sr.save_repair_text, Path.replace, []
            def fail_final(path, text):
                calls.append(path)
                if len(calls) == 3:
                    raise OSError('final report failure')
                return save(path, text)
            def fail_rollback(path, target):
                if '.backup.' in path.name:
                    raise OSError('rollback replace failure')
                return replace(path, target)
            with patch.object(sr, 'save_repair_text', side_effect=fail_final), \
                 patch.object(Path, 'replace', autospec=True, side_effect=fail_rollback), \
                 self.assertRaisesRegex(OSError, 'prior output preserved at'):
                sr.save_source_repair_result(report, {}, output, 'fixed')
            backups = list(Path(tmp).glob('.*.backup.*.tmp'))
            self.assertEqual(1, len(backups))
            self.assertEqual(b'prior bytes', backups[0].read_bytes())
            self.assertEqual('fixed', output.read_text())

    def test_alias_evidence_uses_accepted_variant(self):
        for key in ('BOBBIE','bobbie',' Bobbie '):
            aliases,evidence=find_nicknames._parse_alias_response(json.dumps({'aliases':{key:'ROBERT'},'evidence':{key:'Named Bobbie'}}),['BOBBIE','ROBERT'])
            self.assertEqual({'BOBBIE':'ROBERT'},aliases)
            self.assertEqual({'BOBBIE':'Named Bobbie'},evidence)
        for aliases in ({'missing':'ROBERT'},{'BOBBIE':'BOBBIE'},{'bobbie':'ROBERT','BOBBIE':'ALICE'}):
            safe,evidence=find_nicknames._parse_alias_response(json.dumps({'aliases':aliases,'evidence':{k:'Rejected' for k in aliases}}),['BOBBIE','ROBERT','ALICE'])
            self.assertEqual({},evidence)

    def test_nested_split_provenance(self):
        attempts=[]
        def process(*args,**kwargs):
            attempt={'length':len(args[2])};kwargs['attempt_observer'](attempt);attempt['outcome']='quality_rejected';return []
        def split(text): return [text[:len(text)//2],text[len(text)//2:]] if len(text)>1000 else None
        with patch.object(gs,'process_chunk',side_effect=process),patch.object(gs,'split_failed_chunk',side_effect=split):
            gs.process_chunk_adaptively(object(),'fixture','x'*4000,1,1,gs.LLMGenParams(),attempt_observer=attempts.append)
        self.assertEqual([[1,1],[1,2],[2,1],[2,2]],[a['split_path'] for a in attempts if a['length']==1000])
        self.assertEqual('full',attempts[0]['phase'])
        self.assertEqual([1,2],[a['split_part'] for a in attempts if a['length']==2000])
        self.assertTrue(all(a['outcome']=='quality_rejected' for a in attempts))

    def test_reasoning_sampling_in_serialized_sdk_body(self):
        import httpx
        from openai import OpenAI
        for model,effort,removed in (('o1','medium',True),('gpt-5.1','none',False),('local','medium',False)):
            request={'model':model,'messages':[{'role':'user','content':'hello'}],'temperature':.5,'extra_body':{'temperature':.2,'top_p':.8,'reasoning_effort':effort}}
            before=json.dumps(request,sort_keys=True);bodies=[]
            def transport(req):
                bodies.append(json.loads(req.content))
                return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':model,'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]})
            with OpenAI(api_key='fixture',http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
                client.chat.completions.create(**lp.adapt_request_for_reasoning_model(request))
            self.assertEqual(before,json.dumps(request,sort_keys=True))
            for key in ('temperature','top_p'): self.assertEqual(not removed,key in bodies[0])
            if not removed: self.assertEqual(.2,bodies[0]['temperature'])

    def test_foreign_owner_process_generation(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        pending={'owner_pid':child.pid,'owner_thread':1}
        try:
            self.assertFalse(lp.is_manual_request_owner_alive(pending))
            pending['owner_process_identity']=lp.get_manual_process_identity(child.pid)
            self.assertTrue(lp.is_manual_request_owner_alive(pending))
            pending['owner_process_identity']+='-stale'
            self.assertFalse(lp.is_manual_request_owner_alive(pending))
            self.assertIsNone(child.poll())
        finally: child.terminate();child.wait(timeout=5)
        self.assertFalse(lp.is_manual_request_owner_alive(pending))

    def test_italicized_cyrillic_and_unknown_word(self):
        for source in ('тест','_тест_','Narration _тест_ here'):
            result=script_repair.build_deterministic_repair([{'text':'тест'}],source)
            self.assertEqual([],result['unresolved']);self.assertEqual([{'text':'тест'}],result['entries'])
        self.assertTrue(script_repair.build_deterministic_repair([{'text':'тест'}],'unrelated source')['unresolved'])

    def test_periodic_duplicate_noncontiguous_evidence(self):
        a,b='alpha bravo charlie delta echo one','foxtrot golf hotel india juliet two'
        entries=[{'text':text} for text in [a,b]*4]
        result=script_repair.build_deterministic_repair(entries,a+' narrator speaks between them '+b)
        self.assertEqual(entries[:2],result['entries']);self.assertEqual([],result['unresolved'])
        unsupported=script_repair.build_deterministic_repair(entries,a)
        self.assertEqual(entries,unsupported['entries']);self.assertTrue(unsupported['unresolved'])
        faithful=script_repair.build_deterministic_repair(entries,' '.join([a,b]*4))
        self.assertEqual(entries,faithful['entries']);self.assertEqual([],faithful['changes'])

    def test_attribution_resume_retries_and_replaces_diagnostic(self):
        for succeeds in (False,True):
            with self.subTest(succeeds=succeeds),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                out=str(Path(tmp,'book.json'));params=gs.LLMGenParams(max_tokens=500,segmentation='quotes',structured_output='off');cast=_load_orchestration_fixture_cast(('ALICE',))
                def run(): return tp.run_three_pass(_client_returning([]),'m','"Hello."',params,3000,output_path=out,collect_all_failures=True,cast=cast)
                with patch.object(tp,'attribute_batch_voted',side_effect=tp.PassExhausted) as worker:
                    run();self.assertEqual(1,worker.call_count)
                options={'return_value':([{'speaker':'ALICE','text':'Hello.'}],{})} if succeeds else {'side_effect':tp.PassExhausted}
                with patch.object(tp,'attribute_batch_voted',**options) as worker:
                    run();self.assertEqual(1,worker.call_count)
                manifest=json.loads(Path(tp.three_pass_manifest_path(out)).read_text())
                self.assertEqual(0 if succeeds else 1,len([f for f in manifest['diagnostic_failures'] if f['pass']=='attribute']))
                if not succeeds: self.assertEqual('incomplete',manifest['status'])

    def test_duplicate_cast_rejected_valid_alias_converted(self):
        for name in ('Alice',' alice ','ALICE'):
            with self.assertRaisesRegex(ValueError,'duplicate'): tp.get_cast_from_data([{'name':'Alice','aliases':['Ally']},{'name':name,'aliases':['Alicia']}])
        cast=tp.get_cast_from_data([{'name':'Alice','aliases':['Ally','Alicia']}])
        self.assertEqual('ALICE',tp.get_named_from_answer([{'type':'SPOKEN','text':'Hello.'}],[{'speaker':'ALICIA'}],cast)[0]['speaker'])
        with self.assertRaises(ValueError): tp.get_cast_from_data([{'name':'Alice','aliases':'wrong'}])
