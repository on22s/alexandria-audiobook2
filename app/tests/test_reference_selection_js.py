"""Actual reference handler rejects stale options without persisting old audio."""
from pathlib import Path
import os
import subprocess
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static/js'
SOURCE = Path(os.environ.get('REFERENCE_SELECT_SOURCE', STATIC / 'app-scripts.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8');
const toasts=[],saves=[];
const ctx={window:null,showToast:(...args)=>toasts.push(args),saveVoicesDebounced:()=>saves.push({audio:audio.value,text:text.value})};ctx.window=ctx;
ctx._cloneVoicesCache=[{id:'old',filename:'old #?.wav',ref_text:'original words'},{id:'new',filename:'new.wav'}];ctx._designedVoicesCache=[{id:'design',filename:'design.wav',sample_text:'designed words'}];
vm.createContext(ctx);const a=core.indexOf('function getLibraryVoiceReference(');vm.runInContext(core.slice(a,core.indexOf('function createVoiceCard(',a)),ctx);
const b=source.indexOf('window.onDesignedVoiceSelect =');vm.runInContext(source.slice(b,source.indexOf('// --- Clone Voice Upload Handlers ---',b)),ctx);
const audio={value:'clone_voices/old #?.wav',readOnly:true,focus(){}},text={value:'original words'},play={style:{}},remove={style:{}};
const card={querySelector:selector=>({'.ref-audio':audio,'.ref-text':text,'.clone-play-btn':play,'.clone-delete-btn':remove}[selector])};
function select(value,options=['','__manual__','clone:old','clone:new','design:design',value]){
 let selected=value;return {closest:()=>card,get value(){return selected;},set value(next){selected=options.includes(next)?next:'';}};
}
'''


class ReferenceSelectionJsTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', SETUP + code, str(SOURCE), str(STATIC / 'app-core.js')], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_missing_clone_designed_and_legacy_ids_restore_prior_reference_without_save(self):
        self.run_js(r'''
for(const missing of ['clone:removed','design:removed','removed']){
 const chooser=select(missing);ctx.onDesignedVoiceSelect(chooser);assert.strictEqual(chooser.value,'clone:old');assert.strictEqual(audio.value,'clone_voices/old #?.wav');assert.strictEqual(text.value,'original words');assert.strictEqual(audio.readOnly,true);assert.strictEqual(play.style.display,'inline-block');assert.strictEqual(remove.style.display,'inline-block');assert.strictEqual(saves.length,0);
 assert.strictEqual(toasts.at(-1)[1],'warning');assert.match(toasts.at(-1)[0],/no longer available/);
}
''')

    def test_unknown_or_missing_prior_option_becomes_editable_custom_path_without_data_loss(self):
        self.run_js(r'''
for(const previous of ['outside/custom.wav','clone_voices/old #?.wav','']){
 audio.value=previous;audio.readOnly=true;text.value='keep transcript';const chooser=select('clone:gone',['','__manual__','clone:gone']);ctx.onDesignedVoiceSelect(chooser);
 assert.strictEqual(chooser.value,previous?'__manual__':'');assert.strictEqual(audio.value,previous);assert.strictEqual(text.value,'keep transcript');assert.strictEqual(audio.readOnly,false);assert.strictEqual(remove.style.display,'none');assert.strictEqual(play.style.display,previous?'inline-block':'none');assert.strictEqual(saves.length,0);
}
''')

    def test_valid_clone_designed_legacy_and_existing_manual_behavior_remain(self):
        self.run_js(r'''
for(const [value,path,transcript,deletable] of [['clone:new','clone_voices/new.wav','',true],['design:design','designed_voices/design.wav','designed words',false],['design','designed_voices/design.wav','designed words',false]]){
 const chooser=select(value);const count=saves.length;ctx.onDesignedVoiceSelect(chooser);assert.strictEqual(audio.value,path);assert.strictEqual(text.value,transcript);assert.strictEqual(audio.readOnly,true);assert.strictEqual(saves.length,count+1);assert.deepStrictEqual(saves.at(-1),{audio:path,text:transcript});assert.strictEqual(remove.style.display,deletable?'inline-block':'none');
}
assert.strictEqual(toasts.length,0);const before=saves.length;ctx.onDesignedVoiceSelect(select('__manual__'));assert.strictEqual(audio.value,'');assert.strictEqual(text.value,'');assert.strictEqual(audio.readOnly,false);assert.strictEqual(saves.length,before);
''')
