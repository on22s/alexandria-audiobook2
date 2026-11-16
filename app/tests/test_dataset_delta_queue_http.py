"""Actual Node handlers/API queue over owned HTTP to native FastAPI row storage."""
import json
from http.server import ThreadingHTTPServer
from tests.test_support import FramedHttpFixtureHandler
import threading
import time
import unittest
from fastapi.testclient import TestClient
from tests import test_dataset_ui_ownership_js as ui
from tests import test_dataset_builder_ownership as native


HELD_SAVE_SCENARIO = r'''
async function withHeldSave(text, edit, whileHeld) {
    const previousFetch=context.fetch;
    let arrive,release;
    const arrived=new Promise(resolve=>arrive=resolve);
    const released=new Promise(resolve=>release=resolve);
    context.fetch=async(url,request)=>{
        const response=await previousFetch(url,request);
        if(request&&JSON.parse(request.body).edits?.[0].row.text===text){
            setScenarioPhase('held-save-response');
            arrive();
            await released;
        }
        return response;
    };
    let first,joined;
    try {
        edit();
        first=run('dsbSaveRowsQueue.flush()');
        await Promise.race([arrived,first.then(()=>{
            throw new Error('Save finished without reaching the held response');
        })]);
        joined=whileHeld();
    } finally {
        release();
        context.fetch=previousFetch;
        const settled=await Promise.allSettled([first,joined]);
        for(const result of settled){
            if(result.status==='rejected'){throw result.reason;}
        }
    }
}
'''


class DatasetDeltaQueueHttpTests(unittest.TestCase):
    fixture=native.DatasetBuilderOwnershipTests.fixture

    def test_held_save_refuses_a_flush_that_never_reaches_the_response(self):
        ui.DatasetUiOwnershipJsTests().run_scenario(HELD_SAVE_SCENARIO + r'''
context.fetch=async()=>({});
run('dsbSaveRowsQueue={flush:async()=>{}};');
await assert.rejects(withHeldSave('held',()=>{},()=>assert.fail('not held')),/without reaching the held response/);
''')

    def test_held_save_releases_the_response_when_the_interleaved_assertion_fails(self):
        ui.DatasetUiOwnershipJsTests().run_scenario(HELD_SAVE_SCENARIO + r'''
for(const asynchronous of [false,true]) {
let completed=0;context.markCompleted=async()=>{await new Promise(resolve=>setImmediate(resolve));completed++;};
const originalFetch=context.fetch=async()=>({});
run("dsbSaveRowsQueue={flush:async()=>{await fetch('/save',{body:JSON.stringify({edits:[{row:{text:'held'}}]})});await markCompleted();}};");
await assert.rejects(withHeldSave('held',()=>{},()=>{
    if(asynchronous){return Promise.reject(Error('interleaved assertion'));}
    throw Error('interleaved assertion');
}),/interleaved assertion/);
assert.strictEqual(context.fetch,originalFetch);
assert.strictEqual(completed,1);
await run('dsbSaveRowsQueue.flush()');assert.strictEqual(completed,2);
}
''')

    def test_real_payload_reduction_coalescing_revision_chaining_structural_saves_and_conflict(self):
        with self.fixture() as (_,builder,work,_,states,app),TestClient(app) as client:
            path=work/'state.json'
            rows=[{'text':'line '+str(i),'emotion':'warm','seed':0,'status':'done','audio_url':'/clip_'+str(i)+'.wav'} for i in range(200)]
            path.write_text(json.dumps({'description':'voice','global_seed':'0','samples':rows}))
            clip_bytes={p.name:p.read_bytes() for p in work.glob('*.wav')};requests=[]
            started=time.perf_counter();server_phases=[]
            def server_phase(name):
                server_phases.append(f'{time.perf_counter()-started:.3f}s {name}')
            class Handler(FramedHttpFixtureHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    server_phase('GET started')
                    if self.path=='/fixture/conflict':
                        state=client.get('/api/dataset_builder/status/voice').json()
                        res=client.post('/api/dataset_builder/edit_rows',json={'name':'voice','expected_count':200,'edits':[{'index':25,'expected_revision':state['row_revisions'][25],'row':{'text':'outside','emotion':'warm','seed':0}}]})
                    else:res=client.get(self.path)
                    self.reply(res.status_code,res.content)
                def do_POST(self):
                    server_phase('POST started')
                    body=self.rfile.read(int(self.headers['Content-Length']));requests.append({'path':self.path,'body':json.loads(body),'bytes':len(body)})
                    res=client.post(self.path,content=body,headers={'Content-Type':'application/json'});self.reply(res.status_code,res.content)
                def reply(self,status,body):
                    server_phase('reply ready')
                    self.send_fixture_response(status,body,headers=(('Content-Type','application/json'),))
                    server_phase('reply sent')
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            server_phase('server started')
            code=HELD_SAVE_SCENARIO+r'''
run(core.slice(core.indexOf('        const API = {'),core.indexOf('        // --- Setup Tab ---')));
const base=BASE;
let requestNumber=0;
async function fixtureFetch(url,request) {
    const number=++requestNumber;
    setScenarioPhase('fetch-'+number+'-headers');
    const response=await fetch(base+url,request);
    setScenarioPhase('fetch-'+number+'-body');
    const readJson=response.json.bind(response);
    response.json=async()=>{const result=await readJson();setScenarioPhase('fetch-'+number+'-complete');return result;};
    return response;
}
context.fetch=fixtureFetch;const errors=[];context._toastSaveError=(kind,error)=>errors.push(error);context.showToast=()=>{};context.showConfirm=async()=>true;
setScenarioPhase('load-project');
run("dsbCurrentProject='voice';");await context.dsbLoadProject('voice');assert.strictEqual(run('dsbRows.length'),200);
let reads=0;for(const row of run('dsbRows')){for(const field of ['text','emotion','seed']){let value=row[field];Object.defineProperty(row,field,{enumerable:true,get:()=>{reads++;return value;},set:v=>value=v});}}
setScenarioPhase('single-row-save');context.dsbUpdateRow(17,'text',' changed ');assert.strictEqual(reads,4);await run('dsbSaveRowsQueue.flush()');assert(!run('dsbSaveRowsQueue.isDirty()'));
context.dsbUpdateRow(18,'text','A');context.dsbUpdateRow(19,'text','B');await run('dsbSaveRowsQueue.flush()');
setScenarioPhase('coalesced-same-row');
await withHeldSave('older',()=>context.dsbUpdateRow(20,'text','older'),()=>{
    context.dsbUpdateRow(20,'text','newer');context.dsbUpdateRow(21,'text','second index');return run('dsbSaveRowsQueue.flush()');
});assert(!run('dsbSaveRowsQueue.isDirty()'));
context.fetch=fixtureFetch;setScenarioPhase('structural-add-remove');
context.dsbAddRow('happy','added',0);await run('dsbSaveRowsQueue.flush()');assert.strictEqual(run('dsbRows.length'),201);
context.dsbUpdateRow(22,'text','after add');await run('dsbSaveRowsQueue.flush()');await context.dsbRemoveRow(200);await run('dsbSaveRowsQueue.flush()');
let lost=true;context.fetch=async(url,request)=>{const response=await fixtureFetch(url,request);if(request&&lost){lost=false;await response.arrayBuffer();throw new TypeError('fixture lost response after commit');}return response;};
context.dsbUpdateRow(23,'text','committed without response');await run('dsbSaveRowsQueue.flush()');assert(!run('dsbSaveRowsQueue.isDirty()'));assert.strictEqual(errors.length,0);
context.fetch=fixtureFetch;setScenarioPhase('coalesced-distinct-row');
await withHeldSave('distinct first',()=>context.dsbUpdateRow(26,'text','distinct first'),()=>{
    context.dsbUpdateRow(27,'text','distinct next');return run('dsbSaveRowsQueue.flush()');
});assert(!run('dsbSaveRowsQueue.isDirty()'));
setScenarioPhase('coalesced-structural-save');
await withHeldSave('before structure',()=>context.dsbUpdateRow(28,'text','before structure'),()=>{
    context.dsbAddRow('happy','structural pending',0);context.dsbUpdateRow(29,'text','late structural cell');return run('dsbSaveRowsQueue.flush()');
});assert.strictEqual(run('dsbRows.length'),201);assert(!run('dsbSaveRowsQueue.isDirty()'));await context.dsbRemoveRow(200);await run('dsbSaveRowsQueue.flush()');
context.fetch=fixtureFetch;setScenarioPhase('external-conflict');const conflict=await fixtureFetch('/fixture/conflict');assert.strictEqual(conflict.status,200);await conflict.json();context.dsbUpdateRow(25,'text','stale client');await assert.rejects(run('dsbSaveRowsQueue.flush()'),/row changed/);assert(run('dsbSaveRowsQueue.isDirty()'));assert.strictEqual(errors.length,1);
setScenarioPhase('reload-dirty-project');await context.dsbLoadProject('voice');assert.strictEqual(run('dsbRows[25].text'),'stale client','failed dirty save must not be discarded by reload');
'''.replace('BASE',json.dumps('http://127.0.0.1:'+str(server.server_port)))
            try:
                ui.DatasetUiOwnershipJsTests().run_scenario(code)
            except AssertionError as error:
                raise self.failureException(f'{error}\nServer phase trace:\n'+ '\n'.join(server_phases)) from None
            finally:server.shutdown();server.server_close();thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            saved=json.loads(path.read_text())
            self.assertEqual('changed',saved['samples'][17]['text']);self.assertEqual('newer',saved['samples'][20]['text']);self.assertEqual('second index',saved['samples'][21]['text']);self.assertEqual('committed without response',saved['samples'][23]['text']);self.assertEqual('outside',saved['samples'][25]['text'])
            self.assertEqual(rows[0],saved['samples'][0]);self.assertEqual(200,len(saved['samples']));self.assertEqual(clip_bytes,{p.name:p.read_bytes() for p in work.glob('*.wav')})
            first=requests[0];self.assertEqual('/api/dataset_builder/edit_rows',first['path']);self.assertEqual([17],[e['index'] for e in first['body']['edits']]);self.assertNotIn('rows',first['body'])
            self.assertLess(first['bytes'],500);self.assertEqual([18,19],[e['index'] for e in requests[1]['body']['edits']])
            chained=[r for r in requests if r['path'].endswith('edit_rows') and r['body']['edits'][0]['index']==20]
            self.assertEqual(2,len(chained));self.assertNotEqual(chained[0]['body']['edits'][0]['expected_revision'],chained[1]['body']['edits'][0]['expected_revision'])
            self.assertEqual(4,sum(r['path'].endswith('update_rows') for r in requests));self.assertEqual(1,sum(any(e['index']==23 for e in r['body'].get('edits',[])) for r in requests))
            distinct=[r for r in requests if r['path'].endswith('edit_rows') and any(e['index'] in (26,27) for e in r['body']['edits'])]
            self.assertEqual([[26],[27]],[[e['index'] for e in r['body']['edits']] for r in distinct])
            self.assertEqual('distinct first',saved['samples'][26]['text']);self.assertEqual('distinct next',saved['samples'][27]['text'])
            self.assertEqual('before structure',saved['samples'][28]['text']);self.assertEqual('late structural cell',saved['samples'][29]['text'])
            self.measurements={'rows':200,'changed_rows':1,'definition_field_reads':4,'posted_rows':1,'json_payload_bytes':first['bytes'],'native_storage_verified':True}
