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
