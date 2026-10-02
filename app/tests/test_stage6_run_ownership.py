import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = '''import sys
from pathlib import Path
import run_stage6_listening as runner
root=Path(sys.argv[1]);mode=sys.argv[2]
runner.STATUS=str(root/'nested/status.json')
runner._load_document=lambda *args: {}
def scene():
    with (root/'events').open('a') as handle:handle.write('scene\\n')
    print('SCENE_ENTERED',flush=True)
    if mode=='hold':sys.stdin.readline()
def check(*args):
    print('FINAL_CHECK_ENTERED',flush=True)
    if mode=='hold':sys.stdin.readline()
runner.ensure_scene=scene
runner.ensure_instruction=lambda:None
runner.ensure_casting=lambda:None
runner.ensure_package=lambda:None
runner.run=check
runner.main()
'''


class Stage6RunOwnershipTests(unittest.TestCase):
    def start(self, root, mode):
        return subprocess.Popen([sys.executable,'-c',HARNESS,str(root),mode],cwd=ROOT,
            env={**os.environ,'PYTHONPATH':os.pathsep.join([str(ROOT),str(ROOT/'app')])},
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

    def test_native_owner_excludes_second_start_even_after_status_becomes_human_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);owner=self.start(root,'hold')
            try:
                self.assertEqual('SCENE_ENTERED',owner.stdout.readline().strip())
                status=root/'nested/status.json';before=status.read_bytes()
                intruder=self.start(root,'once');out,err=intruder.communicate(timeout=10)
                self.assertNotEqual(0,intruder.returncode);self.assertIn('Could not acquire file lock',err)
                self.assertNotIn('SCENE_ENTERED',out);self.assertEqual(before,status.read_bytes())
                owner.stdin.write('continue\n');owner.stdin.flush()
                self.assertEqual('FINAL_CHECK_ENTERED',owner.stdout.readline().strip())
                self.assertEqual('human_pending',json.loads(status.read_bytes())['status'])
                before=status.read_bytes()
                intruder=self.start(root,'once');out,err=intruder.communicate(timeout=10)
                self.assertNotEqual(0,intruder.returncode);self.assertIn('Could not acquire file lock',err)
                self.assertNotIn('SCENE_ENTERED',out);self.assertEqual(before,status.read_bytes())
                out,err=owner.communicate('continue\ncontinue\n',timeout=10)
                self.assertEqual(0,owner.returncode,err)
                self.assertIn('materials complete; human ratings are pending',out)
                self.assertEqual('scene\n',(root/'events').read_text())
                next_owner=self.start(root,'once');out,err=next_owner.communicate(timeout=10)
                self.assertEqual(0,next_owner.returncode,err)
                self.assertEqual('scene\nscene\n',(root/'events').read_text())
                self.assertEqual('human_pending',json.loads(status.read_bytes())['status'])
            finally:
                if owner.poll() is None:owner.kill();owner.communicate(timeout=5)

    def test_native_owner_death_releases_lock_but_unfinished_status_still_requires_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);owner=self.start(root,'hold')
            try:
                self.assertEqual('SCENE_ENTERED',owner.stdout.readline().strip())
                status=root/'nested/status.json';before=status.read_bytes()
                owner.kill();owner.communicate(timeout=5)
                retry=self.start(root,'once');out,err=retry.communicate(timeout=10)
                self.assertNotEqual(0,retry.returncode)
                self.assertIn('unfinished status requires inspection',err)
                self.assertNotIn('Could not acquire file lock',err)
                self.assertNotIn('SCENE_ENTERED',out)
                self.assertEqual(before,status.read_bytes())
                self.assertEqual('scene\n',(root/'events').read_text())
            finally:
                if owner.poll() is None:owner.kill();owner.communicate(timeout=5)
