"""Actual empty export listing clears previous chapter links and ZIP controls."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from routers import editor
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class ChapterExportEmptyUiTests(unittest.TestCase):
    def test_native_empty_receipt_and_empty_renderer_remove_stale_downloads(self):
        with tempfile.TemporaryDirectory() as root, patch.object(editor, '_chapter_export_dir', return_value=root):
            receipt = editor._get_chapter_export_listing()
            self.assertEqual(receipt['chapters'], [])
            self.assertEqual(list(Path(root).iterdir()), [])
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8'),receipt=JSON.parse(process.argv[2]);const elements={};const el=id=>elements[id]||(elements[id]={innerHTML:'',style:{}});
const c={document:{getElementById:el},API:{get:async()=>receipt},escapeHtml:String};vm.createContext(c);const a=source.indexOf('function renderChapterList(');vm.runInContext(source.slice(a,source.indexOf('async function getChapterExportPreview(',a)),c);
const previous=[{file:'prior.wav',exists:true,bytes:16}];
(async()=>{
c.renderChapterList(previous,true);assert(el('chapter-list').innerHTML.includes('/api/chapter_exports/file/prior.wav'));assert.strictEqual(el('chapter-zip-link').style.display,'');await c.loadChapterExports();assert(!el('chapter-list').innerHTML.includes('prior.wav'));assert.strictEqual(el('chapter-zip-link').style.display,'none');assert(el('chapter-list').innerHTML.includes('No exported'));
c.renderChapterList(previous,true);c.renderChapterList([],true);assert.strictEqual(el('chapter-zip-link').style.display,'none');c.renderChapterList(previous,true);c.renderChapterList([],false);assert.strictEqual(el('chapter-zip-link').style.display,'none');
c.renderChapterList([{file:'missing.wav',exists:false}],true);assert.strictEqual(el('chapter-zip-link').style.display,'none');assert(!el('chapter-list').innerHTML.includes('<a'));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE), json.dumps(receipt)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
