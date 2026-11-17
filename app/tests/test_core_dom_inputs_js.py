"""Untrusted stored tab names and persisted filenames stay literal UI data."""
import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class CoreDomInputTests(unittest.TestCase):
    def test_phone_disclosures_follow_breakpoints_and_keep_actionable_review_open(self):
        self.run_js(r'''
const details=[{open:true},{open:true}], media={matches:true,addEventListener(event,handler){assert.equal(event,'change');this.change=handler;}};
const sandbox={window:{matchMedia(query){assert.equal(query,'(max-width: 575px)');return media;}},document:{querySelectorAll(query){assert.equal(query,'[data-phone-disclosure]');return details;}}};
vm.runInNewContext(fs.readFileSync(process.argv[1].replace('app-core.js','app-phone-layout.js'),'utf8'),sandbox);
assert(details.every(item=>!item.open));details[0].open=true;
assert.equal(details[0].open,true);media.matches=false;media.change();assert(details.every(item=>item.open));
details[0].dataset={actionable:'true'};media.matches=true;media.change();assert.equal(details[0].open,true);assert.equal(details[1].open,false);
''')

    def run_js(self, code):
        script = "const fs=require('fs'),vm=require('vm'),assert=require('assert');const source=fs.readFileSync(process.argv[1],'utf8');\n" + code
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_tab_restore_uses_exact_existing_names_and_storage_failure_is_safe(self):
        self.run_js(r'''
function client(stored='',hash=''){
 let blocked=false;const loads=[],events={},pushes=[],replaces=[],tabs={};
 const links=['setup','voices','editor'].map(name=>({dataset:{tab:name},classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){},addEventListener(_event,fn){this.click=fn;}}));
 for(const name of ['setup','voices','editor']){tabs[name+'-tab']={style:{display:name==='setup'?'block':'none'}};}
 const window={location:{hash},history:{pushState(_s,_t,h){pushes.push(h);window.location.hash=h;},replaceState(_s,_t,h){replaces.push(h);window.location.hash=h;}},addEventListener:(event,fn)=>events[event]=fn};
 const ctx={window,localStorage:{getItem:()=>{if(blocked){throw Error('private storage');}return stored;},setItem:(_k,v)=>{if(blocked){throw Error('private storage');}stored=v;}},document:{
 querySelector(){throw Error('tab data must not become a CSS selector');},querySelectorAll:selector=>selector==='.nav-link'?links:Object.values(tabs),getElementById:id=>id==='navbarNav'?{classList:{contains:()=>false}}:tabs[id]},pollLmStudioStatus:()=>loads.push('setup'),loadChunks:()=>loads.push('editor'),loadVoices:reuse=>{assert.strictEqual(reuse,false);loads.push('voices');}};
 vm.createContext(ctx);ctx.t=ctx.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english)));vm.runInContext(source.slice(source.indexOf('const TAB_STORAGE_KEY ='),source.indexOf('// --- LLM model picker')),ctx);
 return {ctx,window,links,loads,pushes,replaces,tabs,events,block:()=>blocked=true};
}
for(const name of ['', 'setup', 'unknown', '"x][data-tab="voices', 'voices"] , .nav-link', '日本語']){
 const c=client(name);assert.doesNotThrow(()=>c.ctx.restoreTab());assert.deepStrictEqual(c.loads,[]);assert.deepStrictEqual(c.pushes,[]);assert.deepStrictEqual(c.replaces,['#setup']);assert.strictEqual(c.tabs['setup-tab'].style.display,'block');
}
for(const name of ['voices','editor']){const c=client(name);c.ctx.restoreTab();assert.deepStrictEqual(c.loads,[name]);assert.deepStrictEqual(c.replaces,['#'+name]);assert.deepStrictEqual(c.pushes,[]);}
const privateStorage=client('editor','#voices');privateStorage.block();assert.doesNotThrow(()=>privateStorage.ctx.restoreTab());assert.deepStrictEqual(privateStorage.loads,['voices']);
const c=client('editor','#voices');c.ctx.restoreTab();assert.deepStrictEqual(c.loads,['voices'],'URL overrides remembered tab');
c.links[2].click({currentTarget:c.links[2],preventDefault(){}});assert.deepStrictEqual(c.pushes,['#editor']);
c.links[2].click({currentTarget:c.links[2],preventDefault(){}});assert.deepStrictEqual(c.pushes,['#editor'],'repeated current click must not add a history entry');
c.window.location.hash='#voices';c.events.popstate();c.events.hashchange();assert.deepStrictEqual(c.loads,['voices','editor','editor','voices'],'one transition, one refresh despite both browser events');assert.deepStrictEqual(c.pushes,['#editor'],'Back must not push a new entry');
c.window.location.hash='#bad"selector';c.events.popstate();c.events.hashchange();assert.strictEqual(c.loads.at(-1),'setup');assert.strictEqual(c.loads.filter(v=>v==='setup').length,1);assert.strictEqual(c.tabs['setup-tab'].style.display,'block');
const old=c.loads.length;c.ctx.activateTab('unknown');assert.strictEqual(c.loads.length,old);assert.deepStrictEqual(c.pushes,['#editor']);
''')

    def test_skip_link_preserves_route_and_visible_tab_while_focusing_main(self):
        self.run_js(r'''
for(const tabName of ['editor','voices','setup']){
 for(const reducedMotion of [false,true]){
  const events={},tabs={},loads=[],scrolls=[],skipHandlers={};
  const links=['setup','voices','editor'].map(name=>({dataset:{tab:name},classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){},addEventListener(){}}));
  for(const name of ['setup','voices','editor']){tabs[name+'-tab']={style:{display:name==='setup'?'block':'none'}};}
  const window={location:{hash:'#'+tabName},history:{replaceState(_state,_title,hash){window.location.hash=hash;},pushState(){throw Error('Skipping must not push history');}},addEventListener:(event,handler)=>events[event]=handler,matchMedia(query){assert.equal(query,'(prefers-reduced-motion: reduce)');return {matches:reducedMotion};}};
  const main={id:'main-content',focus(options){assert.equal(options.preventScroll,true);document.activeElement=main;},scrollIntoView:options=>scrolls.push(options)};
  const skip={addEventListener:(event,handler)=>skipHandlers[event]=handler};
  const document={activeElement:skip,querySelectorAll:selector=>selector==='.nav-link'?links:Object.values(tabs),getElementById:id=>id==='skip-main-link'?skip:id==='main-content'?main:id==='navbarNav'?{classList:{contains:()=>false}}:tabs[id]};
  const context={window,document,localStorage:{getItem:()=>null,setItem(){}},loadChunks:()=>loads.push('editor'),loadVoices:()=>loads.push('voices'),pollLmStudioStatus:()=>loads.push('setup')};
  vm.createContext(context);context.t=context.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english)));vm.runInContext(source.slice(source.indexOf('const TAB_STORAGE_KEY ='),source.indexOf('// --- LLM model picker')),context);
  context.restoreTab();const initialLoads=loads.length;
  for(const activation of ['pointer','Enter-generated click']){
   document.activeElement=skip;let prevented=false;
   skipHandlers.click?.({preventDefault(){prevented=true;}});
   if(!prevented){window.location.hash='#main-content';events.hashchange();document.activeElement=main;}
   assert.equal(window.location.hash,'#'+tabName,tabName+' '+activation+' preserves hash');
   assert.deepEqual(Object.keys(tabs).filter(id=>tabs[id].style.display==='block'),[tabName+'-tab'],'visible tab unchanged');
   assert.strictEqual(document.activeElement,main,'main receives focus');
   assert.equal(loads.length,initialLoads,'skipping must not reload tab data');
   assert.equal(scrolls.at(-1).block,'start');
   assert.equal(scrolls.at(-1).behavior,reducedMotion?'instant':'smooth');
  }
 }
}
''')

    def test_actual_config_loader_displays_filenames_as_text(self):
        names = ['normal.epub', '<img src=x onerror="boom()">.epub', '</span><script>boom()</script>', "日本語 & 'quotes' \"test\""]
        output = self.run_js('const names=' + json.dumps(names) + r''';
const fields={},errors=[],rendered=[];let filename;
const element=id=>fields[id]||(fields[id]={value:'preset text',innerHTML:'',checked:false});
const ctx={window:{},console:{error:(...args)=>errors.push(args),warn:(...args)=>errors.push(args)},document:{getElementById:element},llmProfiles:{},passPromptDefaults:{cast:{whole_book_prompt:'cast whole',incremental_prompt:'cast incremental'},pass1:{system_prompt:'preset',user_prompt:'preset'},pass3:{system_prompt:'preset',user_prompt:'preset'}},API:{get:async path=>{assert.strictEqual(path,'/api/config');return {tts:{},current_file:filename};}}};
for(const name of ['renderConfigWarnings','applyPauseSupport','renderActiveLlmModeBadge','populateLlmInputs','onLlmModeChange','toggleSubBatchFields','toggleTTSMode','renderPromptPresets','renderPassPromptPresets']){ctx[name]=()=>{};}
vm.createContext(ctx);ctx.t=ctx.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english)));vm.runInContext(source.slice(source.indexOf('function escapeHtml('),source.indexOf('// Parse a numeric input')),ctx);
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

    def test_collapsed_nav_closes_through_the_actual_large_breakpoint(self):
        root = SOURCE.parent.parent
        self.assertIn('navbar-expand-lg', (root / 'index.html').read_text())
        self.assertIn('@media (min-width:992px){.navbar-expand-lg', (root / 'vendor/bootstrap-5.3.0/bootstrap.min.css').read_text())
        self.run_js(r"""
const target={style:{}},link={dataset:{tab:'setup'},classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){}};
let shown=true,hides=0,loads=0;const nav={classList:{contains:()=>shown}};
const c={window:{innerWidth:700,location:{hash:'#setup'}},document:{querySelectorAll:selector=>selector==='.nav-link'?[link]:[target],getElementById:id=>id==='navbarNav'?nav:target},getTabLink:()=>link,rememberTab(){},pollLmStudioStatus:()=>loads++,bootstrap:{Collapse:{getOrCreateInstance:node=>{assert.strictEqual(node,nav);return{hide(){hides++;shown=false;}};}}}};
vm.createContext(c);c.t=c.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english)));vm.runInContext(source.slice(source.indexOf('function isLocalVoiceLoadObsolete'),source.indexOf('function getActionErrorMessage(')),c);const a=source.indexOf('function activateTab(');vm.runInContext(source.slice(a,source.indexOf('function restoreTab(',a)),c);
for(const width of [700,800,991,992,1200]){shown=true;const count=hides;c.window.innerWidth=width;c.activateTab('setup',false);assert.strictEqual(hides-count,width<992?1:0);assert.strictEqual(target.style.display,'block');}
shown=false;c.window.innerWidth=800;const count=hides;c.activateTab('setup',false);assert.strictEqual(hides,count);assert.strictEqual(loads,6);
""")
