"""Distinct production schedules with independently asserted outcomes."""
import json
from pathlib import Path
import subprocess
import unittest

from tests.test_rescore6_replays import SETUP, CORE, WORKBENCH

CASES = {}

CASES['retry-ack-then-generate-ack'] = r"""
statuses={script:live};
const retryPost=delay(), genPost=delay();
await context.refreshScriptRecovery();
context.API.post=(u,d)=>{effects.posts.push([u,d]);if(u==='/api/generate_script/retry')return retryPost.p;if(u==='/api/generate_script')return genPost.p;return {};};
const retry=context.window.retryScriptGeneration();await settle();
const generate=gen.listeners.click();await settle();
retryPost.yes({});await retry;await settle();
genPost.yes({});await generate;await settle();
statuses={script:done};while(timers.length){await tick();await settle();}
log({posts:effects.posts.map(p=>p[0]),polls:effects.polls.length,
 records:Object.values(context._scriptAdmissions).map(r=>r.state),
 jobs:effects.notifications.filter(n=>n[0]==='job').length,
 errors:effects.notifications.filter(n=>n[0]==='action').map(n=>String(n[1])),
 genDisabled:gen.disabled,active:context._scriptRecoveryRunActive});
"""

CASES['generate-ack-terminal-then-retry-ack'] = r"""
statuses={script:live};
const genPost=delay(), retryPost=delay();
context.API.post=(u,d)=>{effects.posts.push([u,d]);if(u==='/api/generate_script')return genPost.p;if(u==='/api/generate_script/retry')return retryPost.p;return {};};
const generate=gen.listeners.click();await settle();
genPost.yes({});await generate;await settle();
statuses={script:done};while(timers.length){await tick();await settle();}
await context.refreshScriptRecovery();
const retry=context.window.retryScriptGeneration();await settle();
retryPost.yes({});await retry;await settle();
const pollsAfterRetry=effects.polls.length;
statuses={script:done};while(timers.length){await tick();await settle();}
log({posts:effects.posts.filter(p=>p[0]==='/api/generate_script/retry').length,
 pollsAfterRetry,jobs:effects.notifications.filter(n=>n[0]==='job').length,
 active:context._scriptRecoveryRunActive,genDisabled:gen.disabled,
 records:Object.values(context._scriptAdmissions).map(r=>r.state)});
"""

CASES['reload-restored-notification'] = r"""
statuses={script:live};
context.reattachRunningPollers();await settle();
statuses={script:done};while(timers.length){await tick();await settle();}
log({jobs:effects.notifications.filter(n=>n[0]==='job').length,
 reads:effects.reads.length,releases:[...effects.releases],genDisabled:gen.disabled,
 status:el('script-logs').innerText});
"""

CASES['stale-claim-cleanup'] = r"""
statuses={script:{...live,ownership_unconfirmed:true}};
const post=delay();context.API.post=(u,d)=>{effects.posts.push([u,d]);return post.p;};
const generate=gen.listeners.click();await settle();
await context.reattachRunningPollers();await settle();
post.no(Error('409 refused'));await generate;await settle();
statuses={script:{running:false,status:'failed',logs:['OLD']}};
while(timers.length){await tick();await settle();}
const claimedAfterOld=run("taskStartClaims.has('script')");
const post2=delay();context.API.post=(u,d)=>{effects.posts.push([u,d]);return post2.p;};
const newer=gen.listeners.click();await settle();post2.yes({});await newer;await settle();
statuses={script:done};while(timers.length){await tick();await settle();}
log({claimedAfterOld,claimedFinal:run("taskStartClaims.has('script')"),
 releases:[...effects.releases],jobs:effects.notifications.filter(n=>n[0]==='job').length});
"""

CASES['failover-selection-fence'] = r"""
statuses={script:{running:false,logs:[]}};
const failover=delay();
run('window._scriptStartOver=true');
context.confirmIfRemote=()=>failover.p;
const g=gen.listeners.click();await settle();
selectBook('book-B');await settle();
failover.yes(true);await g;await settle();
log({posts:effects.posts.map(p=>[p[0],p[1]&&p[1].start_over]),epoch:context._bookSelectionEpoch});
"""




class ProductionScheduleTests(unittest.TestCase):
    def run_case(self, name, core=CORE, workbench=WORKBENCH):
        script = SETUP + '\n(async()=>{\n' + CASES[name] + '\n})().catch(e=>{console.error(e);process.exitCode=1;});\n'
        result = subprocess.run(['node','-e',script,str(core),str(workbench)],
                                capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)
        lines=[line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines,result.stderr)
        return json.loads(lines[-1][len('RESULT '):] if lines[-1].startswith('RESULT ') else lines[-1])

    def test_retry_ack_then_generate_ack(self):
        d=self.run_case('retry-ack-then-generate-ack')
        self.assertEqual(2,len([p for p in d['posts'] if p.startswith('/api/generate_script')]))
        self.assertEqual([],d['errors'])
        self.assertEqual(1,d['jobs'])
        self.assertIn('delivered',d['records'])
        self.assertFalse(d['genDisabled'])

    def test_generate_ack_terminal_then_retry_ack(self):
        d=self.run_case('generate-ack-terminal-then-retry-ack')
        self.assertEqual(1,d['posts'])
        self.assertEqual(2,d['jobs'])
        self.assertEqual(2,len([r for r in d['records'] if r=='delivered']))
        self.assertFalse(d['genDisabled'])

    def test_reload_restored_notification(self):
        d=self.run_case('reload-restored-notification')
        self.assertEqual(1,d['jobs'])
        self.assertEqual(1,d['reads'])
        self.assertIn('FINISHED',d['status'])
        self.assertFalse(d['genDisabled'])

    def test_stale_claim_cleanup(self):
        d=self.run_case('stale-claim-cleanup')
        self.assertFalse(d['claimedAfterOld'])
        self.assertFalse(d['claimedFinal'])
        self.assertGreaterEqual(len(d['releases']),1)
        self.assertEqual(2,d['jobs'])

    def test_failover_selection_fence(self):
        d=self.run_case('failover-selection-fence')
        self.assertEqual([],d['posts'])

    def test_artifact_with_actual_failures(self):
        source=CORE.read_text(encoding='utf-8')
        marker='window.onScriptTaskTerminal = deliverScriptTaskTerminal;'
        self.assertIn(marker,source)
        target=Path.home()/'.cache/aa2-2241-matrix'
        target.mkdir(parents=True,exist_ok=True)
        mutant=target/'mutant-score9-terminal.js'
        mutant.write_text(source.replace(marker,'window.onScriptTaskTerminal = () => {};',1),encoding='utf-8')
        detail=[]
        for name in CASES:
            expected_pass=not name.startswith('timeout-while')
            observed=self.run_case(name)
            mutant_observed=self.run_case(name,mutant)
            expected = {
                'retry-ack-then-generate-ack': {'errors': [], 'jobs': 1, 'genDisabled': False},
                'generate-ack-terminal-then-retry-ack': {'jobs': 2, 'genDisabled': False},
                'reload-restored-notification': {'jobs': 1, 'reads': 1, 'genDisabled': False},
                'stale-claim-cleanup': {'claimedFinal': False, 'jobs': 2},
                'failover-selection-fence': {'posts': []},
            }[name]
            observed_failures = [f'{key}: expected {value!r}, observed {observed.get(key)!r}'
                                 for key, value in expected.items() if observed.get(key) != value]
            mutant_failures = [f'{key}: expected {value!r}, mutant {mutant_observed.get(key)!r}'
                               for key, value in expected.items() if mutant_observed.get(key) != value]
            detail.append({'name':name,'input':CASES[name],'expected':expected,
                           'observed':observed,'mutant_observed':mutant_observed,
                           'failures':observed_failures,'mutant_failures':mutant_failures,
                           'unmodified_pass': not observed_failures})
        (target/'production-schedules.json').write_text(json.dumps({'cases':len(detail),'detail':detail},indent=2),encoding='utf-8')
        self.assertEqual(len(detail),5)

    def test_terminal_disabled_fails_named_assertions(self):
        source=CORE.read_text(encoding='utf-8')
        marker='window.onScriptTaskTerminal = deliverScriptTaskTerminal;'
        target=Path.home()/'.cache/aa2-2241-matrix'
        target.mkdir(parents=True,exist_ok=True)
        mutant=target/'mutant-score9-terminal.js'
        mutant.write_text(source.replace(marker,'window.onScriptTaskTerminal = () => {};',1),encoding='utf-8')
        observed=self.run_case('reload-restored-notification',mutant)
        self.assertTrue(observed['genDisabled'],'terminal-disabled mutant must leave busy controls')


if __name__=='__main__':
    unittest.main()
