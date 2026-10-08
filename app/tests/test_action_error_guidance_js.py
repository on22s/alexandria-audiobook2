"""Action guidance preserves validation details and distinguishes HTTP from transport."""
from pathlib import Path
import subprocess
import unittest

class ActionErrorGuidanceJsTests(unittest.TestCase):
    def test_settings_refusal_restores_toggle_and_refreshes_authoritative_status(self):
        base = Path(__file__).resolve().parent.parent / 'static/js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'),s=fs.readFileSync(process.argv[2],'utf8');
const toggle={checked:true,disabled:false},badge={};let messages=[],refreshes=0,done=false;
let failure=new TypeError('Failed to fetch');
const c={document:{getElementById:id=>id==='lmstudio-optimize-toggle'?toggle:badge},
API:{post:async()=>{if(failure){throw failure;}}},showToast:(...a)=>messages.push(a),
refreshLmStudioStatus:async()=>refreshes++};vm.createContext(c);
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),c);
const start=s.indexOf('async function toggleLmStudioOptimize()');
vm.runInContext(s.slice(start,s.indexOf('\n        //',start)),c);
(async()=>{
await c.toggleLmStudioOptimize();assert(messages.at(-1)[0].includes('Check the refreshed LM Studio status'));
assert(messages.at(-1)[0].includes('Failed to fetch'));assert(!toggle.checked);assert(!toggle.disabled);
assert.strictEqual(refreshes,1);toggle.checked=true;failure=null;await c.toggleLmStudioOptimize();
assert(toggle.checked);assert(!toggle.disabled);assert.strictEqual(refreshes,2);
assert.strictEqual(messages.at(-1)[1],'success');done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'settings failure checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(base/'app-core.js'),
                                 str(base/'app-workbench.js')], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_voicelab_save_failure_keeps_fields_and_requires_saved_state_check(self):
        base = Path(__file__).resolve().parent.parent / 'static/js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'),s=fs.readFileSync(process.argv[2],'utf8');
const fields={'vl-rocm_python':{value:'/python'},'vl-profiler_model':{value:'model'},
'vl-zips_dir_cfg':{value:'/zips'},'vl-epub_dirs':{value:'/epubs 日本語'}};
let messages=[],loads=0,done=false,failure=new TypeError('Failed to fetch');
const c={window:{},document:{getElementById:id=>fields[id]},showToast:(...a)=>messages.push(a),
API:{post:async()=>{if(failure){throw failure;}}},loadVoicelabConfig:async()=>loads++};vm.createContext(c);
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(s.indexOf('window.saveVoicelabConfig ='),s.indexOf('let _voicelabInspectRequest')),c);
(async()=>{
await c.window.saveVoicelabConfig();assert(messages.at(-1)[0].includes('Check the saved Pipeline settings'));
assert(messages.at(-1)[0].includes('Failed to fetch'));assert.strictEqual(loads,0);
assert.strictEqual(fields['vl-epub_dirs'].value,'/epubs 日本語');
failure=new Error('Interpreter path refused');failure.status=400;await c.window.saveVoicelabConfig();
assert(messages.at(-1)[0].includes('Interpreter path refused'));assert(!messages.at(-1)[0].includes('Could not reach Alexandria'));
failure=null;await c.window.saveVoicelabConfig();assert.strictEqual(loads,1);
assert.strictEqual(messages.at(-1)[1],'success');done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'Voice Lab save checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(base/'app-core.js'),
                                 str(base/'app-voicelab.js')], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_training_cancel_failure_preserves_running_status_and_retry_control(self):
        base = Path(__file__).resolve().parent.parent / 'static/js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'),s=fs.readFileSync(process.argv[2],'utf8');
const button={disabled:false},status={innerHTML:'Training in progress'};let messages=[],done=false;
let failure=new TypeError('Failed to fetch');
const c={window:{},document:{getElementById:id=>id==='btn-lora-cancel'?button:status},
showToast:(...a)=>messages.push(a),API:{post:async()=>{if(failure){throw failure;}}}};vm.createContext(c);
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(s.indexOf('window.cancelLoraTraining ='),s.indexOf('async function onToggleFavoriteAdapter')),c);
(async()=>{
await c.window.cancelLoraTraining();assert(messages.at(-1)[0].includes('training may still be running'));
assert(messages.at(-1)[0].includes('Failed to fetch'));assert(!button.disabled);
assert.strictEqual(status.innerHTML,'Training in progress');
failure=new Error('Task ownership changed');failure.status=409;await c.window.cancelLoraTraining();
assert(messages.at(-1)[0].includes('Task ownership changed'));assert(!messages.at(-1)[0].includes('Could not reach Alexandria'));
assert(!button.disabled);failure=null;await c.window.cancelLoraTraining();
assert(status.innerHTML.includes('Cancellation requested'));assert(button.disabled);done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'training cancellation checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(base/'app-core.js'),
                                 str(base/'app-training.js')], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_report_load_failure_retains_detail_and_offers_refresh(self):
        base = Path(__file__).resolve().parent.parent / 'static/js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'),s=fs.readFileSync(process.argv[2],'utf8');
const list={};let failure=new TypeError('Failed to fetch'),done=false;
const c={document:{getElementById:()=>list},API:{get:async()=>{if(failure){throw failure;}return [];}},
escapeHtml:t=>t.replaceAll('<','&lt;').replaceAll('>','&gt;')};vm.createContext(c);
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(0,s.indexOf('async function loadCheckpoints()')),c);
(async()=>{
await c.loadReports();assert(list.innerHTML.includes('Could not reach Alexandria'));
assert(list.innerHTML.includes('Reopen Reports or refresh the report list'));assert(list.innerHTML.includes('Failed to fetch'));
failure=new Error('Missing <reports>');failure.status=403;await c.loadReports();
assert(list.innerHTML.includes('Missing &lt;reports&gt;'));assert(!list.innerHTML.includes('Could not reach Alexandria'));
failure=null;await c.loadReports();assert(list.innerHTML.includes('No reports yet'));done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'report failure checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(base/'app-core.js'),
                                 str(base/'app-reports.js')], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_insert_failure_checks_for_completed_write_before_retry(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');let messages=[],posts=[],refreshes=0,done=false;
let failure=new TypeError('Failed to fetch');
const c={window:{},showToast:(...a)=>messages.push(a),API:{post:async(p,b)=>{posts.push(p);if(failure){throw failure;}}},
loadChunks:async()=>refreshes++};vm.createContext(c);
vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(s.indexOf('window.insertChunkAfter ='),s.indexOf('window.deleteChunk =')),c);
(async()=>{
await c.window.insertChunkAfter(7);
assert(messages.at(-1)[0].includes('check whether the new line exists before inserting again'));
assert(messages.at(-1)[0].includes('Failed to fetch'));assert.strictEqual(refreshes,0);
failure=new Error('Stale book');failure.status=409;await c.window.insertChunkAfter(7);
assert(messages.at(-1)[0].includes('Stale book'));assert(!messages.at(-1)[0].includes('Could not reach Alexandria'));
assert.strictEqual(messages.at(-1)[1],'error');failure=null;await c.window.insertChunkAfter(7);
assert.strictEqual(refreshes,1);assert.deepStrictEqual(posts,['/api/chunks/7/insert','/api/chunks/7/insert','/api/chunks/7/insert']);done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'insert failure checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_refusal_releases_admission_without_polling_or_false_success(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');let releases=[],polls=[],done=false;
const status={},fields={'audacity-status':status};let failure=new TypeError('Failed to fetch');
const c={window:{},document:{getElementById:id=>fields[id]},claimTaskStart:()=>true,
releaseTaskStart:t=>releases.push(t),pollExport:t=>polls.push(t),
API:{post:async()=>{if(failure){throw failure;}}},
escapeHtml:t=>t.replaceAll('<','&lt;').replaceAll('>','&gt;')};vm.createContext(c);
vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(s.indexOf('window.exportAudacity ='),s.indexOf('// --- Chapter-by-chapter export ---')),c);
(async()=>{
await c.window.exportAudacity();assert(status.innerHTML.includes('Could not reach Alexandria'));
assert(status.innerHTML.includes('Check the export task status and output list before starting again'));
assert.deepStrictEqual(releases,['audacity_export']);assert.deepStrictEqual(polls,[]);
failure=new Error('Missing <audio>');failure.status=400;await c.window.exportAudacity();
assert(status.innerHTML.includes('Missing &lt;audio&gt;'));assert(!status.innerHTML.includes('Could not reach Alexandria'));
assert.deepStrictEqual(polls,[]);failure=null;await c.window.exportAudacity();
assert.deepStrictEqual(polls,['audacity_export']);assert.strictEqual(releases.length,2);done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'export admission checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_designer_preview_failure_keeps_inputs_and_escapes_diagnostics(self):
        base = Path(__file__).resolve().parent.parent / 'static/js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'),s=fs.readFileSync(process.argv[2],'utf8');
const fields={'design-description':{value:'Warm 日本語 voice'},'design-sample-text':{value:'Hello café.'},
'design-status':{},'design-preview-container':{style:{}},'btn-design-preview':{disabled:false}};
let failure=new TypeError('Failed to fetch'),done=false;
const c={window:{},document:{getElementById:id=>fields[id]},
API:{post:async()=>{throw failure;}},showToast:()=>{},stopDesignedVoicePlayback:()=>{},
isDesignerGenerationCurrent:()=>true,
escapeHtml:t=>t.replaceAll('<','&lt;').replaceAll('>','&gt;')};vm.createContext(c);
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(s.indexOf('function getDesignerSynthesisInputs()'),s.indexOf('function getDesignerSaveSnapshot()')),c);
(async()=>{
await c.window.generateDesignPreview();
assert(fields['design-status'].innerHTML.includes('Could not reach Alexandria'));
assert(fields['design-status'].innerHTML.includes('Check that the TTS service and selected model are available'));
assert.strictEqual(fields['btn-design-preview'].disabled,false);
assert.strictEqual(fields['design-description'].value,'Warm 日本語 voice');
assert.strictEqual(fields['design-sample-text'].value,'Hello café.');
failure=new Error('Model <missing>');failure.status=503;await c.window.generateDesignPreview();
assert(fields['design-status'].innerHTML.includes('Model &lt;missing&gt;'));
assert(!fields['design-status'].innerHTML.includes('Could not reach Alexandria'));
assert.strictEqual(fields['btn-design-preview'].disabled,false);done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'preview failure checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(base/'app-core.js'),
                                 str(base/'app-scripts.js')], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_version_and_cast_failures_keep_details_without_claiming_success(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');let messages=[],casts=[],refresh=0,done=false;
const c={window:{},API:{post:async()=>{throw new TypeError('Failed to fetch');},
del:async()=>{throw Error('Rejected <member>');}},showToast:(...a)=>messages.push(a),
loadVoices:async()=>refresh++,loadCastLibrary:async()=>refresh++,
applyConfirmedVoiceRemoval:async(b,m,remove)=>remove(),
escapeHtml:t=>t.replaceAll('<','&lt;').replaceAll('>','&gt;'),
setCastStatus:(...a)=>casts.push(a)};
vm.createContext(c);
vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(s.indexOf('window.selectVoiceVersion ='),s.indexOf('window.addVoiceVersion =')),c);
const start=s.indexOf('async function deleteCastMember(');
vm.runInContext(s.slice(start,s.indexOf('\n        function ',start)),c);
(async()=>{
await c.window.selectVoiceVersion({value:'elderly',closest:()=>({dataset:{voice:'Alice'}})});
assert(messages.at(-1)[0].includes('Reload Voices to check the active saved version before selecting again'));
assert(messages.at(-1)[0].includes('Could not reach Alexandria'));assert.strictEqual(refresh,0);
await c.deleteCastMember('cast','Alice');
assert(casts.at(-1)[0].includes('Refresh the selected cast'));
assert(casts.at(-1)[0].includes('Rejected &lt;member&gt;'));assert.strictEqual(casts.at(-1)[1],true);
assert.strictEqual(refresh,0);done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>assert(done,'version and cast checks must complete'));'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_voice_approval_failure_requires_checking_saved_state(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');let messages=[],posts=[],reloads=0,done=false;
let failure=new TypeError('Failed to fetch');
const c={window:{},API:{post:async(...args)=>{posts.push(args);if(failure){throw failure;}}},
showToast:(...args)=>messages.push(args),loadVoices:async()=>{reloads++;}};
vm.createContext(c);
vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);
vm.runInContext(s.slice(s.indexOf('function getVoiceCardMetadata('),s.indexOf('function createVoiceCard(')),c);
vm.runInContext(s.slice(s.indexOf('window.setVoiceApproval ='),s.indexOf('window.editPersonaVoiceAudit =')),c);
const button={closest:()=>({dataset:{voice:'Alice 日本語'}})};
(async()=>{
await c.window.setVoiceApproval(button,'persona_status','approved');
assert(messages.at(-1)[0].includes('Could not reach Alexandria'));
assert(messages.at(-1)[0].includes('Reload Voices to check the current approval before trying again'));
assert(messages.at(-1)[0].includes('Failed to fetch'));assert.strictEqual(reloads,0);
failure=new Error('Invalid approval status');failure.status=422;
await c.window.setVoiceApproval(button,'persona_status','approved');
assert(messages.at(-1)[0].includes('Invalid approval status'));
assert(!messages.at(-1)[0].includes('Could not reach Alexandria'));assert.strictEqual(reloads,0);
failure=null;await c.window.setVoiceApproval(button,'persona_status','approved');
assert.strictEqual(reloads,1);assert.strictEqual(messages.at(-1)[1],'success');
assert.strictEqual(posts[0][0],'/api/voices/Alice%20%E6%97%A5%E6%9C%AC%E8%AA%9E/approval');
assert.strictEqual(posts[0][1].persona_status,'approved');done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
process.on('beforeExit',()=>{assert(done,'async approval checks must complete');});'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_transport_recovery_and_validation_details(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        code=r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');const s=fs.readFileSync(process.argv[1],'utf8');let messages=[];const c={showToast:(...args)=>messages.push(args)};vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);for(const detail of ['Failed to fetch','NetworkError when attempting to fetch resource.','Load failed']){c.showActionError('Save failed',new Error(detail),'Check required fields.');const [text,type]=messages.at(-1);assert(text.includes('Could not reach Alexandria'));assert(text.includes('Review the current status before retrying'));assert(text.includes(detail));assert(text.includes('Check required fields.'));assert.strictEqual(type,'error');}for(const detail of ['Field X must be positive.','NetworkError is not an allowed model name']){const error=new Error(detail);error.status=422;c.showActionError('Save refused',error,'Check required fields.');const text=messages.at(-1)[0];assert(text.includes('Check required fields.'));assert(text.includes(detail));assert(!text.includes('Could not reach Alexandria'));}c.showActionError('Action failed',new Error('missing module'),'Repair installation.');assert(messages.at(-1)[0].includes('Repair installation.'));assert(messages.at(-1)[0].includes('missing module'));'''
        result=subprocess.run(['node','-e',code,str(source)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
