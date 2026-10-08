"""Native commits isolate explicitly selected files inside the shared directory."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

LIB=Path(os.environ.get('STAGE_LIB_SOURCE',str(Path(__file__).resolve().parents[2]/'run_chains/lib/stage.sh')))


class ArtifactScopeTests(unittest.TestCase):
    def test_subdirectory_repo_argument_commits_only_selected_root_relative_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = self.prepare(root)
            subdirectory = root / "app"
            subdirectory.mkdir()
            artifact = folder / "current[1]*?.json"
            result = self.run_commit(subdirectory, artifact)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            relative = str(artifact.relative_to(root))
            self.assertEqual(relative, self.git(root, "show", "--pretty=", "--name-only", "HEAD"))
            self.assertEqual("measured current", self.git(root, "show", "HEAD:" + relative))
            self.assertEqual("ab_test_runtime/experiments/staged-wip.json",
                             self.git(root, "diff", "--cached", "--name-only"))
            artifact.unlink()
            result = self.run_commit(subdirectory, artifact)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn(relative, self.git(root, "ls-tree", "-r", "--name-only", "HEAD"))

    def git(self,root,*args):
        return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()

    def prepare(self,root):
        folder=root/'ab_test_runtime/experiments';folder.mkdir(parents=True)
        self.git(root,'init','-q','-b','main');self.git(root,'config','user.name','Fixture');self.git(root,'config','user.email','fixture@example.com')
        for name in ('current[1]*?.json','staged-wip.json','unstaged-wip.json'):(folder/name).write_text('base\n')
        self.git(root,'add','-A');self.git(root,'commit','-q','-m','base')
        (folder/'current[1]*?.json').write_text('measured current\n')
        (folder/'staged-wip.json').write_text('staged WIP\n');self.git(root,'add','ab_test_runtime/experiments/staged-wip.json')
        (folder/'unstaged-wip.json').write_text('unstaged WIP\n');(folder/'untracked-wip.json').write_text('untracked WIP\n')
        return folder

    def run_commit(self,root,*paths):
        script='set -uo pipefail; source '+shlex.quote(str(LIB))+'; stage_commit_artifacts current '+shlex.quote(str(root))+' '+ ' '.join(shlex.quote(str(path)) for path in paths)+'; stage_summary fixture'
        return subprocess.run(['bash','-c',script],capture_output=True,text=True,timeout=10)

    def test_current_literal_file_commits_without_other_sessions_same_folder_wip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=self.prepare(root)
            result=self.run_commit(root,folder/'current[1]*?.json')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertEqual('ab_test_runtime/experiments/current[1]*?.json',self.git(root,'show','--pretty=','--name-only','HEAD'))
            self.assertEqual('ab_test_runtime/experiments/staged-wip.json',self.git(root,'diff','--cached','--name-only'))
            self.assertEqual('base',self.git(root,'show','HEAD:ab_test_runtime/experiments/unstaged-wip.json'))
            self.assertEqual('unstaged WIP\n',(folder/'unstaged-wip.json').read_text())
            self.assertEqual('untracked WIP\n',(folder/'untracked-wip.json').read_text())
            self.assertIn('untracked-wip.json',self.git(root,'ls-files','--others'))

    def test_directory_and_outside_repo_scopes_refuse_without_staging_anything(self):
        for invalid in ('ab_test_runtime/experiments','../outside.json'):
            with self.subTest(path=invalid),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.prepare(root);head=self.git(root,'rev-parse','HEAD');index=self.git(root,'diff','--cached')
                result=self.run_commit(root,invalid)
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertIn('artifacts:current = failed:2',result.stdout)
                self.assertEqual(head,self.git(root,'rev-parse','HEAD'))
                self.assertEqual(index,self.git(root,'diff','--cached'))

    def test_missing_new_file_is_noop_and_tracked_deletion_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=self.prepare(root);head=self.git(root,'rev-parse','HEAD')
            result=self.run_commit(root,folder/'not-produced.json')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertEqual(head,self.git(root,'rev-parse','HEAD'))
            (folder/'current[1]*?.json').unlink()
            result=self.run_commit(root,folder/'current[1]*?.json')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertEqual('ab_test_runtime/experiments/current[1]*?.json',self.git(root,'show','--pretty=','--name-only','HEAD'))
            self.assertEqual('ab_test_runtime/experiments/staged-wip.json',self.git(root,'diff','--cached','--name-only'))

    def test_owned_checkpoint_and_stale_sidecars_commit_with_result_and_track_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=self.prepare(root)
            current=folder/'current[1]*?.json'
            checkpoint=folder/'current[1]*?.json.ckpt'
            stale=folder/'current[1]*?.json.ckpt.stale'
            checkpoint.write_text('partial rows\n');stale.write_text('old fingerprint rows\n')
            result=self.run_commit(root,current,checkpoint,stale)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertEqual({str(p.relative_to(root)) for p in (current,checkpoint,stale)},set(self.git(root,'show','--pretty=','--name-only','HEAD').splitlines()))
            self.assertEqual('partial rows',self.git(root,'show','HEAD:'+str(checkpoint.relative_to(root))))
            self.assertEqual('ab_test_runtime/experiments/staged-wip.json',self.git(root,'diff','--cached','--name-only'))
            checkpoint.unlink();current.write_text('finished rows\n')
            result=self.run_commit(root,current,checkpoint,stale)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertEqual({str(p.relative_to(root)) for p in (current,checkpoint)},set(self.git(root,'show','--pretty=','--name-only','HEAD').splitlines()))
            self.assertNotIn(str(checkpoint.relative_to(root)),self.git(root,'ls-tree','-r','--name-only','HEAD').splitlines())
            self.assertEqual('old fingerprint rows',self.git(root,'show','HEAD:'+str(stale.relative_to(root))))
            self.assertEqual('ab_test_runtime/experiments/staged-wip.json',self.git(root,'diff','--cached','--name-only'))

    def test_missing_scope_refuses_without_changing_head_or_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.prepare(root)
            head=self.git(root,'rev-parse','HEAD');index=self.git(root,'diff','--cached')
            result=self.run_commit(root)
            self.assertEqual(1,result.returncode,result.stdout+result.stderr)
            self.assertIn('explicit experiment files are required',result.stdout)
            self.assertIn('artifacts:current = failed:2',result.stdout)
            self.assertEqual(head,self.git(root,'rev-parse','HEAD'))
            self.assertEqual(index,self.git(root,'diff','--cached'))

    def test_actual_replay_loop_commits_only_dispatched_result_and_its_checkpoint(self):
        import json
        import shutil
        import sys
        from tests.test_gpu_lock_owner import run_owned_cpu_chain
        from tests.test_chain_failure_guards import prepare_artifact_commit_fixture
        with tempfile.TemporaryDirectory(prefix='replay scope ') as tmp:
            root=Path(tmp);folder=self.prepare(root)
            app=root/'app/experiments';app.mkdir(parents=True)
            interpreter=root/'app/env/bin/python';interpreter.parent.mkdir(parents=True);interpreter.symlink_to(sys.executable)
            shutil.copyfile(LIB.parents[2]/'app/experiments/replay_artifact.py',app/'replay_artifact.py')
            current=folder/'current[1]*?.json'
            current.write_text(json.dumps({'provenance':{'script':'cpu_scope.py','args':{'out':str(current)}}}))
            worker=app/'cpu_scope.py'
            worker.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index("--out")+1])\np.write_text("remeasured rows\\n")\npathlib.Path(str(p)+".ckpt").write_text("partial evidence\\n")\n')
            audit=root/'ab_test_runtime/audit/artifact_structural_audit.json';audit.parent.mkdir(parents=True)
            audit.write_text(json.dumps({'artifacts':[{'artifact':current.name,'dirty':True}]}))
            # Helper installs the shared shell library and commits a baseline;
            # restore this fixture's unrelated index/WIP afterward.
            prepare_artifact_commit_fixture(root)
            (folder/'staged-wip.json').write_text('new staged WIP\n');self.git(root,'add','ab_test_runtime/experiments/staged-wip.json')
            (folder/'unstaged-wip.json').write_text('new unstaged WIP\n')
            (folder/'fresh-untracked.json').write_text('fresh untracked WIP\n')
            source=(LIB.parents[1]/'replay_dirty_evidence_20260817.sh').read_text()
            source=source.replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git','REPO='+shlex.quote(str(root)))
            source=source.replace('/tmp/replay_rest.txt',shlex.quote(str(root/'remaining.txt')))
            chain=root/'replay.sh';chain.write_text(source)
            result=run_owned_cpu_chain(['bash',str(chain)],root,cwd=root,capture_output=True,text=True,timeout=10)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertEqual('remeasured rows\n',current.read_text())
            self.assertEqual('partial evidence\n',Path(str(current)+'.ckpt').read_text())
            self.assertEqual({str(current.relative_to(root)),str(current.relative_to(root))+'.ckpt'},set(self.git(root,'show','--pretty=','--name-only','HEAD').splitlines()))
            self.assertEqual('ab_test_runtime/experiments/staged-wip.json',self.git(root,'diff','--cached','--name-only'))
            self.assertEqual('new unstaged WIP\n',(folder/'unstaged-wip.json').read_text())
            self.assertEqual('fresh untracked WIP\n',(folder/'fresh-untracked.json').read_text())

    def test_every_chain_commit_call_declares_an_explicit_file_scope(self):
        callers=[]
        for script in LIB.parents[1].glob('*.sh'):
            for line in script.read_text().replace('\\\n',' ').splitlines():
                if line.strip().startswith('stage_commit_artifacts '):
                    args=shlex.split(line)
                    self.assertGreaterEqual(len(args),4,(script.name,line))
                    callers.append((script.name,args[1]))
        self.assertTrue(callers)
