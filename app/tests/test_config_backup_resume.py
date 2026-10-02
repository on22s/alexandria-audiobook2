"""Whole CPU campaign fixtures preserve recovery configuration across runs."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from tests.unseen_fixture_support import COMPLETION_FIXTURE_CODE

ROOT=Path(__file__).resolve().parents[2]
CHAINS=('overnight_2026_08_09c.sh','unseen_books.sh','unseen_books_20260819b.sh')


class ConfigBackupResumeTests(unittest.TestCase):
    def fixture(self, root, name):
        chains=root/'run_chains';(chains/'lib').mkdir(parents=True)
        for helper in (ROOT/'run_chains/lib').glob('*.sh'): shutil.copy2(helper,chains/'lib'/helper.name)
        source=(ROOT/'run_chains'/name).read_text()
        literal=re.search(r'(?m)^REPO=(?:"([^$"\n]*)"|([^$\n]+))$',source)
        if literal:
            old_root=literal.group(1) or literal.group(2)
            source=source.replace(old_root,str(root))
            self.assertNotIn(old_root, source, 'all legacy root references must be isolated')
        (chains/name).write_text(source)
        (chains/name).chmod(0o755)
        logs=root/'ab_test_runtime/logs';logs.mkdir(parents=True)
        (root/'app/env/bin').mkdir(parents=True)
        for module_name in ('generation_completion.py','source_encoding.py'):
            (root/'app'/module_name).write_bytes((ROOT/'app'/module_name).read_bytes())
        inputs=root/'ab_test_runtime/results/collect_all_20260722-155801/inputs';inputs.mkdir(parents=True)
        for book in ('mushoku18','grimgar06','mushoku23','arc4_volume10wn'):
            (inputs/(book+'.txt')).write_text('CPU complete source '+book)
        config=root/'app/config.json'
        original=json.dumps({'llm':{'model_name':'human original'},'llm_local':{'model_name':'human local'},'custom':'keep'},indent=2).encode()
        config.write_bytes(original)
        backup=logs/('config.json.overnight_backup' if name.startswith('overnight') else 'config.json.unseen_backup')
        bin_dir=root/'bin';bin_dir.mkdir()
        def executable(path, source): path.write_text(source);path.chmod(0o755)
        (root/'cpu_generation_fixture.py').write_text(
            COMPLETION_FIXTURE_CODE + 'import json,os,pathlib,sys\nsys.path.insert(0,str(pathlib.Path(__file__).parent/"app"))\n'
            'with open(os.environ["CALLS"],"a") as f:f.write("cpu work\\n")\n'
            'if "--output" in sys.argv:\n'
            ' p=pathlib.Path(sys.argv[sys.argv.index("--output")+1]);p.parent.mkdir(parents=True,exist_ok=True)\n'
            + (' save_complete_fixture(p,sys.argv[sys.argv.index("--output")-1],[{"speaker":"ALICE","text":"CPU completed fixture"}]*51)\n' if name=='unseen_books_20260819b.sh' else ' p.write_text(json.dumps([{"speaker":"ALICE","text":"CPU completed fixture"}]*51))\n') +
            'print("CPU stage success")\n')
        executable(root/'app/env/bin/python', '#!/bin/bash\nif [ "${1:-}" = - ]; then exec '+sys.executable+' "$@"; fi\nexec '+sys.executable+' "'+str(root/'cpu_generation_fixture.py')+'" "$@"\n')
        executable(root/'gpu_job.sh','#!/bin/bash\ncase "${1:-}" in --check-lock-owner|--check-vram|--check-llm) exit 0;; esac\nif [ "${1:-}" = overnight_2026_08_09c ]; then export ALEXANDRIA_GPU_LOCK_HELD=1 ALEXANDRIA_GPU_LOCK_PID=$$ ALEXANDRIA_GPU_LOCK_FD=9; fi\nshift\nexec "$@"\n')
        executable(bin_dir/'llama-server','#!/bin/bash\nexit 0\n')
        executable(root/'ensure_llama_server.sh', '#!/bin/bash\nexit 0\n')
        (root/'app/llama_server_process.py').write_text('raise SystemExit(0)\n')
        executable(bin_dir/'rocm-smi', '#!/bin/sh\nexit 1\n')
        executable(bin_dir/'sleep','#!/bin/bash\nexit 0\n')
        executable(bin_dir/'curl','#!/bin/bash\nprintf "qwen3-14b\\n"\n')
        executable(bin_dir/'cp','#!/bin/bash\n'
            'src="${@: -2:1}"; dst="${@: -1}"\n'
            'if { [ "$COPY_FAILURE" = restore ] && [ "$src" = "$BACKUP_PATH" ]; } || '
            '{ [ "$COPY_FAILURE" = capture ] && [ "$src" = "$CONFIG_PATH" ]; }; then\n'
            ' printf partial > "$dst"\n'
            ' echo "fixture copy refused" >&2\n'
            ' exit 1\nfi\nexec /bin/cp "$@"\n')
        env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],
             'CALLS':str(root/'calls.txt'),'CONFIG_PATH':str(config),'BACKUP_PATH':str(backup),
             'COPY_FAILURE':'','ALEXANDRIA_GPU_LOCK_HELD':'0'}
        return chains/name, config, backup, original, env

    def run_chain(self, script, env):
        return subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=15)

    def test_stale_backup_restored_before_capture_normal_exit_and_new_user_edit_survive_next_run(self):
        for name in CHAINS:
            for stale in (False,True):
                with self.subTest(name=name,stale=stale),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);script,config,backup,original,env=self.fixture(root,name)
                    if stale:
                        backup.write_bytes(original)
                        config.write_text('{"llm":{"model_name":"temporary campaign override"}}')
                    result=self.run_chain(script,env)
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertTrue((root/'calls.txt').exists(),'campaign did not reach CPU work')
                    self.assertEqual(original,config.read_bytes())
                    self.assertFalse(backup.exists(),'successful restoration must retire recovery copy')
                    edited=b'{"llm":{"model_name":"new human model"},"llm_local":{"model_name":"new human local"},"custom":"new human setting"}'
                    config.write_bytes(edited)
                    result=self.run_chain(script,env)
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertEqual(edited,config.read_bytes());self.assertFalse(backup.exists())

    def test_failed_stale_restoration_stops_before_capture_and_preserves_only_recovery_copy(self):
        for name in CHAINS:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);script,config,backup,original,env=self.fixture(root,name)
                backup.write_bytes(original);config.write_text('{"llm":{"model_name":"stale override"}}')
                result=self.run_chain(script,{**env,'COPY_FAILURE':'restore'})
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertIn('fixture copy refused',result.stderr)
                self.assertEqual(original,backup.read_bytes())
                self.assertFalse((root/'calls.txt').exists(),'work started despite restoration failure')
                # Even a partial destination copy can be recovered from intact backup.
                result=self.run_chain(script,env)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertEqual(original,config.read_bytes());self.assertFalse(backup.exists())

    def test_failed_new_backup_capture_stops_without_mutating_config_or_leaving_partial_recovery_file(self):
        for name in CHAINS:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);script,config,backup,original,env=self.fixture(root,name)
                result=self.run_chain(script,{**env,'COPY_FAILURE':'capture'})
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertIn('fixture copy refused',result.stderr)
                self.assertEqual(original,config.read_bytes());self.assertFalse(backup.exists())
                self.assertFalse(list(backup.parent.glob(backup.name+'.*')),'owned failed staging left behind')
                self.assertFalse((root/'calls.txt').exists())


    def test_failed_exit_restoration_reports_failure_and_keeps_backup_for_next_run(self):
        for name in CHAINS:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);script,config,backup,original,env=self.fixture(root,name)
                result=self.run_chain(script,{**env,'COPY_FAILURE':'restore'})
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertTrue((root/'calls.txt').exists(),'exit failure must happen after CPU work')
                self.assertEqual(original,backup.read_bytes())
                self.assertIn('recovery retained',result.stderr)
                result=self.run_chain(script,env)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertEqual(original,config.read_bytes());self.assertFalse(backup.exists())
