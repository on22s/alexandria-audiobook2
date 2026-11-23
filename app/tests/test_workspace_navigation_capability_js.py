"""Joined validator/refresh/Continue/real-activateTab navigation capability tests."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from book_workspace import WORKSPACE_ENUMS, get_book_workspace
from project import ProjectManager
from routers.voices import get_voice_rows
from tests.test_book_workspace_js import SETUP, STATIC
from tests.test_ui_help_js import DOM_FIXTURE


class WorkspaceNavigationCapabilityJsTests(unittest.TestCase):
    def payloads(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'state.json').write_text(json.dumps(
                {'active_book_id': 'Book A', 'book_generation': 'one'}))
            (directory / 'annotated_script.json').write_text(json.dumps(
                [{'type': 'dialogue', 'speaker': 'ALICE', 'text': 'Synthetic line.'}]))
            (directory / 'voice_config.json').write_text(json.dumps(
                {'ALICE': {'type': 'custom', 'description': 'Synthetic voice'}}))
            producer = ProjectManager(root)
            new = get_book_workspace(root, {'MP3': directory / 'out.mp3'},
                                     get_voice_rows, producer.get_chunk_audio_path)
        old = copy.deepcopy(new)
        old.pop('enums', None)
        old['outputs'][0]['status'] = 'not_found'
        for value in ('training', 'result'):
            if value not in new['enums']['nextTab']:
                new['enums']['nextTab'].append(value)
        if 'verified' not in new['enums']['outputStatus']:
            new['enums']['outputStatus'].append('verified')
        return old, new

    def run_js(self, script):
        old, new = self.payloads()
        result = subprocess.run(
            ['node', '-e', DOM_FIXTURE + SETUP + script,
             str(STATIC / 'app-workspace.js'), str(STATIC / 'app-core.js'),
             str(STATIC / 'app-workbench.js'), json.dumps(old), json.dumps(new)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_continue_dispatches_only_client_supported_destinations(self):
        self.run_js(r'''
const old=JSON.parse(process.argv[4]), newer=JSON.parse(process.argv[5]);
const html=fs.readFileSync(process.argv[1].replace('js/app-workspace.js','index.html'),'utf8');
for(const name of ['training']){
 if(html.includes('data-tab="'+name+'"'))body.appendChild(new Element('a',{class:'nav-link','data-tab':name}));
 if(html.includes('id="'+name+'-tab"'))body.appendChild(new Element('div',{class:'tab-content',id:name+'-tab'}));
}
body.appendChild(new Element('div',{id:'navbarNav'}));
Object.defineProperty(Element.prototype,'classList',{get(){const node=this;const set=()=>new Set(node.className.split(/\s+/).filter(Boolean));return {contains:k=>set().has(k),add(k){node.className=[...set().add(k)].join(' ');},remove(k){const next=set();next.delete(k);node.className=[...next].join(' ');}};}});
context.location={hash:'#home'};context.history={pushState:(a,b,h)=>{context.location.hash=h;}};
const loads=[];
for(const name of ['loadChunks','loadLoraDatasets','loadLoraModels','loadFinalAudio','loadVoices']){context[name]=()=>loads.push(name);}
vm.runInContext(core.slice(core.indexOf('function rememberTab('),core.indexOf('function restoreTab(')),context);
(async()=>{
 const expected={script:[],voices:['loadVoices'],editor:['loadChunks'],audio:['loadFinalAudio'],training:[],result:[]};
 for(const [label,payload] of [['old',old],['new',newer]]){
  for(const name of ['script','voices','editor','audio','training','result']){
   const data=structuredClone(payload);
   data.next.tab=name;
   data.outputs[0].status=label==='new'?'verified':'not_found';
   context.currentTabName='home';context.location.hash='#home';loads.length=0;
   responses['/api/workspace']=data;
   await context.refreshBookWorkspace();
   const enabled=!elements('workspace-continue').disabled;
   const valid=context.isWorkspaceSnapshotValid(data);
   const stage=elements('workspace-stages').children[3];
   const status=stage?stage.children[1].textContent:null;
   await context.continueBookWorkspace();
   const supported=['script','voices','editor','audio'].includes(name);
   const dispatched=valid&&supported;
   assert.equal(enabled,dispatched,label+'/'+name+' enabled');
   assert.equal(context.location.hash,dispatched?'#'+name:'#home',label+'/'+name+' hash');
   const target=document.getElementById(name+'-tab');
   assert(dispatched?(target&&target.style.display==='block'):(!target||target.style.display!=='block'),label+'/'+name+' panel');
   assert.deepStrictEqual(loads,dispatched?(expected[name]||[]):[],label+'/'+name+' loaders');
   if(valid){assert(status.includes(label==='new'?'status not recognized':'no file found'),label+'/'+name+' output wording');}
  }
 }
 const undeclared=structuredClone(newer);undeclared.next.tab='training';delete undeclared.enums;
 assert(!context.isWorkspaceSnapshotValid(undeclared),'old payload must reject training');
 const declared=structuredClone(newer);declared.next.tab='training';
 assert(context.isWorkspaceSnapshotValid(declared),'new payload must keep training as valid data');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_explicit_not_found_keeps_no_file_wording(self):
        self.run_js(r'''
const data=structuredClone(snapshot());data.outputs[0].status='not_found';
responses['/api/workspace']=data;
(async()=>{
 await context.refreshBookWorkspace();
 const status=elements('workspace-stages').children[3].children[1].textContent;
 assert(status.includes('no file found'),status);
 assert(!status.includes('status not recognized'),status);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


if __name__ == '__main__':
    unittest.main()
