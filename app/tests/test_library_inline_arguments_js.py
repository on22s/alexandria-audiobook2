"""Render actual library lists, HTML-decode and invoke their real button handlers."""
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static/js'
SOURCE = Path(os.environ.get('LIBRARY_INLINE_SOURCE', STATIC / 'app-scripts.js'))


class Buttons(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        events = [(key, value) for key, value in attrs if key.startswith('on')]
        if events:
            self.events.append((tag, events))


class LibraryInlineArgumentsJsTests(unittest.TestCase):
    def run_js(self, code, payload):
        setup = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),scripts=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8'),payload=JSON.parse(process.argv[3]);
const elements={};const el=id=>elements[id]||(elements[id]={innerHTML:''});
const ctx={window:null,document:{getElementById:el},console,API:{get:async()=>payload},showToast:message=>{throw Error(message);}};ctx.window=ctx;vm.createContext(ctx);
vm.runInContext(core.slice(core.indexOf('function escapeHtml('),core.indexOf('// Parse a numeric input')),ctx);
vm.runInContext(core.slice(core.indexOf('async function _loadScriptList('),core.indexOf('function _renderScriptCheckboxList(')),ctx);
vm.runInContext(scripts.slice(scripts.indexOf('async function loadSavedScripts()'),scripts.indexOf('let _savedScriptAuditRequest =')),ctx);
vm.runInContext(scripts.slice(scripts.indexOf('async function loadDesignedVoices()'),scripts.indexOf('function invalidateDesignerWork()')),ctx);
'''
        script = setup + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(STATIC / 'app-core.js'), json.dumps(payload)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_saved_script_and_designed_voice_buttons_preserve_literals_after_html_decoding(self):
        values = ["O'Brien", "x' onmouseover='globalThis.compromised=true", "');globalThis.compromised=true;//", 'double " quote', 'slash\\end', 'line\nbreak', '</script>&#39;日本🙂', '\u2028\u2029']
        rows = [{'name':value,'id':value,'filename':value+'.wav','created':0,'description':value} for value in values]
        markup = self.run_js("await ctx.loadSavedScripts();await ctx.loadDesignedVoices();console.log(JSON.stringify([el('saved-scripts-list').innerHTML,el('designed-voices-list').innerHTML]));", rows)
        decoded = []
        for html in markup:
            buttons = Buttons(html)
            self.assertEqual(len(values) * 3, len(buttons.events))
            for tag, events in buttons.events:
                self.assertEqual('button', tag)
                self.assertEqual(['onclick'], [key for key, _ in events])
                decoded.append(events[0][1])
        calls = self.run_js(r'''
const calls=[];globalThis.compromised=false;
for(const name of ['auditSavedScript','loadScript','deleteScript','playDesignedVoice','openDesignedVoiceForEdit','deleteDesignedVoice']){globalThis[name]=value=>calls.push({name,value});}
for(const handler of payload){new Function(handler)();}assert.strictEqual(globalThis.compromised,false);console.log(JSON.stringify(calls));
''', decoded)
        expected = []
        for value in values:
            expected.extend({'name':name,'value':value} for name in ('auditSavedScript','loadScript','deleteScript'))
        for value in values:
            expected.extend([{'name':'playDesignedVoice','value':value+'.wav'}, {'name':'openDesignedVoiceForEdit','value':value}, {'name':'deleteDesignedVoice','value':value}])
        self.assertEqual(expected, calls)

    def test_library_removal_captures_selection_and_blocks_duplicates(self):
        script=r'''const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');
let confirm,decision;const messages=[],requests=[],toasts=[];const select={value:'clone:A /'};const card={isConnected:true,querySelector:()=>select};const btn={disabled:false,closest:()=>card};let refreshed=0;
const c={window:null,Set,showConfirm:m=>{messages.push(m);return new Promise(r=>confirm=r);},showToast:(...v)=>toasts.push(v),fetch:async path=>{requests.push(path);return {ok:true};},API:{get:async()=>[]},loadVoices:async()=>refreshed++,loadDesignedVoices:async()=>refreshed++};c.window=c;vm.createContext(c);{const guidanceCore=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm(')),c);}
let a=s.indexOf('const pendingLibraryVoiceRemovals =');vm.runInContext(s.slice(a,s.indexOf('window.openDesignedVoiceForEdit',a)),c);a=s.indexOf('window.deleteCloneVoice =');vm.runInContext(s.slice(a,s.indexOf('// --- LoRA Training ---',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
c._cloneVoicesCache=[{id:'A /',name:'Alice clone'}];let p=c.deleteCloneVoice(btn);assert(btn.disabled);assert(messages.at(-1).includes('Alice clone'));await c.deleteCloneVoice(btn);assert.strictEqual(messages.length,1);confirm(false);await p;assert(!btn.disabled);assert.strictEqual(requests.length,0);
p=c.deleteCloneVoice(btn);select.value='clone:B';confirm(true);await p;assert.strictEqual(requests.length,0);assert(toasts.at(-1)[0].includes('changed'));assert(!btn.disabled);
select.value='clone:A /';p=c.deleteCloneVoice(btn);confirm(true);await p;assert.strictEqual(requests.pop(),'/api/clone_voices/A%20%2F');assert.strictEqual(refreshed,1);assert(!btn.disabled);
c._designedVoicesCache=[{id:'D /',name:'Dana design'}];p=c.deleteDesignedVoice('D /',btn);assert(messages.at(-1).includes('Dana design'));await c.deleteDesignedVoice('D /',{disabled:false});const count=messages.length;confirm(true);await p;assert.strictEqual(messages.length,count);assert.strictEqual(requests.pop(),'/api/voice_design/D%20%2F');assert(!btn.disabled);
c.fetch=async()=>{throw Error('offline');};p=c.deleteDesignedVoice('D /',btn);confirm(true);await p;assert(!btn.disabled);assert(toasts.at(-1)[0].includes('offline'));p=c.deleteDesignedVoice('D /',btn);confirm(false);await p;assert(!btn.disabled);finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result=subprocess.run(['node','-e',script,str(SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_designed_library_failure_keeps_rows_and_stale_responses_cannot_replace_recovery(self):
        result=self.run_js(r'''
ctx.showToast=()=>{};ctx.console={error(){}};await ctx.loadDesignedVoices();const before=el('designed-voices-list').innerHTML;const cache=ctx._designedVoicesCache;
ctx.API.get=async()=>{throw Error('<internal>');};await ctx.loadDesignedVoices();assert.strictEqual(el('designed-voices-list').innerHTML,before);assert.strictEqual(ctx._designedVoicesCache,cache);assert(el('designed-voices-load-status').textContent.includes('use Refresh'));assert(!el('designed-voices-load-status').textContent.includes('<internal>'));
for(const failure of [false,true]){let resolve,reject;ctx.API.get=()=>new Promise((r,j)=>{resolve=r;reject=j;});const old=ctx.loadDesignedVoices();ctx.API.get=async()=>[{id:'fresh',name:'fresh',description:'fresh',filename:'fresh.wav'}];await ctx.loadDesignedVoices();const fresh=el('designed-voices-list').innerHTML;if(failure){reject(Error('old'));}else{resolve([]);}await old;assert.strictEqual(el('designed-voices-list').innerHTML,fresh);assert.strictEqual(ctx._designedVoicesCache[0].id,'fresh');assert.strictEqual(el('designed-voices-load-status').textContent,'');}
console.log(JSON.stringify({verified:true}));
''',[{'id':'old','name':'old','description':'old','filename':'old.wav'}])
        self.assertEqual({'verified':True},result)
