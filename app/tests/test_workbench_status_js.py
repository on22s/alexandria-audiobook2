"""Execute the status handler against successive local/remote server responses."""
from pathlib import Path
import subprocess
import unittest


class WorkbenchStatusJsTests(unittest.TestCase):
    def test_remote_optimize_toggle_tracks_actual_status_after_endpoint_switch(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-workbench.js'
        code = r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('async function refreshLmStudioStatus()');
const end=source.indexOf('async function toggleLmStudioOptimize()',start);
assert(start>=0 && end>start);
const badge={},toggle={checked:true,disabled:false};
let status;
const context={document:{getElementById:id=>({'lmstudio-status-badge':badge,'lmstudio-optimize-toggle':toggle}[id])},
 API:{get:async url=>{assert.strictEqual(url,'/api/lmstudio/status');return status;}}};
vm.runInNewContext(source.slice(start,end),context);
(async()=>{
 for(const [remote,loaded,optimized] of [
  [true,true,false],[true,true,true],[true,false,false],
  [false,true,true],[true,true,false],[false,true,false],[true,true,true]]) {
  status={remote,available:true,loaded,optimized,context_length:4096,parallel:1};
  await context.refreshLmStudioStatus();
  assert.strictEqual(toggle.checked,optimized,'The previous endpoint checkbox must not survive a status change');
  assert.strictEqual(toggle.disabled,false);
  if(remote){assert.strictEqual(badge.textContent,'Remote (optimize via SSH)');}
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node','-e',code,str(source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)


    def test_page_initialization_restores_existing_voicelab_run_and_controls(self):
        static = Path(__file__).resolve().parent.parent / 'static'
        html = (static / 'index.html').read_text()
        self.assertLess(html.index('app-workbench.js'), html.index('app-voicelab.js'))
        code = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const workbench = fs.readFileSync(process.argv[1], 'utf8');
const voicelab = fs.readFileSync(process.argv[2], 'utf8');
const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');
let finished = false;
process.on('beforeExit', () => assert(finished, 'startup must reach its final assertion'));
(async () => {
for (const mode of ['running','idle','unavailable']) {
    const elements = {}, calls = [], timers = [], pollers = [];
    let release, startup, health = 0, resets = 0;
    const gate = new Promise(resolve => { release = resolve; });
    const status = {running:mode === 'running', logs:['existing training progress','epoch 3'],
        tasks:[{name:'train',status:'running'}]};
    const context = {console:{debug:()=>{}},window:{},showToast:()=>{},_makePauseResumeHandler:()=>()=>{},
        document:{addEventListener:()=>{},getElementById: id => elements[id] ||= {style:{display:'none'},disabled:false,innerText:'',innerHTML:'',scrollHeight:12}},
        API:{get: async url => {
            calls.push(url);
            if (url === '/api/status') {
                await gate;
                if (mode === 'unavailable') { throw new Error('registry unavailable'); }
                return {voicelab:{running:mode==='running'}};
            }
            if (url.startsWith('/api/status/voicelab')) {
                await gate;
                if (mode === 'unavailable') { throw new Error('status unavailable'); }
                return status;
            }
            return {running:false,logs:[]};
        }}, setInterval:(fn,ms)=>{timers.push(ms);return timers.length;},
        _resetPauseBtn:id=>{assert.strictEqual(id,'btn-vl-pause');resets++;},
        refreshVoicelabHealth:()=>{health++;},
        _startPolling:(key,fetchFn,options)=>{pollers.push({key,fetchFn,options});},
        escapeHtml:String,
    };
    for (const name of ['loadConfig','loadCastList','loadVoices','loadSavedScripts','loadDesignedVoices',
                        'dsbLoadProjects','updateSystemStats','updateEtaStatus','refreshLmStudioStatus','pollLmStudioStatus']) {
        context[name] = () => {};
    }
    vm.createContext(context);
    const start = workbench.indexOf('function reattachTaskActivity(');
    const init = workbench.indexOf('// Init',start);
    const end = workbench.indexOf('// ── Preparer',init);
    assert(start >= 0 && init > start && end > init);
    vm.runInContext(workbench.slice(start,init),context);
    const actual = context.reattachRunningPollers;
    context.reattachRunningPollers = () => { startup = actual(); return startup; };
    // Execute actual page init before the later Voice Lab script has loaded.
    vm.runInContext(workbench.slice(init,end),context);
    assert(calls.includes('/api/status'));
    assert(!calls.includes('/api/status/voicelab'),'registry is gated until the later script loads');
    assert.strictEqual(pollers.length,0);
    vm.runInContext(core.slice(core.indexOf('function getTaskLogUpdate('),core.indexOf('// --- Setup Tab ---')),context);
    vm.runInContext(voicelab,context);
    context.refreshVoicelabHealth=()=>{health++;};
    release(); await startup;
    assert.deepStrictEqual(timers,[10000,10000,30000]);
    if (mode === 'running') {
        assert.strictEqual(elements['btn-vl-start'].disabled,true);
        assert.strictEqual(elements['btn-vl-pause'].style.display,'inline-block');
        assert.strictEqual(elements['btn-vl-cancel'].style.display,'inline-block');
        assert.strictEqual(resets,1);
        assert.strictEqual(health,1);
        assert.strictEqual(pollers.length,1);
        assert.strictEqual(pollers[0].key,'voicelab');
        const fetched = await pollers[0].fetchFn();
        assert.strictEqual(pollers[0].options.doneCheck(fetched),false);
        pollers[0].options.onTick(fetched);
        assert.strictEqual(elements['voicelab-logs'].innerText,status.logs.join('\n'));
        assert.strictEqual(elements['voicelab-logs'].scrollTop,12);
        assert.strictEqual(elements['vl-stage-progress'].style.display,'flex');
        assert.match(elements['vl-stage-progress'].innerHTML,/train: running/);
        assert.strictEqual(health,1,'poll responses now include health rather than issuing another GET');
    } else {
        assert.strictEqual(pollers.length,0);
        assert.strictEqual(resets,0);
        assert.strictEqual(health,0);
        if (mode === 'idle') {
            assert.strictEqual(elements['voicelab-logs'].innerText,status.logs.join('\n'));
        }
    }
}
finished = true;
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node','-e',code,str(static / 'js/app-workbench.js'),
                                 str(static / 'js/app-voicelab.js')],
                                capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout + result.stderr)
