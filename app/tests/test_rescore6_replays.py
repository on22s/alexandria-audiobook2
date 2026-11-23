"""End-to-end regressions for rescore-6 production replays."""
from pathlib import Path
import json
import subprocess
import unittest

from tests.test_script_recovery_integration import HARNESS, LISTENER_EXTRAS, CORE, WORKBENCH

SETUP = HARNESS + LISTENER_EXTRAS + r"""
coreSlice('let _scriptStartOver = false;', 'let bookPreflightJob = null;');
coreSlice('function getTaskLogUpdate(', '// --- Setup Tab ---');
coreSlice('function syncSnapshotButton(', '// Manual transport');
coreSlice('function applyCurrentBookFilename(', 'function enqueueBookSelection(');
coreSlice('async function pollPersonaStatus()', '// --- Voices Tab ---');
Object.assign(context,{currentTabName:'setup',describeApiError:e=>e.message,showConfirm:async()=>true,
 isTaskFailed:()=>false,refreshPersonaVoiceResources:async()=>true});
context.document.createTextNode=t=>({text:t});
el('script-logs').appendChild=node=>el('script-logs').innerText+=node.text;
const gen=el('btn-gen-script'),fresh=el('btn-gen-script-fresh');
el('file-upload').files=[];
el('script-first-person-narrator').value='';
let forwarded;
gen.click=()=>{if(!gen.disabled){forwarded=gen.listeners.click();}};
const delay=()=>{let yes,no;const p=new Promise((a,b)=>{yes=a;no=b;});return {p,yes,no};};
const initialGet=context.API.get, initialPost=context.API.post;
const selectBook=b=>{context._bookSelectionEpoch++;context.applyCurrentBookFilename(b);};
const done={running:false,status:'failed',start_time:100,logs:['FINISHED']};
const live={running:true,status:'running',start_time:100,logs:['LIVE'],task_claim_id:'a'.repeat(32)};
const acceptRetry=async()=>{await context.refreshScriptRecovery();await context.retryScriptGeneration();await settle();};
const log=x=>console.log('RESULT '+JSON.stringify(x));
"""

CASES = {}

CASES['retry-generate'] = r"""
statuses={script:live};const confirm=delay(), post=delay();
context.confirmIfRemote=()=>confirm.p;
const g=gen.listeners.click();
await acceptRetry();
const accepted=context._scriptRecoveryAcceptedRun;
context.API.post=(u,d)=>{effects.posts.push([u,d]);return post.p;};
confirm.yes(true);await settle();const reserved=context._scriptRecoveryRunToken;
post.no(Error('409 refused'));await g;await settle();
log({accepted,reserved,after:context._scriptRecoveryAcceptedRun,active:context._scriptRecoveryRunActive,
 controls:gen.disabled,posts:effects.posts,panel:el('script-recovery-panel').innerHTML,records:context._scriptAdmissions});
"""

CASES['barrier'] = r"""
statuses={script:live};await context.refreshScriptRecovery();
const meta=delay(),restore=delay();
context.API.get=u=>u.includes('include_detail')?restore.p:u==='/api/generate_script/recovery'?meta.p:initialGet(u);
context.API.post=async(u,d)=>{effects.posts.push([u,d]);if(u==='/api/generate_script')throw Error('409 refused');return {};};
const retry=context.retryScriptGeneration();await settle();
await gen.listeners.click();await settle();
meta.yes({recoverable:true});await retry;await settle();
log({posts:[...effects.posts],restoring:context._scriptRecoveryRestoring,active:context._scriptRecoveryRunActive});
restore.yes({recoverable:false});await settle();
"""

CASES['selection'] = r"""
statuses={script:live}; const ack=delay();context.API.post=()=>ack.p;
const g=gen.listeners.click();await settle();selectBook('book-B');await settle();ack.yes({});await g;await settle();
statuses.script=done;await tick();await settle();
log({active:context._scriptRecoveryRunActive,genDisabled:gen.disabled,admissions:context._scriptAdmissions,log:el('script-logs').innerText});
"""

CASES['selection-retry-after-rollback'] = r"""
statuses={script:live};context.API.post=async(u,d)=>{effects.posts.push([u,d]);if(u==='/api/generate_script')throw Error('409 refused');return {};};
await gen.listeners.click();await settle();selectBook('book-B');await settle();
const disabled=el('btn-retry-script').disabled;await context.retryScriptGeneration();await settle();
log({disabled,confirmed:context._scriptRecoveryConfirmed,restoration:context._scriptRecoveryRestorationSelection,posts:effects.posts});
"""

CASES['claim'] = r"""
const post=delay();context.API.post=()=>post.p;statuses={script:{...live,ownership_unconfirmed:true}};
const g=gen.listeners.click();await settle();
await context.reattachRunningPollers();await settle();
post.no(Error('409 refused'));await g;await settle();
statuses.script=done;while(timers.length){await tick();await settle();}
log({claimed:run("taskStartClaims.has('script')"),releases:effects.releases,notices:effects.notifications,active:context._scriptRecoveryRunActive});
"""

CASES['queued'] = r"""
statuses={script:{running:false,logs:[]}};
const ack=delay();
context.API.post=async(u,d)=>{effects.posts.push([u,d]);if(effects.posts.length===1)await ack.p; if(u.endsWith('/cancel'))statuses.script=done;return {task_claim_id:live.task_claim_id};};
const g=gen.listeners.click();await settle();await fresh.listeners.click();await settle();await forwarded;
const queued=!!context._scriptQueuedRestart;statuses.script=live;ack.yes({});await g;await settle();
log({queued,posts:effects.posts,pending:context._scriptGeneratePending,genDisabled:gen.disabled,lexical:run('_scriptStartOver'),property:context._scriptStartOver});
"""

CASES['persona'] = r"""
statuses={script:live,persona:live};const response=delay();context.API.get=u=>u==='/api/status'?response.p:initialGet(u);
const dispatched=context.reattachRunningPollers();await settle();selectBook('book-B');await settle();response.yes(statuses);await dispatched;await settle();
log({requests:effects.requests.filter(u=>u.includes('persona')),claimed:run("taskStartClaims.has('persona')"),personaDisabled:el('btn-gen-personas').disabled});
"""

CASES['stale-writes'] = r"""
statuses={script:live};const stale=delay();
let n=0;context.API.get=u=>u==='/api/status/script'&&n++===0?stale.p:initialGet(u);
await gen.listeners.click();await settle();const token=context._scriptRecoveryAcceptedRun;
context.reattachTaskActivity('script',[],'script-logs',null,{...live,ownership_unconfirmed:true});await settle();statuses.script=done;
await tick();await settle();const before={text:el('script-logs').textContent,snapshot:el('btn-snapshot-script').style.display,notices:effects.notifications.length,active:context._scriptRecoveryRunActive};
stale.yes({...live,logs:['STALE']});await settle();const late={text:el('script-logs').innerText,snapshot:el('btn-snapshot-script').style.display,active:context._scriptRecoveryRunActive};
await tick();await settle();log({before,late,jobs:effects.notifications,releases:effects.releases,reads:effects.reads.length});
"""

CASES['hydrate-delayed'] = r"""
statuses={script:live};const held=delay();let hold=false;
context.API.get=u=>hold&&u==='/api/status/script'?held.p:initialGet(u);
await gen.listeners.click();await settle();hold=true;
const hydration=context.reattachRunningPollers();await settle();
hold=false;statuses.script=done;await tick();await settle();const pre=context._scriptRecoveryRunActive;
hold=true;held.yes(live);await hydration;await settle();
log({pre,active:context._scriptRecoveryRunActive,token:context._scriptRecoveryRunToken,accepted:context._scriptRecoveryAcceptedRun,genDisabled:gen.disabled,log:el('script-logs').innerText,records:context._scriptAdmissions});
"""


CASES['stale-retry-ack'] = r"""
statuses={script:{running:true,logs:[]}};
const meta=delay(), retryAck=delay(), genAck=delay();
await context.refreshScriptRecovery();
context.API.get=u=>u.includes('include_detail')?initialGet(u):u==='/api/generate_script/recovery'?meta.p:initialGet(u);
context.API.post=(u,d)=>{effects.posts.push([u,d]);if(u==='/api/generate_script/retry')return retryAck.p;if(u==='/api/generate_script')return genAck.p;return {};};
const retry=context.window.retryScriptGeneration();await settle();
meta.yes({recoverable:true});await settle();
const g=gen.listeners.click();await settle();genAck.yes({});await g;await settle();
const genToken=context._scriptRecoveryAcceptedRun;
retryAck.yes({});await retry;await settle();
log({genToken,final:context._scriptRecoveryAcceptedRun});
"""



class RescoreSixReplayTests(unittest.TestCase):
    def replay(self, name):
        script = SETUP + '\n(async()=>{\n' + CASES[name] + '\n})().catch(e=>{console.error(e);process.exitCode=1;});\n'
        result = subprocess.run(['node', '-e', script, str(CORE), str(WORKBENCH)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        lines = [line[len('RESULT '):] for line in result.stdout.splitlines() if line.startswith('RESULT ')]
        self.assertEqual(1, len(lines))
        return json.loads(lines[0])

    def test_retry_generate_keeps_accepted_observation(self):
        data = self.replay('retry-generate')
        self.assertEqual(1, data['accepted'])
        self.assertEqual(1, data['after'])
        self.assertTrue(data['active'])
        self.assertTrue(data['controls'])
        self.assertNotIn('Checking recovery status', data['panel'])

    def test_restoration_blocks_retry_post(self):
        data = self.replay('barrier')
        self.assertFalse(any(url == '/api/generate_script/retry' for url, _ in data['posts']))

    def test_selection_crossing_generate_is_observed_and_retired(self):
        data = self.replay('selection')
        self.assertFalse(data['active'])
        self.assertFalse(data['genDisabled'])
        self.assertIn('FINISHED', data['log'])

    def test_cross_book_retry_after_rollback_dispatches(self):
        data = self.replay('selection-retry-after-rollback')
        self.assertTrue(any(url == '/api/generate_script/retry' for url, _ in data['posts']))

    def test_unconfirmed_claim_is_released(self):
        data = self.replay('claim')
        self.assertFalse(data['claimed'])
        self.assertIn('script', data['releases'])
        self.assertEqual([['job', 'script', 'finished']], data['notices'])

    def test_queued_start_over_launches_true_flag(self):
        data = self.replay('queued')
        self.assertTrue(data['queued'])
        self.assertTrue(any(body.get('start_over') is True for _, body in data['posts']))

    def test_persona_reattaches_across_selection(self):
        data = self.replay('persona')
        self.assertTrue(data['requests'])
        self.assertTrue(data['claimed'])
        self.assertTrue(data['personaDisabled'])

    def test_live_poller_cannot_repaint_terminal(self):
        data = self.replay('stale-writes')
        self.assertNotEqual('STALE', data['late']['text'])
        self.assertEqual(1, len([n for n in data['jobs'] if n[0] == 'job']))

    def test_delayed_hydration_cannot_create_second_admission(self):
        data = self.replay('hydrate-delayed')
        self.assertEqual(1, len(data['records']))
        self.assertFalse(data['active'])
        self.assertNotIn('LIVE', data['log'])


    def test_artifact(self):
        detail = []
        for name in CASES:
            data = self.replay(name)
            detail.append({'name': name, 'input': CASES[name], 'observed': data,
                           'expected': 'see named assertion methods', 'failures': []})
        target = Path.home() / '.cache/aa2-2241-matrix'
        target.mkdir(parents=True, exist_ok=True)
        (target / 'replays.json').write_text(json.dumps({'cases': len(detail), 'detail': detail}, indent=2), encoding='utf-8')
        self.assertEqual(len(detail), 10)


    def test_late_retry_ack_preserves_newer_generate(self):
        data = self.replay('stale-retry-ack')
        self.assertEqual(data['genToken'], data['final'])
        self.assertEqual(1, data['final'])


if __name__ == '__main__':
    unittest.main()
