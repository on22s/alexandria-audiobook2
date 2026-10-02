"""Exercise incremental actual Voice Lab log handler with DOM write accounting."""
import unittest
from tests import test_voicelab_run_state_js as run_state


class VoicelabLogRenderJsTests(unittest.TestCase):
    run_js = run_state.VoicelabRunStateJsTests.run_js

    def test_long_log_appends_only_suffix_and_unchanged_ticks_do_not_write(self):
        self.run_js(r"""
const el=element('voicelab-logs');let text='',replacements=0,appendBytes=0,appends=0,scrolls=0;
Object.defineProperty(el,'innerText',{get:()=>text,set:value=>{replacements++;text=value;}});
Object.defineProperty(el,'scrollTop',{set:value=>{scrolls++;}});
ctx.document.createTextNode=text=>({text});el.appendChild=node=>{appends++;appendBytes+=Buffer.byteLength(node.text);text+=node.text;};
ctx.pollVoicelab('A');const tick=ctx.poll.options.onTick;
const logs=Array.from({length:10000},(_,i)=>'original line '+i);
tick({run_id:'one',running:true,logs:logs.slice(),tasks:[]});assert.strictEqual(replacements,1);
for(let i=0;i<100;i++){const unchanged=logs.slice();unchanged.join=()=>{throw Error('unchanged full log joined');};tick({run_id:'one',running:true,logs:unchanged,tasks:[]});}
assert.strictEqual(replacements,1);assert.strictEqual(appends,0);assert.strictEqual(scrolls,1);
let expectedBytes=0;
for(let i=0;i<100;i++){const line='new line '+i;logs.push(line);expectedBytes+=Buffer.byteLength('\n'+line);
 const next=logs.slice();next.join=()=>{throw Error('growing full log joined');};tick({run_id:'one',running:true,logs:next,tasks:[]});}
assert.strictEqual(replacements,1);assert.strictEqual(appends,100);assert.strictEqual(appendBytes,expectedBytes);assert.strictEqual(text,logs.join('\n'));
""")

    def test_correction_truncation_new_run_and_reattachment_replace_old_text(self):
        self.run_js(r"""
const el=element('voicelab-logs');let text='previous view',replacements=0,appends=0;
Object.defineProperty(el,'innerText',{get:()=>text,set:value=>{replacements++;text=value;}});
ctx.document.createTextNode=text=>({text});el.appendChild=node=>{appends++;text+=node.text;};
ctx.pollVoicelab('A');const tick=ctx.poll.options.onTick;
tick({run_id:'one',running:true,logs:[],tasks:[]});assert.strictEqual(text,'');
let logs=['alpha','middle','omega'];tick({run_id:'one',running:true,logs,tasks:[]});assert.strictEqual(text,logs.join('\n'));
// Keep a copy of the prior input: changes to that input still require repaint.
logs[1]='corrected';tick({run_id:'one',running:true,logs,tasks:[]});assert.strictEqual(text,'alpha\ncorrected\nomega');
tick({run_id:'one',running:true,logs:['alpha'],tasks:[]});assert.strictEqual(text,'alpha');
const prior=replacements;tick({run_id:'two',running:true,logs:['alpha'],tasks:[]});assert.strictEqual(replacements,prior+1);
const beforeAppend=appends;tick({run_id:'two',running:true,logs:['alpha','next'],tasks:[]});assert.strictEqual(text,'alpha\nnext');assert.strictEqual(appends,beforeAppend+1);
tick({run_id:'two',running:false,tasks:[]});assert.strictEqual(text,'');
ctx.pollVoicelab('A');ctx.poll.options.onTick({run_id:'three',logs:['reattached'],tasks:[]});assert.strictEqual(text,'reattached');
""")
