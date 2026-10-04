"""Actual Dataset HTML templates parsed for attribute/text injection and controls."""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-workbench.js'


class ParsedHtml(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags=[];self.text=[]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag,dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)

    def of(self,tag):
        return [attrs for name,attrs in self.tags if name==tag]


class DatasetHtmlRenderJsTests(unittest.TestCase):
    def execute(self, code, payload):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8'),payload=JSON.parse(process.argv[3]);
const elements={};const context={window:{},console,document:{getElementById(id){return elements[id]??=( {innerHTML:'',style:{},value:''});}},showToast(){throw new Error('unexpected toast');}};context.window=context;vm.createContext(context);
const run=code=>vm.runInContext(code,context);
run(core.slice(core.indexOf('function escapeHtml('),core.indexOf('// Parse a numeric input')));
run(source.slice(0,source.indexOf('// Persist on input changes')));
(async()=>{
'''+code+r'''
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',script,str(SOURCE),str(SOURCE.with_name('app-core.js')),json.dumps(payload)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
        return json.loads(result.stdout)

    def assert_no_injected_elements(self,html):
        parsed=ParsedHtml(html)
        self.assertFalse(parsed.of('img'));self.assertFalse(parsed.of('script'))
        for _,attrs in parsed.tags:self.assertNotIn('onerror',attrs)
        return parsed

    def test_project_options_preserve_hostile_names_and_counts_as_data(self):
        projects=[{'name':name,'done_count':0,'sample_count':2} for name in ['ordinary', 'Voice " onerror="bad', '</option><img src=x onerror=bad>', "Café & O'Brien"]]
        projects[0]['sample_count']='<script>bad</script>'
        output=self.execute("context.API={get:async()=>payload};await context.dsbLoadProjects();console.log(JSON.stringify(elements['dsb-project-select'].innerHTML));",projects)
        parsed=self.assert_no_injected_elements(output);options=parsed.of('option');self.assertEqual(len(projects)+1,len(options))
        self.assertEqual([{'value':''}]+[{'value':p['name']} for p in projects],options)
        text=''.join(parsed.text)
        for p in projects:self.assertIn(p['name']+' ('+str(p['done_count'])+'/'+str(p['sample_count'])+')',text)

    def test_row_fields_are_escaped_and_real_controls_survive(self):
        hostile='\"><img src=x onerror=bad></textarea><script>bad</script>&\'café'
        rows=[{'emotion':hostile,'text':hostile,'seed':0,'status':status,'audio_url':'/dataset_builder/voice/clip.wav?t=1&literal=" onerror="bad'} for status in ['pending','done','generating','error',hostile]]
        output=self.execute("console.log(JSON.stringify(payload.map(row=>context.dsbBuildRowHtml(row,2))));",rows)
        busy=self.execute("run('dsbBatchRunning=true');console.log(JSON.stringify(payload.map(row=>context.dsbBuildRowHtml(row,2))));",rows)
        for row,rendered,busy_html in zip(rows,output,busy):
            parsed=self.assert_no_injected_elements(rendered);self.assert_no_injected_elements(busy_html)
            self.assertEqual(row['status'],parsed.of('tr')[0]['data-dsb-status']);self.assertEqual(row['audio_url'],parsed.of('tr')[0]['data-dsb-audio'])
            self.assertEqual(hostile,parsed.of('input')[0]['value']);self.assertEqual('0',parsed.of('input')[1]['value']);self.assertIn(hostile,''.join(parsed.text));self.assertIn(row['status'],''.join(parsed.text))
            self.assertEqual(1,len(parsed.of('textarea')));self.assertEqual(1,len(parsed.of('tr')))
            self.assertEqual([{'controls':None,'src':row['audio_url'],'style':'width:180px;height:28px;','onplay':'dsbStopOthers(2)'}] if row['status']=='done' else [],parsed.of('audio'))
            self.assertEqual(['dsbRemoveRow(2)'] if row['status']=='generating' else ['dsbGenSample(2)','dsbRemoveRow(2)'],[b['onclick'] for b in parsed.of('button')])
            self.assertTrue(all('disabled' in b for b in ParsedHtml(busy_html).of('button')))
            self.assertTrue(all('disabled' not in b for b in parsed.of('button')))

    def test_native_persisted_status_and_directory_name_remain_data_in_rendered_output(self):
        from fastapi.testclient import TestClient
        from tests.test_dataset_builder_ownership import DatasetBuilderOwnershipTests
        with DatasetBuilderOwnershipTests().fixture() as (_,builder,work,_,_,app),TestClient(app) as client:
            row={'text':'</textarea><img src=x onerror=bad>','emotion':'warm','seed':0,'status':'<script>bad</script>','audio_url':'/clip.wav?x=" onerror="bad'}
            path=work/'state.json';path.write_text(json.dumps({'samples':[row]}));before=path.read_bytes()
            name='Voice " onerror="bad';directory=builder/name;directory.mkdir();(directory/'state.json').write_bytes(before)
            projects=client.get('/api/dataset_builder/list').json();self.assertIn(name,[p['name'] for p in projects])
            options=self.execute("context.API={get:async()=>payload};await context.dsbLoadProjects();console.log(JSON.stringify(elements['dsb-project-select'].innerHTML));",projects)
            parsed=self.assert_no_injected_elements(options);self.assertIn({'value':name},parsed.of('option'))
            samples=client.get('/api/dataset_builder/status/voice').json()['samples'];self.assertEqual([row],samples)
            html=self.execute("console.log(JSON.stringify(context.dsbBuildRowHtml(payload[0],0)));",samples)
            parsed=self.assert_no_injected_elements(html);self.assertEqual(row['status'],parsed.of('tr')[0]['data-dsb-status']);self.assertEqual(row['audio_url'],parsed.of('tr')[0]['data-dsb-audio'])
            self.assertEqual(before,path.read_bytes());self.assertEqual(before,(directory/'state.json').read_bytes())

    def test_reference_labels_only_mark_truncated_text_and_keep_indices(self):
        rows=[{'text':'A complete sentence','emotion':'Calm','status':'done'},
              {'text':'x'*40,'emotion':'e'*30,'status':'done'},
              {'text':'y'*41,'emotion':'f'*31,'status':'done'},
              {'text':'<img src=x>','emotion':'" & warm','status':'done'},
              {'text':'not done','status':'pending'}]
        html=self.execute("run('dsbRows=[];');run('dsbRows').push(...payload);context.dsbUpdateRefDropdown();console.log(JSON.stringify(elements['dsb-ref-select'].innerHTML));",rows)
        parsed=self.assert_no_injected_elements(html)
        self.assertEqual(['0','1','2','3'],[o['value'] for o in parsed.of('option')])
        self.assertIn('1. Calm - "A complete sentence"',html)
        self.assertIn('2. '+('e'*30)+' - "'+('x'*40)+'"</option>',html)
        self.assertIn('3. '+('f'*30)+'… - "'+('y'*40)+'"…</option>',html)
        self.assertIn('<img src=x>',''.join(parsed.text))

    def test_shared_template_escapes_new_values_and_targeted_renderer_uses_same_output(self):
        row={'emotion':'happy','text':'literal <img onerror=bad>','seed':0,'status':'done','audio_url':'/clip.wav?x="&y=1'}
        output=self.execute(r'''
const template=context.getEscapedHtml`<span data-value="${payload.text}">${payload.audio_url}</span>`;
run('dsbRows=[];');run('dsbRows').push(payload);context.dsbUpdateProgress=()=>{};
let replacements=0,captured;const existing={getAttribute:name=>name==='data-dsb-status'?payload.status:payload.audio_url,replaceWith:row=>{replacements++;captured=row.html;}};
elements['dsb-table-body']={children:[existing]};context.document.createElement=()=>({set innerHTML(html){this.firstElementChild={html};}});
context.dsbRenderTable([0]);assert.strictEqual(replacements,0);
existing.getAttribute=()=>null;context.dsbRenderTable([0]);assert.strictEqual(replacements,1);assert.strictEqual(captured,context.dsbBuildRowHtml(payload,0));
console.log(JSON.stringify({template,captured}));
''',row)
        parsed=self.assert_no_injected_elements(output['template']);self.assertEqual([{'data-value':row['text']}],parsed.of('span'));self.assertEqual(row['audio_url'],''.join(parsed.text));self.assert_no_injected_elements(output['captured'])
