"""Independent native Git observations overlap without changing fingerprint bytes."""
import subprocess
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
import benchmark_environment_identity as identity


class GitObservationTests(unittest.TestCase):
    def fixture(self, root):
        subprocess.run(['git','init','-q',str(root)],check=True)
        (root/'app').mkdir();(root/'app/tracked.py').write_text('before\n')
        subprocess.run(['git','-C',str(root),'add','app'],check=True)
        subprocess.run(['git','-C',str(root),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','base'],check=True)
        (root/'app/tracked.py').write_text('after\n');(root/'app/untracked.py').write_text('native source\n')

    def test_three_queries_overlap_and_preserve_native_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            expected=identity._get_local_worktree_identity(tmp)
            real_run=subprocess.run;gate=threading.Barrier(3,timeout=2);threads=set();commands=[]
            def run(argv,**kwargs):
                threads.add(threading.get_ident());commands.append((argv,kwargs));gate.wait()
                return real_run(argv,**kwargs)
            with patch.object(identity.subprocess,'run',side_effect=run):actual=identity._get_local_worktree_identity(tmp)
            self.assertEqual(expected,actual);self.assertEqual(3,len(threads))
            self.assertEqual({'status','diff','ls-files'},{argv[3] for argv,_ in commands})
            self.assertTrue(all(kwargs['timeout']==20 and kwargs['check'] is False for _,kwargs in commands))
            self.assertTrue(actual['dirty'])

    def test_failed_query_still_joins_other_started_reads_and_returns_no_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            real_run=subprocess.run;gate=threading.Barrier(3,timeout=2);finished=[]
            def run(argv,**kwargs):
                gate.wait();result=real_run(argv,**kwargs);finished.append(argv[3])
                if argv[3]=='diff':return subprocess.CompletedProcess(argv,1,stdout=b'',stderr=b'fixture refusal')
                return result
            with patch.object(identity.subprocess,'run',side_effect=run):
                with self.assertRaisesRegex(ValueError,'could not be identified'):identity._get_local_worktree_identity(tmp)
            self.assertEqual({'status','diff','ls-files'},set(finished))
