"""Import regression simulation; native Windows evidence comes from CI."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WindowsImportBoundaryTests(unittest.TestCase):
    def test_application_and_preparer_import_with_fcntl_unavailable(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            code = """import sys,types,os
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/'app'),str(root)]
# Prevent model dependency imports; core itself and all lock code stay real.
project=types.ModuleType('project');project.ProjectManager=lambda *a,**k:None
sys.modules['project']=project
sys.modules['fcntl']=None
for name in ('core','alexandria_run_manifest','alexandria_preparer_rocm_compatible','corpus_alignment_prescan','corpus_run_report'):
 __import__(name)
 print('IMPORTED',name)
"""
            result = subprocess.run([sys.executable, '-c', code, str(root)],
                env={**os.environ, 'ALEXANDRIA_DATA_DIR': tmp}, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(5, result.stdout.count('IMPORTED'))
