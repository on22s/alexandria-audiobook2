"""Native status and notification behavior with failures and known terminal outcomes."""
from pathlib import Path
import subprocess
import unittest

STATIC=Path(__file__).resolve().parent.parent/'static/js'
class TaskStatusFeedbackJsTests(unittest.TestCase):
    def run_js(self,script):
        result=subprocess.run(['node','-e',script,str(STATIC/'app-core.js'),str(STATIC/'app-workbench.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_failed_eta_retains_last_known_task_and_idle_requires_success(self):
        self.run_js(r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');const s=fs.readFileSync(process.argv[2],'utf8');
const wrap={style:{display:'none'}},value={title:''};let response=null,text='',writes=0;Object.defineProperty(value,'textContent',{get:()=>text,set:v=>{writes++;text=v;}});
const c={document:{getElementById:id=>id==='sys-eta'?wrap:value},console:{error(){}},formatDuration:x=>x+'s',API:{get:async()=>{if(response instanceof Error){throw response;}return response;}}};vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function applySystemStatusText('),s.indexOf('let _systemStatsPending'))+'let _etaStatusPending=false;'+s.slice(s.indexOf('async function updateEtaStatus()'),s.indexOf('function pollLmStudioStatus()')),c);
let finished=false;process.on('beforeExit',()=>assert(finished,'eta assertions must finish'));
(async()=>{response=Error('offline');await c.updateEtaStatus();assert.strictEqual(wrap.style.display,'none');response={running:true,label:'Script',eta_seconds:10};await c.updateEtaStatus();assert.strictEqual(value.textContent,'Script (ETA 10s)');const before=writes;for(let i=0;i<10;i++)await c.updateEtaStatus();assert.strictEqual(writes,before,'unchanged ETA must not rewrite live text');response=Error('offline');await c.updateEtaStatus();assert.strictEqual(wrap.style.display,'flex');assert.strictEqual(value.title,'Script (ETA 10s)');assert.match(value.textContent,/Status unavailable/);await c.updateEtaStatus();assert.strictEqual(value.title,'Script (ETA 10s)');response={running:true,label:'Script',eta_seconds:5};await c.updateEtaStatus();assert.strictEqual(value.title,'');assert.strictEqual(value.textContent,'Script (ETA 5s)');response={running:false};await c.updateEtaStatus();assert.strictEqual(wrap.style.display,'none');finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});''')

    def test_notifications_use_terminal_outcomes_without_false_failure_or_cancel(self):
        self.run_js(r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');const s=fs.readFileSync(process.argv[1],'utf8');let notifications=[];
class Notification{static permission='granted';constructor(title,options){notifications.push({title,options});}}
const c={Notification,window:{Notification},document:{visibilityState:'hidden',hasFocus:()=>false}};vm.createContext(c);const start=s.indexOf('function isTaskFailed('),end=s.indexOf('// Ask for permission',start);vm.runInContext(s.slice(start,end),c);
const cases=[['failed',{status:'failed',logs:[]}],['failed',{tasks:[{status:'failed'},{status:'cancelled'}],logs:[]}],['cancelled',{status:'cancelled',logs:[]}],['cancelled',{logs:['Task script cancelled.']}],['finished',{logs:['[ERROR] temporary','Task script completed successfully.']}],['finished',{cancel:true,logs:['Task script completed successfully.']}],['finished',{logs:['0 failed sections']}]];
for(const [expected,status]of cases){c.notifyJobDone('script','','finished',status);assert.strictEqual(notifications.at(-1).title,'Script generation '+expected);if(expected!=='finished'){assert.match(notifications.at(-1).options.body,/recovery options/);}}
const count=notifications.length;c.document.visibilityState='visible';c.document.hasFocus=()=>true;c.notifyJobDone('script','','finished',{status:'failed'});assert.strictEqual(notifications.length,count);''')

    def test_notification_permission_is_explicit_single_flight_and_recovers(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let calls=0,resolve,fail=false;const elements={};const el=id=>elements[id]||(elements[id]={});const notices=[];
const Notification={permission:'default',requestPermission:async()=>{calls++;if(fail){throw Error('blocked');}await new Promise(done=>resolve=done);}};
const c={Notification,window:{Notification},document:{getElementById:el,addEventListener(){throw Error('arbitrary clicks must not request permission');}},showToast:(...args)=>notices.push(args)};vm.createContext(c);const a=s.indexOf('// Ask for permission');vm.runInContext(s.slice(a,s.indexOf('// --- Navigation ---',a)),c);
assert.strictEqual(calls,0);assert.strictEqual(el('notification-enable').disabled,false);assert(el('notification-status').textContent.includes('Optional'));
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{const pending=c.requestTaskNotifications();assert(el('notification-enable').disabled);assert(el('notification-enable').textContent.includes('Requesting'));await c.requestTaskNotifications();assert.strictEqual(calls,1);Notification.permission='granted';resolve();await pending;assert(el('notification-enable').disabled);assert.strictEqual(el('notification-enable').textContent,'Notifications enabled');await c.requestTaskNotifications();assert.strictEqual(calls,1);
Notification.permission='denied';c.renderNotificationPermission();assert(el('notification-status').textContent.includes('browser permissions'));await c.requestTaskNotifications();assert.strictEqual(calls,1);
Notification.permission='default';fail=true;await c.requestTaskNotifications();assert.strictEqual(calls,2);assert(!el('notification-enable').disabled);assert.strictEqual(notices.at(-1)[1],'warning');delete c.window.Notification;c.renderNotificationPermission();assert(el('notification-enable').disabled);assert(el('notification-status').textContent.includes('does not support'));finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')
