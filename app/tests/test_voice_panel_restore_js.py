"""Open voice-change panels retain exact DOM and reject ownership changes."""
from pathlib import Path
import subprocess
import unittest

class VoicePanelRestoreJsTests(unittest.TestCase):
    def test_preserves_completed_panels_and_rejects_pending_or_other_books(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        code=r'''const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const panel={innerHTML:'rows',selected:'unsaved elderly'},empty={innerHTML:''},loading={innerHTML:'Loading'},saving={innerHTML:'Saving'};const card=(name,p)=>({dataset:{voice:name},querySelector:()=>p});const cards=[card('A 日本語',panel),card('empty',empty),card('loading',loading),card('saving',saving)];const pending=new WeakMap([[loading,true]]),saves=new Set(['book-A\0saving']);const c={currentBookFilename:'book-A',pendingVoiceStateLoads:pending,pendingVoiceStateSaves:saves};vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function getVoicePanelSnapshot('),s.indexOf('function getVoiceListFocusSnapshot(')),c);const snap=c.getVoicePanelSnapshot({querySelectorAll:()=>cards},'token-A');assert.strictEqual(snap.panels.length,1);let replaced=[];const replacement={replaceWith:p=>replaced.push(p)};const target={querySelectorAll:()=>[card('wrong speaker',replacement),card('A 日本語',replacement)]};c.restoreVoicePanels(target,snap,'token-B');assert.strictEqual(replaced.length,0);c.restoreVoicePanels({querySelectorAll:()=>[card('other',replacement)]},snap,'token-A');assert.strictEqual(replaced.length,0);c.restoreVoicePanels(target,snap,'token-A');assert.strictEqual(replaced[0],panel);assert.strictEqual(replaced[0].selected,'unsaved elderly');'''
        result=subprocess.run(['node','-e',code,str(source)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
