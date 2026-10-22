"""Run workspace code with gated reads, stale replies and hostile display text."""
import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent/'static/js'
SETUP = r'''
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
const visibilityListeners=[];document.addEventListener=(name,fn)=>{if(name==='visibilitychange'){visibilityListeners.push(fn);}};
Element.prototype.append=function(...nodes){for(const node of nodes){this.appendChild(node);}};
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children]){child.remove();}this.append(...nodes);};
Element.prototype.focus=function(){document.activeElement=this;};
for(const id of ['workspace-book-name','workspace-next-reason','workspace-continue','workspace-stages',
 'workspace-feedback','workspace-library-feedback','workspace-library','workspace-monitor-feedback',
 'workspace-monitor','workspace-monitor-title']){body.appendChild(new Element('div',{id}));}
const elements=id=>document.getElementById(id);
const requests=[],activated=[],timers=[],actionErrors=[];let responses={},fail=false;
context.window=context;context.currentTabName='home';document.visibilityState='visible';
context.TASK_LABELS={script:'Script generation'};
context.API={get:async path=>{requests.push(path);if(fail){throw Error('Offline');}const result=responses[path];if(result instanceof Error){throw result;}return typeof result==='function'?result():result;}};
context.activateTab=name=>{activated.push(name);context.currentTabName=name;};
context.reattachRunningPollers=async()=>{requests.push('reattach-existing-controls');};
context.showActionError=(action,error,recovery)=>{actionErrors.push({action,error:error.message,recovery});};
context.loadScript=async name=>{requests.push('confirmed-loader:'+name);};
context.setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;};context.clearTimeout=()=>{};
const core=fs.readFileSync(process.argv[2],'utf8'),workbench=fs.readFileSync(process.argv[3],'utf8');
let start=core.indexOf('const ACTIVITY_MARKER');let end=core.indexOf('// Every task that renders',start);
vm.runInContext(core.slice(start,end),context);
start=workbench.indexOf('function formatDuration(');end=workbench.indexOf('// Always-visible',start);
vm.runInContext(workbench.slice(start,end),context);
start=workbench.indexOf('function applySystemStatusText(');end=workbench.indexOf('async function updateSystemStats(',start);
vm.runInContext(workbench.slice(start,end),context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
function snapshot(name='Book A',tab='editor'){
 return {name,book_token:'a'.repeat(64),script:{entries:1},cast:{total:1,pending:0},audio:{status:'not_started',total:0,present:0,failed:0,missing:0,generating:0,message:'Not prepared'},outputs:[{format:'MP3',status:'not_found'}],next:{tab,label:'Continue',reason:'Review the current book'}};
}
responses['/api/workspace']=snapshot();responses['/api/scripts']=[];responses['/api/status']={};
'''


class BookWorkspaceJsTests(unittest.TestCase):
    def run_js(self, script):
        script = "let finished=false;process.on('beforeExit',()=>assert(finished,'Test did not finish'));\n" + script.replace('})().catch', '})().then(()=>{finished=true;}).catch')
        result = subprocess.run(['node','-e',DOM_FIXTURE+SETUP+script,
                                 str(STATIC/'app-workspace.js'),str(STATIC/'app-core.js'),
                                 str(STATIC/'app-workbench.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_declared_task_targets_exist_and_audio_uses_its_actual_cancel_screen(self):
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__(); self.tab = None; self.stack = []; self.links = set(); self.cancel_tab = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'div':
                    self.stack.append(self.tab)
                    if 'tab-content' in attrs.get('class', '').split(): self.tab = attrs['id'].removesuffix('-tab')
                if attrs.get('data-tab'): self.links.add(attrs['data-tab'])
                if attrs.get('id') == 'btn-cancel-merge': self.cancel_tab = self.tab
            def handle_endtag(self, tag):
                if tag == 'div' and self.stack: self.tab = self.stack.pop()
        html = (STATIC.parent/'index.html').read_text()
        self.assertIn('/static/js/app-workspace.js?v=__APP_BUILD__', html)
        self.assertLess(html.index('app-workspace.js'), html.index('app-reports.js'))
        controls = Controls(); controls.feed(html)
        tree = ast.parse((STATIC.parent.parent/'core.py').read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'process_state' for target in node.targets))
        names = [key.value for key in assignment.value.keys]
        self.run_js('const registered='+json.dumps(names)+';const tabs='+json.dumps(sorted(controls.links))+
                    ';const audioCancelTab='+json.dumps(controls.cancel_tab)+r'''
for(const name of registered){assert(tabs.includes(context.getWorkspaceTaskTab(name)),'Missing task screen for '+name);}
assert.equal(context.getWorkspaceTaskTab('audio'),audioCancelTab);
assert.equal(context.getWorkspaceTaskTab('toString'),null);
finished=true;
''')

    def test_script_recovery_candidate_is_navigation_only_and_has_no_eligibility_promise(self):
        self.run_js(r'''
(async()=>{
 const data=snapshot('Interrupted book','script');
 data.script.recovery={status:'candidate',eligibility:'not_rechecked'};
 responses['/api/workspace']=data;await context.refreshBookWorkspace();
 assert(elements('workspace-stages').children[0].children[2].textContent.includes('must be rechecked'));
 await context.continueBookWorkspace();assert.deepStrictEqual(activated,['script']);
 assert(requests.every(path=>['/api/workspace','/api/scripts'].includes(path)));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_readiness_evidence_distinguishes_saved_metadata_and_historical_export_checks(self):
        self.run_js(r'''
(async()=>{
 const data=snapshot();data.audio.review={status:'observed',speech_current_metadata:2,
  speech_not_current:1,speech_not_checked:3,speech_attention:1,drift_flagged:0,unknown_speakers:0};
 data.export_review={status:'observed',verified_at_export:1,review_at_export:0,unavailable:2,
  failed_attempts:1,running:0,current_inputs:'not_rechecked'};
 responses['/api/workspace']=data;await context.refreshBookWorkspace();
 const cards=elements('workspace-stages').children;
 assert(cards[2].children[2].textContent.includes('1 no longer match'));
 assert(cards[2].children[2].textContent.includes('3 unchecked'));
 assert.equal(cards[3].children[0].textContent,'Export');
 assert(cards[3].children[2].textContent.includes('1 passed at export'));
 assert(cards[3].children[2].textContent.includes('Current script and voice inputs have not been rechecked'));
 assert(requests.every(path=>['/api/workspace','/api/scripts'].includes(path)));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_continue_refreshes_identity_and_navigates_without_any_mutating_api(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();assert.equal(elements('workspace-book-name').textContent,'Book A');
 responses['/api/workspace']=snapshot('Book B','voices');
 await context.continueBookWorkspace();assert.deepStrictEqual(activated,['voices']);
 assert.equal(elements('workspace-book-name').textContent,'Book B');
 assert(requests.every(path=>['/api/workspace','/api/scripts'].includes(path)));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_read_failure_keeps_display_but_disables_continue_and_never_navigates(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();fail=true;await context.continueBookWorkspace();
 assert.equal(elements('workspace-book-name').textContent,'Book A');assert(elements('workspace-continue').disabled);
 assert(elements('workspace-feedback').textContent.includes('earlier reading'));assert.equal(activated.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_late_reply_is_ignored_and_queued_refresh_does_not_overlap_requests(self):
        self.run_js(r'''
(async()=>{
 let release;responses['/api/workspace']=()=>new Promise(resolve=>release=resolve);
 const first=context.refreshBookWorkspace();const second=context.refreshBookWorkspace();
 assert.equal(requests.filter(p=>p==='/api/workspace').length,1);
 responses['/api/workspace']=snapshot('Book B');release(snapshot('Book A'));
 assert.equal(await first,null);await second;assert.equal(elements('workspace-book-name').textContent,'Book B');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_invalidation_during_a_read_queues_the_newest_book_and_preserves_library_focus_nodes(self):
        self.run_js(r'''
(async()=>{
 let release;responses['/api/workspace']=()=>new Promise(resolve=>release=resolve);
 responses['/api/scripts']=[{name:'Book A',has_voice_config:true}];
 const first=context.refreshBookWorkspace();context.invalidateBookWorkspace();
 responses['/api/workspace']=snapshot('Book B');release(snapshot('Book A'));await first;
 await new Promise(setImmediate);assert.equal(elements('workspace-book-name').textContent,'Book B');
 assert(!elements('workspace-continue').disabled);assert.equal(requests.filter(p=>p==='/api/workspace').length,2);
 const button=elements('workspace-library').children[0].children[2];await context.refreshBookWorkspace();
 assert.strictEqual(elements('workspace-library').children[0].children[2],button);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_malformed_counts_and_unsafe_next_destination_are_rejected(self):
        self.run_js(r'''
(async()=>{
 for(const bad of [null,{...snapshot(),book_token:'missing'}, {...snapshot(),audio:{...snapshot().audio,total:-1}},
  {...snapshot(),cast:{total:1,pending:2}},
  {...snapshot(),audio:{...snapshot().audio,review:{status:'observed',speech_not_checked:-1}}},
  {...snapshot(),export_review:{status:'observed',current_inputs:'certified'}},
  {...snapshot(),script:{entries:1,recovery:{status:'candidate',eligibility:'certified'}}},snapshot('Bad','training')]){
  responses['/api/workspace']=bad;await context.continueBookWorkspace();assert(elements('workspace-continue').disabled);
 }
 assert.equal(activated.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_hostile_names_are_text_and_library_load_uses_existing_confirmed_loader(self):
        self.run_js(r'''
(async()=>{
 const hostile='<img onerror=steal()>',button={disabled:false};responses['/api/workspace']=snapshot(hostile);
 responses['/api/scripts']=[{name:hostile,has_voice_config:true}];await context.refreshBookWorkspace();
 assert.equal(elements('workspace-book-name').textContent,hostile);
 assert.equal(elements('workspace-book-name').children.length,0);
 assert.equal(elements('workspace-library').children[0].children[0].textContent,hostile);
 await context.openWorkspaceSavedBook(hostile,button);assert(!button.disabled);assert(requests.includes('confirmed-loader:'+hostile));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_failed_library_loader_releases_button_and_refreshes_actual_selected_book(self):
        self.run_js(r'''
(async()=>{
 const button={disabled:false};context.loadScript=async()=>{responses['/api/workspace']=snapshot('Book B');throw Error('Reply lost');};
 await context.openWorkspaceSavedBook('Book B',button);
 assert(!button.disabled);assert.equal(actionErrors.length,1);assert.equal(elements('workspace-book-name').textContent,'Book B');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_monitor_keeps_disclosure_nodes_and_withholds_eta_for_paused_manual_and_cancel(self):
        self.run_js(r'''
(async()=>{
 responses['/api/status']={script:{running:true},new_task:{running:true}};
 responses['/api/status/new_task']={running:true,logs:['<img onerror=bad()>']};
 for(const flags of [{paused:true},{manual_request:{id:'one'}},{cancel:true}]){
  responses['/api/status/script']={running:true,logs:['Step 2 (speakers): 2/4','Retrying... 2 of 3'],eta:{eta_seconds:999,elapsed_seconds:30},...flags};
  await context.refreshWorkspaceMonitor();
  const view=vm.runInContext('workspaceTaskViews.get("script")',context);
  assert(!view.activity.textContent.includes('999'));assert(view.timing.textContent.includes('withheld'));
  if(!flags.cancel){assert(view.activity.textContent.includes('Retrying'));}
  const before=view.card;view.card.querySelectorAll('details')[0].open=true;
  await context.refreshWorkspaceMonitor();assert.strictEqual(vm.runInContext('workspaceTaskViews.get("script").card',context),before);
  assert(view.card.querySelectorAll('details')[0].open);
 }
 await context.openWorkspaceTask('new_task');assert.equal(activated.length,0);
 await context.openWorkspaceTask('script');assert.deepStrictEqual(activated,['script']);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_leaving_home_while_an_action_reads_status_prevents_late_navigation(self):
        self.run_js(r'''
(async()=>{
 let release;responses['/api/workspace']=()=>new Promise(resolve=>release=resolve);
 const pending=context.continueBookWorkspace();context.currentTabName='setup';context.refreshWorkspaceView();
 release(snapshot('Book A','voices'));await pending;assert.equal(activated.length,0);
 context.currentTabName='home';context.reattachRunningPollers=()=>new Promise(resolve=>release=resolve);
 const controls=context.openWorkspaceTask('script');context.currentTabName='setup';context.refreshWorkspaceView();
 release();await controls;assert.equal(activated.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_monitor_ignores_obsolete_visit_and_invalid_registry_without_claiming_idle(self):
        self.run_js(r'''
(async()=>{
 let release;responses['/api/status']=()=>new Promise(resolve=>release=resolve);
 const pending=context.refreshWorkspaceMonitor();context.currentTabName='setup';context.refreshWorkspaceView();
 responses['/api/status/script']={running:true,logs:['Earlier visit']};release({script:{running:true}});
 await pending;assert.equal(elements('workspace-monitor').children.length,0);
 context.currentTabName='home';responses['/api/status']={script:{running:'invalid'}};
 await context.refreshWorkspaceMonitor();assert(elements('workspace-monitor-feedback').textContent.includes('unavailable'));
 await context.openWorkspaceTask('toString');assert.equal(activated.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_hidden_and_stale_monitor_results_do_not_erase_previous_known_work(self):
        self.run_js(r'''
(async()=>{
 responses['/api/status']={script:{running:true}};responses['/api/status/script']={running:true,logs:['Progress: 1/2']};
 await context.refreshWorkspaceMonitor();const before=elements('workspace-monitor').children[0];
 responses['/api/status/script']=Error('status unavailable');await context.refreshWorkspaceMonitor();
 assert.strictEqual(elements('workspace-monitor').children[0],before);
 assert(before.children[2].textContent.includes('earlier reading'));
 fail=true;await context.refreshWorkspaceMonitor();assert.strictEqual(elements('workspace-monitor').children[0],before);
 assert(elements('workspace-monitor-feedback').textContent.includes('registry unavailable'));
 const count=requests.length;context.currentTabName='setup';await context.refreshWorkspaceMonitor();assert.equal(requests.length,count);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
