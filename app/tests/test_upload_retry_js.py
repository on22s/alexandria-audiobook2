"""Real upload handlers with parsed input markup; Node models same-file change events.

This does not qualify a browser or native file picker.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'static/js/app-core.js'


class InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.upload = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get('id') == 'file-upload':
            self.upload = {'tag': tag, **values}


class UploadRetryJSTests(unittest.TestCase):
    def run_js(self, scenario):
        parser = InputParser()
        parser.feed((ROOT / 'static/index.html').read_text(encoding='utf-8'))
        self.assertIsNotNone(parser.upload)
        self.assertEqual('input', parser.upload['tag'])
        self.assertEqual('file', parser.upload['type'])
        self.assertIn('.docx', parser.upload['accept'])
        source = SOURCE.read_text(encoding='utf-8')
        source = source[source.index('let docxPreviewCandidate = null;'):
                        source.index('// Generate resumes saved progress')]
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {style:{}, files:[], value:'',
    addEventListener(event, handler) {this[event] = handler;}});
  return elements.get(id);
}
const input = element(markup.id);
let fileValue = '';
Object.defineProperty(input, 'value', {
  get() {return fileValue;},
  set(value) {fileValue=value; if(value==='') this.files=[];}
});
async function pick(file) {
  const value='C:\\fakepath\\'+file.name;
  if (input.value===value) return false;
  input.value=value; input.files=[file]; await input.change(); return true;
}
let uploads=0, reads=0;
const context = {
  document:{getElementById:element}, _bookSelectionEpoch:0,
  currentBookFilename:'old.txt',
  ensureCastListEditsDiscardable:async()=>true,
  enqueueBookSelection:async(request, operation)=>operation(),
  applyCurrentBookFilename:name=>{context.currentBookFilename=name;},
  clearCastListEditor:()=>{}, loadCastList:async()=>{},
  loadExistingScriptUploads:async()=>{}, escapeHtml:value=>value,
  getActionErrorMessage:(label,error)=>label+': '+error.message,
  FormData:class {append(){}}, encodeURIComponent,
  API:{upload:async()=>{uploads++; if(uploads===1) throw Error('held');
    return {stored_filename:'book.txt'};}, _handleError:async()=>{}},
  fetch:async()=>{reads++; if(reads===2) throw Error('held');
    return {json:async()=>({text:'Synthetic source',policy_text:'Review',
      warnings:[],preview_digest:'a'.repeat(64)})};}
};
context.window=context; vm.createContext(context);
vm.runInContext(source,context);
'''
        program = 'const markup=' + json.dumps(parser.upload) + ';\n'
        program += 'const source=' + json.dumps(source) + ';\n' + harness
        program += '\n(async()=>{' + scenario + '\n})().then(()=>console.log("complete"))'
        program += '.catch(error=>{console.error(error);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', program], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('complete', result.stdout)

    def test_failed_upload_allows_same_file_retry(self):
        self.run_js(r'''
const file={name:'book.txt'};
assert.equal(await pick(file),true);
assert.equal(uploads,1);
assert.equal(input.value,''); assert.equal(input.files.length,0);
assert.equal(await pick(file),true);
assert.equal(uploads,2);
assert.equal(context.currentBookFilename,'book.txt');
assert.match(element('upload-status').innerHTML,/Loaded/);
''')

    def test_failed_docx_acceptance_allows_same_file_preview_again(self):
        self.run_js(r'''
const file={name:'book.docx'};
await pick(file);
assert.equal(reads,1); assert.equal(element('docx-preview-panel').hidden,false);
await context.acceptDocxPreview();
assert.equal(reads,2); assert.equal(element('docx-preview-panel').hidden,true);
assert.equal(input.value,''); assert.equal(input.files.length,0);
assert.equal(await pick(file),true);
assert.equal(reads,3); assert.equal(element('docx-preview-panel').hidden,false);
assert.equal(context.currentBookFilename,'old.txt');
''')
