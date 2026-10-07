"""Actual cast match/apply handlers retain request, cast and book ownership."""
from pathlib import Path
import subprocess
import unittest
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class CastApplyContextJsTests(unittest.TestCase):
    def test_stale_matches_and_old_panel_submission_are_refused(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const panel={innerHTML:''},select={value:'A'},posts=[],statuses=[],rows=[];const c={window:null,currentBookFilename:'book-A',_voiceSaveSnapshot:{book_token:'token-A'},_selectedCast:'A',document:{getElementById:id=>id==='cast-select'?select:panel,querySelectorAll:()=>rows,querySelector:()=>({value:'role'})},CSS:{escape:String},escapeHtml:String,API:{post:(path,body)=>new Promise((resolve,reject)=>posts.push({path,body,resolve,reject}))},setCastStatus:(...args)=>statuses.push(args),clearVoiceSuggestions(){},renderCastMembers:()=>panel.innerHTML='Current cast members',_getCastMatchPool:()=>[],_renderCastMatchRows:proposals=>proposals.map(p=>p.character).join(','),loadVoices:async()=>{}};c.window=c;vm.createContext(c);
let a=source.indexOf('function showActionError(');vm.runInContext(source.slice(a,source.indexOf('function showConfirm(',a)),c);
a=source.indexOf('function onCastChange()');vm.runInContext(source.slice(a,source.indexOf('function renderCastMembers()',a)),c);
function changeCast(name){select.value=name;c.onCastChange();}
a=source.indexOf('async function openCastApply(');vm.runInContext(source.slice(a,source.indexOf('// --- Apply a cast to multiple saved books at once ---',a)),c);
(async()=>{
for(const change of ['cast','cast-cycle','book','token']){
 c._selectedCast='A';c.currentBookFilename='book-A';c._voiceSaveSnapshot={book_token:'token-A'};const n=posts.length,pending=c.openCastApply();
 if(change==='cast'){changeCast('B');}else if(change==='cast-cycle'){changeCast('B');changeCast('A');}else if(change==='book'){c.currentBookFilename='book-B';}else{c._voiceSaveSnapshot={book_token:'token-B'};}
 panel.innerHTML='Current context';posts[n].resolve({proposals:[{character:'Old',match:{key:'role'}}]});await pending;assert.strictEqual(panel.innerHTML,'Current context');
}
c._selectedCast='A';c.currentBookFilename='book-A';c._voiceSaveSnapshot={book_token:'token-A'};const index=posts.length,first=c.openCastApply(),second=c.openCastApply();posts[index+1].resolve({proposals:[{character:'New',match:{key:'role'}}]});await second;const current=panel.innerHTML;posts[index].reject(Error('stale error'));await first;assert.strictEqual(panel.innerHTML,current);assert(current.includes('New'));assert(!current.includes('Old'));assert.strictEqual(statuses.length,0);
rows.push({dataset:{char:'New'}});c._selectedCast='B';const before=posts.length;await c.submitCastApply();assert.strictEqual(posts.length,before);
c._selectedCast='A';const freshIndex=posts.length,fresh=c.openCastApply();posts[freshIndex].resolve({proposals:[{character:'New',match:{key:'role'}}]});await fresh;const apply=c.submitCastApply();assert.strictEqual(posts.at(-1).path,'/api/voice_library/apply');assert.strictEqual(posts.at(-1).body.cast,'A');posts.at(-1).resolve({count:1});await apply;assert(statuses.some(row=>row[0].includes('Applied 1 voice')));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
