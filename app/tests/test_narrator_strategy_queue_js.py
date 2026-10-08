"""Execute actual strategy handler with deferred writes and book switches."""
from pathlib import Path
import subprocess
import unittest
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class NarratorStrategyQueueJsTests(unittest.TestCase):
    def test_writes_are_ordered_and_bound_to_the_starting_book(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const posts=[],toasts=[],errors=[];const c={window:null,currentBookFilename:'A',_voiceSaveSnapshot:{book_token:'a'.repeat(64),revision:'0'.repeat(64)},updateNarratorPreviewFields(){},flushVoiceSaves:async()=>{},API:{post:(path,body)=>new Promise((resolve,reject)=>posts.push({path,body,resolve,reject}))},showToast:(...args)=>toasts.push(args),showActionError:(...args)=>errors.push(args)};c.window=c;vm.createContext(c);
const a=source.indexOf('window.saveNarratorStrategy =');vm.runInContext(source.slice(a,source.indexOf('window.updateNarratorPreviewFields =',a)),c);
const turn=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
const first=c.saveNarratorStrategy('focus'),second=c.saveNarratorStrategy('chapter');await turn();assert.strictEqual(posts.length,1);assert.strictEqual(posts[0].body.strategy,'focus');assert.strictEqual(posts[0].body.book_token,'a'.repeat(64));
posts[0].resolve({strategy:'focus',revision:'1'.repeat(64)});await first;await turn();assert.strictEqual(posts.length,2);assert.strictEqual(posts[1].body.strategy,'chapter');posts[1].resolve({strategy:'chapter',revision:'2'.repeat(64)});await second;assert.strictEqual(toasts.length,1);assert.strictEqual(c._voiceSaveSnapshot.revision,'2'.repeat(64));
const old=c.saveNarratorStrategy('focus'),queued=c.saveNarratorStrategy('global');await turn();assert.strictEqual(posts.length,3);c.currentBookFilename='B';c._voiceSaveSnapshot={book_token:'b'.repeat(64),revision:'b'.repeat(64)};posts[2].resolve({revision:'3'.repeat(64)});await old;await queued;assert.strictEqual(posts.length,3);assert.strictEqual(toasts.length,1);assert.strictEqual(c._voiceSaveSnapshot.revision,'b'.repeat(64));
const failed=c.saveNarratorStrategy('focus'),latest=c.saveNarratorStrategy('chapter');await turn();posts[3].reject(Error('transport'));await failed;await turn();assert.strictEqual(posts.length,5);posts[4].resolve({revision:'4'.repeat(64)});await latest;assert.strictEqual(errors.length,0);assert.strictEqual(toasts.length,2);
const rejected=c.saveNarratorStrategy('focus');await turn();posts[5].reject(Error('current failure'));await rejected;assert.strictEqual(errors.length,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
