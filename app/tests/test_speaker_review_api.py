"""Synthetic speaker review API and transport regressions; no real provider calls."""
import os, sys, json, tempfile, socket, copy, unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
import routers.speaker_review as r
import speaker_review as m
import three_pass_generate as t
from utils import atomic_json_write
import httpx, llm_provider
c = r.core
app = FastAPI(); app.include_router(r.router)
client = TestClient(app)


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value), encoding='utf-8')


def fixture(name='book', pairs=1, checkpoint=False, partial=False):
    for p in [c.CHARACTER_ALIASES_PATH, c.CHUNKS_PATH, c.VOICE_CONFIG_PATH, r._checkpoint_path()]:
        Path(p).unlink(missing_ok=True)
    rows = [{'text': f'Synthetic sentence {i}.', 'type':'SPOKEN'} for i in range(pairs)]
    named = t.get_named_from_answer(rows, [{'speaker': f'ALICE{i}'} for i in range(pairs)])
    assert all('type' not in row for row in named), 'Production checkpoint adapter must be tested'
    script = [{**row, 'instruct':'Natural.'} for row in named]
    source = Path(c.DATA_DIR)/'synthetic.txt'; source.write_text('\n\n'.join(row['text'] for row in rows))
    state = {'active_book_id':name,'book_generation':name,'input_file_path':str(source)}
    save(Path(c.DATA_DIR)/'state.json',state)
    save(c.SCRIPT_PATH,script)
    save(Path(c.SCRIPTS_DIR)/'reference.json',[{**row, 'speaker':f'AL{i}'} for i,row in enumerate(script)])
    save(c.CONFIG_PATH,{'llm_mode':'remote','llm_remote':{
        'model_name':'synthetic-model','base_url':'https://example.invalid/v1','api_key':'synthetic-secret',
        'provider_headers':{'X-Test':'synthetic-secret'},'on_this_gpu':False,'context_length':32768}})
    if checkpoint:
        source_text, _ = t.get_prepared_source(str(source))
        fingerprint = t.three_pass_fingerprint(source_text,'synthetic-model',100)
        t._save_three_pass_checkpoint(c.SCRIPT_PATH,fingerprint,'attribute' if partial else 'done',rows,1,
                                     named[:1] if partial else named,[] if partial else script)
    return {'source':'checkpoint' if checkpoint else 'current','reference_name':'reference',
            'max_pairs':20,'allow_partial':partial}

class Fake:
    def close(self): pass

class SpeakerReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths = {'DATA_DIR': self.tmp.name, 'SCRIPTS_DIR': str(Path(self.tmp.name, 'scripts')),
                 'SCRIPT_PATH': str(Path(self.tmp.name, 'annotated_script.json')),
                 'CONFIG_PATH': str(Path(self.tmp.name, 'config.json')),
                 'CHARACTER_ALIASES_PATH': str(Path(self.tmp.name, 'character_aliases.json')),
                 'CHUNKS_PATH': str(Path(self.tmp.name, 'chunks.json')),
                 'VOICE_CONFIG_PATH': str(Path(self.tmp.name, 'voice_config.json'))}
        for key, value in paths.items():
            change = patch.object(c, key, value)
            change.start()
            self.addCleanup(change.stop)
        Path(c.SCRIPTS_DIR).mkdir()
        for target in ((socket.socket, 'connect'), (socket, 'create_connection')):
            guard = patch.object(*target, side_effect=AssertionError('REAL NETWORK FORBIDDEN'))
            guard.start()
            self.addCleanup(guard.stop)
        self.calls = []
        def make(profile):
            def review(candidate,mode):
                self.calls.append((candidate['id'],mode))
                return {'same_identity':True,'reason':'Synthetic evidence only'}
            return Fake(), review
        self.make_patch=patch.object(r,'make_reviewer',side_effect=make); self.make_patch.start()
    def tearDown(self):
        self.make_patch.stop()
        if c.is_task_running(r.TASK): c.release_gpu_task_claim(r.TASK)
    def preview(self,sel):
        response=client.post('/api/speaker_review/preview',json=sel)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def start(self,sel,p):
        response=client.post('/api/speaker_review/start',json={**sel,'snapshot':p['snapshot'],'allow_network':True})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['run_id']
    def test_native_saved_script_api_lifecycle(self):
        sel=fixture('native'); p=self.preview(sel)
        self.assertEqual(len(self.calls),0); self.assertIn('native narrator labels',p['source_alignment'])
        self.assertNotIn('synthetic-secret',json.dumps(p))
        rid=self.start(sel,p); self.assertEqual(len(self.calls),2)
        report=client.get('/api/speaker_review/'+rid).json()
        self.assertEqual(report['status'],'completed'); self.assertTrue(report['candidates'][0]['can_apply'])
        self.assertFalse(Path(c.CHARACTER_ALIASES_PATH).exists())
        cand=p['candidates'][0]; payload={'snapshot':p['snapshot'],'candidate_id':cand['id'],'alias':'ALICE0','canonical':'AL0'}
        response=client.post('/api/speaker_review/'+rid+'/apply',json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(json.loads(Path(c.CHARACTER_ALIASES_PATH).read_text()),{'ALICE0':'AL0'})
        self.assertEqual(client.post('/api/speaker_review/'+rid+'/apply',json=payload).status_code,409)
    def test_real_checkpoint_prefix_missing_native_types(self):
        sel=fixture('checkpoint',pairs=2,checkpoint=True,partial=True)
        p=self.preview(sel); self.assertEqual(p['entries'],1); self.assertEqual(p['phase'],'provisional')
        self.assertEqual(p['candidates'][0]['context'][0]['type'],'SPOKEN')
        self.assertEqual(client.post('/api/speaker_review/preview',json={**sel,'allow_partial':False}).status_code,409)
        self.start(sel,p); self.assertEqual(len(self.calls),2)
    def test_real_completed_checkpoint_script_alignment(self):
        sel=fixture('complete',checkpoint=True)
        p=self.preview({**sel,'source':'current'}); self.assertEqual(p['source_alignment'],'frozen generation text/type')
        script=json.loads(Path(c.SCRIPT_PATH).read_text()); script[0]['text']='Changed'; save(c.SCRIPT_PATH,script)
        self.assertEqual(client.post('/api/speaker_review/preview',json={**sel,'source':'current'}).status_code,409)
    def test_wrong_checkpoint_source_and_type_rejected(self):
        sel=fixture('wrongsource',checkpoint=True)
        Path(c.DATA_DIR,'synthetic.txt').write_text('A different synthetic book')
        self.assertEqual(client.post('/api/speaker_review/preview',json=sel).status_code,409)
        fixture('wrongtype',checkpoint=True)
        checkpoint=json.loads(Path(r._checkpoint_path()).read_text()); checkpoint['named'][0]['type']='NARRATOR'
        save(r._checkpoint_path(),checkpoint)
        self.assertEqual(client.post('/api/speaker_review/preview',json=sel).status_code,409)
    def test_model_free_preview_unavailable_profile(self):
        sel=fixture('manual'); save(c.CONFIG_PATH,{'llm':{'transport':'manual'}})
        p=self.preview(sel); self.assertFalse(p['profile']['available']); self.assertEqual(len(p['candidates']),1)
        response=client.post('/api/speaker_review/start',json={**sel,'snapshot':p['snapshot'],'allow_network':True})
        self.assertEqual(response.status_code,409); self.assertEqual(self.calls,[])
    def test_requires_explicit_consent_and_fresh_preview(self):
        sel=fixture('consent'); p=self.preview(sel)
        response=client.post('/api/speaker_review/start',json={**sel,'snapshot':p['snapshot']})
        self.assertEqual(response.status_code,400)
        response=client.post('/api/speaker_review/start',json={**sel,'snapshot':'0'*64,'allow_network':True})
        self.assertEqual(response.status_code,409); self.assertEqual(self.calls,[])
    def test_unique_reference_matching_and_evidence_bounds(self):
        sel=fixture('unique'); ref=Path(c.SCRIPTS_DIR,'reference.json'); rows=json.loads(ref.read_text());save(ref,rows+rows)
        p=self.preview(sel); self.assertEqual(p['candidates'],[]);self.assertEqual(p['skipped'][0]['reason'],'ambiguous')
        fixture('huge'); script=[{'speaker':'ALICE','text':'z'*5000,'instruct':'Natural'}];save(c.SCRIPT_PATH,script);save(ref,[dict(script[0],speaker='AL')])
        p=self.preview(sel);self.assertEqual(p['candidates'],[]);self.assertEqual(p['skipped'][0]['reason'],'evidence_too_large')
    def test_attempt_ceiling_and_resume_cache(self):
        sel=fixture('cap',pairs=25); p=self.preview(sel);self.assertEqual(len(p['candidates']),20)
        rid=self.start(sel,p);self.assertEqual(len(self.calls),40)
        self.start(sel,p);self.assertEqual(len(self.calls),40)
        report=client.get('/api/speaker_review/'+rid).json();self.assertEqual(report['attempts'],40)
        self.assertTrue(all(row['cached'] for row in report['reviews']))
    def test_failed_calls_do_not_leak_or_retry_past_ceiling(self):
        sel=fixture('failurecap',pairs=20); p=self.preview(sel)
        def bad(profile):
            def review(candidate,mode):
                self.calls.append(mode);raise RuntimeError('synthetic-secret private provider payload')
            return Fake(),review
        with patch.object(r,'make_reviewer',side_effect=bad):
            rid=self.start(sel,p);self.start(sel,p)
        self.assertEqual(len(self.calls),40)
        report=client.get('/api/speaker_review/'+rid).json();self.assertNotIn('synthetic-secret',json.dumps(report))
        self.assertFalse(any(row['can_apply'] for row in report['candidates']))
    def test_disagreement_prevents_apply(self):
        sel=fixture('disagreement');p=self.preview(sel)
        def different(profile):
            return Fake(),lambda candidate,mode: {'same_identity': mode=='none','reason':'Synthetic disagreement'}
        with patch.object(r,'make_reviewer',side_effect=different):rid=self.start(sel,p)
        report=client.get('/api/speaker_review/'+rid).json();self.assertFalse(report['candidates'][0]['can_apply'])
        response=client.post('/api/speaker_review/'+rid+'/apply',json={'snapshot':p['snapshot'],'candidate_id':p['candidates'][0]['id'],'alias':'ALICE0','canonical':'AL0'})
        self.assertEqual(response.status_code,409)
    def test_each_changed_revision_refuses_apply_without_relabeling(self):
        for target in ('state', 'script', 'reference', 'profile', 'aliases', 'checkpoint'):
            with self.subTest(target=target):
                sel = fixture('stale_' + target)
                preview = self.preview(sel)
                run_id = self.start(sel, preview)
                candidate = preview['candidates'][0]
                paths = {'state': Path(c.DATA_DIR, 'state.json'), 'script': Path(c.SCRIPT_PATH),
                         'reference': Path(c.SCRIPTS_DIR, 'reference.json'),
                         'profile': Path(c.CONFIG_PATH), 'aliases': Path(c.CHARACTER_ALIASES_PATH),
                         'checkpoint': Path(r._checkpoint_path())}
                path = paths[target]
                if target in ('script', 'reference'):
                    rows = json.loads(path.read_text()); rows[0]['speaker'] = 'CHANGED'; save(path, rows)
                elif target == 'state':
                    state = json.loads(path.read_text()); state['book_generation'] = 'CHANGED'; save(path, state)
                elif target == 'profile':
                    config = json.loads(path.read_text()); config['llm_remote']['model_name'] = 'CHANGED'; save(path, config)
                elif target == 'aliases':
                    save(path, {'OTHER': 'NAME'})
                else:
                    save(path, {'stage': 'invalid'})
                before_script = Path(c.SCRIPT_PATH).read_bytes()
                before_aliases = Path(c.CHARACTER_ALIASES_PATH).read_bytes() if Path(c.CHARACTER_ALIASES_PATH).exists() else None
                response = client.post('/api/speaker_review/' + run_id + '/apply', json={
                    'snapshot': preview['snapshot'], 'candidate_id': candidate['id'],
                    'alias': 'ALICE0', 'canonical': 'AL0'})
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before_script, Path(c.SCRIPT_PATH).read_bytes())
                self.assertEqual(before_aliases, Path(c.CHARACTER_ALIASES_PATH).read_bytes()
                                 if Path(c.CHARACTER_ALIASES_PATH).exists() else None)

    def test_existing_alias_disables_only_owned_direction(self):
        sel = fixture('existing_alias')
        save(c.CHARACTER_ALIASES_PATH, {'ALICE0': 'OTHER'})
        preview = self.preview(sel)
        run_id = self.start(sel, preview)
        report = client.get('/api/speaker_review/' + run_id).json()
        self.assertEqual('completed', report['status'])
        self.assertTrue(report['candidates'][0]['can_apply'])
        directions = report['candidates'][0]['apply_directions']
        self.assertFalse(directions[0]['can_apply'])
        self.assertTrue(directions[1]['can_apply'])
        self.assertIn('already has a saved alias', directions[0]['apply_refusal'])
        self.assertEqual({'ALICE0': 'OTHER'}, json.loads(Path(c.CHARACTER_ALIASES_PATH).read_text()))

    def test_directional_eligibility_matches_guarded_apply(self):
        for aliases, expected in (({'ALICE0': 'OTHER'}, [False, True]),
                                  ({'AL0': 'OTHER'}, [True, False]),
                                  ({'ALICE0': 'OTHER', 'AL0': 'OTHER'}, [False, False]),
                                  ({'AL0': 'ALICE0'}, [False, False])):
            with self.subTest(aliases=aliases):
                sel = fixture('direction_' + json.dumps(aliases, sort_keys=True))
                save(c.CHARACTER_ALIASES_PATH, aliases)
                preview = self.preview(sel)
                run_id = self.start(sel, preview)
                report = client.get('/api/speaker_review/' + run_id).json()
                candidate = report['candidates'][0]
                self.assertEqual(expected, [item['can_apply'] for item in candidate['apply_directions']])
                self.assertEqual(any(expected), candidate['can_apply'])
                before_script = Path(c.SCRIPT_PATH).read_bytes()
                for item in candidate['apply_directions']:
                    if not item['can_apply']:
                        response = client.post('/api/speaker_review/' + run_id + '/apply', json={
                            'snapshot': preview['snapshot'], 'candidate_id': candidate['id'],
                            'alias': item['alias'], 'canonical': item['canonical']})
                        self.assertEqual(409, response.status_code)
                        self.assertEqual(aliases, json.loads(Path(c.CHARACTER_ALIASES_PATH).read_text()))
                if any(expected):
                    item = next(item for item in candidate['apply_directions'] if item['can_apply'])
                    response = client.post('/api/speaker_review/' + run_id + '/apply', json={
                        'snapshot': preview['snapshot'], 'candidate_id': candidate['id'],
                        'alias': item['alias'], 'canonical': item['canonical']})
                    self.assertEqual(200, response.status_code, response.text)
                    stale = client.get('/api/speaker_review/' + run_id).json()
                    self.assertTrue(stale['stale'])
                    self.assertFalse(any(item['can_apply'] for item in stale['candidates'][0]['apply_directions']))
                self.assertEqual(before_script, Path(c.SCRIPT_PATH).read_bytes())

    def test_protected_labels_and_cycles(self):
        candidate={'id':'x','labels':['ALICE','NARRATOR']};reviews=[{'candidate_id':'x','mode':mode,'verdict':{'same_identity':True,'reason':'x'}} for mode in m.MODES]
        with self.assertRaises(ValueError):m.require_alias_pair(candidate,reviews,'ALICE','NARRATOR',{})
        candidate['labels']=['ALICE','BOB']
        with self.assertRaises(ValueError):m.require_alias_pair(candidate,reviews,'ALICE','BOB',{'BOB':'ALICE'})
    def test_transport_caps_reasoning_adaptation_and_no_retries(self):
        profile={'model_name':'gpt-5.4','base_url':'https://example.invalid/v1','api_key':'synthetic-secret',
                 'provider_headers':{'X-Test':'synthetic-secret'},'provider_extra_body':{'max_completion_tokens':999999}}
        requests=[]; status=[200]; finish=['stop']
        def handler(request):
            requests.append(request);body=json.loads(request.content)
            return httpx.Response(status[0],json={'id':'synthetic','object':'chat.completion','created':1,'model':'gpt-5.4','choices':[{'index':0,'finish_reason':finish[0],'message':{'role':'assistant','content':json.dumps({'same_identity':True,'reason':'Synthetic verdict'})}}]})
        real=llm_provider.OpenAI
        def create(**kwargs):return real(http_client=httpx.Client(transport=httpx.MockTransport(handler)),**kwargs)
        candidate={'id':'x','entry_index':0,'labels':['ALICE','AL'],'prediction_label':'ALICE','reference_label':'AL','context':[],'reference_context':[]}
        with patch.object(llm_provider,'OpenAI',side_effect=create):
            transport,review=m.make_reviewer(profile)
            try:
                review(candidate,'none');review(candidate,'low')
                bodies=[json.loads(req.content) for req in requests]
                self.assertEqual([b['max_completion_tokens'] for b in bodies],[512,1024])
                self.assertEqual([b['reasoning_effort'] for b in bodies],['none','low'])
                self.assertNotIn('temperature',bodies[1]);self.assertEqual(requests[0].headers['x-test'],'synthetic-secret')
                status[0]=500
                with self.assertRaises(Exception):review(candidate,'none')
                self.assertEqual(len(requests),3)
                status[0]=200;finish[0]='length'
                with self.assertRaises(ValueError):review(candidate,'low')
                self.assertEqual(len(requests),4)
            finally:transport.close()

if __name__ == '__main__':
    unittest.main()
