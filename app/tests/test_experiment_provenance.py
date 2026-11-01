import hashlib
import os
from pathlib import Path
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
