"""A chain that captures an exit code must do something with it.

The re-gate printed COMPLETE and exited 0 after all 67 of its adapters failed,
because it captured `rc=$?` per adapter and only echoed it. Bash discards a
loop iteration's status and `set -e` does not reach inside a loop body, so the
capture was decorative. Two GPU hours were logged as OK.

This does NOT retrofit the older chains - several are historical records of a
run that already happened, and rewriting them would edit the record. It stops
the pattern SPREADING: a chain written from today on either aggregates its
failures or is listed below as a known-legacy exception, which makes the
exception a decision rather than an oversight.
"""
import os
import re
import pathlib
import subprocess
import tempfile
import unittest

from tests.test_gpu_lock_owner import run_owned_cpu_chain

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAINS = os.path.join(REPO, "run_chains")

# Chains that captured per-item exit codes without aggregating them, as of
# 2026-08-18. They are frozen history: each one describes a run that already
# happened, and its log is the evidence. Do not add to this list - use
# lib/stage.sh. Removing an entry (by fixing the chain) is always welcome.
LEGACY_UNGUARDED = {
    "daisymiller_preflight_proof.sh",
    "determinism_chain.sh", "intervention_chain.sh",
    "medoid_retrain_chain.sh", "moss_vs_lora.sh",
    "overnight_2026_08_09.sh", "ref_audit_chain.sh",
    "run_rebuild_retrain.sh", "sharp_intervention_chain.sh",
    "unseen_books_run_20260818.sh",
    "unseen_books_run_20260818b.sh",
}

CAPTURES_RC = re.compile(r"rc=\$\?|\brc\s*=\s*\$\?")
AGGREGATES = re.compile(r'stage_summary|failed_n|failures=|exit 1|return 1|'
                        r'^\s*exit\s+"?\$\{?rc\}?"?\s*$', re.MULTILINE)
# A separate input guard does not consume a failed worker's status. A helper
# ending with an error echo still returns success to its caller.
IGNORES_FAILURE = re.compile(r'else\n\s*echo "[^\n]*FAILED rc=\$\?[^\n]*\n\s*fi\n(?:\s*stage_commit_artifacts[^\n]*\n)?}', re.MULTILINE)


def is_unguarded_chain(source):
    # Ownership refusal happens before worker execution; it cannot aggregate
    # a later worker failure. Do not mistake that independent gate for one.
    source = re.sub(r'(?m)^\s*bash [^\n]*--check-lock-owner[^\n]*\|\| exit 1\s*$', '', source)
    return bool(CAPTURES_RC.search(source) and
                (not AGGREGATES.search(source) or IGNORES_FAILURE.search(source)))


def prepare_artifact_commit_fixture(root):
    """Provide real scoped commits for disposable composite-chain fixtures."""
    import shutil
    root = pathlib.Path(root)
    library = root / 'run_chains/lib'
    library.mkdir(parents=True, exist_ok=True)
    for name in ('stage.sh', 'server_cleanup.sh'):
        shutil.copyfile(pathlib.Path(CHAINS, 'lib', name), library / name)
    for args in (('init', '-q', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                 ('config', 'user.email', 'fixture@example.com'),
                 ('config', 'core.hooksPath', str(root / 'no-hooks')),
                 ('add', '--', 'ab_test_runtime/experiments'),
                 ('commit', '-q', '--allow-empty', '-m', 'fixture baseline')):
        subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


class ChainFailureGuardTest(unittest.TestCase):
    def _chains(self):
        for name in sorted(os.listdir(CHAINS)):
            if not name.endswith(".sh"):
                continue
            with open(os.path.join(CHAINS, name), encoding="utf-8") as fh:
                yield name, fh.read()

    def test_new_chains_that_capture_an_exit_code_also_act_on_it(self):
        offenders = [name for name, src in self._chains()
                     if is_unguarded_chain(src) and name not in LEGACY_UNGUARDED]
        self.assertEqual([], offenders,
                         "these chains record a per-item exit code and never "
                         "look at it, so they will report success while every "
                         "item fails - use run_chains/lib/stage.sh")

    def test_separate_input_guard_does_not_hide_an_ignored_worker_failure(self):
        source = 'if ! build_list; then exit 1; fi\n' + (
            'replay_one() {\n'
            '    if worker; then\n'
            '        echo ok\n'
            '    else\n'
            '        echo "    FAILED rc=$?"\n'
            '    fi\n'
            '}\n')
        self.assertTrue(is_unguarded_chain(source))
        self.assertFalse(is_unguarded_chain(source.replace(
            '        echo "    FAILED rc=$?"', '        exit "$rc"')))

    def test_ownership_gate_does_not_mask_unhandled_worker_exit(self):
        source = ('bash "$REPO/gpu_job.sh" --check-lock-owner "$OWNER" || exit 1\n'
                  'worker\nrc=$?\necho "DONE rc=$rc"\n')
        self.assertTrue(is_unguarded_chain(source))
        self.assertFalse(is_unguarded_chain(source + 'exit "$rc"\n'))

    def test_the_legacy_list_does_not_name_chains_that_no_longer_exist(self):
        """A stale exemption silently re-permits the pattern under that name."""
        present = {name for name, _ in self._chains()}
        self.assertEqual(set(), LEGACY_UNGUARDED - present,
                         "exempted chains that are gone; drop them from the list")

    def test_the_legacy_list_does_not_exempt_a_chain_that_is_already_fixed(self):
        # An exemption that is no longer needed hides the fact that the debt
        # was paid, and makes the list look larger than the problem.
        still_unguarded = {name for name, src in self._chains()
                           if is_unguarded_chain(src)}
        needless = LEGACY_UNGUARDED - still_unguarded
        self.assertEqual(set(), needless,
                         "these are guarded now - remove them from "
                         "LEGACY_UNGUARDED")


if __name__ == "__main__":
    unittest.main()


# Chains that skip work when an artifact already exists. The n1200 respelling
# block proved that skipping on EXISTENCE is not the same as skipping on
# COMPLETION: it was killed at 1129 of 1200 terms, left a file that looked
# finished, and every later run would have skipped it forever.
SKIP_ON_EXISTENCE = re.compile(r"\[\s*-e\s+\"?\$\{?out|\[\s*-f\s+\"?\$\{?out|"
                               r"\[\s*-e\s+\"\$out\"\s*\]")
CHECKS_COMPLETION = re.compile(r'status.*complete|completeness|candidates_considered|'
    r'(?m:^\s*(?:if \[ [^\n]+ \] && )?"\$[^"\n]+" "\$[Rr][Ee][Pp][Oo]/app/experiments/respelling_completion\.py")|'
    r'(?m:^\s*"\$[^"\n]+" "\$repo/app/experiments/asr_backends\.py"[^\n]* --check-artifact )')


class ChainIdempotencyTest(unittest.TestCase):
    """Re-running a chain must do nothing, and must know what "done" means.

    Every pipeline guide gives the same advice for proving idempotency: run it
    twice and assert the second run changes nothing. lib/stage.sh has that test
    directly; this one covers the part a twice-run cannot see - what the chain
    BELIEVES finished work looks like.

    A chain that skips on existence will skip a truncated artifact forever, and
    the truncation is biased rather than merely small wherever items are
    ordered. That is not hypothetical: respelling_e_row__ay_n1200.json is in
    this repository at 1129 of 1200 terms.
    """

    def _chains(self):
        for name in sorted(os.listdir(CHAINS)):
            if name.endswith(".sh"):
                with open(os.path.join(CHAINS, name), encoding="utf-8") as fh:
                    yield name, fh.read()

    def test_a_chain_that_skips_on_an_artifact_also_checks_it_finished(self):
        offenders = []
        for name, src in self._chains():
            if not SKIP_ON_EXISTENCE.search(src):
                continue
            if not CHECKS_COMPLETION.search(src):
                offenders.append(name)
        self.assertEqual(
            [], offenders,
            "these chains treat an artifact's existence as completion; a run "
            "killed mid-way leaves a file that looks finished and is skipped "
            "forever. Check status == complete, as morning_20260818.sh does.")

    def test_the_completion_check_is_not_satisfied_by_the_word_alone(self):
        """Guards this test: a chain merely mentioning 'complete' in prose
        must not pass. Keeps the pattern honest about what it matches."""
        self.assertIsNone(CHECKS_COMPLETION.search("# this run is complete-ish"))
        self.assertIsNotNone(CHECKS_COMPLETION.search("d.get('status')=='complete'"))
        self.assertIsNotNone(CHECKS_COMPLETION.search('    "$1" "$REPO/app/experiments/respelling_completion.py" "$2" "$3"'))
        self.assertIsNone(CHECKS_COMPLETION.search('# "$1" "$REPO/app/experiments/respelling_completion.py"'))
        self.assertIsNotNone(CHECKS_COMPLETION.search('    if [ -e "$out" ] && "$python" "$REPO/app/experiments/respelling_completion.py" "$out" "$limit"; then'))
        self.assertIsNone(CHECKS_COMPLETION.search('# if [ -e "$out" ] && "$python" "$REPO/app/experiments/respelling_completion.py"'))
        self.assertIsNotNone(CHECKS_COMPLETION.search('    "$1" "$repo/app/experiments/asr_backends.py" "${asr_args[@]}" --check-artifact "$2"'))
        self.assertIsNone(CHECKS_COMPLETION.search('# "$1" "$repo/app/experiments/asr_backends.py" --check-artifact "$2"'))
        self.assertIsNone(CHECKS_COMPLETION.search('    "$1" "$repo/app/experiments/asr_backends.py" --out "$2"'))


class FallbackChainConfigPreservationTests(unittest.TestCase):
    def test_exit_and_job_completion_do_not_restore_unowned_config_backup(self):
        source = pathlib.Path(CHAINS, 'fallback_5_3.sh').read_text()
        for ready in (False, True):
            with self.subTest(ready=ready), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                (root/'app').mkdir()
                logs = root/'ab_test_runtime/logs'
                logs.mkdir(parents=True)
                config = root/'app/config.json'
                config.write_bytes(b'current user configuration')
                backup = logs/'config.json.pre_fallback_backup'
                backup.write_bytes(b'stale unrelated configuration')
                bins = root/'bin'
                bins.mkdir()
                curl = bins/'curl'
                curl.write_text('#!/bin/sh\n' + ('echo qwen3\n' if ready else 'exit 1\n'))
                curl.chmod(0o755)
                gpu_job = root/'gpu_job.sh'
                gpu_job.write_text('#!/bin/sh\nprintf concurrent-config > "'+str(config)+'"\nprintf dispatched > "'+str(root/'dispatched')+'"\n')
                gpu_job.chmod(0o755)
                chain = root/'fallback.sh'
                chain.write_text(source.replace(
                    'REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                    'REPO="'+str(root)+'"'))
                env = os.environ.copy()
                env['PATH'] = str(bins)+os.pathsep+env['PATH']
                result = subprocess.run(['bash', str(chain)], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(0 if ready else 1, result.returncode, result.stderr)
                self.assertEqual(b'concurrent-config' if ready else b'current user configuration',
                                 config.read_bytes())
                self.assertEqual(b'stale unrelated configuration', backup.read_bytes())
                self.assertEqual(ready, (root/'dispatched').exists())



class ChainFooterExitTests(unittest.TestCase):
    def test_explanatory_footer_preserves_actual_shared_stage_summary_failure(self):
        for name in ('dialogue_map_5_3_20260826','anchor_and_separator_table_20260826'):
            source=pathlib.Path(CHAINS,name+'.sh').read_text()
            footer=source[source.rindex('run_stage indexes'):].split('\n',1)[1]
            for failures in (0,1,3):
                with self.subTest(chain=name,failures=failures):
                    code='set -uo pipefail\nsource "$1"\nSTAGE_FAILURES=$2\nSTAGE_TOTAL=3\n'+footer
                    result=subprocess.run(['bash','-c',code,'fixture',
                        str(pathlib.Path(CHAINS,'lib/stage.sh')),str(failures)],
                        capture_output=True,text=True,timeout=10)
                    self.assertEqual(1 if failures else 0,result.returncode,result.stdout+result.stderr)
                    self.assertIn('HOW TO READ IT',result.stdout)
                    self.assertIn(name,result.stdout)


class RegateDiagnosticExitTests(unittest.TestCase):
    def test_unchanged_scores_are_success_and_worker_failure_stays_nonzero(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        source = Path(__file__).resolve().parents[2] / 'run_chains/regate_with_provenance_20260817.sh'
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                from tests.regate_campaign_fixture import copy_campaign_cli, write_campaign_inputs, write_campaign_worker
                gate, previous = write_campaign_inputs(root, 'voice')
                experiments = gate.parent
                python = root/'app/env/bin/python'
                python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                copy_campaign_cli(root)
                write_campaign_worker(root, f'sys.exit(2) if {fail!r} else None\n')
                text=source.read_text(encoding='utf-8')
                live='REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git'
                self.assertEqual(1,text.count(live))
                text=text.replace(live,f'REPO="{root}"').replace('/tmp/regate_queue.tsv',str(root/'queue.tsv'))
                script=root/'regate.sh'
                script.write_text(text,encoding='utf-8')
                prepare_artifact_commit_fixture(root)
                unrelated = experiments / 'other-session.json'
                unrelated.write_text('base\n')
                subprocess.run(['git','-C',str(root),'add',str(unrelated)],check=True)
                subprocess.run(['git','-C',str(root),'commit','-q','-m','other baseline'],check=True)
                unrelated.write_text('staged WIP\n')
                subprocess.run(['git','-C',str(root),'add',str(unrelated)],check=True)
                untracked = experiments / 'untracked-session.json'
                untracked.write_text('untracked WIP\n')
                result=run_owned_cpu_chain(['bash',str(script)],root,capture_output=True,text=True)
                self.assertEqual(1 if fail else 0,result.returncode,result.stderr)
                measured=json.loads(gate.read_text(encoding='utf-8'))
                self.assertEqual('staged WIP\n',unrelated.read_text())
                self.assertEqual('untracked WIP\n',untracked.read_text())
                staged=subprocess.check_output(['git','-C',str(root),'diff','--cached','--name-only'],text=True).splitlines()
                self.assertEqual(['ab_test_runtime/experiments/other-session.json'],staged)
                if not fail:
                    committed=subprocess.check_output(['git','-C',str(root),'show','--pretty=','--name-only','HEAD'],text=True).splitlines()
                    self.assertEqual(['ab_test_runtime/experiments/regate_provenance_campaign.json'],committed)
                    history = subprocess.check_output(['git','-C',str(root),'log','--format=','--name-only','-3'],text=True)
                    self.assertIn('ab_test_runtime/experiments/gate_promote__voice.json', history)
                self.assertEqual(previous['passed'],measured['passed'])
                self.assertEqual(previous['median_ecapa'],measured['median_ecapa'])
                if fail:
                    self.assertIn('REGATE FAILED',result.stderr)
                    self.assertNotIn('REGATE COMPLETE',result.stdout)
                    self.assertEqual(previous,measured)
                else:
                    self.assertIn('REGATE COMPLETE',result.stdout)
                    self.assertIn('git diff ab_test_runtime/experiments | grep',result.stdout)
                    self.assertEqual({'fixture':True},measured['provenance'])


class AdapterQueueFailureStatusTests(unittest.TestCase):
    def test_recipe_failure_records_exit_code_and_stops_remaining_recipes(self):
        import json
        import os
        from pathlib import Path
        import shlex
        import subprocess
        import tempfile
        source = Path(__file__).resolve().parents[2]/'run_chains/qwen35_adapter_queue_20260825.sh'
        text = source.read_text(encoding='utf-8')
        recipe_and_dispatch = text[text.index('run_recipe() {'):]
        for code in (0,7,130,143):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root/'logs').mkdir()
                (root/'output_qwen35').mkdir()
                (root/'data').mkdir()
                (root/'data/train__pdnc_fixture.jsonl').write_text('{}\n')
                trainer = root/'trainer.py'
                trainer.write_text(
                    'import json,os,sys\nfrom pathlib import Path\n'
                    'with open("calls.jsonl","a") as f:f.write(json.dumps({"argv":sys.argv[1:],"offline":os.environ.get("HF_HUB_OFFLINE"),"allocator":os.environ.get("PYTORCH_CUDA_ALLOC_CONF")})+"\\n")\n'
                    f'print("CPU fixture worker rc={code}",flush=True)\n'
                    f'sys.exit({code}) if {code!r} else None\n'
                    'p=Path(sys.argv[sys.argv.index("--out")+1]);p.mkdir(parents=True,exist_ok=True)\n'
                    '(p/"adapter_model.safetensors").write_bytes(b"lifecycle fixture")\n',encoding='utf-8')
                script = root/'queue.sh'
                prefix = 'set -euo pipefail\n'
                values={'ROOT':str(root),'TRAINER':str(trainer),'MODEL':str(root/'model'),
                        'SUFFIX':'fixture','QUANT':'','COMMIT':'fixture-commit','STATUS':str(root/'status')}
                prefix += ''.join(f'{key}={shlex.quote(value)}\n' for key,value in values.items())
                script.write_text(prefix+recipe_and_dispatch,encoding='utf-8')
                result = subprocess.run(['bash',str(script)],capture_output=True,text=True)
                self.assertEqual(code,result.returncode,result.stderr)
                status = (root/'status').read_text(encoding='utf-8')
                calls = [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
                self.assertEqual(3 if code==0 else 1,len(calls))
                for call in calls:
                    self.assertEqual('1',call['offline'])
                    self.assertEqual('expandable_segments:True',call['allocator'])
                    self.assertIn('--grad_accum',call['argv'])
                if code:
                    self.assertIn('FAIL qwen35_fixture_author_heldout_balanced',status)
                    self.assertIn(f'rc={code}',status)
                    self.assertIn('commit=fixture-commit',status)
                    self.assertNotIn('COMPLETE',status)
                    self.assertIn(f'CPU fixture worker rc={code}',(root/'logs/qwen35_fixture_author_heldout_balanced.log').read_text())
                else:
                    self.assertIn('COMPLETE qwen35_fixture all recipes',status)
                    repeat = subprocess.run(['bash',str(script)],capture_output=True,text=True)
                    self.assertEqual(0,repeat.returncode,repeat.stderr)
                    self.assertEqual(3,len((root/'calls.jsonl').read_text().splitlines()))


class ExitStatusGuardPatternTests(unittest.TestCase):
    def test_exiting_with_saved_status_is_guarded_but_echo_and_return_are_not(self):
        for code in ('rc=$?\nexit "$rc"\n', 'rc=$?\n  exit ${rc}\n'):
            self.assertIsNotNone(AGGREGATES.search(code))
        # A return inside an unchecked loop callback can still lose the status.
        for code in ('rc=$?\necho "exit $rc"\n', 'rc=$?\necho "$rc"\n',
                     'rc=$?\nreturn "$rc"\n', 'rc=$?\nexit 0\n'):
            self.assertIsNone(AGGREGATES.search(code))


class QueueRepositoryInspectionTests(unittest.TestCase):
    def test_git_status_failure_refuses_instead_of_reporting_a_clean_tree(self):
        helper = pathlib.Path(REPO) / "run_chains/lib/queue.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            valid = root / "repo"
            valid.mkdir()
            subprocess.run(["git", "init", "-q", str(valid)], check=True)
            tracked = valid / "source.py"
            tracked.write_text("original = True\n")
            (valid / "RESULTS_INDEX.md").write_text("baseline generated index")
            subprocess.run(["git", "-C", str(valid), "add", "."], check=True)
            subprocess.run(["git", "-C", str(valid), "-c", "user.name=fixture", "-c",
                            "user.email=fixture@example.test", "commit", "-qm", "fixture"], check=True)
            def inspect(repo, env=None):
                return subprocess.run(["bash", "-c", 'set -euo pipefail; source "$1"; refuse_if_dirty "$2"',
                                       "fixture", str(helper), str(repo)], capture_output=True, text=True, env=env)
            self.assertEqual(0, inspect(valid).returncode)
            (valid / "ab_test_runtime").mkdir()
            (valid / "ab_test_runtime/untracked.txt").write_text("generated runtime")
            (valid / "RESULTS_INDEX.md").write_text("derived")
            self.assertEqual(0, inspect(valid).returncode)
            tracked.write_text("original = False\n")
            self.assertNotEqual(0, inspect(valid).returncode)
            tracked.write_text("original = True\n")
            for repo in (root / "missing", root):
                with self.subTest(repo=repo):
                    result = inspect(repo)
                    self.assertNotEqual(0, result.returncode, result.stdout)
                    self.assertIn("REFUSING", result.stdout + result.stderr)
            index = valid / ".git/index"
            original = index.read_bytes()
            index.write_bytes(b"corrupt index")
            result = inspect(valid)
            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertEqual(b"corrupt index", index.read_bytes())
            index.write_bytes(original)
            shim = root / "bin"
            shim.mkdir()
            fake_git = shim / "git"
            fake_git.write_text('#!/bin/sh\nprintf " M source.py\\n"\nexit 7\n')
            fake_git.chmod(0o755)
            env = dict(os.environ, PATH=str(shim) + os.pathsep + os.environ["PATH"])
            result = inspect(valid, env)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("could not inspect", result.stdout + result.stderr)
            self.assertEqual("original = True\n", tracked.read_text())


class ResumeArtifactShapeTests(unittest.TestCase):
    def test_actual_chain_refuses_non_objects_and_preserves_checkpoint_bytes(self):
        import json
        import sys
        source = pathlib.Path(REPO) / "run_chains/resume_partial_arm_20260826.sh"
        for data, expected, dispatch in (([], 1, False), (None, 1, False),
                                        ("complete", 1, False), (42, 1, False),
                                        (True, 1, False), ("malformed-json", 1, False),
                                        ({"status": "complete", "candidates_considered": 2, "results": [{"term": "Alice"}, {"term": "Bob"}]}, 0, False),
                                        ({"status": "partial", "results": []}, 0, True),
                                        (None, 0, True)):
            missing = data is None and expected == 0
            with self.subTest(data=data, missing=missing), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                chains = root / "run_chains"
                (chains / "lib").mkdir(parents=True)
                script = chains / source.name
                script.write_bytes(source.read_bytes())
                helper = root / "app/experiments/respelling_completion.py"
                helper.parent.mkdir(parents=True)
                helper.write_bytes((pathlib.Path(REPO) / "app/experiments/respelling_completion.py").read_bytes())
                (chains / "lib/queue.sh").write_text(
                    'resolve_python() { printf "%s" ' + repr(sys.executable) + '; }\n'
                    'refuse_if_dirty() { return 0; }\n')
                (chains / "lib/stage.sh").write_text(
                    'run_stage() { touch "$REPO/dispatched"; '
                    'if [ ! -f "$out" ]; then printf \'%s\' \'{"status":"complete","results":[]}\' > "$out"; fi; }\n'
                    'stage_commit_artifacts() { :; }\n'
                    'stage_summary() { :; }\n')
                artifact = root / "ab_test_runtime/experiments/respelling_space_allrows_n2.json"
                artifact.parent.mkdir(parents=True)
                if not missing:
                    artifact.write_text("{" if data == "malformed-json" else json.dumps(data))
                    before = artifact.read_bytes()
                result = subprocess.run(["bash", str(script), "space", "2"],
                                        capture_output=True, text=True)
                self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
                if not missing:
                    self.assertEqual(before, artifact.read_bytes())
                else:
                    self.assertEqual("complete", json.loads(artifact.read_text())["status"])
                self.assertEqual(dispatch, (root / "dispatched").exists(), result.stdout + result.stderr)
                if expected == 1:
                    self.assertIn("REFUSING: could not determine", result.stderr)


class StageDependencyReclamationTests(unittest.TestCase):
    def test_skipped_dependencies_do_not_stop_a_server_or_dispatch_work(self):
        helper = pathlib.Path(REPO) / "run_chains/lib/stage.sh"
        for predecessor, second in ((None, None), ("failed:7", None),
                                     ("ok", "failed:1")):
            with self.subTest(predecessor=predecessor, second=second), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                program = r'''
set -uo pipefail
source "$1"
STAGE_LOG_DIR="$2/logs"
# These fixtures never find or kill an actual process, or read actual VRAM.
pgrep() { test "$*" = "-x llama-server"; }
rocm-smi() { printf 'total used memory: 0\n'; }
fixture_root="$2"
pkill() { printf '%s\n' "$*" >> "$fixture_root/stopped"; }
if [ "$3" != missing ]; then STAGE_RESULT[first]="$3"; fi
args=(--needs-vram --requires-ok first)
if [ "$4" != none ]; then STAGE_RESULT[second]="$4"; args+=(--requires-ok second); fi
run_stage child 2s "${args[@]}" -- bash -c 'touch "$1"' fixture "$2/dispatched"
printf '%s %s %s\n' "${STAGE_RESULT[child]}" "$STAGE_TOTAL" "$STAGE_FAILURES" > "$2/state"
'''
                result = subprocess.run(["bash", "-c", program, "fixture", str(helper), str(root),
                    predecessor or "missing", second or "none"], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertFalse((root / "stopped").exists(), result.stdout)
                self.assertFalse((root / "dispatched").exists(), result.stdout)
                self.assertEqual("skipped 1 1\n", (root / "state").read_text())
                self.assertIn("SKIP  child", result.stdout)
                self.assertNotIn("VRAM reclaimed", result.stdout)
                self.assertFalse((root / "logs").exists())


class KnownIdentityGateContractTests(unittest.TestCase):
    def test_known_good_and_bad_verification_asserts_both_expected_exit_codes(self):
        import json
        import shlex
        source = (pathlib.Path(REPO) / "run_chains/verify_gate_test.sh").read_text()
        body = source[source.index("A=husky_baritone_20s_m_anime"):]
        for good, bad in ((0, 3), (0, 0), (7, 3), (0, 7), (3, 0)):
            with self.subTest(good=good, bad=bad), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                (root / "app").mkdir()
                calls = root / "calls.jsonl"
                wrapper = root / "gpu_job.sh"
                wrapper.write_text('#!/usr/bin/env python3\nimport os,sys,json\n'
                    'from pathlib import Path\n'
                    'with open(os.environ["FIXTURE_CALLS"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                    'sys.exit(int(os.environ["FIXTURE_GOOD" if sys.argv[1]=="gate_known_good" else "FIXTURE_BAD"]))\n')
                wrapper.chmod(0o755)
                original = wrapper.read_bytes()
                script = root / "verify.sh"
                script.write_text('set -uo pipefail\nREPO=' + shlex.quote(str(root)) + '\n' + body)
                before = script.read_bytes()
                env = dict(os.environ, FIXTURE_CALLS=str(calls), FIXTURE_GOOD=str(good), FIXTURE_BAD=str(bad))
                result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
                self.assertEqual(0 if (good, bad) == (0, 3) else 1,
                                 result.returncode, result.stdout + result.stderr)
                requests = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(["gate_known_good", "gate_known_bad"], [row[0] for row in requests])
                for row, relative in zip(requests, ("ab_test_runtime/retrain_honest/husky_baritone_20s_m_anime/adapter",
                                                   "lora_models/husky_baritone_20s_m_anime")):
                    self.assertEqual(str(root / relative), row[row.index("--adapter") + 1])
                    self.assertEqual(str(root / "ab_test_runtime/retrain_honest/husky_baritone_20s_m_anime/data"),
                                     row[row.index("--dataset") + 1])
                    self.assertEqual("5", row[row.index("--lines") + 1])
                self.assertEqual(before, script.read_bytes())
                self.assertEqual(original, wrapper.read_bytes())
                if result.returncode:
                    self.assertIn("expected", result.stdout + result.stderr)


class RegateQueueConstructionTests(unittest.TestCase):
    def test_failed_queue_construction_never_dispatches_partial_or_empty_queue(self):
        import json
        import sys
        from pathlib import Path
        source = Path(CHAINS, 'regate_with_provenance_20260817.sh').read_text()
        for failure in ('malformed_first', 'malformed_after_valid', 'missing_interpreter'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                experiments = root / 'ab_test_runtime/experiments'
                experiments.mkdir(parents=True)
                adapter = root / 'models/voice/adapter'
                adapter.mkdir(parents=True)
                data = adapter.parent / 'data/val'
                data.mkdir(parents=True)
                (data / 'metadata.jsonl').write_text('{}\n')
                if failure == 'malformed_after_valid':
                    (experiments / 'gate_promote__a_valid.json').write_text(
                        json.dumps({'adapter': str(adapter), 'passed': True}))
                (experiments / 'gate_promote__z_broken.json').write_bytes(b'{broken')
                original = {p.name: p.read_bytes() for p in experiments.iterdir()}
                python = root / 'app/env/bin/python'
                python.parent.mkdir(parents=True)
                if failure != 'missing_interpreter':
                    python.symlink_to(sys.executable)
                worker = root / 'app/experiments/verify_adapter_identity.py'
                worker.parent.mkdir(parents=True)
                worker.write_text('from pathlib import Path\n'
                    'Path("unexpected_dispatch").write_text("called")\n')
                queue = root / 'queue.tsv'
                queue.write_text('stale queue from earlier run\n')
                script = root / 'regate.sh'
                script.write_text(source.replace(
                    'REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                    'REPO="' + str(root) + '"').replace('/tmp/regate_queue.tsv', str(queue)))
                prepare_artifact_commit_fixture(root)
                result = run_owned_cpu_chain(['bash', str(script)], root,
                    env={**os.environ, 'TMPDIR': str(root)},
                    capture_output=True, text=True, timeout=10)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn('REGATE FAILED: could not build adapter queue', result.stderr)
                self.assertNotIn('REGATE START', result.stdout)
                self.assertNotIn('REGATE COMPLETE', result.stdout)
                self.assertFalse((root / 'unexpected_dispatch').exists())
                self.assertEqual(original, {p.name: p.read_bytes() for p in experiments.iterdir()})
                self.assertEqual('stale queue from earlier run\n', queue.read_text())
                self.assertEqual([], list(root.glob('regate_queue.*')))


class AdapterRecheckRosterTests(unittest.TestCase):
    def test_roster_must_exist_and_select_adapters_before_stage_can_pass(self):
        import json
        import shlex
        import sys
        from pathlib import Path
        source = Path(CHAINS, 'everything_20260818.sh').read_text()
        functions = source[source.index('recheck_one() {'):source.index('# COUNTED LIKE ANY OTHER STAGE.')]
        caller_start = source.index('stage_note "START recheck_failures"')
        caller = source[caller_start:source.index('stage_commit_artifacts recheck_failures', caller_start)]
        for kind in ('missing', 'empty', 'blank', 'no_adapter_rows', 'directory', 'measured', 'worker_failed'):
            with self.subTest(roster=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime = root / 'ab_test_runtime'
                experiments = runtime / 'experiments'
                experiments.mkdir(parents=True)
                logs = runtime / 'logs'
                logs.mkdir()
                roster = runtime / 'failed_adapters.tsv'
                if kind == 'empty':
                    roster.write_text('')
                elif kind == 'blank':
                    roster.write_text('\n\n')
                elif kind == 'no_adapter_rows':
                    roster.write_text('not_an_adapter_row\n')
                elif kind == 'directory':
                    roster.mkdir()
                elif kind in ('measured', 'worker_failed'):
                    name = 'failed' if kind == 'worker_failed' else 'below'
                    roster.write_text('passed\tadapter/pass\tdata/pass\t0.2\n' +
                                      name + '\tadapter/' + name + '\tdata/' + name + '\t0.1\n')
                before = roster.read_bytes() if roster.is_file() else None
                gate = root / 'app/experiments/verify_adapter_identity.py'
                gate.parent.mkdir(parents=True)
                import shutil
                shutil.copyfile(Path(REPO, 'app/experiments/recheck_result.py'), gate.parent / 'recheck_result.py')
                gate.write_text('import json,sys\nfrom pathlib import Path\n'
                    'args=sys.argv\nname=Path(args[args.index("--adapter")+1]).name\n'
                    'if name=="failed":sys.exit(2)\n'
                    'out=Path(args[args.index("--out")+1])\n'
                    'out.write_text(json.dumps({"adapter":str(Path(args[args.index("--adapter")+1]).relative_to(Path(__file__).parents[2])),"median_ecapa":0.2 if name=="below" else 0.8,"lines":1,"passed":name!="below"}))\n'
                    'sys.exit(3 if name=="below" else 0)\n')
                wrapper = root / 'gpu_job.sh'
                wrapper.write_text('#!/bin/bash\nprintf "%s\n" "$1" >> "$DISPATCH_LOG"\nshift\nexec "$@"\n')
                wrapper.chmod(0o755)
                script = root / 'recheck.sh'
                script.write_text('set -uo pipefail\nsource ' + shlex.quote(str(Path(CHAINS, 'lib/stage.sh'))) + '\n' +
                    'REPO=' + shlex.quote(str(root)) + '\nruntime=' + shlex.quote(str(runtime)) + '\n' +
                    'python=' + shlex.quote(sys.executable) + '\nSTAGE_LOG_DIR=' + shlex.quote(str(logs)) + '\n' +
                    'FAILED_LIST=' + shlex.quote(str(roster)) + '\n' + functions + caller + '\n' +
                    'printf "%s\\n" "${recheck_artifacts[@]}" > ' + shlex.quote(str(root / 'owned_paths')) + '\n' +
                    'stage_summary recheck_fixture\n')
                dispatch_log = root / 'dispatch.log'
                result = subprocess.run(['bash', str(script)],
                    env=dict(os.environ, DISPATCH_LOG=str(dispatch_log)),
                    capture_output=True, text=True, timeout=10)
                output = result.stdout + result.stderr
                valid = kind in ('measured', 'worker_failed')
                expected_paths = [str(experiments / ('gate_recheck__' + name + '.json'))
                    for name in (['passed', 'below'] if kind == 'measured' else ['passed'] if kind == 'worker_failed' else [])]
                self.assertEqual(expected_paths, [line for line in (root / 'owned_paths').read_text().splitlines() if line])
                self.assertEqual(0 if kind == 'measured' else 1, result.returncode, output)
                if valid:
                    names = ['regate2_passed', 'regate2_failed' if kind == 'worker_failed' else 'regate2_below']
                    self.assertEqual(names, dispatch_log.read_text().splitlines())
                    self.assertIn('recheck PASS', output)
                    if kind == 'measured':
                        self.assertIn('2 of 2 measured; 1 below threshold; 0 never ran', output)
                        self.assertIn('recheck BELOW', output)
                        self.assertEqual(0.2, json.loads((experiments / 'gate_recheck__below.json').read_text())['median_ecapa'])
                    else:
                        self.assertIn('recheck NOT MEASURED failed', output)
                        self.assertIn('1 of 2 measured; 0 below threshold; 1 never ran', output)
                        self.assertFalse((experiments / 'gate_recheck__failed.json').exists())
                    self.assertEqual(0.8, json.loads((experiments / 'gate_recheck__passed.json').read_text())['median_ecapa'])
                else:
                    self.assertIn('recheck NOT MEASURED', output)
                    self.assertFalse(dispatch_log.exists())
                    self.assertEqual([], list(experiments.iterdir()))
                    self.assertIn('recheck_failures = incomplete', output)
                if before is not None:
                    self.assertEqual(before, roster.read_bytes())


class CloudEvaluationProvenanceTests(unittest.TestCase):
    def test_gpu_probe_failure_refuses_evaluation_and_cleans_up_its_cpu_server(self):
        import json
        import platform
        import shutil
        import sys
        from pathlib import Path
        source = Path(CHAINS, 'cloud_balanced_eval_20260824.sh').read_text()
        for probe in ('success', 'error', 'missing'):
            with self.subTest(probe=probe), tempfile.TemporaryDirectory(prefix='cloud fixture ') as tmp:
                root = Path(tmp)
                bins = root / 'bin'
                bins.mkdir()
                for command in ('dirname', 'mkdir', 'seq', 'bash', 'flock', 'readlink', 'stat', 'awk', 'ps', 'sleep', 'tail', 'curl'):
                    executable = shutil.which(command)
                    self.assertIsNotNone(executable)
                    (bins / command).symlink_to(executable)
                (bins / 'python3').symlink_to(sys.executable)
                server = bins / 'llama-server'
                server.write_text('#!' + sys.executable + '\nimport os,signal,sys\nfrom pathlib import Path\n'
                    'Path(os.environ["SERVER_PID_PATH"]).write_text(str(os.getpid()))\n'
                    'signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))\n'
                    'import http.server\nport=int(sys.argv[sys.argv.index("--port")+1])\n'
                    'class Handler(http.server.BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200);self.end_headers()\n def log_message(self,*args):pass\n'
                    'http.server.HTTPServer(("127.0.0.1",port),Handler).serve_forever()\n')
                server.chmod(0o755)
                if probe != 'missing':
                    gpu = bins / 'nvidia-smi'
                    gpu.write_text('#!' + sys.executable + '\nimport sys\n' +
                        ('print("Fixture GPU, 24576 MiB")\n' if probe == 'success' else
                         'print("fixture driver unavailable",file=sys.stderr)\nsys.exit(9)\n'))
                    gpu.chmod(0o755)
                for name in ('model.gguf', 'adapter.gguf'):
                    (root / name).write_bytes(b'test input; never loaded')
                evaluation = root / 'app/experiments/lora_serving_eval_20260824.py'
                evaluation.parent.mkdir(parents=True)
                evaluation.write_text('import json,os,sys\nfrom pathlib import Path\n'
                    'Path(os.environ["EVALUATION_PATH"]).write_text(json.dumps({'
                    '"environment":os.environ.get("EXPERIMENT_ENV"),"argv":sys.argv[1:]}))\n')
                script = root / 'run_chains/cloud_balanced_eval_20260824.sh'
                script.parent.mkdir()
                text = re.sub(r'(?m)^REPO="[^"\n]*"$', 'REPO="' + str(root) + '"', source)
                text = re.sub(r'(?m)^MODEL="[^"\n]*"$', 'MODEL="' + str(root / 'model.gguf') + '"', text)
                text = re.sub(r'(?m)^ADAPTER="[^"\n]*"$', 'ADAPTER="' + str(root / 'adapter.gguf') + '"', text)
                import socket
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                text=text.replace('PORT=8090','PORT='+str(port))
                library=script.parent/'lib';library.mkdir()
                for name in ('managed_server.sh','llm_campaign.sh','server_cleanup.sh'):
                    shutil.copyfile(Path(CHAINS,'lib',name),library/name)
                for name in ('llama_server_process.py','subprocess_ownership.py'):
                    shutil.copyfile(Path(REPO,'app',name),root/'app'/name)
                script.write_text(text)
                pid_path = root / 'server.pid'
                evaluation_path = root / 'evaluated.json'
                from tests.test_gpu_lock_owner import run_owned_cpu_chain
                result = run_owned_cpu_chain(['/usr/bin/bash', str(script)], root, cwd=root,
                    env=dict(os.environ, PATH=str(bins), SERVER_PID_PATH=str(pid_path), EVALUATION_PATH=str(evaluation_path)),
                    capture_output=True, text=True, timeout=10)
                self.assertTrue(pid_path.exists(), result.stdout + result.stderr)
                pid = int(pid_path.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                self.assertEqual(b'test input; never loaded', (root / 'model.gguf').read_bytes())
                self.assertEqual(b'test input; never loaded', (root / 'adapter.gguf').read_bytes())
                if probe == 'success':
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    record = json.loads(evaluation_path.read_text())
                    self.assertEqual({'host': platform.node(), 'gpu': 'Fixture GPU, 24576 MiB', 'backend': 'CUDA'},
                                     json.loads(record['environment']))
                    args = record['argv']
                    self.assertEqual(['index18', 'mushoku16', 'owarimonogatari3'],
                                     args[args.index('--books') + 1:args.index('--base_url')])
                else:
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn('cannot capture GPU provenance', result.stderr)
                    self.assertFalse(evaluation_path.exists())


class RemainingGoalGateStatusTests(unittest.TestCase):
    def test_failed_rank_two_gate_count_controls_actual_chain_exit_and_completion_message(self):
        import json
        import sys
        from pathlib import Path
        source = Path(CHAINS, 'remaining_goal_work.sh').read_text()
        names = ['breathy_alto_50s_f_fantasy', 'husky_baritone_20s_m_supernatural',
            'silky_alto_40s_f_literary_1', 'silky_baritone_45s_m',
            'velvety_mezzo_30s_f_gothic', 'warm_alto_50s_f_gothic']
        baselines = ['husky_baritone_40s_m_2', 'husky_baritone_40s_m_scifi',
            'husky_tenor_30s_m_literary', 'silky_baritone_30s_m_fantasy',
            'warm_baritone_30s_m_2', 'warm_baritone_30s_m_scifi']
        for cached in (False, True):
            for failures in (0, 1, 3, 6):
                with self.subTest(cached=cached, failures=failures), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    runtime = root / 'ab_test_runtime'
                    experiments = runtime / 'experiments'
                    experiments.mkdir(parents=True)
                    completed_asr = experiments / 'asr_ja_largev3_hybrid.json'
                    completed_asr.write_text(json.dumps({'status': 'complete', 'fixture': 'CPU test'}))
                    for name in baselines:
                        (runtime / 'reference_rank1_all21' / name / 'data').mkdir(parents=True)
                    for name in names:
                        adapter = runtime / 'reference_rank2_failed' / name / 'adapter/adapter_model.safetensors'
                        adapter.parent.mkdir(parents=True)
                        adapter.write_bytes(b'fixture weights; never loaded')
                        if cached:
                            (experiments / ('gate_reference_rank2__' + name + '.json')).write_text(
                                json.dumps({'passed': name not in names[:failures], 'fixture': 'CPU test'}))
                    original = {p: p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
                    python = root / 'app/env/bin/python'
                    python.parent.mkdir(parents=True)
                    python.symlink_to(sys.executable)
                    worker = ('import json,os,sys\nfrom pathlib import Path\n'
                        'out=Path(sys.argv[sys.argv.index("--out")+1])\n'
                        'if "--check-artifact" in sys.argv:\n'
                        '    try: doc=json.loads(out.read_text())\n'
                        '    except Exception: sys.exit(1)\n'
                        '    if out.name.startswith("gate_reference_rank2__"):\n'
                        '        sys.exit(0 if doc.get("passed") is True else 3)\n'
                        '    sys.exit(0 if doc.get("status")=="complete" else 1)\n'
                        'failed=json.loads(os.environ["FAILED_GATES"])\n'
                        'is_gate=out.name.startswith("gate_reference_rank2__")\n'
                        'name=out.name[len("gate_reference_rank2__"):-5] if is_gate else ""\n'
                        'passed=name not in failed\n'
                        'out.parent.mkdir(parents=True,exist_ok=True)\n'
                        'out.write_text(json.dumps({"passed":passed,"status":"complete","fixture":"CPU test"}))\n'
                        'sys.exit(3 if is_gate and not passed else 0)\n')
                    for script_name in ('verify_adapter_identity.py', 'retrain_honest.py', 'asr_backends.py'):
                        script = root / 'app/experiments' / script_name
                        script.parent.mkdir(parents=True, exist_ok=True)
                        script.write_text(worker)
                    wrapper = root / 'gpu_job.sh'
                    wrapper.write_text('#!/bin/bash\nprintf "%s\n" "$1" >> "$DISPATCH_LOG"\nshift\nexec "$@"\n')
                    wrapper.chmod(0o755)
                    script = root / 'run_chains/remaining_goal_work.sh'
                    script.parent.mkdir()
                    script.write_text(source)
                    dispatch = root / 'dispatch.log'
                    result = subprocess.run(['bash', str(script)], cwd=root,
                        env=dict(os.environ, FAILED_GATES=json.dumps(names[:failures]), DISPATCH_LOG=str(dispatch)),
                        capture_output=True, text=True, timeout=10)
                    output = result.stdout + result.stderr
                    self.assertEqual(1 if failures else 0, result.returncode, output)
                    self.assertIn('rank-2 gate failures: ' + str(failures) + ' of 6', output)
                    if failures:
                        self.assertIn('REMAINING GOAL WORK FAILED', output)
                        self.assertNotIn('REMAINING GOAL WORK COMPLETE', output)
                    else:
                        self.assertIn('REMAINING GOAL WORK COMPLETE', output)
                    self.assertIn('Do not promote anything', output)
                    calls = dispatch.read_text().splitlines()
                    expected = ['baseline_heldout__' + name for name in baselines] + ['reference_rank2_failed']
                    if not cached:
                        expected += ['gate_reference_rank2__' + name for name in names]
                    self.assertEqual(expected, calls)
                    for name in names:
                        doc = json.loads((experiments / ('gate_reference_rank2__' + name + '.json')).read_text())
                        self.assertIs(name not in names[:failures], doc['passed'])
                    for path, content in original.items():
                        self.assertEqual(content, path.read_bytes())


class ReplayListFailureTests(unittest.TestCase):
    def test_replay_list_builder_must_succeed_before_remaining_dispatch(self):
        import json
        import shlex
        import sys
        from pathlib import Path
        production = Path(CHAINS, 'replay_dirty_evidence_20260817.sh').read_text()
        replay_source = Path(REPO, 'app/experiments/replay_artifact.py').read_text()
        for case in ('missing_module', 'missing_audit', 'bad_json', 'bad_shape',
                     'command_error_first', 'command_error_after_row', 'empty', 'valid'):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='replay checkout ') as tmp:
                root = Path(tmp)
                experiments = root / 'app/experiments'
                data = root / 'ab_test_runtime/experiments'
                audit = root / 'ab_test_runtime/audit/artifact_structural_audit.json'
                for folder in (experiments, data, audit.parent, root / 'app/env/bin'):
                    folder.mkdir(parents=True, exist_ok=True)
                (root / 'app/env/bin/python').symlink_to(sys.executable)
                (experiments / '__init__.py').write_text('')
                if case != 'missing_module':
                    (experiments / 'replay_artifact.py').write_text(replay_source)
                producer = experiments / 'cpu_replay.py'
                producer.write_text('from pathlib import Path\n'
                    'root=Path(__file__).parent.parent.parent\n'
                    'with (root/"dispatch.log").open("a") as f:f.write("ran\\n")\n')
                good = {'provenance': {'script': 'cpu_replay.py', 'args': {}}}
                bad = {'provenance': {'script': ['invalid-script-type'], 'args': {}}}
                (data / 'valid.json').write_text(json.dumps(good))
                (data / 'broken.json').write_text(json.dumps(bad))
                selected = []
                if case in ('valid', 'command_error_after_row'):
                    selected.append({'artifact': 'valid.json', 'dirty': True})
                if case.startswith('command_error'):
                    selected.append({'artifact': 'broken.json', 'dirty': True})
                if case != 'missing_audit':
                    audit.write_text('{' if case == 'bad_json' else
                                     json.dumps({} if case == 'bad_shape' else {'artifacts': selected}))
                replay_list = root / 'remaining.txt'
                replay_list.write_text('old list must never be reused\n')
                source = production.replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                                            'REPO=' + shlex.quote(str(root)))
                # A legacy list must survive; new lists use private TMPDIR names.
                source = source.replace('/tmp/replay_rest.txt', shlex.quote(str(replay_list)))
                chain = root / 'chain.sh'
                chain.write_text(source)
                env = dict(os.environ, ALEXANDRIA_GPU_LOCK_HELD='1')
                temporary = root / 'private-lists'
                temporary.mkdir()
                env['TMPDIR'] = str(temporary)
                env.pop('PYTHONPATH', None)
                before = {name: (data / name).read_bytes() for name in ('valid.json', 'broken.json')}
                prepare_artifact_commit_fixture(root)
                result = run_owned_cpu_chain(['bash', str(chain)], root, env=env, cwd=root,
                                        capture_output=True, text=True, timeout=10)
                output = result.stdout + result.stderr
                failed = case not in ('empty', 'valid')
                self.assertEqual(1 if failed else 0, result.returncode, output)
                if failed:
                    self.assertIn('REPLAY INCOMPLETE: could not build the remaining replay list', output)
                    self.assertNotIn('REPLAY COMPLETE', result.stdout)
                    self.assertFalse((root / 'dispatch.log').exists(), output)
                    self.assertIn('Traceback', result.stderr)
                elif case == 'valid':
                    self.assertIn('REPLAY COMPLETE', result.stdout)
                    self.assertEqual('ran\n', (root / 'dispatch.log').read_text())
                    self.assertIn('1 remaining', result.stdout)
                    log = root / 'ab_test_runtime/logs/replay_evidence/valid.log'
                    self.assertTrue(log.is_file())
                else:
                    self.assertIn('REPLAY COMPLETE', result.stdout)
                    self.assertIn('0 remaining', result.stdout)
                    self.assertFalse((root / 'dispatch.log').exists())
                self.assertEqual('old list must never be reused\n', replay_list.read_text())
                self.assertEqual([], list(temporary.iterdir()))
                for name, data_before in before.items():
                    self.assertEqual(data_before, (data / name).read_bytes())


class RegressionChainExitTests(unittest.TestCase):
    def test_generator_status_survives_diagnostics_and_config_restore(self):
        import json
        import shlex
        import sys
        for returncode in (0, 7):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                app = root / "app"
                logs = root / "ab_test_runtime/logs"
                binary = root / "bin"
                for path in (app / "env/bin", logs, binary):
                    path.mkdir(parents=True)
                config = app / "config.json"
                prior = b'{"llm":{"model_name":"prior"},"llm_local":{"model_name":"prior"}}'
                config.write_bytes(prior)
                curl = binary / "curl"
                curl.write_text('#!/bin/sh\nprintf qwen3\n')
                curl.chmod(0o755)
                python = app / "env/bin/python"
                python.write_text('#!/bin/bash\n'
                    'if [ "$1" = "-" ]; then exec ' + shlex.quote(sys.executable) + ' "$@"; fi\n'
                    '[ "$2" = generate_script.py ] || exit 2\n'
                    'printf "Split into 1 chunk\\nGot 1 entries\\n"\n'
                    'if [ "$GENERATOR_EXIT" = 0 ]; then printf "[]" > "$5"; fi\n'
                    'exit "$GENERATOR_EXIT"\n')
                python.chmod(0o755)
                source = pathlib.Path(CHAINS, "regression_after_pipeline_changes.sh").read_text()
                match = re.search(r'(?m)^REPO=(/[^\n ]+)$', source)
                adapted = source.replace(match.group(1), str(root)) if match else source
                chain = root / "regression.sh"
                chain.write_text(adapted)
                chain.chmod(0o755)
                env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                       "GENERATOR_EXIT": str(returncode)}
                result = run_owned_cpu_chain(["bash", str(chain)], root, env=env,
                                            capture_output=True, text=True, timeout=10)
                self.assertEqual(returncode, result.returncode, result.stdout + result.stderr)
                self.assertIn(f"rc={returncode}", result.stdout)
                self.assertIn("chunks completed", result.stdout)
                self.assertEqual(prior, config.read_bytes())
                self.assertEqual(prior, (logs / "config.json.regression_backup").read_bytes())
                output = root / "ab_test_runtime/regression/grimgar03_regression.json"
                if returncode == 0:
                    self.assertEqual([], json.loads(output.read_text()))
                else:
                    self.assertFalse(output.exists())


class IndependentChainSummaryTests(unittest.TestCase):
    def test_all_attempts_run_and_failures_reach_summary(self):
        import shlex
        import shutil
        import sys
        for filename, count in (("three_pass_validation.sh", 3),
                                ("overnight_tail_2026_08_17.sh", 7)):
            for mode in ("none", "first", "all"):
                with self.subTest(chain=filename, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    root = pathlib.Path(tmp)
                    app = root / "app"
                    logs = root / "ab_test_runtime/logs"
                    binary = root / "bin"
                    chains = root / "run_chains"
                    for path in (app / "env/bin", logs, binary, chains / "lib"):
                        path.mkdir(parents=True)
                    for name in ("stage.sh", "server_cleanup.sh"):
                        shutil.copyfile(pathlib.Path(CHAINS, "lib", name), chains / "lib" / name)
                    config = app / "config.json"
                    prior = b'{"llm":{"model_name":"prior"},"llm_local":{"model_name":"prior"}}'
                    config.write_bytes(prior)
                    curl = binary / "curl"
                    curl.write_text('#!/bin/sh\nprintf qwen3\n')
                    curl.chmod(0o755)
                    calls = root / "calls"
                    python = app / "env/bin/python"
                    python.write_text('#!/bin/bash\n'
                        'if [ "$1" = "-" ]; then exec ' + shlex.quote(sys.executable) + ' "$@"; fi\n'
                        'printf "%s\\n" "$*" >> "$CALLS_PATH"\n'
                        'printf "Split into 1 chunk\\n"\n'
                        'if [ "$FAIL_MODE" = all ] || { [ "$FAIL_MODE" = first ] && '
                        '[ "$(wc -l < "$CALLS_PATH")" -eq 1 ]; }; then exit 7; fi\n'
                        'while [ "$#" -gt 0 ]; do if [ "$1" = --output ] || [ "$1" = --out ]; then '
                        'shift; mkdir -p "$(dirname "$1")"; printf "[]" > "$1"; break; fi; shift; done\n'
                        'exit 0\n')
                    python.chmod(0o755)
                    source = pathlib.Path(CHAINS, filename).read_text()
                    chain = chains / filename
                    match = re.search(r'(?m)^REPO=(/[^\n ]+)$', source)
                    chain.write_text(source.replace(match.group(1), str(root)) if match else source)
                    env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                           "CALLS_PATH": str(calls), "FAIL_MODE": mode}
                    if count == 3:
                        result = run_owned_cpu_chain(["bash", str(chain)], root, env=env,
                                                    capture_output=True, text=True, timeout=10)
                    else:
                        wrapper = root / "gpu_job.sh"
                        wrapper.write_text('#!/bin/bash\nshift\nexec "$@"\n')
                        wrapper.chmod(0o755)
                        result = subprocess.run(["bash", str(chain)], env=env,
                                                capture_output=True, text=True, timeout=10)
                    output = result.stdout + result.stderr
                    failures = 0 if mode == "none" else 1 if mode == "first" else count
                    self.assertEqual(1 if failures else 0, result.returncode, output)
                    self.assertEqual(count, len(calls.read_text().splitlines()), output)
                    self.assertRegex(result.stdout, rf"SUMMARY .*: {count-failures}/{count} stages ok")
                    self.assertEqual(failures, result.stdout.count(" = failed:7"), output)
                    self.assertEqual(prior, config.read_bytes())
                    if count == 3:
                        self.assertEqual(prior, (logs / "config.json.tpv_backup").read_bytes())
                        self.assertEqual(failures, result.stdout.count("rc=7"))
                        for book in ("grimgar03", "mushoku16", "owarimonogatari3"):
                            self.assertIn("Split into 1 chunk", (logs / f"tpv_{book}.log").read_text())
                        self.assertIn("THREE-PASS VALIDATION DONE", result.stdout)
                    else:
                        self.assertEqual(failures, result.stdout.count("FAIL "))
                        self.assertIn("READ FIRST:", result.stdout)

    def test_overnight_attempt_returns_worker_status_and_keeps_existing_skip(self):
        import shlex
        source = pathlib.Path(CHAINS, "overnight_tail_2026_08_17.sh").read_text()
        function = source[source.index("attempt() {"):source.index("\n# 1.")]
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            marker = root / "worker-ran"
            artifact = root / "artifact"
            library = pathlib.Path(CHAINS, "lib/stage.sh")
            setup = 'set -uo pipefail\nsource ' + shlex.quote(str(library)) + '\nnote() { echo "$*"; }\n' + function
            for exists in (False, True):
                with self.subTest(existing_artifact=exists):
                    if marker.exists():
                        marker.unlink()
                    if exists:
                        artifact.write_text("prior bytes")
                    command = setup + '\nattempt probe ' + shlex.quote(str(artifact)) + ' bash -c ' + shlex.quote(
                        'printf ran > ' + shlex.quote(str(marker)) + '; exit 7') + '\nrc=$?\nprintf "rc=%s total=%s failures=%s\\n" "$rc" "$STAGE_TOTAL" "$STAGE_FAILURES"\nexit "$rc"\n'
                    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=5)
                    self.assertEqual(0 if exists else 7, result.returncode, result.stdout + result.stderr)
                    self.assertIn("rc=0 total=0 failures=0" if exists else "rc=7 total=1 failures=1", result.stdout)
                    if exists:
                        self.assertEqual("prior bytes", artifact.read_text())
                        self.assertIn("SKIP probe", result.stdout)
                        self.assertFalse(marker.exists())
                    else:
                        self.assertEqual("ran", marker.read_text())
                        marker.unlink()
