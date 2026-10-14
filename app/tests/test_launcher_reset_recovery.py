"""Real interrupted transactions must be settled before launcher Reset deletes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import book_state_transaction as books


ROOT = Path(__file__).resolve().parents[2]
ACTIVE = ('annotated_script.json', 'voice_config.json', 'state.json', 'chunks.json')
INTERRUPT = '''import os,sys
from pathlib import Path
import book_state_transaction as books
root=Path(sys.argv[1]);boundary=sys.argv[2];point=int(sys.argv[3]);count=0
native=books._move if boundary=='move' else books._save_journal
def stop(*args,**kwargs):
 global count
 result=native(*args,**kwargs);count+=1
 if count==point:os._exit(77)
 return result
if boundary=='move':books._move=stop
else:books._save_journal=stop
with books.ensure_book_state(root):
 books.apply_book_state_locked(root,{name:b'new complete artifact' for name in ('annotated_script.json','voice_config.json','state.json','chunks.json')},[])
'''
RESET = r'''const fs=require('node:fs'),path=require('node:path');
const root=process.argv[1],source=process.argv[2],python=process.argv[3];
const steps=require(path.join(source,'reset.js')).run;
const Lifecycle=require(path.join(root,'launcher_lifecycle.js')),helper=new Lifecycle();
const req={parent:{path:path.join(root,'reset.js')}};
const events=[],kernel={which:()=>python,api:{stop:async r=>{events.push('stop:'+path.basename(r.params.uri))}}};
(async()=>{
 let removed=0;
 for(const step of steps){
  if(step.uri==='launcher_lifecycle.js'){await helper[step.method](req,()=>{},kernel);events.push(step.method)}
  else{
   if(step.method!=='fs.rm'){throw new Error('unexpected Reset mutation')}
   fs.rmSync(path.join(root,step.params.path),{recursive:true,force:true});removed++;
   if(process.argv[4]==='interrupt'){process.exit(77)}
  }
 }
 console.log(JSON.stringify({removed,events}));
})().catch(error=>{console.error(error);process.exitCode=1});
'''


class LauncherResetRecoveryTests(unittest.TestCase):
    def prepare(self, root):
        (root / 'app').mkdir()
        for name in ('book_state_transaction.py', 'utils.py', 'adapter_publication.py',
                     'editor_poll_snapshot.py', 'chunk_status_journal.py'):
            shutil.copyfile(ROOT / 'app' / name, root / 'app' / name)
        shutil.copyfile(ROOT / 'gpu_stats.py', root / 'gpu_stats.py')
        shutil.copyfile(ROOT / 'launcher_lifecycle.js', root / 'launcher_lifecycle.js')
        for name in ACTIVE:
            (root / name).write_bytes(('old ' + name).encode())
        (root / 'scripts').mkdir()
        (root / 'scripts/saved.json').write_bytes(b'exact reusable saved book')
        (root / 'voice_library.json').write_bytes(b'exact reusable cast')

    def artifacts(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
                if p.is_file() and not p.name.endswith('.lock')}

    def interrupt(self, root, boundary='move', point=8):
        result = subprocess.run([sys.executable, '-c', INTERRUPT, str(root), boundary, str(point)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(77, result.returncode, result.stdout + result.stderr)
        self.assertTrue((root / books.JOURNAL).is_file())

    def reset(self, root, interrupt=False):
        return subprocess.run(['node', '-e', RESET, str(root), str(ROOT), sys.executable,
                               'interrupt' if interrupt else 'complete'], capture_output=True,
                              text=True, timeout=20,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})

    def test_every_interrupted_switch_boundary_stays_reset_after_restart(self):
        cases = [('move', n) for n in range(1, 9)] + [('journal', 2)]
        for boundary, point in cases:
            with self.subTest(boundary=boundary, point=point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.prepare(root)
                self.interrupt(root, boundary, point)
                result = self.reset(root)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(9, json.loads(result.stdout)['removed'])
                with books.ensure_book_state(root):
                    pass
                self.assertTrue(all(not (root / name).exists() for name in ACTIVE))
                self.assertFalse((root / books.JOURNAL).exists())
                self.assertEqual([], list(root.glob('.book-switch-*')))
                self.assertEqual(b'exact reusable saved book', (root / 'scripts/saved.json').read_bytes())
                self.assertEqual(b'exact reusable cast', (root / 'voice_library.json').read_bytes())

    def test_invalid_recovery_evidence_refuses_before_any_reset_deletion(self):
        for kind in ('changed replacement', 'missing original', 'malformed journal'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.prepare(root)
                self.interrupt(root)
                journal = root / books.JOURNAL
                if kind == 'changed replacement':
                    (root / 'annotated_script.json').write_bytes(b'human edit after interruption')
                elif kind == 'missing original':
                    data = json.loads(journal.read_text())
                    (root / data['workspace'] / 'old-0').unlink()
                else:
                    journal.write_text('not valid JSON')
                before = self.artifacts(root)
                result = self.reset(root)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertEqual(before, self.artifacts(root))

    def test_reset_without_journal_and_reset_interruption_do_not_resurrect_deleted_files(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.prepare(root)
                if interrupt:
                    self.interrupt(root)
                result = self.reset(root, interrupt=interrupt)
                self.assertEqual(77 if interrupt else 0, result.returncode, result.stderr)
                self.assertFalse((root / 'annotated_script.json').exists())
                with books.ensure_book_state(root):
                    pass
                self.assertFalse((root / 'annotated_script.json').exists())
                self.assertFalse((root / books.JOURNAL).exists())
                self.assertEqual(b'exact reusable saved book', (root / 'scripts/saved.json').read_bytes())
