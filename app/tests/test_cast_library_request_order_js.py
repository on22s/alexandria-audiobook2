"""Actual cast creation and library reads reject superseded list responses."""
from pathlib import Path
import subprocess
import unittest
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class CastLibraryRequestOrderJsTests(unittest.TestCase):
    def test_creation_and_current_book_keep_newer_library_despite_old_response(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={},reads=[],toasts=[],statuses=[];let cleared=0,painted=0;
const el=id=>elements[id]||(elements[id]={innerHTML:'',disabled:false,value:'A'});
const c={window:null,currentBookFilename:'book-A',_selectedCast:'A',document:{getElementById:el},escapeHtml:String,API:{get:()=>new Promise((resolve,reject)=>reads.push({resolve,reject})),post:async()=>({})},clearVoiceSuggestions:()=>cleared++,renderCastMembers:()=>painted++,showPresetEditor:async()=>({name:'B'}),showToast:(...args)=>toasts.push(args),setCastStatus:(...args)=>statuses.push(args)};c.window=c;vm.createContext(c);
let a=source.indexOf('async function loadCastLibrary()');vm.runInContext(source.slice(a,source.indexOf('function getSelectedCastObj()',a)),c);
a=source.indexOf('async function createCast()');vm.runInContext(source.slice(a,source.indexOf('async function deleteCast()',a)),c);
const old={casts:[{name:'A'}],current_characters:[{name:'Old',line_count:10}]},fresh={casts:[{name:'A'},{name:'B'}],current_characters:[{name:'New',line_count:5}]};const turn=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
const older=c.loadCastLibrary(),create=c.createCast();await turn();assert.strictEqual(reads.length,2);reads[1].resolve(fresh);await create;assert.strictEqual(c._selectedCast,'B');const newerMarkup=el('cast-select').innerHTML;reads[0].resolve(old);await older;assert.strictEqual(c._selectedCast,'B');assert.strictEqual(el('cast-select').innerHTML,newerMarkup);assert.strictEqual(c._voiceLibrary,fresh);assert.strictEqual(cleared,0);
const n=reads.length,oldError=c.loadCastLibrary(),latest=c.loadCastLibrary();reads[n+1].resolve(fresh);await latest;reads[n].reject(Error('superseded read'));await oldError;
const count=reads.length,before=painted,previous=c._voiceLibrary,wrongBook=c.loadCastLibrary();c.currentBookFilename='book-B';reads[count].resolve(old);await wrongBook;assert.strictEqual(c._voiceLibrary,previous);assert.strictEqual(painted,before);
const current=reads.length,failing=c.loadCastLibrary();reads[current].reject(Error('current failure'));await assert.rejects(failing,/current failure/);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
