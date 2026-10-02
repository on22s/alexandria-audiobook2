"""Actual isolated interpreter import behavior, without poisoning this suite."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class CIImportPolicyTests(unittest.TestCase):
    def run_policy(self, body):
        source = os.environ.get('CI_ENV_SOURCE', str(Path(__file__).resolve().parent.parent / 'ci_env.py'))
        setup = ('import importlib.util, sys, types\n'
                 f'spec=importlib.util.spec_from_file_location("ci_policy", {source!r})\n'
                 'ci=importlib.util.module_from_spec(spec);spec.loader.exec_module(ci)\n')
        result = subprocess.run([sys.executable, '-c', setup + body],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_repeated_policy_has_one_finder_and_cannot_be_masked_by_loaded_module(self):
        self.run_policy('''import json
ci.block_ml_imports(['json'])
first=[finder for finder in sys.meta_path if isinstance(finder, ci._BlockedImportFinder)][0]
sys.modules['json']=types.ModuleType('json')
ci.block_ml_imports(['json'])
assert 'json' not in sys.modules
finders=[finder for finder in sys.meta_path if isinstance(finder, ci._BlockedImportFinder)]
assert len(finders)==1, len(finders)
assert finders[0] is first
try:
 import json
except ImportError:
 pass
else:
 raise AssertionError('cached module masked blocking')
''')

    def test_changed_policy_replaces_old_roots_and_preserves_foreign_finders(self):
        self.run_policy('''class Foreign:
 def find_spec(self, *args):return None
foreign=Foreign();sys.meta_path.insert(0,foreign)
ci.block_ml_imports(['json'])
ci.block_ml_imports(['math'])
assert foreign in sys.meta_path
import json
try:
 import math
except ImportError:
 pass
else:
 raise AssertionError('new policy was ignored')
assert sum(isinstance(f,ci._BlockedImportFinder) for f in sys.meta_path)==1
''')

    def test_generator_policy_purges_submodules_and_disables_cleanly(self):
        self.run_policy('''sys.modules['fictional']=types.ModuleType('fictional')
sys.modules['fictional.child']=types.ModuleType('fictional.child')
ci.block_ml_imports(iter(['fictional','fictional']))
assert 'fictional' not in sys.modules and 'fictional.child' not in sys.modules
assert sum(isinstance(f,ci._BlockedImportFinder) for f in sys.meta_path)==1
ci.block_ml_imports([])
assert not any(isinstance(f,ci._BlockedImportFinder) for f in sys.meta_path)
''')

    def test_preexisting_duplicate_finders_are_consolidated(self):
        self.run_policy('''sys.meta_path.insert(0,ci._BlockedImportFinder(['json']))
sys.meta_path.insert(0,ci._BlockedImportFinder(['math']))
ci.block_ml_imports(['json'])
assert sum(isinstance(f,ci._BlockedImportFinder) for f in sys.meta_path)==1
import math
try:
 import json
except ImportError:
 pass
else:
 raise AssertionError('consolidated policy is ineffective')
''')
