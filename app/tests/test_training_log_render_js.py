"""Execute actual polling callbacks and count DOM writes and metric inspections."""
import unittest
from tests import test_training_ui_contract as ui


class TrainingLogRenderTests(unittest.TestCase):
    run_js = ui.TrainingUiContractTests.run_js

    def test_unchanged_and_appended_logs_avoid_full_dom_and_metric_work(self):
        self.run_js(r"""
let text='',replacements=0,appends=0,scrolls=0;
const el=element('lora-train-logs');Object.defineProperty(el,'innerText',{set:v=>{replacements++;text=v;}});
Object.defineProperty(el,'scrollTop',{set:v=>scrolls++});el.scrollHeight=42;
ctx.document.createTextNode=text=>({text});el.appendChild=node=>{appends++;text+=node.text;};
ctx._startPolling=(key,fetch,options)=>ctx.poll={key,fetch,options};ctx.pollLoraTraining(8);
assert.strictEqual(ctx.poll.options.intervalMs,2000);const tick=ctx.poll.options.onTick;
const logs=['[EPOCH] 2/8 avg_loss=0.43',...Array.from({length:10000},(_,i)=>'ordinary '+i)];
tick({run_id:'one',running:true,logs:logs.slice()});assert.strictEqual(replacements,1);
assert.strictEqual(element('lora-epoch-display').innerText,'2/8');
let reads=0;Object.defineProperty(element('lora-loss-display'),'innerText',{set:v=>reads++});
for(let i=0;i<100;i++){const unchanged=logs.slice();unchanged.join=()=>{throw Error('full join');};tick({run_id:'one',logs:unchanged});}
assert.strictEqual(replacements,1);assert.strictEqual(appends,0);assert.strictEqual(scrolls,1);assert.strictEqual(reads,0);
for(let i=0;i<100;i++){logs.push('new '+i);const next=logs.slice();next.join=()=>{throw Error('full join');};
 tick({run_id:'one',logs:next});}
assert.strictEqual(replacements,1);assert.strictEqual(appends,100);assert.strictEqual(text,logs.join('\n'));assert.strictEqual(reads,0);
logs.push('[TRAIN] epoch=3/8 step=4/5 loss=0.25');tick({run_id:'one',logs});
assert.strictEqual(element('lora-epoch-display').innerText,'3/8');assert.strictEqual(element('lora-progress-bar').style.width,'25%');assert.strictEqual(reads,1);
""")

    def test_corrections_truncation_new_run_and_reattach_reset_metrics(self):
        self.run_js(r"""
let text='';const el=element('lora-train-logs');Object.defineProperty(el,'innerText',{set:v=>text=v});
ctx.document.createTextNode=text=>({text});el.appendChild=node=>text+=node.text;
ctx._startPolling=(key,fetch,options)=>ctx.poll={key,fetch,options};ctx.pollLoraTraining(8);let tick=ctx.poll.options.onTick;
const logs=['intro','[EPOCH] 2/8 avg_loss=0.5','suffix'];tick({start_time:1,logs});assert.strictEqual(element('lora-loss-display').innerText,'0.5');
logs[1]='[EPOCH] 4/8 avg_loss=0.3';tick({start_time:1,logs});assert.strictEqual(text,logs.join('\n'));assert.strictEqual(element('lora-progress-bar').style.width,'50%');
tick({start_time:1,logs:['intro']});assert.strictEqual(text,'intro');assert.strictEqual(element('lora-loss-display').innerText,'');assert.strictEqual(element('lora-progress-bar').style.width,'0%');
tick({start_time:2,logs:['[TRAIN] epoch=1/8 step=1/5 loss=1.2']});assert.strictEqual(element('lora-loss-display').innerText,'1.2');
tick({run_id:'new',logs:[]});assert.strictEqual(text,'');assert.strictEqual(element('lora-epoch-display').innerText,'');
ctx.pollLoraTraining(8);tick=ctx.poll.options.onTick;tick({run_id:'reattach',logs:['[EPOCH] 8/8 avg_loss=0.2']});assert.strictEqual(element('lora-progress-bar').style.width,'100%');
assert(ctx.poll.options.doneCheck({running:false}));assert(!ctx.poll.options.doneCheck({running:true}));
""")

    def test_completion_error_and_stop_controls_remain_distinct(self):
        self.run_js(r"""
ctx._startPolling=(key,fetch,options)=>ctx.poll={key,fetch,options};let notifications=0,loads=0;
ctx.notifyJobDone=key=>{assert.strictEqual(key,'lora_training');notifications++;};ctx.loadLoraModels=()=>loads++;
element('lora-progress-bar').classList={remove:()=>{},replace:()=>{}};
for(const [line,label] of [['[DONE]','Training complete'],['[ERROR]','Training failed'],['cancelled','Training stopped']]){
 ctx.pollLoraTraining(8);element('btn-lora-train').disabled=true;element('btn-lora-cancel').disabled=true;
 ctx.poll.options.onTick({running:false,logs:[line]});ctx.poll.options.onDone({running:false,logs:[line]});
 assert(!element('btn-lora-train').disabled);assert(!element('btn-lora-cancel').disabled);assert.strictEqual(element('btn-lora-cancel').style.display,'none');
 assert(element('lora-train-status').innerHTML.includes(label));
}
assert.strictEqual(notifications,3);assert.strictEqual(loads,1);
""")
