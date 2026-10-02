"""Actual chain rejects unsafe paths before creating directories or dispatch."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[2]


class ResumeArmArgumentTests(unittest.TestCase):
    def test_invalid_separator_and_limit_refuse_before_any_runtime_work(self):
        source=Path('/tmp/before306_resume_partial_arm.sh') if os.environ.get('RESUME_ARGUMENT_BASELINE') else REPO/'run_chains/resume_partial_arm_20260826.sh'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);chain=root/'run_chains/resume_partial_arm_20260826.sh';chain.parent.mkdir();chain.write_bytes(source.read_bytes())
            for sep,limit in (('../../escape','2'),('space','../../../../tmp/out'),('space','-1'),('space','1.5'),('SPACE','2'),('','2')):
                with self.subTest(separator=sep,limit=limit):
                    result=subprocess.run(['bash',str(chain),sep,limit],cwd=root,capture_output=True,text=True,timeout=5)
                    self.assertEqual(2,result.returncode,result.stdout+result.stderr)
                    self.assertFalse((root/'ab_test_runtime').exists())
                    self.assertNotIn('No such file or directory',result.stderr)

    def test_allowed_separators_and_nonnegative_limits_reach_existing_admission(self):
        # Actual chain admission prefix, stopping before shared helpers/work.
        source=(REPO/'run_chains/resume_partial_arm_20260826.sh').read_text()
        prefix=source[:source.index('\nREPO=')]
        for sep in ('none','space','dot','hyphen'):
            for limit in ('0','2','1600'):
                with self.subTest(separator=sep,limit=limit):
                    result=subprocess.run(['bash','-c',prefix+'\necho admitted\n','fixture',sep,limit],capture_output=True,text=True,timeout=5)
                    self.assertEqual(0,result.returncode,result.stderr)
                    self.assertEqual('admitted\n',result.stdout)
