"""Truthful prerequisite, empty, locale and dependency states in real SPA code."""
from pathlib import Path
from html.parser import HTMLParser
import json
import subprocess
import unittest

from tests.test_ui_help_js import DOM_FIXTURE
from tts_capabilities import get_tts_capability_modes

STATIC = Path(__file__).resolve().parent.parent / 'static/js'
SETUP = r'''
context.window=context;context.console={error(){}};
document.querySelector=selector=>body.querySelectorAll(selector)[0]||null;
Element.prototype.append=function(...nodes){for(const node of nodes){this.appendChild(node);}};
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children]){child.remove();}this.textContent='';this.append(...nodes);};
function add(id,tag='div'){return body.appendChild(new Element(tag,{id}));}
function source(name){return fs.readFileSync(require('path').join(process.argv[1],name),'utf8');}
function run(code){return vm.runInContext(code,context);}
function section(name,start,end){const text=source(name);const offset=text.indexOf(start);assert(offset>=0,start);return text.slice(offset,text.indexOf(end,offset));}
context.currentBookFilename='';context._selectedUploadIdentity=null;
context.escapeHtml=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
context.describeApiError=error=>error.message;
context.getActionErrorMessage=(action,error,recovery)=>action+' '+error.message+' '+recovery;
context.showActionError=()=>{};
'''


class UiBatchAJsTests(unittest.TestCase):
    def test_editor_recovery_is_outside_the_hidden_tools_drawer(self):
        class RecoveryLocation(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.ancestors = None

            def handle_starttag(self, tag, attributes):
                attributes = dict(attributes)
                if attributes.get('id') == 'chunk-load-status':
                    self.ancestors = list(self.stack)
                if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
                    self.stack.append((tag, attributes))

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, -1, -1):
                    if self.stack[index][0] == tag:
                        del self.stack[index:]
                        break

        parser = RecoveryLocation()
        parser.feed((STATIC.parent / 'index.html').read_text(encoding='utf-8'))
        self.assertIsNotNone(parser.ancestors)
        self.assertNotIn('editor-tool-drawer', [attributes.get('id') for _, attributes in parser.ancestors])
        self.assertFalse(any('hidden' in attributes for _, attributes in parser.ancestors))

    def run_js(self, body):
        script = DOM_FIXTURE + SETUP + "\nlet finished=false;process.on('beforeExit',()=>assert(finished,'Test did not finish'));\n(async()=>{\n" + body + r'''
})().then(()=>{finished=true;}).catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(STATIC)], capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_editor_refusal_has_safe_reason_script_action_and_failure_log(self):
        self.run_js(r'''
add('chunk-load-status');const table=add('chunks-table-body');const row=table.appendChild(new Element('tr'));row.appendChild(new Element('td'));
context.invalidateEditorIntegrity=()=>{};context.markEditorReviewStale=()=>{};
run('let deliveryReviewView=0;');
run(section('app-core.js','async function loadChunks(','function getChunkAudioIdentity('));
let error=Object.assign(Error('The selected source has no published script; previous book files were preserved. <unsafe>'),{status:409});
context.ensureChunkRefresh=async()=>{throw error;};await context.loadChunks(true);
const status=document.getElementById('chunk-load-status');
assert.match(status.innerHTML,/no published script/);assert.match(status.innerHTML,/previous book files were preserved/);
assert(!status.innerHTML.includes('<unsafe>'));assert.match(status.innerHTML,/Open Script/);assert(!status.innerHTML.includes('Retry chunks'));
assert.match(status.innerHTML,/\/api\/logs\/diagnostics\?download=true/);assert.match(status.innerHTML,/Review.*sharing/);assert.match(status.innerHTML,/support report/);
assert.match(status.innerHTML,/<div class="alert alert-warning mb-2">/);assert(!status.innerHTML.includes('text-warning'));
error=Object.assign(Error('HTTP 503'),{status:503});await context.loadChunks(true);assert.match(status.innerHTML,/Retry chunks/);
assert.match(status.innerHTML,/<div class="alert alert-warning mb-2">/);assert(!status.innerHTML.includes('text-warning'));
assert(!status.innerHTML.includes('no published script'));
error=TypeError('Failed to fetch');await context.loadChunks(true);assert.match(status.innerHTML,/Retry chunks/);
assert.equal(table.children[0],row);
''')

    def test_locale_repaints_retained_capabilities_without_checking_server(self):
        self.run_js('const modes=' + json.dumps(get_tts_capability_modes()) + ';\n' + r'''
document.documentElement=new Element('html');add('tts-mode').value='local';
const summary=add('tts-capability-summary');summary.setAttribute('data-i18n','setup.tts.backend-capabilities-have-not-loaded-exist');summary.textContent='Backend capabilities have not loaded. Existing settings are preserved.';
run(source('app-i18n.js'));run(source('app-tts-capabilities.js'));
context.API={post(){throw Error('must not contact server');}};
context.renderSetupSearchStatus=()=>{};
run(section('app-settings.js','function renderUiLocaleChrome(','function renderSetupSearchStatus('));
for(const locale of ['en','es-419','ja','zh-CN']){
 run('uiCatalogs.set('+JSON.stringify(locale)+',{messages:'+fs.readFileSync(require('path').join(process.argv[1],'../i18n/ui.'+locale+'.json'),'utf8')+'});');
}
await context.applyUiLocaleAndSearchStatus('en');assert.match(summary.textContent,/unavailable|not loaded/);
context.setTtsCapabilityModes(modes);
for(const mode of ['local','external','mlx']){
 document.getElementById('tts-mode').value=mode;context.applyTtsCapabilityGuidance();
 for(const locale of ['es-419','ja','zh-CN','en']){
  await context.applyUiLocaleAndSearchStatus(locale);
  assert(!summary.textContent.includes('not loaded'));assert(!summary.textContent.includes('have not loaded'));
  assert(summary.textContent.length>80);assert(!summary.textContent.includes('Backend capabilities unavailable'));
  if(locale==='en'){assert.match(summary.textContent,/not.*loaded|not.*readiness|does not.*loaded/i);}
 }
}
context.setTtsCapabilityModes(null);await context.applyUiLocaleAndSearchStatus('en');assert.match(summary.textContent,/unavailable/);
''')

    def test_initial_workspace_invalidation_does_not_claim_active_book_changed(self):
        self.run_js(r'''
for(const id of ['workspace-collection','workspace-collection-feedback','workspace-continue']){add(id);}
context.currentTabName='script';context.refreshBookWorkspace=()=>{};
run(source('app-workspace.js'));context.invalidateBookWorkspace();
assert(!document.getElementById('workspace-collection-feedback').textContent.includes('active book changed'));
run('workspaceBookSnapshot={name:"No book selected",script:{entries:0},cast:{total:0}};');context.invalidateBookWorkspace();
assert(!document.getElementById('workspace-collection-feedback').textContent.includes('active book changed'));
run('workspaceBookSnapshot={name:"Earlier book",script:{entries:1}};');context.invalidateBookWorkspace();
assert.match(document.getElementById('workspace-collection-feedback').textContent,/active book changed/);
''')

    def test_empty_ingest_is_neutral_but_retained_facts_and_network_errors_are_not(self):
        self.run_js(r'''
for(const id of ['script-ingest-feedback','script-ingest-metadata','script-ingest-source-state','script-ingest-cast-state','script-ingest-script-state']){add(id);}
run(source('app-ui-followups.js'));
let error=Object.assign(Error('No input file selected for the active book.'),{status:400,messageKey:'error.upload.no_input_active'});
context.API={get:async()=>{throw error;}};
await context.refreshScriptIngestFacts();assert.match(document.getElementById('script-ingest-feedback').textContent,/No book selected/);
assert(!document.getElementById('script-ingest-feedback').textContent.includes('could not be verified'));
run('scriptIngestFacts={source:{source_name:"Earlier book"}};');await context.refreshScriptIngestFacts();
assert.match(document.getElementById('script-ingest-feedback').textContent,/could not be verified/);
error=Error('Offline');await context.refreshScriptIngestFacts();assert.match(document.getElementById('script-ingest-feedback').textContent,/could not be verified/);
''')

    def test_empty_cast_does_not_claim_rows_were_kept(self):
        self.run_js(r'''
add('cast-list-panel');const status=add('cast-list-status');
run(source('app-i18n.js'));
run('let castListRequest=0,castListLoaded=false,castListMutationPending=false;');
let snapshot='[]';context.getCastListEditorSnapshot=()=>snapshot;
context.API={get:async()=>{throw Object.assign(Error('No input file selected'),{status:400,messageKey:'error.generation.no_input'});}};
run(section('app-core.js','async function loadCastList(show)','window.addCastListRow'));
await context.loadCastList(false);assert.match(status.textContent,/No book selected/);assert.match(status.textContent,/Choose a source above/);assert(!status.textContent.includes('rows were kept'));
snapshot='[["Mira",""]]';await context.loadCastList(false);assert.match(status.textContent,/rows were kept/);
''')

    def test_voicelab_paths_are_expandable_not_readiness_help(self):
        self.run_js(r'''
for(const id of ['vl-rocm_python','vl-profiler_model','vl-zips_dir_cfg','vl-epub_dirs','vl-zips_dir','voicelab-readiness','voicelab-readiness-help','voicelab-diagnostics','voicelab-diagnostics-text','voicelab-recovery']){add(id);}
context.API={get:async()=>({config:{},checks:{rocm_python:false,profiler_model:false,profiler_environment:false},profiler_errors:['Missing llama_cpp at C:/private/python','Missing manifest C:/private/model.json']})};
run(section('app-voicelab.js','function _vlSetCheckIcon(','window.saveVoicelabConfig ='));
await context.loadVoicelabConfig();
const help=document.getElementById('voicelab-readiness-help'),badge=document.getElementById('voicelab-readiness');
assert.match(help.textContent,/Not ready/);assert.match(badge.innerHTML,/3 issues/);assert(!badge.innerHTML.includes('bg-success'));
assert(!help.textContent.includes('C:/private'));assert(!badge.title.includes('C:/private'));
assert.match(document.getElementById('voicelab-diagnostics-text').textContent,/C:\/private\/python/);
assert.equal(document.getElementById('voicelab-diagnostics').hidden,false);
context.API.get=async()=>({config:{},checks:{rocm_python:true},profiler_errors:['Import refused']});await context.loadVoicelabConfig();assert(!badge.innerHTML.includes('bg-success'));
context.API.get=async()=>({config:{},checks:{},profiler_errors:[]});await context.loadVoicelabConfig();assert(!badge.innerHTML.includes('bg-success'));
context.API.get=async()=>({config:{},checks:{rocm_python:null},profiler_errors:[]});await context.loadVoicelabConfig();assert(!badge.innerHTML.includes('bg-success'));
context.API.get=async()=>({config:{},checks:{rocm_python:true},profiler_errors:[]});await context.loadVoicelabConfig();assert(!badge.innerHTML.includes('bg-success'),'Partial dependency metadata is unknown, not ready');
''')
