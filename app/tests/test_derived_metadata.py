"""Native disposable checkout exercises generation and metadata modes separately."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_support import get_bash_executable

ROOT=Path(__file__).resolve().parents[2]


def shell_path(path):
    """A path usable inside a POSIX shell command (Git Bash accepts C:/x)."""
    return str(path).replace(os.sep, "/")


def shell_quote(text):
    """Quote one shell word, including the spaces this workspace's paths have."""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def fixture_environment(overrides=None):
    """The environment a disposable fixture checkout's scripts must run under.

    The fixture supplies its OWN fake interpreter and that interpreter IS the
    subject: these tests exist to see the real scripts call it. A valid
    AA2_PYTHON inherited from the caller's shell is a real interpreter, and the
    scripts under test correctly prefer an explicit override - so the fixture
    would run real Python against scripts that only exist as fixture stubs and
    fail for a reason the test never meant to create.

    Dropping that one variable is the fixture's decision, not a production
    change: AA2_PYTHON still wins for every real caller, and the resolver tests
    that pin that behaviour keep setting it deliberately. Everything else -
    PATH, HOME, locale - is inherited unchanged, and os.environ itself is never
    modified, so the caller's environment comes back exactly as it was found.
    """
    environment = os.environ.copy()
    environment.pop("AA2_PYTHON", None)
    if overrides:
        environment.update(overrides)
    return environment


class DerivedMetadataTests(unittest.TestCase):
    def test_goal_audit_direct_script_records_provenance_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals = root / 'goals.md'
            audit = root / 'audit.json'
            output = root / 'output.json'
            goals.write_text('### 1.1 Fixture\n`fixture.json`\n', encoding='utf-8')
            audit.write_text(json.dumps({'artifacts': [
                {'artifact': 'fixture.json', 'classification': 'exploratory'}]}),
                encoding='utf-8')
            result = subprocess.run(
                [sys.executable, '-I', '-B',
                 str(ROOT / 'app/experiments/goal_evidence_audit.py'),
                 '--goals', str(goals), '--audit', str(audit), '--out', str(output)],
                cwd=tmp, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn(b"\r", output.read_bytes())
            provenance = json.loads(output.read_text(encoding='utf-8'))['provenance']
            self.assertNotIn('error', provenance)
            self.assertEqual('goal_evidence_audit.py', provenance['script'])
            self.assertTrue(provenance['git']['commit'])
            # NEW records name the digest scheme; historical artifacts keep
            # whatever they recorded.
            self.assertEqual('sha256-lf-v1',
                             provenance['git']['harness_sha256_scheme'])

    def run_goal_audit(self, root, goals, audit, output):
        return subprocess.run(
            [sys.executable, '-I', '-B',
             str(ROOT / 'app/experiments/goal_evidence_audit.py'),
             '--goals', str(goals), '--audit', str(audit), '--out', str(output)],
            cwd=root, capture_output=True, text=True, timeout=30)

    def goal_audit_fixture(self, root):
        goals = root / 'goals.md'
        audit = root / 'audit.json'
        output = root / 'output.json'
        goals.write_text('### 1.1 Fixture\n`fixture.json`\n', encoding='utf-8')
        audit.write_text(json.dumps({'artifacts': [
            {'artifact': 'fixture.json', 'classification': 'exploratory'}]}),
            encoding='utf-8')
        result = self.run_goal_audit(root, goals, audit, output)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return goals, audit, output

    def stamp(self, output, value='2000-01-01T00:00:00'):
        document = json.loads(output.read_text(encoding='utf-8'))
        document['provenance']['written'] = value
        output.write_text(json.dumps(document, indent=1, ensure_ascii=False),
                          encoding='utf-8')
        return output.read_text(encoding='utf-8')

    def verifiable_provenance(self):
        """A stored provenance block that really reproduces the real HEAD.

        Independent of the ambient working tree on purpose: a clean record at a
        commit verifies from that commit's blobs alone.
        """
        sys.path.insert(0, str(ROOT / 'app'))
        from experiments.provenance import get_harness_sha256_at_commit
        commit = subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
            text=True, timeout=10).strip()
        return {'script': 'goal_evidence_audit.py',
                'git': {'commit': commit, 'dirty': False,
                        'harness_sha256': get_harness_sha256_at_commit(
                            str(ROOT), commit)}}

    def check_goal_audit(self, root, goals, audit, output):
        return subprocess.run(
            [sys.executable, '-I', '-B',
             str(ROOT / 'app/experiments/goal_evidence_audit.py'),
             '--goals', str(goals), '--audit', str(audit),
             '--out', str(output), '--check'],
            cwd=root, capture_output=True, text=True, timeout=30)

    def write_document(self, output, document):
        output.write_text(json.dumps(document, indent=1, ensure_ascii=False),
                          encoding='utf-8')

    def test_goal_audit_check_validates_stored_provenance_before_fresh_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            document = json.loads(output.read_text(encoding='utf-8'))
            document['provenance'] = self.verifiable_provenance()
            self.write_document(output, document)
            current = self.check_goal_audit(root, goals, audit, output)
            self.assertEqual(0, current.returncode,
                             current.stdout + current.stderr)
            # The rows are current, but a clean record whose harness is not its
            # commit cannot identify the code that wrote the file.
            document['provenance']['git']['harness_sha256'] = '0' * 64
            self.write_document(output, document)
            broken = self.check_goal_audit(root, goals, audit, output)
            self.assertNotEqual(0, broken.returncode)
            self.assertIn('provenance-inconsistent', broken.stderr)
            self.assertNotIn('is stale', broken.stderr)
            # Rows alone are a different diagnostic.
            document['provenance'] = self.verifiable_provenance()
            document['rows'][0]['title'] = 'Changed'
            self.write_document(output, document)
            stale = self.check_goal_audit(root, goals, audit, output)
            self.assertNotEqual(0, stale.returncode)
            self.assertIn('goal evidence audit is stale', stale.stderr)
            self.assertNotIn('provenance-inconsistent', stale.stderr)

    def test_goal_audit_check_calls_unknown_source_state_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            document = json.loads(output.read_text(encoding='utf-8'))
            provenance = self.verifiable_provenance()
            provenance['git'].update({'dirty': None,
                                      'harness_sha256': '0' * 64})
            document['provenance'] = provenance
            self.write_document(output, document)
            checked = self.check_goal_audit(root, goals, audit, output)
        self.assertNotEqual(0, checked.returncode)
        self.assertIn('provenance-unverifiable', checked.stderr)
        self.assertNotIn('provenance-inconsistent', checked.stderr)

    def test_goal_audit_repeat_generation_keeps_bytes_and_write_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            kept = self.stamp(output)
            result = self.run_goal_audit(root, goals, audit, output)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(kept, output.read_text(encoding='utf-8'))
            self.assertEqual('2000-01-01T00:00:00',
                             json.loads(kept)['provenance']['written'])

    def test_goal_audit_repairs_corrupt_output_only_in_generation_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            expected = json.loads(output.read_text(encoding='utf-8'))
            expected.pop('provenance')
            for corrupt in (b'', b'{"provenance":', b'\xff'):
                with self.subTest(corrupt=corrupt):
                    output.write_bytes(corrupt)
                    command = [sys.executable, '-I', '-B',
                               str(ROOT / 'app/experiments/goal_evidence_audit.py'),
                               '--goals', str(goals), '--audit', str(audit),
                               '--out', str(output), '--check']
                    checked = subprocess.run(command, cwd=root, capture_output=True,
                                             text=True, timeout=30)
                    self.assertNotEqual(0, checked.returncode)
                    self.assertIn('goal evidence audit is unreadable', checked.stderr)
                    self.assertEqual(corrupt, output.read_bytes())
                    repaired = self.run_goal_audit(root, goals, audit, output)
                    self.assertEqual(0, repaired.returncode,
                                     repaired.stdout + repaired.stderr)
                    document = json.loads(output.read_text(encoding='utf-8'))
                    provenance = document.pop('provenance')
                    self.assertNotIn('error', provenance)
                    self.assertEqual(expected, document)
                    kept = output.read_bytes()
                    repeated = self.run_goal_audit(root, goals, audit, output)
                    self.assertEqual(0, repeated.returncode,
                                     repeated.stdout + repeated.stderr)
                    self.assertEqual(kept, output.read_bytes())
                    current = subprocess.run(command, cwd=root, capture_output=True,
                                             text=True, timeout=30)
                    self.assertEqual(0, current.returncode, current.stdout + current.stderr)

    def test_goal_audit_rewrites_when_goals_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            self.stamp(output)
            goals.write_text('### 1.1 Renamed\n`fixture.json`\n', encoding='utf-8')
            result = self.run_goal_audit(root, goals, audit, output)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            document = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual('Renamed', document['rows'][0]['title'])
            self.assertNotEqual('2000-01-01T00:00:00',
                                document['provenance']['written'])

    def test_goal_audit_rewrites_when_recorded_host_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            self.stamp(output)
            document = json.loads(output.read_text(encoding='utf-8'))
            document['provenance']['host'] = 'stale-host'
            output.write_text(json.dumps(document, indent=1, ensure_ascii=False),
                              encoding='utf-8')
            result = self.run_goal_audit(root, goals, audit, output)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            document = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(platform.node(), document['provenance']['host'])
            self.assertNotEqual('2000-01-01T00:00:00',
                                document['provenance']['written'])

    def test_goal_audit_rewrites_when_recorded_input_path_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            self.stamp(output)
            other = root / 'goals-2.md'
            other.write_text(goals.read_text(encoding='utf-8'), encoding='utf-8')
            result = self.run_goal_audit(root, other, audit, output)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            document = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual('goals-2.md', document['provenance']['args']['goals'])
            self.assertNotEqual('2000-01-01T00:00:00',
                                document['provenance']['written'])

    def test_goal_audit_repairs_recorded_provenance_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goals, audit, output = self.goal_audit_fixture(root)
            self.stamp(output)
            document = json.loads(output.read_text(encoding='utf-8'))
            document['provenance']['git'] = {'error': 'previous run failed'}
            output.write_text(json.dumps(document, indent=1, ensure_ascii=False),
                              encoding='utf-8')
            result = self.run_goal_audit(root, goals, audit, output)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            provenance = json.loads(output.read_text(encoding='utf-8'))['provenance']
            self.assertNotIn('error', provenance)
            self.assertNotIn('error', provenance['git'])
            self.assertEqual('goal_evidence_audit.py', provenance['script'])
            self.assertTrue(provenance['git']['commit'])
            self.assertNotEqual('2000-01-01T00:00:00', provenance['written'])

    def fake_interpreter(self, python, root):
        """A disposable interpreter: a shell launcher around a Python payload.

        WHY NOT A `#!<sys.executable>` SHEBANG. MEASURED on Windows: sys.executable
        contains spaces, so the kernel ends the shebang at the first one and the
        fixture tries to execute a program named `...\\aa2` - every fixture test
        then fails as "Permission denied" for a reason unrelated to what it
        asserts. The launcher quotes both paths, so POSIX and Git Bash run it.

        The payload stays ordinary Python so the scripts still take their real
        invocation path, including the heredoc that `$python - <file>` feeds to
        stdin: sys.argv[0] is the payload, argv[1] is the flag.
        """
        payload = python.with_name("fixture_interpreter.py")
        payload.write_text('import json,pathlib,sys\nroot=pathlib.Path('+repr(str(root))+')\n'
            'with (root/"calls").open("a") as handle:handle.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'if sys.argv[1]=="-":\n sys.argv=sys.argv[1:];exec(sys.stdin.read())\n'
            'elif sys.argv[1].endswith("collect_results.py") and "--check" not in sys.argv:\n (root/"RESULTS_INDEX.md").write_text("resolved\\n")\n')
        python.write_text('#!/bin/sh\nexec %s %s "$@"\n'
            % (shell_quote(shell_path(sys.executable)), shell_quote(shell_path(payload))))
        python.chmod(0o755)
        return python

    def fixture(self, root):
        subprocess.run(['git','init','-q','-b','main',str(root)],check=True)
        for key,value in (('user.name','fixture'),('user.email','fixture@example.invalid')):
            subprocess.run(['git','-C',str(root),'config',key,value],check=True)
        (root/'tools').mkdir();(root/'app/env/bin').mkdir(parents=True)
        for name in ('ready.sh','resolve_generated.sh','tools/regen_derived.sh','tools/install_git_hooks.sh'):
            shutil.copyfile(ROOT/name,root/name);(root/name).chmod(0o755)
        python=self.fake_interpreter(root/'app/env/bin/python',root)
        for name in ('results_index.csv','LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md','ab_test_runtime/audit/artifact_structural_audit.json',
                     'ab_test_runtime/audit/legacy_attribution_audit.json',
                     'ab_test_runtime/audit/goal_evidence_audit.json','app/tests/unit_test_inventory.json'):
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('base\n')
        (root/'GOALS.md').write_text('header\n')
        (root/'RESULTS_INDEX.md').write_text('base\n')
        return python

    def run_script(self, root, name, *args):
        return subprocess.run([get_bash_executable(),str(root/name),*args],cwd=root,capture_output=True,text=True,timeout=10,
                              env=fixture_environment())

    def test_fixture_environment_ignores_ambient_override_and_leaves_caller_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            canary=root/'ambient-python'
            canary.write_text('#!/bin/sh\nprintf ran >> "$0.ran"\nexit 64\n');canary.chmod(0o755)
            for label,value in (('valid',shell_path(canary)),
                                ('missing',shell_path(root/'no such python')),
                                ('unset',None)):
                with self.subTest(ambient=label),mock.patch.dict(os.environ,{}):
                    if value is None:
                        os.environ.pop('AA2_PYTHON',None)
                    else:
                        os.environ['AA2_PYTHON']=value
                    before=dict(os.environ)
                    calls=root/'calls'
                    if calls.exists():calls.unlink()
                    marker=Path(str(canary)+'.ran')
                    if marker.exists():marker.unlink()
                    result=self.run_script(root,'tools/regen_derived.sh','--quiet')
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertEqual(value,os.environ.get('AA2_PYTHON'))
                    self.assertFalse(marker.exists(),
                                     'the inherited AA2_PYTHON ran as the fixture interpreter')
                    calls=[json.loads(line) for line in calls.read_text().splitlines()]
                    self.assertEqual(5,len(calls),calls)
                    self.assertEqual(before,dict(os.environ),
                                     'the fixture run changed the caller environment')

    def test_metadata_queries_do_not_execute_generators_and_match_existing_interfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);python=self.fixture(root)
            result=self.run_script(root,'tools/regen_derived.sh','--metadata')
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertFalse((root/'calls').exists(),'query executed generators')
            lines=result.stdout.splitlines();self.assertEqual(shell_path(python),lines[0])
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
            result=self.run_script(root,'ready.sh','--shard','2/3')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            calls=[json.loads(line) for line in (root/'calls').read_text().splitlines()]
            self.assertEqual([
                'audit_experiment_artifacts.py', 'audit_legacy_attribution.py',
                'collect_results.py', 'goal_evidence_audit.py',
                'update_test_inventory.py', 'verify_release.py'],
                [Path(args[0]).name for args in calls])
            self.assertFalse(any('--check' in args for args in calls),
                             'ready must delegate checks instead of repeating them')
            self.assertEqual(['verify_release.py','--skip-evidence-recheck','--shard','2/3'],
                             calls[-1])

    def test_ready_targets_its_own_checkout_when_invoked_from_another_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            caller=Path(tmp)/'caller';target=Path(tmp)/'target'
            for root in (caller,target):
                root.mkdir()
                self.fixture(root)
                if root == caller:
                    (root/'RESULTS_INDEX.md').write_text('resolved\n')
                subprocess.run(['git','-C',str(root),'add','-A'],check=True)
                subprocess.run(['git','-C',str(root),'commit','-qm','base'],check=True)
            result=subprocess.run([get_bash_executable(),str(target/'ready.sh')],cwd=caller,
                                  capture_output=True,text=True,timeout=10,env=fixture_environment())
            self.assertEqual(2,result.returncode,result.stdout+result.stderr)
            self.assertFalse((caller/'calls').exists(),'generator ran in caller checkout')
            calls=[json.loads(line) for line in (target/'calls').read_text().splitlines()]
            self.assertEqual(['audit_experiment_artifacts.py','audit_legacy_attribution.py',
                              'collect_results.py','goal_evidence_audit.py',
                              'update_test_inventory.py'],
                             [Path(args[0]).name for args in calls])
            self.assertEqual('resolved\n',(target/'RESULTS_INDEX.md').read_text())
            self.assertIn('NOT READY: regeneration changed',result.stdout)

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
