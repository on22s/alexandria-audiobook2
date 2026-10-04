"""Render actual Training UI, parse resulting HTML, and exercise DELETE callbacks."""
import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-training.js'
CORE = SOURCE.with_name('app-core.js')
SETUP = r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8');
const elements={},toasts=[];
function element(id){return elements[id]||(elements[id]={value:'',innerHTML:'',style:{}});}
const ctx={window:null,document:{getElementById:element},showToast:(...args)=>toasts.push(args),showConfirm:async()=>true,confirm:()=>true,console,Date:{now:()=>1700000000000}};ctx.window=ctx;
vm.createContext(ctx);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),ctx);
const a=core.indexOf('const API = {'),b=core.indexOf('// --- Setup Tab ---',a);vm.runInContext(core.slice(a,b),ctx);
const c=core.indexOf('function escapeHtml('),d=core.indexOf('// Parse a numeric input',c);vm.runInContext(core.slice(c,d),ctx);
const outcomeStart=core.indexOf('function isTaskFailed('),outcomeEnd=core.indexOf('// Ask for permission only',outcomeStart);vm.runInContext(core.slice(outcomeStart,outcomeEnd),ctx);
vm.runInContext(source,ctx);
(async()=>{
"""


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class TrainingUiContractTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE), str(CORE)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_dataset_attributes_preserve_ids_without_injected_tags_or_handlers(self):
        ids = ['normal', "voice' onmouseover='boom()", 'x"><img src=x onerror="boom()">', '日本語 & \\ "\nline']
        html = self.run_js("const ids=" + json.dumps(ids) + r""";
ctx.fetch=async path=>{assert.strictEqual(path,'/api/lora/datasets');return new Response(JSON.stringify(ids.map(dataset_id=>({dataset_id,sample_count:1}))));};
await ctx.loadLoraDatasets();console.log(JSON.stringify(element('lora-datasets-list').innerHTML));
""")
        parsed = Tags(json.loads(html))
        self.assertFalse(any(tag == 'img' for tag, _ in parsed.tags))
        buttons = [attrs for tag, attrs in parsed.tags if tag == 'button']
        self.assertEqual(len(ids), len(buttons))
        self.assertEqual(ids, [button.get('data-dataset-id') for button in buttons])
        for attrs in buttons:
            self.assertEqual({'class', 'data-dataset-id', 'onclick', 'aria-label', 'title'}, set(attrs))
            self.assertEqual('Delete dataset ' + attrs['data-dataset-id'], attrs['aria-label'])
            self.assertEqual(attrs['aria-label'], attrs['title'])
            self.assertEqual('deleteLoraDataset(this.dataset.datasetId)', attrs['onclick'])
        # Run the actual decoded handler body against its decoded dataset value.
        self.run_js('const buttons=' + json.dumps(buttons) + ',expected=' + json.dumps(ids) + r""";
const calls=[];ctx.deleteLoraDataset=id=>calls.push(id);
for(const button of buttons){ctx.target={dataset:{datasetId:button['data-dataset-id']}};vm.runInContext('(function(){'+button.onclick+'}).call(target)',ctx);}
assert.deepStrictEqual(calls,expected);
""")

    def test_test_audio_url_remains_one_src_attribute(self):
        url = '/lora_models/x" onplay="boom()"><img src=x>&value=\'unsafe'
        html = self.run_js('const url=' + json.dumps(url) + r""";
element('lora-test-adapter').value='adapter';element('lora-test-text').value='  Hello.  ';element('lora-test-instruct').value='calm';
ctx.fetch=async(path,req)=>{assert.strictEqual(path,'/api/lora/test');assert.deepStrictEqual(JSON.parse(req.body),{adapter_id:'adapter',text:'Hello.',instruct:'calm'});return new Response(JSON.stringify({audio_url:url}));};
await ctx.runLoraTest();assert.strictEqual(element('lora-test-status').innerHTML,'');console.log(JSON.stringify(element('lora-test-audio').innerHTML));
""")
        parsed = Tags(json.loads(html))
        self.assertEqual(1, len(parsed.tags))
        tag, attrs = parsed.tags[0]
        self.assertEqual('audio', tag)
        self.assertEqual({'controls', 'autoplay', 'src'}, set(attrs))
        self.assertEqual(url + '?t=1700000000000', attrs['src'])

    def test_all_delete_errors_use_shared_handler_without_reload_and_204_succeeds(self):
        self.run_js(r"""
let datasetLoads=0,modelLoads=0;ctx.loadLoraDatasets=()=>{datasetLoads++;};ctx.loadLoraModels=async()=>{modelLoads++;};
const api=vm.runInContext('API',ctx),handler=api._handleError;let handled=0;api._handleError=async res=>{handled++;return handler(res);};
const id='voice # ? 日本語',encoded=encodeURIComponent(id);
const actions=[['deleteLoraDataset','/api/lora/datasets/'+encoded],['deleteLoraModel','/api/lora/models/'+encoded],['deleteLoraRollbackBackup','/api/lora/models/'+encoded+'/rollback-backup']];
const cases=[['<html>proxy failed</html>',503,'Service Unavailable','Service Unavailable'],['',502,'Bad Gateway','Bad Gateway'],['',503,'','HTTP 503'],[JSON.stringify({detail:{message:'specific refusal'}}),422,'Unprocessable Content','specific refusal'],[JSON.stringify({detail:'denied'}),400,'Bad Request','denied']];
for(const [action,url] of actions){
 for(const [body,status,statusText,expected] of cases){ctx.fetch=async(path,req)=>{assert.strictEqual(path,url);assert.strictEqual(req.method,'DELETE');return new Response(body,{status,statusText});};
  const n=toasts.length;await ctx[action](id);assert.strictEqual(toasts.length,n+1);assert(toasts.at(-1)[0].includes(expected));assert.strictEqual(toasts.at(-1)[1],'error');assert.strictEqual(datasetLoads,0);assert.strictEqual(modelLoads,0);}
}
assert.strictEqual(handled,15);
for(const [action,url] of actions){ctx.fetch=async(path,req)=>{assert.strictEqual(path,url);return new Response(null,{status:204});};await ctx[action](id);}
assert.strictEqual(handled,18);assert.strictEqual(datasetLoads,1);assert.strictEqual(modelLoads,2);
ctx.showConfirm=async()=>false;ctx.confirm=()=>false;ctx.fetch=()=>{throw Error('confirmation was bypassed');};
for(const [action] of actions){await ctx[action](id);}assert.strictEqual(handled,18);
""")
