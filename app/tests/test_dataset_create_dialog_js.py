"""Dataset creation keeps admission errors in its shared dialog callback."""
from pathlib import Path
import subprocess
import unittest
SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-workbench.js'
class DatasetCreateDialogJsTests(unittest.TestCase):
    def test_native_callback_validation_and_selection_ownership(self):
        code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let options,posts=0,loads=[];const c={dsbCurrentProject:'old',API:{post:async(path,body)=>{posts++;assert.strictEqual(path,'/api/dataset_builder/create');return{name:body.name};}},showPresetEditor:async o=>{options=o;return null;},dsbLoadProjects:async name=>loads.push(name),showToast(){},console:{error(){}}};c.window=c;vm.createContext(c);const a=s.indexOf('window.dsbCreateProject =');vm.runInContext(s.slice(a,s.indexOf('window.dsbDeleteProject =',a)),c);let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{await c.dsbCreateProject();assert.strictEqual(posts,0);assert.strictEqual(options.nameLabel,'Dataset name');c.dsbCurrentProject='changed';await assert.rejects(options.submitValues({name:'new'}),/selected project changed/);assert.strictEqual(posts,0);c.dsbCurrentProject='old';c.API.post=async()=>{posts++;throw Object.assign(Error('already exists'),{status:400});};await assert.rejects(options.submitValues({name:'existing'}),/already exists/);c.API.post=async()=>{throw Error('private traceback');};await assert.rejects(options.submitValues({name:'new'}),e=>e.message.includes('not confirmed')&&!e.message.includes('private traceback'));
c.API.post=async(path,body)=>({name:body.name});c.showPresetEditor=async o=>({name:'new',receipt:await o.submitValues({name:'new'})});await c.dsbCreateProject();assert.deepStrictEqual(loads,['new']);c.showPresetEditor=async o=>{const receipt=await o.submitValues({name:'other'});c.dsbCurrentProject='later';return{name:'other',receipt};};await c.dsbCreateProject();assert.deepStrictEqual(loads,['new']);assert.strictEqual(c.dsbCurrentProject,'later');finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        out=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(out.returncode,0,out.stderr)
