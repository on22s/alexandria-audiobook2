"""Execute the remote ownership protocol locally with native detached workers."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
import uuid

from tests.test_benchmark_local_process_cancel import is_alive
from benchmark_remote_command import (get_remote_benchmark_receipt,
                                      apply_remote_benchmark_receipt_cleanup)

OWNER = Path(__file__).resolve().parent.parent/'benchmark_remote_command.py'


@unittest.skipUnless(sys.platform=='linux','native Linux remote-owner protocol proof')
class BenchmarkRemoteOwnerTests(unittest.TestCase):
    def test_failed_command_admission_proves_shutdown_and_cleanup_requires_complete_receipt(self):
        token=uuid.uuid4().hex;receipt=self.receipt_for(token)
        packet={'token':token,'command':['/missing-executable-'+token],'input':None}
        result=subprocess.run([sys.executable,str(OWNER)],input=json.dumps(packet)+'\n',capture_output=True,text=True,timeout=5)
        self.assertNotEqual(0,result.returncode)
        self.assertIn('BENCHMARK_OWNER_STOPPED='+token,result.stdout.splitlines())
        self.assertEqual('stopped\n',receipt.read_text())
        self.assertIn('No such file',result.stderr)
        receipt.write_text('partial')
        with self.assertRaises(ValueError):apply_remote_benchmark_receipt_cleanup(token)
        self.assertTrue(receipt.exists())
        receipt.write_text('stopped\n')
        apply_remote_benchmark_receipt_cleanup(token)
        self.assertFalse(receipt.parent.exists())

    def receipt_for(self, token):
        receipt=get_remote_benchmark_receipt(token)
        self.addCleanup(shutil.rmtree,receipt.parent,ignore_errors=True)
        return receipt

    def test_command_input_output_and_shutdown_acknowledgment(self):
        token=uuid.uuid4().hex
        receipt=self.receipt_for(token)
        process=subprocess.Popen([sys.executable,str(OWNER)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            packet={'token':token,'command':[sys.executable,'-c','import sys,time;data=sys.stdin.read();time.sleep(.1);print(data);print("worker-error-channel",file=sys.stderr)'], 'input':'音声🙂'}
            process.stdin.write(json.dumps(packet)+'\n');process.stdin.flush()
            self.assertEqual('音声🙂\n',process.stdout.readline())
            self.assertEqual('\n',process.stdout.readline())
            self.assertEqual('BENCHMARK_OWNER_STOPPED='+token+'\n',process.stdout.readline())
            self.assertEqual(0,process.wait(timeout=5))
            self.assertEqual('stopped\n',receipt.read_text())
            self.assertIn('worker-error-channel',process.stderr.read())
        finally:
            if process.poll() is None:process.kill();process.wait()
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def test_cancel_and_connection_eof_reap_detached_worker_before_ack(self):
        worker='''import json,os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'],start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(30)
'''
        for action in ('cancel','eof'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as tmp:
                marker=Path(tmp)/'pids.json';token=uuid.uuid4().hex
                receipt=self.receipt_for(token)
                sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
                process=subprocess.Popen([sys.executable,str(OWNER)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    process.stdin.write(json.dumps({'token':token,'command':[sys.executable,'-c',worker,str(marker)],'input':None})+'\n');process.stdin.flush()
                    deadline=time.monotonic()+5
                    while not marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                    self.assertTrue(marker.exists(),'owned work must actually start')
                    pids=json.loads(marker.read_text());self.assertTrue(all(is_alive(pid) for pid in pids))
                    if action=='cancel':process.stdin.write('cancel\n');process.stdin.flush()
                    else:process.stdin.close()
                    self.assertEqual('\n',process.stdout.readline())
                    self.assertEqual('BENCHMARK_OWNER_STOPPED='+token+'\n',process.stdout.readline())
                    self.assertEqual(130,process.wait(timeout=5))
                    self.assertTrue(all(not is_alive(pid) for pid in pids))
                    self.assertEqual('stopped\n',receipt.read_text())
                    self.assertIsNone(sentinel.poll())
                finally:
                    if process.poll() is None:process.kill();process.wait()
                    sentinel.terminate();sentinel.wait(timeout=5)
                    for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def test_lost_supervisor_still_leaves_receipt_only_after_detached_worker_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'pids.json';token=uuid.uuid4().hex
            receipt=self.receipt_for(token)
            worker='''import json,os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(30)
'''
            process=subprocess.Popen([sys.executable,str(OWNER)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            try:
                process.stdin.write(json.dumps({'token':token,'command':[sys.executable,'-c',worker,str(marker)],'input':None})+'\n');process.stdin.flush()
                deadline=time.monotonic()+5
                while not marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(marker.exists())
                pids=json.loads(marker.read_text())
                self.assertTrue(all(is_alive(pid) for pid in pids))
                self.assertFalse(receipt.exists(),'live workers cannot have a shutdown receipt')
                process.kill();process.wait(timeout=5)
                deadline=time.monotonic()+5
                while not receipt.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual('stopped\n',receipt.read_text())
                self.assertTrue(all(not is_alive(pid) for pid in pids))
                self.assertIsNone(sentinel.poll())
            finally:
                if process.poll() is None:process.kill();process.wait()
                sentinel.terminate();sentinel.wait(timeout=5)
                for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def test_reused_token_cannot_launch_new_work_using_stale_receipt(self):
        token=uuid.uuid4().hex;receipt=self.receipt_for(token)
        receipt.parent.mkdir(mode=0o700);receipt.write_text('stopped\n')
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'not-admitted'
            packet={'token':token,'command':[sys.executable,'-c',f'from pathlib import Path;Path({str(marker)!r}).touch()'],'input':None}
            result=subprocess.run([sys.executable,str(OWNER)],input=json.dumps(packet)+'\n',capture_output=True,text=True,timeout=5)
            self.assertNotEqual(0,result.returncode)
            self.assertFalse(marker.exists())
            self.assertNotIn('BENCHMARK_OWNER_STOPPED=',result.stdout)
            self.assertEqual('stopped\n',receipt.read_text())

    def test_malformed_packet_cannot_start_worker_or_claim_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'should-not-exist'
            packet={'token':'bad','command':[sys.executable,'-c',f'from pathlib import Path;Path({str(marker)!r}).touch()'],'input':None}
            result=subprocess.run([sys.executable,str(OWNER)],input=json.dumps(packet)+'\n',capture_output=True,text=True,timeout=5)
            self.assertNotEqual(0,result.returncode)
            self.assertFalse(marker.exists())
            self.assertNotIn('BENCHMARK_OWNER_STOPPED=',result.stdout)
            self.assertIn('Invalid remote benchmark command packet',result.stderr)
