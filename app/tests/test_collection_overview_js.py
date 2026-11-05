"""Collection overview UI: selection, stale replies and stale-row load refusal."""
import json
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static/js'
SETUP = r'''
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
const visibilityListeners=[];document.addEventListener=(name,fn)=>{if(name==='visibilitychange'){visibilityListeners.push(fn);}};
Element.prototype.append=function(...nodes){for(const node of nodes){this.appendChild(node);}};
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children]){child.remove();}this.append(...nodes);};
Element.prototype.focus=function(){document.activeElement=this;};
for(const id of ['workspace-book-name','workspace-next-reason','workspace-continue','workspace-stages',
 'workspace-feedback','workspace-library-feedback','workspace-library','workspace-monitor-feedback',
 'workspace-monitor','workspace-monitor-title','workspace-collection','workspace-collection-feedback',
 'workspace-collection-show']){body.appendChild(new Element('div',{id}));}
const elements=id=>document.getElementById(id);
const requests=[],activated=[],actionErrors=[],posts=[];
let responses={},fail=false;
context.window=context;context.currentTabName='home';document.visibilityState='visible';
context.TASK_LABELS={script:'Script generation'};
context.API={get:async path=>{requests.push(path);if(fail){throw Error('Offline');}const result=responses[path];if(result instanceof Error){throw result;}return typeof result==='function'?result():result;},
 post:async (path,body)=>{posts.push([path,body]);const key='POST '+path;const result=responses[key];if(result instanceof Error){throw result;}return typeof result==='function'?result(body):result;}};
context.activateTab=name=>{activated.push(name);};
context.reattachRunningPollers=async()=>{};
context.showActionError=(action,error,recovery)=>{actionErrors.push({action,error:error.message,recovery});};
context.loadScript=async name=>{requests.push('confirmed-loader:'+name);};
context.setTimeout=(fn,ms)=>1;context.clearTimeout=()=>{};
const core=fs.readFileSync(process.argv[2],'utf8'),workbench=fs.readFileSync(process.argv[3],'utf8');
let start=core.indexOf('const ACTIVITY_MARKER');let end=core.indexOf('// Every task that renders',start);
vm.runInContext(core.slice(start,end),context);
start=workbench.indexOf('function formatDuration(');end=workbench.indexOf('// Always-visible',start);
vm.runInContext(workbench.slice(start,end),context);
start=workbench.indexOf('function applySystemStatusText(');end=workbench.indexOf('async function updateSystemStats(',start);
vm.runInContext(workbench.slice(start,end),context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
function snapshot(name='Book A'){return {name,book_token:'a'.repeat(64),script:{entries:1},cast:{total:1,pending:0},audio:{status:'not_started',total:0,present:0,failed:0,missing:0,generating:0,message:'Not prepared'},outputs:[{format:'MP3',status:'not_found'}],next:{tab:'editor',label:'Continue',reason:'Review'}};}
function crow(name,sha,overall='needs_review'){return {name,book_id:name,is_active:false,overall,
 script:{status:'available',entries:1,script_sha256:sha,evidence:'Saved entries present.'},
 review:{status:'not_started',evidence:'No receipt.'},
 voices:{status:'needs_decisions',total:1,pending:1,evidence:'1 needs a voice.'},
 render:{status:'unknown',evidence:'Not bound to this saved book.'},
 revision:(sha||'a'.repeat(64)),
 next:{tab:'script',label:'Continue with Script review',reason:'No completed review.'}};}
responses['/api/workspace']=snapshot();responses['/api/scripts']=[{name:'Alpha',has_voice_config:true},{name:'Beta',has_voice_config:false}];responses['/api/status']={};
'''


class CollectionOverviewJsTests(unittest.TestCase):
    def run_js(self, script):
        script = "let finished=false;process.on('beforeExit',()=>assert(finished,'Test did not finish'));\n" + script.replace('})().catch', '})().then(()=>{finished=true;}).catch')
        result = subprocess.run(['node', '-e', DOM_FIXTURE + SETUP + script,
                                 str(STATIC / 'app-workspace.js'), str(STATIC / 'app-core.js'),
                                 str(STATIC / 'app-workbench.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_selection_renders_distinct_rows_with_evidence_and_no_mutating_calls(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 assert.equal(boxes.length,2);boxes.forEach(box=>{box.checked=true;});
 responses['POST /api/collection/overview']=[crow('Alpha','a'.repeat(64)),crow('Beta','b'.repeat(64),'needs_voices')];
 await context.showCollectionOverview();
 assert.equal(JSON.stringify(posts),JSON.stringify([['/api/collection/overview',{names:['Alpha','Beta']}]]));
 const rows=elements('workspace-collection').querySelectorAll('tbody')[0].children;
 assert.equal(rows.length,2);
 assert.equal(rows[0].children[0].children[0].textContent,'Alpha');
 assert(rows[0].children[2].children[0].children[0].textContent.includes('Script: available'));
 assert(rows[0].children[2].children[0].children[3].textContent.includes('Render/export: unknown'));
 assert.equal(rows[1].children[0].children[0].textContent,'Beta');
 assert.equal(rows[1].children[1].textContent,'needs_voices');
 assert(elements('workspace-collection-feedback').textContent.includes('started no work'));
 assert(!requests.some(p=>p.startsWith('confirmed-loader')));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_no_selection_and_stale_reply_are_refused(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 await context.showCollectionOverview();
 assert(elements('workspace-collection-feedback').textContent.includes('at least one'));
 assert.equal(posts.length,0);
 const releases=[];responses['POST /api/collection/overview']=()=>new Promise(resolve=>releases.push(resolve));
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes[0].checked=true;
 const first=context.showCollectionOverview();
 boxes[0].checked=false;boxes[1].checked=true;
 const second=context.showCollectionOverview();
 releases[0]([crow('Alpha','a'.repeat(64))]);await first;
 releases[1]([crow('Beta','b'.repeat(64))]);await second;
 const rows=elements('workspace-collection').querySelectorAll('tbody')[0].children;
 assert.equal(rows.length,1);assert.equal(rows[0].children[0].children[0].textContent,'Beta');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_stale_row_load_is_refused_after_revision_change(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes[0].checked=true;
 responses['POST /api/collection/overview']=[crow('Alpha','a'.repeat(64))];
 await context.showCollectionOverview();
 const open=elements('workspace-collection').querySelectorAll('tbody')[0].children[0].children[4].children[0];
 const before=posts.length;
 responses['POST /api/collection/overview']=[crow('Alpha','c'.repeat(64))];
 await context.openCollectionBook(crow('Alpha','a'.repeat(64)),open);
 assert(!requests.some(p=>p.startsWith('confirmed-loader')));
 assert(elements('workspace-collection-feedback').textContent.includes('changed since'));
 // With a matching fresh revision, the existing confirmed loader is used.
 responses['POST /api/collection/overview']=[crow('Alpha','a'.repeat(64))];
 await context.openCollectionBook(crow('Alpha','a'.repeat(64)),open);
 assert(requests.includes('confirmed-loader:Alpha'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_hostile_book_names_are_text_only(self):
        self.run_js(r'''
(async()=>{
 const hostile='<img onerror=steal()>';
 responses['/api/scripts']=[{name:hostile,has_voice_config:true}];
 await context.refreshBookWorkspace();
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 assert.equal(boxes.length,1);boxes[0].checked=true;
 responses['POST /api/collection/overview']=[crow(hostile,'d'.repeat(64))];
 await context.showCollectionOverview();
 const rows=elements('workspace-collection').querySelectorAll('tbody')[0].children;
 assert.equal(rows.length,1);
 assert.equal(rows[0].children[0].children[0].textContent,hostile);
 assert.equal(rows[0].children[0].children[0].children.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_malformed_overview_is_rejected_without_loading(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes[0].checked=true;
 responses['POST /api/collection/overview']=[{name:'Alpha'}];
 await context.showCollectionOverview();
 assert.equal(elements('workspace-collection').children.length,0);
 assert(elements('workspace-collection-feedback').textContent.includes('incomplete'));
 assert(!requests.some(p=>p.startsWith('confirmed-loader')));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_empty_show_invalidates_inflight_request(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const releases=[];responses['POST /api/collection/overview']=()=>new Promise(resolve=>releases.push(resolve));
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes[0].checked=true;
 const first=context.showCollectionOverview();
 boxes[0].checked=false;
 await context.showCollectionOverview();
 assert(elements('workspace-collection-feedback').textContent.includes('at least one'));
 releases[0]([crow('Alpha','a'.repeat(64))]);await first;
 assert.equal(elements('workspace-collection').children.length,0);
 assert(elements('workspace-collection-feedback').textContent.includes('at least one'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_over_fifty_selection_is_refused_before_post(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const many=[];for(let i=0;i<51;i++){many.push({name:'Book'+i,has_voice_config:false});}
 responses['/api/scripts']=many;
 await context.refreshBookWorkspace();
 [...elements('workspace-library').querySelectorAll('input[data-collection-select]')].forEach(box=>{box.checked=true;});
 const before=posts.length;
 await context.showCollectionOverview();
 assert(elements('workspace-collection-feedback').textContent.includes('at most 50'));
 assert.equal(posts.length,before);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_selection_survives_library_refresh_and_error_shows_detail(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 let boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes[0].checked=true;
 responses['/api/scripts']=[{name:'Alpha',has_voice_config:false},{name:'Beta',has_voice_config:false}];
 await context.refreshBookWorkspace();
 boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 assert.equal(boxes[0].checked,true);
 assert.equal(boxes[1].checked,false);
 responses['POST /api/collection/overview']=new Error('Saved books could not be read');
 await context.showCollectionOverview();
 assert(elements('workspace-collection-feedback').textContent.includes('could not be read'));
 assert(elements('workspace-collection-feedback').textContent.includes('Saved books could not be read'));
 assert.equal(elements('workspace-collection').children.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_revision_proof_is_transported_to_load(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const loaderCalls=[];
 context.loadScript=async (name,revision)=>{loaderCalls.push([name,revision]);};
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes[0].checked=true;
 responses['POST /api/collection/overview']=[crow('Alpha','a'.repeat(64))];
 await context.showCollectionOverview();
 const open=elements('workspace-collection').querySelectorAll('tbody')[0].children[0].children[4].children[0];
 assert.equal(open.disabled,false);
 await context.openCollectionBook(crow('Alpha','a'.repeat(64)),open);
 assert.equal(JSON.stringify(loaderCalls),JSON.stringify([['Alpha','a'.repeat(64)]]));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_missing_row_shows_but_disables_load(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes[0].checked=true;
 const missing={name:'Alpha',book_id:'Alpha',is_active:false,overall:'unknown',
  script:{status:'missing',entries:0,script_sha256:null,evidence:'Missing.'},
  review:{status:'unknown',evidence:'No script.'},
  voices:{status:'unknown',total:0,pending:0,evidence:'No script.'},
  render:{status:'unknown',evidence:'Not bound.'},revision:null,
  next:{tab:'script',label:'Inspect saved book',reason:'Missing.'}};
 responses['POST /api/collection/overview']=[missing];
 await context.showCollectionOverview();
 const open=elements('workspace-collection').querySelectorAll('tbody')[0].children[0].children[4].children[0];
 assert.equal(open.disabled,true);
 responses['POST /api/collection/overview']=[missing];
 await context.openCollectionBook(missing,open);
 assert(!requests.some(p=>p.startsWith('confirmed-loader')));
 assert(elements('workspace-collection-feedback').textContent.includes('no valid revision proof'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_recorded_review_ready_and_completed_rows_render(self):
        self.run_js(r'''
(async()=>{
 await context.refreshBookWorkspace();
 const boxes=[...elements('workspace-library').querySelectorAll('input[data-collection-select]')];
 boxes.forEach(box=>{box.checked=true;});
 const ready={name:'Alpha',book_id:'Alpha',is_active:false,overall:'ready_for_audio',
  script:{status:'available',entries:1,script_sha256:'e'.repeat(64),evidence:'Saved entries present.'},
  review:{status:'recorded',current_settings:'not_rechecked',evidence:'Recorded for these entries.'},
  voices:{status:'ready',total:1,pending:0,evidence:'Prepared.'},
  render:{status:'unknown',evidence:'Not bound to this saved book.'},
  revision:'e'.repeat(64),
  next:{tab:'editor',label:'Continue in Editor',reason:'Ready.'}};
 const done={name:'Beta',book_id:'Beta',is_active:true,overall:'completed',
  script:{status:'available',entries:1,script_sha256:'f'.repeat(64),evidence:'Saved entries present.'},
  review:{status:'recorded',current_settings:'not_rechecked',evidence:'Recorded for these entries.'},
  voices:{status:'ready',total:1,pending:0,evidence:'Prepared.'},
  render:{status:'complete',evidence:'Bound full-book receipt (metadata-only).'},
  revision:'f'.repeat(64),
  next:{tab:'audio',label:'Review bound export evidence',reason:'Bound.'}};
 responses['POST /api/collection/overview']=[ready,done];
 await context.showCollectionOverview();
 const cells=elements('workspace-collection').querySelectorAll('tbody')[0].children;
 assert.equal(cells.length,2);
 assert.equal(cells[0].children[1].textContent,'ready_for_audio');
 assert(cells[0].children[2].children[0].children[1].textContent.includes('Review: recorded'));
 assert(cells[0].children[2].children[0].children[3].textContent.includes('Render/export: unknown'));
 assert.equal(cells[1].children[1].textContent,'completed');
 assert(cells[1].children[2].children[0].children[3].textContent.includes('Render/export: complete'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
