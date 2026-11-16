import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments.manifest import HARNESS_DIGEST_SCHEME, _source_fingerprint
from experiments.provenance import (
    check_harness_provenance, get_harness_sha256_at_commit,
    get_reproducible_harness_source)


def _has_git_ancestor(path):
    probe = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return True
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent


def _temp_root():
    """A directory to make synthetic repositories in, never a stranger's repo.

    The machine-wide TEMP on this workspace contains an unrelated `.git`, so a
    control that asks git a question before its own `git init` could answer
    about somebody else's checkout. Prefer a verified git-free ancestor, then
    an owned directory under the workspace (excluded from the source policy),
    which the fixture immediately makes its own repository root.
    """
    workspace = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    for base in (os.environ.get("TMPDIR"), tempfile.gettempdir(),
                 os.path.join(tempfile.gettempdir(), "opencode")):
        if base and os.path.isdir(base) and not _has_git_ancestor(base):
            return base
    return os.path.join(workspace, "ab_test_runtime")


class ReproducibleHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.repo = Path(self.temp.name)
        self.harness = self.repo / "app" / "experiments"
        self.harness.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo,
                       check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo,
                       check=True)

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, text):
        (self.harness / "probe.py").write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "app/experiments/probe.py"],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", text], cwd=self.repo,
                       check=True)
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

    def block(self, commit, fingerprint, dirty):
        return {"git": {"commit": commit, "harness_sha256": fingerprint,
                        "dirty": dirty,
                        "harness_sha256_scheme": HARNESS_DIGEST_SCHEME}}

    def prepare_goal_audit(self):
        """Run the real CLI in a synthetic repository, including its source probe."""
        root = Path(__file__).resolve().parents[2]
        for name in ("__init__.py", "manifest.py", "provenance.py", "goal_evidence_audit.py"):
            shutil.copyfile(root / "app/experiments" / name, self.harness / name)
        shutil.copyfile(root / "gpu_job.sh", self.repo / "gpu_job.sh")
        (self.repo / "GOALS.md").write_text(
            "### 1.1 Synthetic goal\n`fixture.json`\n", encoding="utf-8")
        (self.repo / "inputs.json").write_text(json.dumps({"artifacts": [
            {"artifact": "fixture.json", "classification": "exploratory"}]}), encoding="utf-8")
        (self.repo / "app/core.py").write_bytes(b"VALUE = 1\n")
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "synthetic audit inputs"],
                       cwd=self.repo, check=True)
        self.run_goal_audit()
        document = json.loads(self.goal_audit_path().read_text(encoding="utf-8"))
        self.assertIs(document["provenance"]["git"]["dirty"], False)
        return document

    def goal_audit_path(self, repo=None):
        return (repo or self.repo) / "ab_test_runtime/audit/goal_evidence_audit.json"

    def run_goal_audit(self, repo=None, check=False, expected=0):
        repo = repo or self.repo
        command = [sys.executable, "-I", "-B", str(repo / "app/experiments/goal_evidence_audit.py"),
                   "--goals", str(repo / "GOALS.md"), "--audit", str(repo / "inputs.json"),
                   "--out", str(self.goal_audit_path(repo))]
        if check:
            command.append("--check")
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=30)
        if expected == 0:
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        else:
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def test_goal_check_rejects_current_harness_changed_after_clean_generation(self):
        document = self.prepare_goal_audit()
        self.run_goal_audit(check=True)
        probe = self.harness / "provenance.py"
        probe.write_bytes(probe.read_bytes() + b"\n# Synthetic change with identical audit rows.\n")
        rejected = self.run_goal_audit(check=True, expected=1)
        self.assertIn("current harness", rejected.stderr)
        # Regeneration changes only the provenance, proving the old BODY check
        # could not distinguish these two harnesses.
        self.run_goal_audit()
        regenerated = json.loads(self.goal_audit_path().read_text(encoding="utf-8"))
        self.assertNotEqual(document.pop("provenance"), regenerated.pop("provenance"))
        self.assertEqual(document, regenerated)
        self.run_goal_audit(check=True)

    def test_goal_check_rejects_clean_flag_contradicting_recorded_diagnostics(self):
        self.prepare_goal_audit()
        (self.repo / "app/core.py").write_bytes(b"VALUE = 2\n")
        self.run_goal_audit()
        document = json.loads(self.goal_audit_path().read_text(encoding="utf-8"))
        git = document["provenance"]["git"]
        self.assertIs(git["dirty"], True)
        self.assertTrue(git["source_state"].startswith("dirty:"))
        self.assertEqual([" M app/core.py"], git["modified_tracked_files"])
        self.run_goal_audit(check=True)
        git["dirty"] = False  # Change exactly the claim, retaining the real diagnostics.
        self.goal_audit_path().write_text(json.dumps(document), encoding="utf-8")
        rejected = self.run_goal_audit(check=True, expected=1)
        self.assertIn("provenance-inconsistent", rejected.stderr)

    def test_goal_check_accepts_clean_record_and_non_harness_line_ending_changes(self):
        self.prepare_goal_audit()
        self.run_goal_audit(check=True)
        # Git reports this as modified with autocrlf=false. The stored clean
        # diagnostics describe generation, not the checker's working tree.
        (self.repo / "app/core.py").write_bytes(b"VALUE = 1\r\n")
        self.assertIn("app/core.py", subprocess.check_output(
            ["git", "status", "--short"], cwd=self.repo, text=True))
        self.run_goal_audit(check=True)

    def test_goal_check_accepts_squash_in_main_only_clone(self):
        self.prepare_goal_audit()
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=self.repo, check=True)
        self.commit("# synthetic branch harness\n")
        self.run_goal_audit()
        document = json.loads(self.goal_audit_path().read_text(encoding="utf-8"))
        recorded = document["provenance"]["git"]["commit"]
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "branch audit"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "merge", "--squash", "feature"], cwd=self.repo,
                       capture_output=True, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "squashed audit"], cwd=self.repo, check=True)
        clone = self.repo / "main-only-clone"
        subprocess.run(["git", "clone", "-q", "--no-local", "--single-branch", "--branch", "main",
                        str(self.repo), str(clone)], check=True)
        self.assertNotEqual(0, subprocess.run(["git", "cat-file", "-e", recorded + "^{commit}"],
                                             cwd=clone, capture_output=True).returncode)
        self.run_goal_audit(clone, check=True)

    def commit_proof(self, commit):
        def read(object_type, revision):
            return base64.b64encode(subprocess.check_output(
                ["git", "cat-file", object_type, revision], cwd=self.repo)).decode("ascii")
        return {"scheme": "git-commit-tree-v1", "commit": read("commit", commit),
                "trees": [read("tree", commit + "^{tree}"),
                          read("tree", commit + ":app")]}

    def squash_clone(self):
        subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=self.repo, check=True)
        base = self.commit("base\n")
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=self.repo, check=True)
        (self.harness / "probe.py").write_bytes(b"captured\r\n")
        subprocess.run(["git", "add", "app/experiments/probe.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "captured"], cwd=self.repo, check=True)
        captured = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.assertEqual(b"", subprocess.check_output(["git", "status", "--short"], cwd=self.repo))
        fingerprint = _source_fingerprint(str(self.harness))
        self.assertEqual(fingerprint, get_harness_sha256_at_commit(str(self.repo), captured))
        block = self.block(captured, fingerprint, False)
        block["git"]["harness_commit_proof"] = self.commit_proof(captured)
        (self.repo / "audit.json").write_text(json.dumps(block), encoding="utf-8")
        subprocess.run(["git", "add", "audit.json"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "capture audit"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "merge", "--squash", "feature"], cwd=self.repo,
                       capture_output=True, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "squashed feature"], cwd=self.repo, check=True)
        clone = self.repo / "main-only-clone"
        subprocess.run(["git", "clone", "-q", "--no-local", "--single-branch", "--branch", "main",
                        "--config", "core.autocrlf=false", str(self.repo), str(clone)], check=True)
        missing = subprocess.run(["git", "cat-file", "-e", captured + "^{commit}"],
                                 cwd=clone, capture_output=True)
        self.assertNotEqual(0, missing.returncode, "clone accidentally retained the PR-only commit")
        return clone, block, base

    def test_clean_crlf_audit_verifies_after_squash_without_pr_commit_objects(self):
        clone, block, unused_base = self.squash_clone()
        expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone, text=True).strip()
        self.assertEqual("verified", check_harness_provenance(block, str(clone))[0])
        self.assertEqual(expected, get_reproducible_harness_source(block, str(clone)))
        self.assertNotEqual(0, subprocess.run(
            ["git", "cat-file", "-e", block["git"]["commit"] + "^{commit}"],
            cwd=clone, capture_output=True).returncode)
        self.assertEqual(b"", subprocess.check_output(["git", "status", "--short"], cwd=clone))

    def test_squash_proof_cannot_rescue_false_commit_hash_content_or_source_state(self):
        clone, block, base = self.squash_clone()
        mutations = [
            ("false commit", lambda git: git.update(commit="0" * 40)),
            ("known inconsistent commit", lambda git: git.update(
                commit=base, harness_commit_proof=self.commit_proof(base))),
            ("wrong content", lambda git: git.update(harness_sha256="0" * 64)),
            ("unknown source", lambda git: git.update(dirty=None)),
            ("dirty source", lambda git: git.update(dirty=True)),
            ("missing proof", lambda git: git.pop("harness_commit_proof")),
            ("legacy scheme", lambda git: git.pop("harness_sha256_scheme")),
            ("wrong proof scheme", lambda git: git["harness_commit_proof"].update(scheme="future")),
            ("invalid base64", lambda git: git["harness_commit_proof"].update(commit="!")),
            ("missing tree", lambda git: git["harness_commit_proof"]["trees"].pop()),
            ("changed tree", lambda git: git["harness_commit_proof"]["trees"].__setitem__(
                0, base64.b64encode(b"different tree").decode("ascii"))),
        ]
        for name, mutate in mutations:
            with self.subTest(case=name):
                changed = copy.deepcopy(block)
                mutate(changed["git"])
                self.assertNotEqual("verified", check_harness_provenance(changed, str(clone))[0])

    def test_provenance_captures_commit_tree_proof_only_for_clean_source(self):
        from experiments.provenance import provenance
        commit = self.commit("capture\n")
        fingerprint = get_harness_sha256_at_commit(str(self.repo), commit)
        for dirty in (False, True, None):
            with self.subTest(dirty=dirty), \
                    patch("experiments.provenance.REPO", str(self.repo)), \
                    patch("experiments.manifest._git_state", return_value=self.block(
                        commit, fingerprint, dirty)["git"]):
                captured = provenance("fixture.py")
                self.assertEqual(dirty is False, "harness_commit_proof" in captured["git"])
                if dirty is False:
                    self.assertEqual(self.commit_proof(commit), captured["git"]["harness_commit_proof"])

    def test_squash_proof_recovers_historical_source_but_not_unrelated_objects(self):
        clone, block, base = self.squash_clone()
        squash = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone, text=True).strip()
        (clone / "app/experiments/probe.py").write_text("later\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=clone, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "-q", "-m", "later harness"], cwd=clone, check=True)
        self.assertEqual(squash, get_reproducible_harness_source(block, str(clone)))
        subprocess.run(["git", "checkout", "-q", "--detach", base], cwd=clone, check=True)
        self.assertIsNone(get_reproducible_harness_source(block, str(clone)))

    def test_verified_commit_proofs_do_not_churn_an_identical_audit(self):
        from experiments.goal_evidence_audit import _same_except_write_time
        first = self.commit("unchanged\n")
        fingerprint = get_harness_sha256_at_commit(str(self.repo), first)
        old = self.block(first, fingerprint, False)
        old["git"]["harness_commit_proof"] = self.commit_proof(first)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "new ref only"],
                       cwd=self.repo, check=True)
        second = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        new = self.block(second, fingerprint, False)
        new["git"]["harness_commit_proof"] = self.commit_proof(second)
        verify = check_harness_provenance
        with patch("experiments.provenance.check_harness_provenance",
                   side_effect=lambda value: verify(value, str(self.repo))):
            self.assertTrue(_same_except_write_time(
                {"rows": [], "provenance": old}, {"rows": [], "provenance": new}))
            new["git"]["harness_commit_proof"]["commit"] = "!"
            self.assertFalse(_same_except_write_time(
                {"rows": [], "provenance": old}, {"rows": [], "provenance": new}))

    def test_bare_import_retains_script_metadata_without_the_app_package(self):
        script = """import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import provenance
block = provenance.provenance('public_probe.py', SimpleNamespace(seed=7))
assert block['script'] == 'public_probe.py', block
assert block['seed'] == 7, block
assert block['written'] and block['host'], block
assert 'error' in block['git'], block
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script,
             str(Path(__file__).resolve().parents[1] / "experiments")],
            cwd=self.repo, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_commit_digest_batches_binary_and_spaced_names_in_two_git_calls(self):
        self.commit("initial\n")
        for name, content in (("a space.py", b"binary\0body\r\n"),
                              ("z.py", b"last\rline\n")):
            (self.harness / name).write_bytes(content)
        nested = self.harness / "nested"
        nested.mkdir()
        (nested / "ignored.py").write_bytes(b"not part of the direct harness")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "binary fixtures"],
                       cwd=self.repo, check=True)
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        expected = _source_fingerprint(str(self.harness))
        with patch("experiments.provenance.subprocess.run", wraps=subprocess.run) as run:
            actual = get_harness_sha256_at_commit(str(self.repo), commit)
        self.assertEqual(expected, actual)
        self.assertEqual(["ls-tree", "cat-file"],
                         [call.args[0][1] for call in run.call_args_list])

    def test_commit_digest_refuses_invalid_batch_framing(self):
        commit = self.commit("public synthetic\n")
        real_run = subprocess.run
        mutations = (
            lambda output: b"missing missing\n",
            lambda output: output.replace(b" blob ", b" tree ", 1),
            lambda output: b"0" * 40 + output[40:],
            lambda output: output[:-1],
            lambda output: output[:-3],
            lambda output: output + b"unexpected",
            lambda output: output.split(b" ", 2)[0] + b" blob " + b"9" * 100 + b"\n",
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                def run(command, **kwargs):
                    result = real_run(command, **kwargs)
                    if command[1] == "cat-file":
                        result.stdout = mutate(result.stdout)
                    return result
                with patch("experiments.provenance.subprocess.run", side_effect=run):
                    with self.assertRaises(ValueError):
                        get_harness_sha256_at_commit(str(self.repo), commit)

    def test_commit_digest_preserves_batch_process_failure(self):
        commit = self.commit("public synthetic\n")
        real_run = subprocess.run
        def run(command, **kwargs):
            if command[1] == "cat-file":
                raise subprocess.CalledProcessError(7, command, stderr=b"synthetic refusal")
            return real_run(command, **kwargs)
        with patch("experiments.provenance.subprocess.run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                get_harness_sha256_at_commit(str(self.repo), commit)

    def test_clean_run_must_match_its_recorded_commit(self):
        commit = self.commit("one\n")
        fingerprint = get_harness_sha256_at_commit(str(self.repo), commit)
        self.assertEqual(commit, get_reproducible_harness_source(
            self.block(commit, fingerprint, False), str(self.repo)))
        self.commit("two\n")
        current = _source_fingerprint(str(self.harness))
        self.assertIsNone(get_reproducible_harness_source(
            self.block(commit, current, False), str(self.repo)))

    def test_dirty_run_can_match_exact_descendant_commit(self):
        recorded = self.commit("one\n")
        captured = self.commit("two\n")
        fingerprint = get_harness_sha256_at_commit(str(self.repo), captured)
        self.assertEqual(captured, get_reproducible_harness_source(
            self.block(recorded, fingerprint, True), str(self.repo)))

    def test_dirty_uncommitted_harness_can_match_worktree(self):
        recorded = self.commit("one\n")
        (self.harness / "probe.py").write_text("dirty\n", encoding="utf-8")
        fingerprint = _source_fingerprint(str(self.harness))
        self.assertEqual("WORKTREE", get_reproducible_harness_source(
            self.block(recorded, fingerprint, True), str(self.repo)))

    def test_unrelated_branch_does_not_rescue_recorded_run(self):
        recorded = self.commit("one\n")
        subprocess.run(["git", "checkout", "-q", "-b", "sibling"],
                       cwd=self.repo, check=True)
        sibling = self.commit("sibling\n")
        fingerprint = get_harness_sha256_at_commit(str(self.repo), sibling)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo,
                       check=True)
        self.assertIsNone(get_reproducible_harness_source(
            self.block(recorded, fingerprint, True), str(self.repo)))

    def test_malformed_identity_is_rejected(self):
        commit = self.commit("one\n")
        self.assertIsNone(get_reproducible_harness_source(
            self.block(commit, "not-a-sha", True), str(self.repo)))

    def test_autocrlf_checkout_fingerprint_matches_its_commit(self):
        subprocess.run(["git", "config", "core.autocrlf", "true"],
                       cwd=self.repo, check=True)
        (self.harness / "probe.py").write_bytes(b"VALUE = 1\r\n")
        subprocess.run(["git", "add", "app/experiments/probe.py"],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "crlf"],
                       cwd=self.repo, check=True)
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.assertIn(b"\r\n", (self.harness / "probe.py").read_bytes())
        self.assertEqual(
            get_harness_sha256_at_commit(str(self.repo), commit),
            _source_fingerprint(str(self.harness)))

    def test_sound_same_commit_verifies_without_new_diagnostic_fields(self):
        commit = self.commit("one\n")
        fingerprint = get_harness_sha256_at_commit(str(self.repo), commit)
        block = self.block(commit, fingerprint, False)
        block["git"].pop("harness_sha256_scheme")
        status, detail = check_harness_provenance(
            block, str(self.repo))
        self.assertEqual("verified", status, detail)

    def test_current_check_preserves_legacy_raw_identity_and_historical_lookup(self):
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=self.repo, check=True)
        self.commit("initial\n")
        (self.harness / "probe.py").write_bytes(b"legacy\r\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "legacy CRLF"], cwd=self.repo, check=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                         cwd=self.repo, text=True).strip()
        target = get_harness_sha256_at_commit(str(self.repo), commit, scheme=None)
        block = self.block(commit, target, False)
        block["git"].pop("harness_sha256_scheme")
        self.assertEqual("verified", check_harness_provenance(
            block, str(self.repo), require_current=True)[0])
        (self.harness / "probe.py").write_bytes(b"legacy\n")
        self.assertEqual("verified", check_harness_provenance(block, str(self.repo))[0])
        self.assertEqual("inconsistent", check_harness_provenance(
            block, str(self.repo), require_current=True)[0])

    def test_clean_diagnostics_must_agree_without_requiring_missing_legacy_fields(self):
        commit = self.commit("one\n")
        block = self.block(commit, get_harness_sha256_at_commit(str(self.repo), commit), False)
        for fields in ({}, {"source_state": "clean", "modified_tracked_files": None,
                            "untracked_harness_files": [], "source_state_detail": None}):
            with self.subTest(accepted=fields):
                candidate = copy.deepcopy(block)
                candidate["git"].update(fields)
                self.assertEqual("verified", check_harness_provenance(
                    candidate, str(self.repo), require_current=True)[0])
        for fields in ({"source_state": "dirty:abc"}, {"source_state": "unknown"},
                       {"source_state": None}, {"modified_tracked_files": [" M app/core.py"]},
                       {"untracked_harness_files": ["probe.txt"]},
                       {"source_state_detail": "probe failed"}):
            with self.subTest(rejected=fields):
                candidate = copy.deepcopy(block)
                candidate["git"].update(fields)
                self.assertEqual("inconsistent", check_harness_provenance(
                    candidate, str(self.repo))[0])
                self.assertIsNone(get_reproducible_harness_source(candidate, str(self.repo)))

    def test_current_check_fails_closed_when_harness_cannot_be_read(self):
        commit = self.commit("one\n")
        block = self.block(commit, get_harness_sha256_at_commit(str(self.repo), commit), False)
        with patch("experiments.manifest._source_fingerprint", side_effect=OSError("unreadable")):
            self.assertEqual("unverifiable", check_harness_provenance(
                block, str(self.repo), require_current=True)[0])

    def test_legacy_dirty_crlf_identity_requires_the_exact_raw_bytes(self):
        recorded = self.commit("one\n")
        raw = b"dirty\r\n"
        (self.harness / "probe.py").write_bytes(raw)
        target = hashlib.sha256(b"probe.py" + raw).hexdigest()
        block = self.block(recorded, target, True)
        self.assertIsNone(get_reproducible_harness_source(block, str(self.repo)))
        block["git"].pop("harness_sha256_scheme")
        self.assertEqual("WORKTREE", get_reproducible_harness_source(block, str(self.repo)))
        block["git"]["dirty"] = None
        self.assertIsNone(get_reproducible_harness_source(block, str(self.repo)))
        block["git"]["dirty"] = True
        (self.harness / "probe.py").write_bytes(raw.replace(b"\r\n", b"\n"))
        self.assertIsNone(get_reproducible_harness_source(block, str(self.repo)))

    def test_legacy_clean_crlf_commit_uses_raw_committed_bytes(self):
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=self.repo, check=True)
        raw = b"committed\r\n"
        (self.harness / "probe.py").write_bytes(raw)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "raw CRLF"], cwd=self.repo, check=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                         cwd=self.repo, text=True).strip()
        target = hashlib.sha256(b"probe.py" + raw).hexdigest()
        block = self.block(commit, target, False)
        block["git"].pop("harness_sha256_scheme")
        self.assertEqual(commit, get_reproducible_harness_source(block, str(self.repo)))

    def test_unknown_or_null_digest_scheme_cannot_verify_matching_bytes(self):
        commit = self.commit("one\n")
        target = get_harness_sha256_at_commit(str(self.repo), commit)
        for scheme in ("sha256-future-v2", None, []):
            with self.subTest(scheme=scheme):
                block = self.block(commit, target, False)
                block["git"]["harness_sha256_scheme"] = scheme
                self.assertIsNone(get_reproducible_harness_source(block, str(self.repo)))

    def test_clean_run_with_mismatched_harness_is_inconsistent(self):
        recorded = self.commit("one\n")
        later = self.commit("two\n")
        other = get_harness_sha256_at_commit(str(self.repo), later)
        status, detail = check_harness_provenance(
            self.block(recorded, other, False), str(self.repo))
        self.assertEqual("inconsistent", status)
        self.assertIn("clean recorded commit", detail)

    def test_unknown_source_state_never_rescues_descendant_or_worktree(self):
        recorded = self.commit("one\n")
        captured = self.commit("two\n")
        descendant = get_harness_sha256_at_commit(str(self.repo), captured)
        # Only an explicit dirty=True makes a later commit eligible.
        self.assertEqual("verified", check_harness_provenance(
            self.block(recorded, descendant, True), str(self.repo))[0])
        self.assertEqual("unverifiable", check_harness_provenance(
            self.block(recorded, descendant, None), str(self.repo))[0])
        (self.harness / "probe.py").write_text("dirty\n", encoding="utf-8")
        worktree = _source_fingerprint(str(self.harness))
        self.assertEqual("verified", check_harness_provenance(
            self.block(captured, worktree, True), str(self.repo))[0])
        self.assertEqual("unverifiable", check_harness_provenance(
            self.block(captured, worktree, None), str(self.repo))[0])
        self.assertIsNone(get_reproducible_harness_source(
            self.block(captured, worktree, None), str(self.repo)))


class HarnessDigestTests(unittest.TestCase):
    """One byte identity, independent of the checkout's line endings."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.harness = Path(self.temp.name)
        self.probe = self.harness / "probe.py"

    def tearDown(self):
        self.temp.cleanup()

    def test_crlf_and_lf_agree_but_content_and_lone_cr_still_differ(self):
        self.probe.write_bytes(b"A = 1\r\nB = 2\r\n")
        crlf = _source_fingerprint(str(self.harness))
        self.probe.write_bytes(b"A = 1\nB = 2\n")
        lf = _source_fingerprint(str(self.harness))
        self.probe.write_bytes(b"A = 1\nB = 3\n")
        changed = _source_fingerprint(str(self.harness))
        self.probe.write_bytes(b"A = 1\rB = 2\n")
        lone_cr = _source_fingerprint(str(self.harness))
        self.assertEqual(lf, crlf, "CRLF and LF are one content identity")
        self.assertNotEqual(lf, changed, "a content change must move the hash")
        self.assertNotEqual(lf, lone_cr, "a lone CR is not a line ending")


if __name__ == "__main__":
    unittest.main()
