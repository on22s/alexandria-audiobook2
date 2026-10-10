"""Execute real polling, navigation, visibility, and Optimize handlers in Node."""
from pathlib import Path
import subprocess
import unittest

class LmStudioVisiblePollJsTests(unittest.TestCase):
    def test_navigation_collapses_below_large_breakpoint_and_desktop_stays_open(self):
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8');
function classes(){const values=new Set();return {add:v=>values.add(v),remove:v=>values.delete(v),contains:v=>values.has(v)};}
const links=['script','voices'].map(tab=>({dataset:{tab},classList:classes(),attrs:{},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},addEventListener(_event,handler){this.click=handler;}}));
const tabs=Object.fromEntries(links.map(l=>[l.dataset.tab+'-tab',{style:{display:'none'}}]));
const nav={classList:classes()};let hidden=0,voiceLoads=0;
const ctx={window:{innerWidth:800},rememberTab:()=>{},loadVoices:()=>voiceLoads++,bootstrap:{Collapse:{getOrCreateInstance:()=>({hide:()=>{hidden++;nav.classList.remove('show');}})}},document:{querySelectorAll:selector=>selector==='.nav-link'?links:Object.values(tabs),getElementById:id=>id==='navbarNav'?nav:tabs[id]}};
ctx.window=ctx.window||{};ctx.window.location={hash:'#setup'};ctx.window.history={pushState:(_s,_t,hash)=>ctx.window.location.hash=hash};ctx.window.addEventListener=()=>{};
vm.createContext(ctx);vm.runInContext(core.slice(core.indexOf('const TAB_STORAGE_KEY ='),core.indexOf('// --- LLM model picker')),ctx);
for(const width of [767,768,800,991,992,1200]){
 ctx.window.innerWidth=width;nav.classList.add('show');const before=hidden;
 links[0].click({currentTarget:links[0],preventDefault(){}});
 assert.strictEqual(hidden-before,width<992?1:0);assert.strictEqual(nav.classList.contains('show'),width>=992);
 assert.strictEqual(tabs['script-tab'].style.display,'block');assert.strictEqual(links[0].attrs['aria-current'],'page');
 if(width>=992){links[1].click({currentTarget:links[1],preventDefault(){}});assert(nav.classList.contains('show'));assert.strictEqual(tabs['voices-tab'].style.display,'block');assert.strictEqual(links[1].attrs['aria-current'],'page');assert.strictEqual(links[0].attrs['aria-current'],undefined);}
}
assert.strictEqual(voiceLoads,2);nav.classList.remove('show');ctx.window.innerWidth=600;const before=hidden;links[0].click({currentTarget:links[0],preventDefault(){}});assert.strictEqual(hidden,before);
"""
        static = Path(__file__).resolve().parent.parent / 'static/js'
        result = subprocess.run(['node', '-e', code, str(static / 'app-core.js')], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_foreground_poll_wiring_retains_immediate_and_explicit_refreshes(self):
        static = Path(__file__).resolve().parent.parent / 'static/js'
        code = r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const work=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8');
const timers=[],events={},calls=[],posts=[],toasts=[];
const setup={style:{display:'block'}},editor={style:{display:'none'}},badge={},toggle={checked:true};
function classes(){const s=new Set();return {add:x=>s.add(x),remove:x=>s.delete(x),contains:x=>s.has(x)};}
function link(tab){return {dataset:{tab},classList:classes(),setAttribute:()=>{},removeAttribute:()=>{},addEventListener:(event,fn)=>{assert.equal(event,'click');links[tab].click=fn;}};}
const links={};links.setup=link('setup');links.editor=link('editor');
const elements={'setup-tab':setup,'editor-tab':editor,'lmstudio-status-badge':badge,'lmstudio-optimize-toggle':toggle,'navbarNav':{classList:classes()}};
const doc={hidden:false,getElementById:id=>elements[id],querySelectorAll:selector=>selector==='.nav-link'?Object.values(links):[setup,editor],addEventListener:(event,fn)=>{assert(!events[event]);events[event]=fn;}};
let failGet=false,failPost=false;const status={remote:true,optimized:true};
const ctx={document:doc,setInterval:(fn,ms)=>{timers.push({fn,ms});},rememberTab:()=>{},
 showToast:(...args)=>toasts.push(args),API:{get:async url=>{assert.equal(url,'/api/lmstudio/status');calls.push(url);if(failGet){throw Error('offline');}return status;},post:async(url,body)=>{assert.equal(url,'/api/lmstudio/optimize');posts.push(body);if(failPost){throw Error('rejected');}}}};
for(const name of ['loadConfig','loadCastList','loadVoices','loadSavedScripts','loadDesignedVoices','dsbLoadProjects','updateSystemStats','updateEtaStatus','reattachRunningPollers','loadChunks']){ctx[name]=name==='loadConfig'?async()=>{}:()=>{};}
ctx.window=ctx.window||{};ctx.window.location={hash:'#setup'};ctx.window.history={pushState:(_s,_t,hash)=>ctx.window.location.hash=hash};ctx.window.addEventListener=()=>{};
vm.createContext(ctx);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),ctx);
let a=work.indexOf('function pollLmStudioStatus()'),b=work.indexOf('function reattachTaskActivity(',a);assert(a>=0&&b>a);vm.runInContext(work.slice(a,b),ctx);
a=work.indexOf('// Init');b=work.indexOf('// ── Preparer',a);assert(a>=0&&b>a);vm.runInContext(work.slice(a,b),ctx);
a=core.indexOf('const TAB_STORAGE_KEY =');b=core.indexOf('// --- LLM model picker',a);assert(a>=0&&b>a);vm.runInContext(core.slice(a,b),ctx);
let finished=false;process.on('beforeExit',()=>assert(finished,'All async assertions must execute'));
(async()=>{
 await Promise.resolve();assert.equal(calls.length,1,'visible Setup initializes immediately');
 const timer=timers.find(t=>t.ms===30000);assert(timer);assert.strictEqual(timer.fn,ctx.pollLmStudioStatus);assert.strictEqual(events.visibilitychange,ctx.pollLmStudioStatus);
 for(let i=0;i<20;i++){await timer.fn();}assert.equal(calls.length,21);
 function click(name){let prevented=false;links[name].click({currentTarget:links[name],preventDefault:()=>{prevented=true;}});assert(prevented);}
 click('editor');for(let i=0;i<20;i++){await timer.fn();}assert.equal(calls.length,21,'Editor must not poll Setup status');
 await events.visibilitychange();assert.equal(calls.length,21,'visibility must not refresh inactive Setup');
 doc.hidden=true;click('setup');for(let i=0;i<20;i++){await timer.fn();}assert.equal(calls.length,21,'hidden Setup must not poll');
 doc.hidden=false;await events.visibilitychange();assert.equal(calls.length,22,'returning to visible Setup refreshes immediately');
 click('editor');click('setup');await Promise.resolve();assert.equal(calls.length,23,'tab entry refreshes immediately');
 failGet=true;await timer.fn();assert.equal(badge.textContent,'Status unavailable');failGet=false;
 doc.hidden=true;setup.style.display='none';toggle.checked=true;await ctx.toggleLmStudioOptimize();assert.equal(posts.length,1);assert.equal(calls.length,25,'Optimize success refreshes even when polling is suspended');
 failPost=true;status.optimized=false;await ctx.toggleLmStudioOptimize();assert.equal(posts.length,2);assert.equal(calls.length,26,'Optimize failure also refreshes');assert(toasts.some(t=>t[1]==='error'));assert.equal(toggle.checked,false);
 finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node','-e',code,str(static/'app-workbench.js'),str(static/'app-core.js')],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
