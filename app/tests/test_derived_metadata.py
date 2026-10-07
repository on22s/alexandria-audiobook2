"""Native disposable checkout exercises generation and metadata modes separately."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class DerivedMetadataTests(unittest.TestCase):
    def fixture(self, root):
        subprocess.run(['git','init','-q','-b','main',str(root)],check=True)
        for key,value in (('user.name','fixture'),('user.email','fixture@example.invalid')):
            subprocess.run(['git','-C',str(root),'config',key,value],check=True)
        (root/'tools').mkdir();(root/'app/env/bin').mkdir(parents=True)
        for name in ('ready.sh','resolve_generated.sh','tools/regen_derived.sh','tools/install_git_hooks.sh'):
            shutil.copyfile(ROOT/name,root/name);(root/name).chmod(0o755)
        python=root/'app/env/bin/python'
        python.write_text('#!'+sys.executable+'\nimport json,pathlib,sys\nroot=pathlib.Path('+repr(str(root))+')\n'
            'with (root/"calls").open("a") as handle:handle.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'if sys.argv[1]=="-":\n sys.argv=sys.argv[1:];exec(sys.stdin.read())\n'
            'elif sys.argv[1].endswith("collect_results.py") and "--check" not in sys.argv:\n (root/"RESULTS_INDEX.md").write_text("resolved\\n")\n')
        python.chmod(0o755)
        for name in ('results_index.csv','LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md','ab_test_runtime/audit/artifact_structural_audit.json',
                     'ab_test_runtime/audit/legacy_attribution_audit.json',
                     'ab_test_runtime/audit/goal_evidence_audit.json','app/tests/unit_test_inventory.json'):
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('base\n')
        (root/'GOALS.md').write_text('header\n')
        (root/'RESULTS_INDEX.md').write_text('base\n')
        return python

    def run_script(self, root, name, *args):
        return subprocess.run(['bash',str(root/name),*args],cwd=root,capture_output=True,text=True,timeout=10)

    def test_metadata_queries_do_not_execute_generators_and_match_existing_interfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);python=self.fixture(root)
            result=self.run_script(root,'tools/regen_derived.sh','--metadata')
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertFalse((root/'calls').exists(),'query executed generators')
            lines=result.stdout.splitlines();self.assertEqual(str(python),lines[0])
            self.assertEqual(self.run_script(root,'tools/regen_derived.sh','--python').stdout.strip(),lines[0])
            self.assertEqual(self.run_script(root,'tools/regen_derived.sh','--paths').stdout.splitlines(),lines[1:])
            self.assertFalse((root/'calls').exists())

    def test_ready_regenerates_once_then_checks_and_runs_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            # Keep regenerated content identical so the production staging gate passes.
            (root/'RESULTS_INDEX.md').write_text('resolved\n')
            subprocess.run(['git','-C',tmp,'add','-A'],check=True)
            subprocess.run(['git','-C',tmp,'commit','-qm','base'],check=True)
            result=self.run_script(root,'ready.sh')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            calls=[json.loads(line) for line in (root/'calls').read_text().splitlines()]
            self.assertEqual(10,len(calls))
            self.assertEqual(5,sum('--check' not in args and not args[0].endswith('verify_release.py') for args in calls))
            self.assertTrue(calls[-1][0].endswith('verify_release.py'))

    def test_pointer_only_resolver_queries_metadata_once_and_regenerates_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            helper=root/'tools/regen_derived.sh';helper.rename(root/'tools/regen_real.sh')
            helper.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> '+repr(str(root/'queries'))+'\nexec bash '+repr(str(root/'tools/regen_real.sh'))+' "$@"\n');helper.chmod(0o755)
            def git(*args):return subprocess.run(['git','-C',tmp,*args],capture_output=True,text=True)
            def goals(number):
                (root/'GOALS.md').write_text(''.join(f'header{i}\n' for i in range(29))+f'met goals begin at line {number}\n\n# Part II\nreal prose\n')
            goals(10);self.assertEqual(0,git('add','-A').returncode);self.assertEqual(0,git('commit','-qm','base').returncode)
            self.assertEqual(0,git('checkout','-qb','side').returncode);goals(11);git('add','GOALS.md');git('commit','-qm','side')
            git('checkout','-q','main');goals(12);git('add','GOALS.md');git('commit','-qm','main')
            self.assertNotEqual(0,git('merge','side').returncode)
            result=self.run_script(root,'resolve_generated.sh')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertEqual(['--metadata',''],(root/'queries').read_text().splitlines())
            calls=[json.loads(line) for line in (root/'calls').read_text().splitlines()]
            self.assertEqual(6,len(calls));self.assertEqual('-',calls[0][0])
            self.assertEqual('',git('diff','--name-only','--diff-filter=U').stdout)
            self.assertNotIn('<<<<<<<',(root/'GOALS.md').read_text())
            self.assertIn('met goals begin at line 32',(root/'GOALS.md').read_text())
            self.assertIn('real prose',(root/'GOALS.md').read_text())
