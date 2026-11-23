"""Rescore-7 production replay regressions."""
import json
from pathlib import Path
import subprocess
import unittest

from tests.test_rescore6_replays import SETUP, CORE, WORKBENCH, CASES as R6_CASES

CASES = {
    'ordinary-completion': r"""
statuses={script:{running:true,logs:['LIVE']}};
const ack=delay();context.API.post=(u,d)=>{effects.posts.push([u,d]);return ack.p;};
await gen.listeners.click();await settle();ack.yes({});await settle();
statuses={script:{running:false,status:'failed',logs:['FINISHED']}};
while(timers.length){await tick();await settle();}
log({jobs:effects.notifications.filter(n=>n[0]==='job').length,reads:effects.reads.length});
""",
    'serial-retries': r"""
statuses={script:{running:true,logs:[]}};
const firstAck=delay(), secondAck=delay();
context.API.post=(u,d)=>{effects.posts.push([u,d]);return effects.posts.length===1?firstAck.p:secondAck.p;};
await context.refreshScriptRecovery();
const first=context.window.retryScriptGeneration();await settle();firstAck.yes({});await first;await settle();
const token1=context._scriptRecoveryAcceptedRun;
await context.refreshScriptRecovery();
const second=context.window.retryScriptGeneration();await settle();secondAck.yes({});await second;await settle();
log({token1,final:context._scriptRecoveryAcceptedRun,polls:effects.polls.length,
 notices:effects.notifications.filter(n=>n[0]==='action').map(n=>String(n[1]))});
""",
    'two-retries-after-terminal': r"""
statuses={script:{running:true,logs:[]}};
const firstAck=delay(), secondAck=delay();
context.API.post=(u,d)=>{effects.posts.push([u,d]);return effects.posts.length===1?firstAck.p:effects.posts.length===2?secondAck.p:Promise.resolve({});};
await context.refreshScriptRecovery();
const first=context.window.retryScriptGeneration();await settle();firstAck.yes({});await first;await settle();
const token1=context._scriptRecoveryAcceptedRun;
statuses={script:{running:false,status:'failed',logs:['FINISHED']}};
while(timers.length){await tick();await settle();}
await context.refreshScriptRecovery();
const second=context.window.retryScriptGeneration();await settle();secondAck.yes({});await second;await settle();
const token2=context._scriptRecoveryAcceptedRun;
statuses={script:{running:false,status:'failed',logs:['FINISHED2']}};
while(timers.length){await tick();await settle();}
log({token1,token2,posts:effects.posts.filter(p=>p[0]==='/api/generate_script/retry').length,
 jobs:effects.notifications.filter(n=>n[0]==='job').length,
 states:Object.values(context._scriptAdmissions).map(r=>r.state)});
""",
    'stale-no-input-after-selection': r"""
statuses={script:{running:true,logs:[]}};
const ack=delay();context.API.post=(u,d)=>{effects.posts.push([u,d]);return ack.p;};
const g=gen.listeners.click();await settle();selectBook('book-B');await settle();
el('upload-status').innerHTML='BOOK-B STATUS';
ack.no(Error('No input file'));await g;await settle();
log({status:el('upload-status').innerHTML,disabled:gen.disabled,epoch:context._bookSelectionEpoch});
""",
}


class RescoreSevenReplayTests(unittest.TestCase):
    def replay(self, name):
        script = SETUP + '\n(async()=>{\n' + CASES[name] + '\n})().catch(e=>{console.error(e);process.exitCode=1;});\n'
        result = subprocess.run(['node','-e',script,str(CORE),str(WORKBENCH)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
        lines=[line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, result.stderr)
        return json.loads(lines[-1][len('RESULT '):] if lines[-1].startswith('RESULT ') else lines[-1])

    def _run_case(self, name, core=CORE):
        script = SETUP + '\n(async()=>{\n' + CASES[name] + '\n})().catch(e=>{console.error(e);process.exitCode=1;});\n'
        result = subprocess.run(['node','-e',script,str(core),str(WORKBENCH)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
        lines=[line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines,result.stderr)
        return json.loads(lines[-1][len('RESULT '):] if lines[-1].startswith('RESULT ') else lines[-1])

    def _run_text(self, code, core=CORE):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});\n'
        result = subprocess.run(['node','-e',script,str(core),str(WORKBENCH)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
        lines=[line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines,result.stderr)
        return json.loads(lines[-1][len('RESULT '):] if lines[-1].startswith('RESULT ') else lines[-1])

    def test_dual_notification_positive_and_mutant(self):
        original=self._run_text(R6_CASES['stale-writes'])
        self.assertEqual(1,len(original['jobs']),'unmodified source must notify exactly once')
        source=CORE.read_text(encoding='utf-8')
        marker="notifyJobDone('script', '', 'finished', data);"
        self.assertEqual(1, source.count(marker), 'Script terminal notification emitter missing or duplicated')
        target=Path.home()/'.cache/aa2-2241-matrix'
        target.mkdir(parents=True,exist_ok=True)
        mutant=target/'mutant-score8-notify.js'
        mutant.write_text(source.replace(marker,'return;',1),encoding='utf-8')
        failed=self._run_text(R6_CASES['stale-writes'],mutant)
        self.assertEqual([], failed['jobs'], 'disabling Script terminal notification must drop it')

    def test_serial_retries_keep_observation(self):
        d=self.replay('serial-retries')
        self.assertTrue(d['token1']>0)
        self.assertEqual(d['token1'],d['final'])
        self.assertEqual([],d['notices'])

    def test_two_retries_after_terminal(self):
        d=self.replay('two-retries-after-terminal')
        self.assertEqual(2,d['posts'])
        self.assertTrue(d['token2']>d['token1'])
        self.assertEqual(2,d['jobs'])

    def test_stale_no_input_does_not_touch_new_book(self):
        d=self.replay('stale-no-input-after-selection')
        self.assertEqual('BOOK-B STATUS',d['status'])
        self.assertFalse(d['disabled'])


if __name__=='__main__':
    unittest.main()
