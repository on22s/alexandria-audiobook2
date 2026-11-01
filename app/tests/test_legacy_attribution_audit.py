import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.audit import audit_legacy_attribution as audit

GOLD_DOC = {"entries": [{"id": "one", "expected_speaker": "ALICE"}],
            "aliases": [["ALICE", "AL"]]}
GOLD_ROWS = [{"id": "one", "expected": "ALICE", "predicted": "AL",
              "correct": True}]


def _git(root, *args, **kwargs):
    return subprocess.run(
        ["git", "-c", "user.name=Fixture",
         "-c", "user.email=fixture@example.invalid",
         "-C", str(root), *args],
        check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def _gold_repo(root, *init_args):
    fixtures = root / "app" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "attribution_gold.json").write_text(
        json.dumps(GOLD_DOC), encoding="utf-8")
    (fixtures / "untracked.json").write_text(
        json.dumps(GOLD_DOC), encoding="utf-8")
    _git(root, "init", "-q", "-b", "main", *init_args)
    _git(root, "add", "app/fixtures/attribution_gold.json")
    _git(root, "commit", "-qm", "gold")


def _materialised_link(root, name, target):
    """Write exactly what Git checks out for a 120000 entry with core.symlinks=false."""
    relative = f"app/fixtures/{name}"
    path = root / relative
    path.write_text(target, encoding="utf-8")
    blob = _git(root, "hash-object", "-w", "--stdin", input=target)
    _git(root, "update-index", "--add", "--cacheinfo", f"120000,{blob},{relative}")
    return path


class LegacyAttributionAuditTests(unittest.TestCase):
    def test_committed_audit_is_identical_for_unrelated_and_missing_commit_objects(self):
        import subprocess
        structural = json.loads(Path(audit.STRUCTURAL).read_text())
        name = next(row['artifact'] for row in structural['artifacts']
                    if row['classification'] == 'provisional')
        doc = json.loads(Path(audit.EXPERIMENTS, name).read_text())
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp, 'original')
            clone = Path(tmp, 'clone')
            original.mkdir()
            def git(root, *args):
                return subprocess.check_output(['git', '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', '-C', str(root), *args],
                    stderr=subprocess.DEVNULL, text=True).strip()
            git(original, 'init', '-b', 'main')
            Path(original, 'tracked').write_text('fixture tree')
            git(original, 'add', 'tracked')
            git(original, 'commit', '-m', 'ancestor')
            ancestor = git(original, 'rev-parse', 'HEAD')
            unrelated = git(original, 'commit-tree', 'HEAD^{tree}', '-m', 'unrelated root')
            git(Path(tmp), 'clone', '--no-local', str(original), str(clone))
            doc['meta']['git']['commit'] = unrelated
            records = []
            for root, expected in ((original, False), (clone, None)):
                Path(root, name).write_text(json.dumps(doc))
                with mock.patch.object(audit, 'REPO', str(root)), mock.patch.object(audit, 'EXPERIMENTS', str(root)):
                    self.assertIs(expected, audit._commit_is_in_history(unrelated))
                    records.append(audit.inspect_artifact(name))
            self.assertEqual(records[0], records[1])
            self.assertEqual('exploratory', records[0]['classification'])
            self.assertIn('recorded commit ancestry is not verified in current history', records[0]['problems'])
            doc['meta']['git']['commit'] = ancestor
            Path(clone, name).write_text(json.dumps(doc))
            with mock.patch.object(audit, 'REPO', str(clone)), mock.patch.object(audit, 'EXPERIMENTS', str(clone)):
                self.assertTrue(audit._commit_is_in_history(ancestor))
                self.assertNotIn('recorded commit ancestry is not verified in current history',
                                 audit.inspect_artifact(name)['problems'])

    def test_commit_identity_requires_ancestry_not_unrelated_object_presence(self):
        with mock.patch.object(audit.subprocess, "run") as run:
            run.return_value.returncode = 1
            self.assertFalse(audit._commit_is_in_history("abc123"))
            self.assertEqual(
                ["git", "merge-base", "--is-ancestor", "abc123", "HEAD"],
                run.call_args.args[0])

    def test_history_query_is_bounded_offline_and_errors_are_unknown(self):
        import subprocess
        from types import SimpleNamespace
        for outcome, expected in ((0, True), (1, False), (128, None),
                                  (subprocess.TimeoutExpired('git', 10), None),
                                  (OSError('fixture Git unavailable'), None)):
            with self.subTest(outcome=outcome), mock.patch.object(audit.subprocess, 'run') as run:
                if isinstance(outcome, BaseException):
                    run.side_effect = outcome
                else:
                    run.return_value = SimpleNamespace(returncode=outcome)
                self.assertIs(expected, audit._commit_is_in_history('abc123'))
                self.assertEqual(10, run.call_args.kwargs['timeout'])
                self.assertEqual('1', run.call_args.kwargs['env']['GIT_NO_LAZY_FETCH'])
                self.assertEqual('0', run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'])

    def test_missing_promisor_commit_cannot_trigger_implicit_network_fetch(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for args in (('init', '-q'), ('config', 'user.name', 'Fixture'),
                         ('config', 'user.email', 'fixture@example.invalid'),
                         ('commit', '--allow-empty', '-qm', 'fixture'),
                         ('remote', 'add', 'origin', 'https://example.invalid/fixture.git'),
                         ('config', 'remote.origin.promisor', 'true'),
                         ('config', 'remote.origin.partialclonefilter', 'blob:none')):
                subprocess.run(['git', '-C', tmp, *args], check=True, capture_output=True)
            marker = root / 'network-attempt'
            helper = root / 'git-remote-https'
            helper.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 71\n')
            helper.chmod(0o755)
            with mock.patch.object(audit, 'REPO', tmp), mock.patch.dict(os.environ, {'GIT_EXEC_PATH': tmp}):
                self.assertIsNone(audit._commit_is_in_history('1' * 40))
            self.assertFalse(marker.exists(), 'history check attempted an implicit fetch')

    def test_current_inventory_is_exhaustive_bounded_and_rendered_once(self):
        result = audit.build_audit()
        rows = result["artifacts"]
        with self.subTest(check="inventory semantics"):
            structural = json.loads(Path(audit.STRUCTURAL).read_text(encoding="utf-8"))
            expected = {row["artifact"] for row in structural["artifacts"]
                        if row["classification"] == "provisional"}
            self.assertEqual(expected, {row["artifact"] for row in rows})
            self.assertEqual(len(expected), sum(result["summary"].values()))
            self.assertTrue(all(row["semantic_limit"] for row in rows))
            self.assertTrue(any(row["classification"] == "historical_only" for row in rows))
        rendered = audit.render_markdown(result)
        for row in rows:
            with self.subTest(artifact=row["artifact"], check="markdown membership"):
                self.assertEqual(1, rendered.count(f"`{row['artifact']}`"))

    def test_current_gold_rescore_exposes_changed_and_unmapped_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = {
                "entries": [{"id": "one", "expected_speaker": "ALICE"}],
                "aliases": [["ALICE", "AL"]],
            }
            path = Path(tmp, "gold.json")
            path.write_text(json.dumps(fixture), encoding="utf-8")
            rows = [
                {"id": "one", "expected": "BOB", "predicted": "AL",
                 "correct": False},
                {"id": "missing", "expected": "BOB", "predicted": "BOB",
                 "correct": True},
            ]
            meta = {"gold_path": "gold.json", "gold_sha256": "old",
                    "gold_lines": 2}
            with mock.patch.object(audit, "REPO", tmp):
                result = audit._current_gold(meta, rows)
            self.assertTrue(result["hash_changed"])
            self.assertTrue(result["line_count_changed"])
            self.assertEqual(1, result["expected_changed_rows"])
            self.assertEqual(1, result["correctness_changed_rows"])
            self.assertEqual(1, result["missing_rows"])

    def test_current_gold_never_reads_outside_the_checkout(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as outside:
            fixture = Path(outside, "gold.json")
            fixture.write_text(json.dumps({
                "entries": [{"id": "one", "expected_speaker": "ALICE"}]
            }), encoding="utf-8")
            meta = {"gold_path": str(fixture), "gold_sha256": "old",
                    "gold_lines": 1}
            rows = [{"id": "one", "expected": "BOB", "predicted": "ALICE",
                     "correct": False}]
            with mock.patch.object(audit, "REPO", repo):
                result = audit._current_gold(meta, rows)
            self.assertEqual({
                "available": False,
                "hash_changed": None,
                "missing_rows": None,
                "expected_changed_rows": None,
                "correctness_changed_rows": None,
            }, result)

    def test_materialised_in_repo_link_scores_and_hashes_like_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _gold_repo(root)
            shim = _materialised_link(root, "gold_provisional.json",
                                     "attribution_gold.json")
            self.assertFalse(os.path.islink(shim), "fixture is the materialised form")
            target = root / "app" / "fixtures" / "attribution_gold.json"
            meta = {"gold_path": "app/fixtures/gold_provisional.json",
                    "gold_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "gold_lines": 1}
            with mock.patch.object(audit, "REPO", str(root)):
                shimmed = audit._current_gold(meta, GOLD_ROWS)
                meta["gold_path"] = "app/fixtures/attribution_gold.json"
                direct = audit._current_gold(meta, GOLD_ROWS)
            self.assertTrue(shimmed["available"])
            self.assertFalse(shimmed["hash_changed"],
                             "the pointer's own bytes must not be hashed")
            self.assertFalse(shimmed["line_count_changed"])
            self.assertEqual(0, shimmed["correctness_changed_rows"])
            self.assertEqual(direct, shimmed)

    def test_link_refuses_absolute_traversal_cycle_chain_and_untracked_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            _gold_repo(root)
            (base / "outside.json").write_text(json.dumps(GOLD_DOC), encoding="utf-8")
            _materialised_link(root, "chain_target.json", "attribution_gold.json")
            cases = [("absolute.json", str(base / "outside.json")),
                     ("traversal.json", "../../../outside.json"),
                     ("loop.json", "loop.json"),
                     ("chain.json", "chain_target.json"),
                     ("untracked.json", "untracked.json")]
            with mock.patch.object(audit, "REPO", str(root)):
                for name, target in cases:
                    with self.subTest(case=name, target=target):
                        _materialised_link(root, name, target)
                        meta = {"gold_path": f"app/fixtures/{name}",
                                "gold_sha256": "old", "gold_lines": 1}
                        with self.assertRaises(audit.GoldPathRefused):
                            audit._current_gold(meta, GOLD_ROWS)

    def test_edited_pointer_bytes_are_never_read_as_a_target_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _gold_repo(root)
            path = _materialised_link(root, "edited.json", "attribution_gold.json")
            path.write_text("attribution_gold.json\n", encoding="utf-8")
            meta = {"gold_path": "app/fixtures/edited.json",
                    "gold_sha256": "old", "gold_lines": 1}
            with mock.patch.object(audit, "REPO", str(root)):
                with self.assertRaises(audit.GoldPathRefused):
                    audit._current_gold(meta, GOLD_ROWS)

    def _bind_posix_link(self, path, observed=None):
        """Make the audit see PATH as a POSIX link to OBSERVED, or to its text.

        Only used where this host cannot create a symlink at all (Windows
        without the privilege): islink() is true for this one path and
        readlink() returns the working text, which is the pair of facts the
        POSIX form supplies. os.readlink is the code path either way.
        """
        real_islink, real_readlink = os.path.islink, os.readlink
        this = os.path.normcase(os.path.abspath(str(path)))

        def islink(candidate):
            return (os.path.normcase(os.path.abspath(os.fspath(candidate))) == this
                    or real_islink(candidate))

        def readlink(candidate):
            if os.path.normcase(os.path.abspath(os.fspath(candidate))) == this:
                return observed if observed is not None else path.read_text(
                    encoding="utf-8")
            return real_readlink(candidate)

        for module, attr, replacement in ((audit.os.path, "islink", islink),
                                          (audit.os, "readlink", readlink)):
            patcher = mock.patch.object(module, attr, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _put_link(self, path, observed):
        """Put a real symlink to OBSERVED at PATH, else bind that state."""
        staging = path.with_name(path.name + ".staged-link")
        try:
            os.symlink(observed, staging)
        except (OSError, NotImplementedError):
            path.write_text(observed, encoding="utf-8")
            return self._bind_posix_link(path)
        os.replace(staging, path)

    def _posix_link(self, root, name, indexed_target, observed_target=None):
        """A POSIX-form link whose index entry names INDEXED_TARGET.

        Passing a different OBSERVED_TARGET is a link retargeted in the working
        tree without touching the index.
        """
        path = _materialised_link(root, name, indexed_target)
        self._put_link(
            path, indexed_target if observed_target is None else observed_target)
        return path

    def test_ordinary_gold_is_opened_and_parsed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _gold_repo(root)
            target = root / "app" / "fixtures" / "attribution_gold.json"
            meta = {"gold_path": "app/fixtures/attribution_gold.json",
                    "gold_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "gold_lines": 1}
            opened = []
            real_open = open

            def counting_open(file, *args, **kwargs):
                opened.append(os.fspath(file))
                return real_open(file, *args, **kwargs)

            with mock.patch.object(audit, "REPO", str(root)), \
                    mock.patch("builtins.open", counting_open), \
                    mock.patch.object(audit.json, "loads",
                                      wraps=json.loads) as loads:
                result = audit._current_gold(meta, GOLD_ROWS)
            self.assertTrue(result["available"])
            self.assertFalse(result["hash_changed"])
            reads = [path for path in opened
                     if os.path.abspath(path) == os.path.abspath(str(target))]
            self.assertEqual(1, len(reads), opened)
            self.assertEqual(1, loads.call_count,
                             "an ordinary gold fixture was parsed more than once")

    def test_posix_and_materialised_links_score_like_the_direct_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _gold_repo(root)
            target = root / "app" / "fixtures" / "attribution_gold.json"
            meta = {"gold_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "gold_lines": 1}
            self._posix_link(root, "posix_link.json", "attribution_gold.json")
            _materialised_link(root, "materialised.json", "attribution_gold.json")
            with mock.patch.object(audit, "REPO", str(root)):
                results = {
                    name: audit._current_gold(
                        dict(meta, gold_path=f"app/fixtures/{name}"), GOLD_ROWS)
                    for name in ("attribution_gold.json", "posix_link.json",
                                 "materialised.json")}
            self.assertTrue(results["attribution_gold.json"]["available"])
            self.assertFalse(results["posix_link.json"]["hash_changed"],
                             "the link's own bytes must not be hashed")
            self.assertEqual(results["attribution_gold.json"],
                             results["posix_link.json"])
            self.assertEqual(results["attribution_gold.json"],
                             results["materialised.json"])
            target_raw = target.read_bytes()
            for name in ("attribution_gold.json", "posix_link.json",
                         "materialised.json"):
                with mock.patch.object(audit, "REPO", str(root)):
                    raw, fixture = audit.read_gold_fixture(
                        str(root / "app" / "fixtures" / name),
                        f"app/fixtures/{name}")
                self.assertEqual(target_raw, raw, name)
                self.assertEqual(GOLD_DOC, fixture, name)

    def test_posix_link_retargeted_without_the_index_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _gold_repo(root)
            fixtures = root / "app" / "fixtures"
            other = fixtures / "other_gold.json"
            other.write_text(json.dumps({"entries": [
                {"id": "one", "expected_speaker": "BOB"}]}), encoding="utf-8")
            _git(root, "add", "app/fixtures/other_gold.json")
            self._posix_link(root, "link.json", "attribution_gold.json",
                             "other_gold.json")
            meta = {"gold_path": "app/fixtures/link.json",
                    "gold_sha256": hashlib.sha256(other.read_bytes()).hexdigest(),
                    "gold_lines": 1}
            with mock.patch.object(audit, "REPO", str(root)):
                with self.assertRaisesRegex(audit.GoldPathRefused,
                                            "not the tracked link"):
                    audit._current_gold(meta, GOLD_ROWS)

    def test_target_that_is_a_link_on_disk_but_regular_in_git_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _gold_repo(root)
            inner = root / "app" / "fixtures" / "inner.json"
            inner.write_text(json.dumps(GOLD_DOC), encoding="utf-8")
            _git(root, "add", "app/fixtures/inner.json")
            _git(root, "commit", "-qm", "inner")
            _materialised_link(root, "outer.json", "inner.json")
            self._put_link(inner, "attribution_gold.json")
            meta = {"gold_path": "app/fixtures/outer.json",
                    "gold_sha256": "old", "gold_lines": 1}
            with mock.patch.object(audit, "REPO", str(root)):
                with self.assertRaisesRegex(audit.GoldPathRefused,
                                            "a link on disk but a regular file "
                                            "in Git"):
                    audit._current_gold(meta, GOLD_ROWS)

    def test_corrupt_tracked_regular_file_is_refused_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _gold_repo(root)
            corrupt = root / "app" / "fixtures" / "corrupt.json"
            corrupt.write_text('{"entries": [', encoding="utf-8")
            _git(root, "add", "app/fixtures/corrupt.json")
            _git(root, "commit", "-qm", "corrupt")
            listing = _git(root, "ls-files", "-s", "--", "app/fixtures/corrupt.json")
            self.assertTrue(listing.startswith("100644 "), listing)
            meta = {"gold_path": "app/fixtures/corrupt.json",
                    "gold_sha256": "old", "gold_lines": 1}
            with mock.patch.object(audit, "REPO", str(root)):
                with self.assertRaisesRegex(audit.GoldPathRefused,
                                            "is not the tracked link it claims"):
                    audit._current_gold(meta, GOLD_ROWS)
            for index, (content, message) in enumerate((
                    ('{"entries": [', 'is not JSON'),
                    ('[]', 'is not a JSON object'),
                    ('"name.json"', 'is not a JSON object'))):
                corrupt.write_text(content, encoding="utf-8")
                if message == 'is not a JSON object':
                    with self.subTest(content=content, form="ordinary"):
                        meta["gold_path"] = "app/fixtures/corrupt.json"
                        with mock.patch.object(audit, "REPO", str(root)):
                            with self.assertRaisesRegex(audit.GoldPathRefused,
                                                        message) as caught:
                                audit._current_gold(meta, GOLD_ROWS)
                        self.assertIn('corrupt.json', str(caught.exception))
                for form in ("materialised", "posix"):
                    with self.subTest(content=content, form=form):
                        name = f"corrupt_{index}_{form}.json"
                        if form == "posix":
                            self._posix_link(root, name, "corrupt.json")
                        else:
                            _materialised_link(root, name, "corrupt.json")
                        meta["gold_path"] = f"app/fixtures/{name}"
                        with mock.patch.object(audit, "REPO", str(root)):
                            with self.assertRaisesRegex(audit.GoldPathRefused,
                                                        message) as caught:
                                audit._current_gold(meta, GOLD_ROWS)
                        self.assertIn(name, str(caught.exception))
                        if message == 'is not JSON':
                            self.assertIn('corrupt.json', str(caught.exception))

    def test_sha256_repository_link_uses_its_own_oid_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            init = subprocess.run(
                ["git", "init", "-q", "-b", "main", "--object-format=sha256",
                 str(root)], capture_output=True, text=True)
            if init.returncode:
                self.skipTest("this Git cannot create a sha256 repository: "
                              + (init.stderr.strip() or init.stdout.strip()))
            fixtures = root / "app" / "fixtures"
            fixtures.mkdir(parents=True)
            target = fixtures / "attribution_gold.json"
            target.write_text(json.dumps(GOLD_DOC), encoding="utf-8")
            _git(root, "add", "app/fixtures/attribution_gold.json")
            _git(root, "commit", "-qm", "gold")
            _materialised_link(root, "gold_provisional.json",
                               "attribution_gold.json")
            oid = _git(root, "ls-files", "-s", "--",
                       "app/fixtures/gold_provisional.json").split()[1]
            self.assertEqual(64, len(oid), oid)
            meta = {"gold_path": "app/fixtures/gold_provisional.json",
                    "gold_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "gold_lines": 1}
            with mock.patch.object(audit, "REPO", str(root)):
                result = audit._current_gold(meta, GOLD_ROWS)
            self.assertTrue(result["available"])
            self.assertFalse(result["hash_changed"])
            self.assertEqual(0, result["correctness_changed_rows"])

    def test_ambiguous_or_unanswerable_index_is_refused(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app" / "fixtures").mkdir(parents=True)
            (root / "app" / "fixtures" / "link.json").write_text(
                "attribution_gold.json", encoding="utf-8")
            meta = {"gold_path": "app/fixtures/link.json",
                    "gold_sha256": "old", "gold_lines": 1}
            entry = (b"120000 " + b"a" * 40 + b" 0\tapp/fixtures/link.json\0")
            ambiguous = SimpleNamespace(returncode=0, stderr=b"",
                                        stdout=entry + entry)
            with mock.patch.object(audit, "REPO", str(root)):
                with mock.patch.object(audit.subprocess, "run",
                                       return_value=ambiguous):
                    with self.assertRaisesRegex(audit.GoldPathRefused,
                                                "index entries"):
                        audit._current_gold(meta, GOLD_ROWS)
                with mock.patch.object(audit.subprocess, "run",
                                       side_effect=OSError("git unavailable")) as run:
                    with self.assertRaisesRegex(audit.GoldPathRefused,
                                                "Git cannot confirm"):
                        audit._current_gold(meta, GOLD_ROWS)
                self.assertEqual("1", run.call_args.kwargs["env"]["GIT_NO_LAZY_FETCH"])
                self.assertEqual("0", run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"])
                self.assertEqual(10, run.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
