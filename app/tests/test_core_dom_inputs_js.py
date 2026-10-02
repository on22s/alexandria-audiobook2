"""Untrusted stored tab names and persisted filenames stay literal UI data."""
import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class CoreDomInputTests(unittest.TestCase):
    def run_js(self, code):
        script = "const fs=require('fs'),vm=require('vm'),assert=require('assert');const source=fs.readFileSync(process.argv[1],'utf8');\n" + code
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_tab_restore_uses_exact_existing_names_and_storage_failure_is_safe(self):
        self.run_js(r'''
let stored='',blocked=false,clicked=[];
const links=['setup','voices','editor'].map(name=>({dataset:{tab:name},click:()=>clicked.push(name)}));
const ctx={localStorage:{getItem:()=>{if(blocked){throw Error('private storage');}return stored;}},document:{
 querySelector:selector=>{if(selector.includes('"x')){throw Error('invalid CSS selector');}return links.find(link=>selector===`.nav-link[data-tab="${link.dataset.tab}"]`);},
 querySelectorAll:selector=>{assert.strictEqual(selector,'.nav-link');return links;}}};
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('const TAB_STORAGE_KEY ='),source.indexOf("document.querySelectorAll('.nav-link').forEach")),ctx);
for(const name of ['', 'setup', 'unknown', '"x][data-tab="voices', 'voices"] , .nav-link', '日本語']){stored=name;assert.doesNotThrow(()=>ctx.restoreTab());assert.deepStrictEqual(clicked,[]);}
for(const name of ['voices','editor']){stored=name;ctx.restoreTab();}assert.deepStrictEqual(clicked,['voices','editor']);
blocked=true;assert.doesNotThrow(()=>ctx.restoreTab());assert.deepStrictEqual(clicked,['voices','editor']);
''')

    def test_actual_config_loader_displays_filenames_as_text(self):
        names = ['normal.epub', '<img src=x onerror="boom()">.epub', '</span><script>boom()</script>', "日本語 & 'quotes' \"test\""]
        output = self.run_js('const names=' + json.dumps(names) + r''';
const fields={},errors=[],rendered=[];let filename;
const element=id=>fields[id]||(fields[id]={value:'preset text',innerHTML:'',checked:false});
const ctx={console:{error:(...args)=>errors.push(args),warn:(...args)=>errors.push(args)},document:{getElementById:element},llmProfiles:{},passPromptDefaults:{pass1:{system_prompt:'preset',user_prompt:'preset'},pass3:{system_prompt:'preset',user_prompt:'preset'}},API:{get:async path=>{assert.strictEqual(path,'/api/config');return {tts:{},current_file:filename};}}};
for(const name of ['renderConfigWarnings','applyPauseSupport','renderActiveLlmModeBadge','populateLlmInputs','onLlmModeChange','toggleSubBatchFields','toggleTTSMode','renderPromptPresets','renderPassPromptPresets']){ctx[name]=()=>{};}
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function escapeHtml('),source.indexOf('// Parse a numeric input')),ctx);
const bookStart=source.indexOf('let currentBookFilename =');if(bookStart>=0){vm.runInContext(source.slice(bookStart,source.indexOf('async function loadConfig()',bookStart)),ctx);}
vm.runInContext(source.slice(source.indexOf('async function loadConfig()'),source.indexOf('// Reset prompts and generation settings to factory defaults')),ctx);
(async()=>{for(const name of names){filename=name;await ctx.loadConfig();rendered.push(element('upload-status').innerHTML);}assert.deepStrictEqual(errors,[]);console.log(JSON.stringify(rendered));})().catch(e=>{console.error(e);process.exitCode=1;});
''')
        class Parsed(HTMLParser):
            def __init__(self, html):
                super().__init__(convert_charrefs=True)
                self.tags = []
                self.text = ''
                self.feed(html)

            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, dict(attrs)))

            def handle_data(self, text):
                self.text += text

        for name, html in zip(names, json.loads(output)):
            with self.subTest(name=name):
                parsed = Parsed(html)
                self.assertEqual('Loaded: ' + name, parsed.text)
                self.assertEqual(['span', 'i'], [tag for tag, _ in parsed.tags])
                self.assertTrue(all(set(attrs) == {'class'} for _, attrs in parsed.tags))
