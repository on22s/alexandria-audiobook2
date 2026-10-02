"""Saved-script handlers use actual shared HTTP decoding and preserve failed state."""
from pathlib import Path
import os
import subprocess
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static/js'
SOURCE = Path(os.environ.get('SAVED_SCRIPT_SOURCE', STATIC / 'app-scripts.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8');
const turn=()=>new Promise(setImmediate);
function client(){
 const fields={},calls=[],toasts=[],effects=[];let response=new Response('{}'),flush=Promise.resolve(),choice=true;
 const el=id=>fields[id]||(fields[id]={value:id==='save-script-name'?'Book #? 日本':'',style:{}});
 const ctx={document:{getElementById:el},console:{error(){}},fetch:async(url,options)=>{calls.push({url,...options});return response;},showConfirm:async()=>choice,flushVoiceSaves:()=>flush,showToast:(...args)=>toasts.push(args),
 applyCurrentBookFilename:name=>effects.push(['book',name]),clearCharacterAliases:()=>effects.push(['aliases']),resetDesignerForm:()=>effects.push(['designer']),clearVoiceSuggestions:()=>effects.push(['suggestions']),loadCharacterAliases:async()=>effects.push(['alias-load']),loadChunks:async()=>effects.push(['chunks']),loadVoices:async()=>effects.push(['voices']),loadDesignedVoices:()=>effects.push(['designs'])};
 vm.createContext(ctx);vm.runInContext(core.slice(core.indexOf('const API ='),core.indexOf('function getTaskLogUpdate(')),ctx);
 vm.runInContext(source.slice(source.indexOf('async function saveScript()'),source.indexOf('// --- Voice Designer ---')),ctx);ctx.loadSavedScripts=()=>effects.push(['saved-list']);
 return {ctx,el,calls,toasts,effects,setResponse:value=>response=value,setFlush:value=>flush=value,setChoice:value=>choice=value};
}
const operations=c=>[()=>c.ctx.saveScript(),()=>c.ctx.loadScript('Book #? 日本'),()=>c.ctx.deleteScript('Book #? 日本')];
'''


class SavedScriptApiJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\nlet finished=false;process.on("beforeExit",()=>assert(finished,"Saved-script assertions must finish"));\n(async()=>{\n' + code + '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(STATIC / 'app-core.js')], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_html_http_errors_keep_status_message_and_do_not_apply_success_effects(self):
        self.run_js(r'''
for(let index=0;index<3;index++){for(const statusText of ['Bad Gateway','']){
 const c=client();c.setResponse(new Response('<html>proxy failure</html>',{status:502,statusText,headers:{'Content-Type':'text/html'}}));await operations(c)[index]();
 assert.strictEqual(c.calls.length,1);assert.strictEqual(c.toasts.length,1);assert.strictEqual(c.toasts[0][1],'error');assert(c.toasts[0][0].endsWith(statusText||'HTTP 502'),c.toasts[0][0]);assert.deepStrictEqual(c.effects,[]);assert.strictEqual(c.el('save-script-name').value,'Book #? 日本');
}}
''')

    def test_structured_server_error_message_and_declined_confirmation_are_preserved(self):
        self.run_js(r'''
for(let index=0;index<3;index++){
 const c=client();c.setResponse(new Response(JSON.stringify({detail:{message:'Keep validated checkpoint',code:'fixture'}}),{status:409}));await operations(c)[index]();assert.match(c.toasts[0][0],/Keep validated checkpoint/);assert.deepStrictEqual(c.effects,[]);
}
for(const index of [1,2]){const c=client();c.setChoice(false);await operations(c)[index]();assert.strictEqual(c.calls.length,0);assert.strictEqual(c.toasts.length,0);assert.deepStrictEqual(c.effects,[]);}
''')

    def test_success_uses_acknowledged_book_name_and_existing_flush_and_json_contract(self):
        self.run_js(r'''
for(const index of [0,1]){
 const c=client();let resolve;c.setFlush(new Promise(r=>resolve=r));c.setResponse(new Response(JSON.stringify({status:index===0?'saved':'loaded',name:'Canonical Book'})));
 const pending=operations(c)[index]();await turn();assert.strictEqual(c.calls.length,0);resolve();await pending;
 assert.strictEqual(c.calls[0].method,'POST');assert.strictEqual(c.calls[0].headers['Content-Type'],'application/json');assert.deepStrictEqual(JSON.parse(c.calls[0].body),{name:'Book #? 日本'});
 if(index===0){assert.strictEqual(c.el('save-script-name').value,'');assert.deepStrictEqual(c.effects,[['saved-list']]);}else{assert.deepStrictEqual(c.effects,[['book','Canonical Book.json'],['aliases'],['designer'],['suggestions'],['alias-load'],['chunks'],['voices'],['saved-list'],['designs']]);}
}
const c=client();c.setResponse(new Response(JSON.stringify({status:'deleted'})));await operations(c)[2]();assert.strictEqual(c.calls[0].method,'DELETE');assert.strictEqual(c.calls[0].url,'/api/scripts/'+encodeURIComponent('Book #? 日本'));assert.deepStrictEqual(c.effects,[['saved-list']]);
''')
