"""Execute rendered report handlers after HTML attribute entity decoding."""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import unittest

APP = Path(__file__).resolve().parent.parent


class ReportLinks(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'a' and 'report-list-item' in values.get('class', '').split():
            self.links.append(values)


class ReportFilenameHandlerTests(unittest.TestCase):
    def run_node(self, script, argument):
        result = subprocess.run(['node', '-e', script, str(APP), json.dumps(argument)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_rendered_handlers_keep_filenames_as_data_and_fetch_exact_encoded_path(self):
        filenames = ["apostrophe's.md", r'back\slash.md', 'double"quote.md',
                     '日本語 #?%.md', "x');reportInjected=true;viewReport('other"]
        markup = self.run_node(r'''
const fs=require('fs'),vm=require('vm'),app=process.argv[1],names=JSON.parse(process.argv[2]);
const core=fs.readFileSync(app+'/static/js/app-core.js','utf8');
const escape=core.slice(core.indexOf('function escapeHtml('),core.indexOf('\n        }',core.indexOf('function escapeHtml('))+10);
const list={};const context={document:{getElementById:()=>list},restoreTab(){},
 API:{get:async()=>names.map((filename,i)=>({filename,type:i%2?'batch':'single',mtime:1}))}};
vm.runInNewContext(escape,context);
vm.runInNewContext(fs.readFileSync(app+'/static/js/app-reports.js','utf8'),context);
(async()=>{await context.loadReports();console.log(JSON.stringify(list.innerHTML));})().catch(e=>{console.error(e);process.exitCode=1;});
''', filenames)
        # HTML entity decoding occurs before evaluating an inline handler.
        links = ReportLinks(markup).links
        self.assertEqual(filenames, [link['data-filename'] for link in links])
        results = self.run_node(r'''
const fs=require('fs'),vm=require('vm'),app=process.argv[1],links=JSON.parse(process.argv[2]);
const title={},content={},explainButton={style:{}},requests=[],sanitized=[],active=[];
const elements=links.map(link=>({dataset:{filename:link['data-filename']},classList:{toggle:(key,on)=>active.push({key,on})}}));
const context={restoreTab(){},document:{getElementById:id=>id==='report-view-title'?title:id==='btn-report-explain'?explainButton:content,querySelectorAll:()=>elements},
 fetch:async url=>{requests.push(url);return {ok:true,text:async()=>'<unsafe report>'};},
 marked:{parse:text=>text},DOMPurify:{sanitize:html=>{sanitized.push(html);return 'sanitized report';}}};
vm.runInNewContext(fs.readFileSync(app+'/static/js/app-reports.js','utf8'),context);
(async()=>{
 const outcomes=[];
 for(let i=0;i<links.length;i++) {
  requests.length=0;sanitized.length=0;active.length=0;
  let handler;
  try {handler=vm.runInNewContext('(function(){'+links[i].onclick+'})',context);}
  catch(error) {outcomes.push({error:String(error)});continue;}
  const returned=handler.call(elements[i]);await new Promise(resolve=>setImmediate(resolve));
  outcomes.push({returned,requests:[...requests],injected:!!context.reportInjected,title:title.textContent,
                rendered:content.innerHTML,sanitized:[...sanitized],active:active.map(row=>row.on)});
 }
 console.log(JSON.stringify(outcomes));
})().catch(e=>{console.error(e);process.exitCode=1;});
''', links)
        encoded = self.run_node('console.log(JSON.stringify(JSON.parse(process.argv[2]).map(encodeURIComponent)))', filenames)
        for index, (filename, result) in enumerate(zip(filenames, results)):
            with self.subTest(filename=filename):
                self.assertNotIn('error', result)
                self.assertIs(result['returned'], False)
                self.assertFalse(result['injected'])
                self.assertEqual(['/api/reports/' + encoded[index]], result['requests'])
                self.assertEqual(filename, result['title'])
                self.assertEqual('sanitized report', result['rendered'])
                self.assertEqual(['<unsafe report>'], result['sanitized'])
                self.assertEqual([i == index for i in range(len(filenames))], result['active'])
