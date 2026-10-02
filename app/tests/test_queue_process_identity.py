import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import uuid


LIB = Path(__file__).resolve().parent.parent.parent / 'run_chains/lib/queue.sh'


class QueueProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'run_chains').mkdir()
        self.name = 'identity_'+uuid.uuid4().hex+'.sh'
        self.script = self.root/'run_chains'/self.name
        self.script.write_text('printf ready > "$1"\nread -r finish\n')
        self.children = []; self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None: child.terminate()
            try: child.communicate(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.communicate()

    def start(self, argv):
        ready = self.root/('ready_'+str(len(self.children)))
        child = subprocess.Popen(argv+[str(ready)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        deadline = time.monotonic()+3
        while not ready.exists() and child.poll() is None and time.monotonic()<deadline: time.sleep(.01)
        self.assertTrue(ready.exists(), (argv, child.poll()))
        return child

    def check(self, helper=LIB):
        return subprocess.run(['bash','-c','source "$1"; chain_running "$2"',
            'fixture',str(helper),self.name],capture_output=True,text=True,timeout=5)

    def test_mentions_in_other_programs_command_strings_and_stale_suffixes_are_not_chains(self):
        self.start([os.sys.executable,'-c', 'import pathlib,sys;pathlib.Path(sys.argv[-1]).write_text("ready");input()',str(self.script)])
        self.start(['bash','-c','printf ready > "${@: -1}"; read -r finish','fixture',str(self.script)])
        stale = Path(str(self.script)+'.old'); stale.write_text(self.script.read_text())
        self.start(['bash',str(stale)])
        self.assertEqual(1, self.check().returncode)

    def test_actual_shell_script_with_options_and_spaces_is_detected_until_exit(self):
        self.root = self.root/'directory with spaces'; (self.root/'run_chains').mkdir(parents=True)
        self.script = self.root/'run_chains'/self.name; self.script.write_text('printf ready > "$1"\nread -r finish\n')
        child = self.start(['bash','--norc','-o','pipefail','-e',str(self.script)])
        self.assertEqual(0,self.check().returncode)
        child.communicate('finish\n',timeout=3)
        self.assertEqual(1,self.check().returncode)

    def test_waiter_rechecks_actual_process_using_five_second_poll_contract(self):
        child = self.start(['bash',str(self.script)])
        polls = self.root/'polls'
        def release_after_poll():
            deadline = time.monotonic()+4
            while not polls.exists() and time.monotonic()<deadline: time.sleep(.01)
            child.communicate('finish\n',timeout=3)
        release = threading.Thread(target=release_after_poll);release.start()
        try:
            result = subprocess.run(['bash','-c',
                'set -euo pipefail; source "$1"; poll_log="$3"; sleep() { printf "%s\\n" "$1" >> "$poll_log"; command sleep .02; }; wait_for_chain "$2"',
                'fixture',str(LIB),self.name,str(polls)],capture_output=True,text=True,timeout=5)
        finally: release.join(timeout=4)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertIn('finished',result.stdout)
        self.assertTrue(polls.exists())
        self.assertTrue(all(line=='5' for line in polls.read_text().splitlines()))

    def test_invalid_identity_is_error_not_finished(self):
        result = subprocess.run(['bash','-c','source "$1"; wait_for_chain "bad/name"',
            'fixture',str(LIB)],capture_output=True,text=True,timeout=5)
        self.assertEqual(2,result.returncode)
        self.assertNotIn('continuing',result.stdout)
