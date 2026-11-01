import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import atomic_json_write
from tools.audit import audit_experiment_artifacts as audit
from tools.audit import audit_legacy_attribution as legacy


class StructuralAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", self.temp.name], check=True, capture_output=True)
        self.repo_patch = patch.object(audit, "REPO", self.temp.name)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        subprocess.run(["git", "add", path.name], cwd=self.root, check=True, capture_output=True)
        return path

    def test_classifies_all_identity_contracts_without_scientific_claims(self):
        self.write("supported.json", {
            "status": "complete", "rows": [],
            "provenance": {"seed": 7, "args": {"seed": 7},
                           "git": {"commit": "abc", "dirty": True,
                                   "harness_sha256": "0" * 64}}})
        self.write("provisional.json", {
            "rows": [{"arm": "x"}],
            "meta": {"git": {"commit": "def", "dirty": False}}})
        self.write("unidentified.json", {"rows": []})
        self.write("list.json", [])
        result = audit.build_audit(str(self.root))

        by_name = {row["artifact"]: row for row in result["artifacts"]}
        self.assertEqual("supported_structure",
                         by_name["supported.json"]["classification"])
        self.assertEqual("provisional",
                         by_name["provisional.json"]["classification"])
        self.assertEqual("exploratory",
                         by_name["unidentified.json"]["classification"])
        self.assertEqual("exploratory",
                         by_name["list.json"]["classification"])
        self.assertIn("do not validate scientific conclusions", result["scope"])

    def test_unreadable_json_is_visible_not_skipped(self):
        (self.root / "broken.json").write_text("{", encoding="utf-8")
        subprocess.run(["git", "add", "broken.json"], cwd=self.root, check=True, capture_output=True)
        result = audit.build_audit(str(self.root))
        self.assertEqual(1, len(result["artifacts"]))
        self.assertIn("unreadable JSON", result["artifacts"][0]["reason"])

    def test_matching_stored_summary_is_accepted(self):
        self.write("matching.json", {
            "rows": [{"arm": "base", "correct": True}],
            "summary": {"base": {"n": 1, "correct": 1}},
        })
        result = audit.build_audit(str(self.root))
        self.assertEqual([], result["artifacts"][0]["stored_summary_problems"])

    def test_inconsistent_stored_summary_stops_the_audit(self):
        self.write("inconsistent.json", {
            "rows": [{"arm": "base", "correct": False}],
            "summary": {"base": {"n": 1, "correct": 1}},
        })
        with self.assertRaisesRegex(ValueError, "summaries inconsistent"):
            audit.build_audit(str(self.root))


class IndexableArtifactTests(unittest.TestCase):
    """A checked-in index may only contain what everyone else can see.

    PR #340 failed CI on exactly this: six artifacts existed on this machine
    and not in the repository, the local index counted them, and CI - which
    checks out the committed files and regenerates - reported the index stale.
    Every run of the overnight queue would have reproduced it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.tmp.name, check=True,
                       capture_output=True)

    def _write(self, name):
        (self.root / name).write_text(json.dumps({"rows": []}), encoding="utf-8")

    def test_only_tracked_artifacts_are_indexed_and_the_rest_are_named(self):
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        self._write("committed.json")
        self._git("add", "committed.json")
        self._git("commit", "-qm", "add")
        self._write("local_only.json")

        with patch.object(audit, "REPO", self.tmp.name):
            keep, skipped = audit.indexable_artifacts(self.tmp.name)

        self.assertEqual(["committed.json"], [os.path.basename(p) for p in keep])
        self.assertEqual(["local_only.json"], skipped,
                         "an unindexed artifact must still be reported")

    def test_a_directory_without_git_refuses_unknown_membership(self):
        self._write("a.json")
        self._write("b.json")
        with patch.object(audit, "REPO", self.tmp.name):
            with self.assertRaisesRegex(RuntimeError, "Cannot determine tracked"):
                audit.indexable_artifacts(self.tmp.name)




class RowCountTest(unittest.TestCase):
    """`has_rows` must mean there ARE rows.

    It was `isinstance(doc.get("rows"), list)`, which is true of an empty list,
    and it looked under one key while the measurement scripts write their list
    as `results` - so it answered "is there a rows key" for half the directory
    and "no" for the other half regardless of content.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, payload):
        path = os.path.join(self.tmp.name, "artifact.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_an_empty_rows_list_is_not_rows(self):
        row = audit.classify_artifact(self.write({"rows": [], "meta": {}}))
        self.assertFalse(row["has_rows"])
        self.assertEqual(0, row["row_count"])

    def test_a_populated_rows_list_counts(self):
        row = audit.classify_artifact(self.write({"rows": [{"a": 1}, {"a": 2}],
                                            "meta": {}}))
        self.assertTrue(row["has_rows"])
        self.assertEqual(2, row["row_count"])

    def test_the_results_contract_is_counted_too(self):
        """respelling_e_row__ay_n1200.json holds 1,129 measured terms under
        `results`; reading only `rows` would report it as having none."""
        row = audit.classify_artifact(self.write({"results": [1, 2, 3],
                                            "provenance": {}}))
        self.assertTrue(row["has_rows"])
        self.assertEqual(3, row["row_count"])

    def test_an_artifact_with_neither_reports_no_count_rather_than_zero(self):
        """A comparison artifact keeps its data under its own keys. Zero would
        claim it was measured and found empty; None says it was not counted."""
        row = audit.classify_artifact(self.write({"pilots": [{"p": 1}],
                                            "provenance": {}}))
        self.assertIsNone(row["row_count"])
        self.assertFalse(row["has_rows"])


class AuditInputLineEndingTests(unittest.TestCase):
    """Pinned audit inputs must not depend on the checkout's core.autocrlf.

    Measured on this repository: with core.autocrlf=true, a checkout rewrote
    every audited experiment JSON from LF to CRLF. The parsed documents were
    identical and every audit row differed only in bytes/sha256 - a pin
    failing on evidence nobody had changed. Those inputs are now marked
    `text eol=lf` down to any depth under their roots. Structural audit JSON
    and provenance-hashed experiment Python sources need the same byte pin.
    `-text` was rejected
    because it only disables EOL conversion - diff and merge stay textual
    unless those attributes are unset too - and it does not affect 120000
    entries either.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.root / "ab_test_runtime" / "experiments").mkdir(parents=True)
        (self.root / "app" / "fixtures").mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / ".gitattributes"
        (self.root / ".gitattributes").write_bytes(source.read_bytes())

    def _git(self, *args, input=None):
        return subprocess.run(["git", *args], cwd=self.tmp.name,
                              input=input, check=True, capture_output=True)

    def test_experiment_and_gold_json_check_out_as_pinned_lf_bytes(self):
        inputs = {
            "RESULTS_INDEX.md": b'# Results index\nfixture\n',
            "results_index.csv": b'artifact,n\nfixture,1\n',
            "LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md": b'# Legacy audit\nfixture\n',
            "app/tests/unit_test_inventory.json": b'{\n  "fixture": []\n}\n',
            "ab_test_runtime/evidence/fixture/fixtures/gold.json":
                b'{\n  "entries": []\n}\n',
            "ab_test_runtime/evidence/fixture/fixtures/nested/gold.json":
                b'{\n  "entries": []\n}\n',
            "ab_test_runtime/experiments/artifact.json": b'{\n  "rows": []\n}\n',
            "ab_test_runtime/experiments/nested_run/artifact.json":
                b'{\n  "rows": []\n}\n',
            "ab_test_runtime/experiments/nested_run/deeper/artifact.json":
                b'{\n  "rows": []\n}\n',
            "app/fixtures/attribution_gold.json": b'{\n  "entries": []\n}\n',
            "app/fixtures/nested/attribution_gold.json":
                b'{\n  "entries": []\n}\n',
            "ab_test_runtime/audit/artifact_structural_audit.json":
                b'{\n  "artifacts": []\n}\n',
            "ab_test_runtime/audit/nested/artifact_structural_audit.json":
                b'{\n  "artifacts": []\n}\n',
            "app/experiments/goal_evidence_audit.py":
                b'def build_audit():\n    return {}\n',
            "app/experiments/nested/harness.py":
                b'def run():\n    return None\n',
        }
        controls = {
            "notes.txt": b"one\ntwo\n",
            "ab_test_runtime/experiments/nested_run/notes.txt": b"one\ntwo\n",
            "app/fixtures/nested/notes.csv": b"one,two\n",
            "ab_test_runtime/audit/nested/notes.txt": b"one\ntwo\n",
            "app/experiments/nested/notes.json": b'{"note": true}\n',
            "ab_test_runtime/evidence/fixture/notes.json": b'{"note": true}\n',
        }
        for relative, content in {**inputs, **controls}.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self._git("-c", "core.autocrlf=false", "add", relative)

        # A provisional gold fixture is a 120000 entry; the attributes must not
        # turn its link target into text.
        target = b"attribution_gold.json"
        blob = self._git("hash-object", "-w", "--stdin", input=target)
        self._git("update-index", "--add", "--cacheinfo",
                  "120000,%s,app/fixtures/attribution_gold_provisional.json"
                  % blob.stdout.strip().decode())
        self._git("-c", "core.autocrlf=false", "add", ".gitattributes")
        self._git("commit", "-qm", "add")

        for relative in inputs:
            attrs = self._git("check-attr", "text", "eol", "--", relative)
            lines = attrs.stdout.decode().splitlines()
            self.assertIn("%s: text: set" % relative, lines)
            self.assertIn("%s: eol: lf" % relative, lines)
        for relative in controls:
            attrs = self._git("check-attr", "text", "eol", "--", relative)
            lines = attrs.stdout.decode().splitlines()
            self.assertIn("%s: text: unspecified" % relative, lines)
            self.assertIn("%s: eol: unspecified" % relative, lines)
        for relative in ("RESULTS_INDEX.md", "results_index.csv",
                         "LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md",
                         "app/tests/unit_test_inventory.json"):
            attrs = self._git("check-attr", "merge", "--", relative)
            self.assertIn("%s: merge: ours" % relative, attrs.stdout.decode())

        self._git("config", "core.autocrlf", "true")
        for relative in [*inputs, *controls,
                         "app/fixtures/attribution_gold_provisional.json"]:
            (self.root / relative).unlink(missing_ok=True)
        self._git("checkout", "--", ".")

        for relative, content in inputs.items():
            checked_out = (self.root / relative).read_bytes()
            self.assertNotIn(b"\r", checked_out,
                             "%s was CRLF-converted at checkout" % relative)
            self.assertEqual(content, checked_out)
            self.assertEqual(hashlib.sha256(content).hexdigest(),
                             hashlib.sha256(checked_out).hexdigest())
        for relative, content in controls.items():
            self.assertEqual(content.replace(b"\n", b"\r\n"),
                             (self.root / relative).read_bytes(),
                             "%s must keep normal autocrlf behaviour" % relative)

        for relative, content in inputs.items():
            (self.root / relative).write_bytes(content)
        status = self._git("status", "--porcelain", "--", *inputs)
        self.assertEqual(b"", status.stdout,
                         "rewriting canonical generated bytes must stay clean")

        link = self.root / "app/fixtures/attribution_gold_provisional.json"
        listing = self._git("ls-files", "-s", "--",
                            "app/fixtures/attribution_gold_provisional.json")
        self.assertTrue(listing.stdout.decode().startswith("120000 "),
                        "the attributes changed a symlink mode")
        if os.path.islink(link):
            self.assertEqual(target.decode(), os.readlink(link))
        else:
            payload = link.read_bytes()
            self.assertEqual(target, payload)
        with patch.object(legacy, "REPO", str(self.root)):
            result = legacy._current_gold({
                "gold_path": "app/fixtures/attribution_gold_provisional.json",
                "gold_sha256": hashlib.sha256(
                    inputs["app/fixtures/attribution_gold.json"]).hexdigest(),
                "gold_lines": 0,
            }, [])
        self.assertTrue(result["available"])
        self.assertFalse(result["hash_changed"])


class CanonicalStructuralWriteTests(unittest.TestCase):
    """The structural audit is byte-pinned, not just object-pinned.

    `audit_legacy_attribution.build_audit` hashes this file's RAW bytes into
    `source_structural_sha256`, so regenerating it on Windows with the shell's
    default CRLF line endings changes a committed hash while changing no data.
    The shared writer keeps its native-newline default; only the structural
    caller opts into LF. These tests read the real bytes written to disk.
    """

    DOCUMENT = {
        "scope": "structural audit only; classifications do not validate "
                 "scientific conclusions",
        "summary": {"provisional": 1},
        "artifacts": [{"artifact": "caf\u00e9.json", "rows": 1}],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.expected = json.dumps(self.DOCUMENT, indent=2,
                                   ensure_ascii=False).encode("utf-8")

    def leftovers(self):
        return [path.name for path in self.root.iterdir()
                if path.name.startswith(".tmp_")]

    def test_invalid_newline_is_refused_before_allocating_a_temp_file(self):
        for value in (True, 1, b"\n", "invalid"):
            with self.subTest(value=value), patch('utils.tempfile.mkstemp') as create:
                with self.assertRaises(ValueError):
                    atomic_json_write(self.DOCUMENT, str(self.root / 'invalid.json'),
                                      newline=value)
                create.assert_not_called()

    def test_lf_mode_writes_exactly_the_committed_serialization(self):
        out = self.root / "artifact_structural_audit.json"
        atomic_json_write(self.DOCUMENT, str(out), newline="\n")
        self.assertEqual(self.expected, out.read_bytes())
        self.assertNotIn(b"\r", out.read_bytes())
        self.assertEqual([], self.leftovers())

    def test_default_writer_still_uses_native_line_endings(self):
        out = self.root / "native.json"
        with patch('utils.os.fdopen', wraps=os.fdopen) as stream:
            atomic_json_write(self.DOCUMENT, str(out))
        self.assertIsNone(stream.call_args.kwargs['newline'])
        self.assertEqual(
            self.expected.replace(b"\n", os.linesep.encode("ascii")),
            out.read_bytes())
        self.assertEqual([], self.leftovers())

    def test_structural_audit_main_writes_the_canonical_bytes(self):
        """Exercise the caller, not just the writer: main must opt in."""
        out = self.root / "artifact_structural_audit.json"
        argv = ["audit_experiment_artifacts.py", "--out", str(out)]
        with patch.object(audit, "build_audit", return_value=self.DOCUMENT), \
                patch.object(audit, "indexable_artifacts",
                             return_value=([], [])), \
                patch.object(sys, "argv", argv), \
                patch('utils.os.fdopen', wraps=os.fdopen) as stream, \
                contextlib.redirect_stdout(io.StringIO()):
            audit.main()
        self.assertEqual("\n", stream.call_args.kwargs['newline'])
        self.assertEqual(self.expected, out.read_bytes())
        self.assertNotIn(b"\r", out.read_bytes())
        self.assertEqual([], self.leftovers())

    def test_legacy_audit_main_writes_canonical_json_bytes(self):
        document = dict(self.DOCUMENT, artifacts=[])
        out = self.root / "legacy_attribution_audit.json"
        markdown = self.root / "legacy.md"
        argv = ["audit_legacy_attribution.py", "--json", str(out),
                "--markdown", str(markdown)]
        with patch.object(legacy, "build_audit", return_value=document), \
                patch.object(legacy, "render_markdown", return_value="fixture\n"), \
                patch.object(sys, "argv", argv), \
                patch('utils.os.fdopen', wraps=os.fdopen) as stream, \
                contextlib.redirect_stdout(io.StringIO()):
            legacy.main()
        self.assertEqual("\n", stream.call_args.kwargs['newline'])
        self.assertEqual(json.dumps(document, indent=2,
                                   ensure_ascii=False).encode("utf-8"),
                         out.read_bytes())
        self.assertEqual(b"fixture\n", markdown.read_bytes())
        self.assertEqual([], self.leftovers())

    def test_crlf_output_changes_the_pinned_structural_hash(self):
        out = self.root / "old_behavior.json"
        atomic_json_write(self.DOCUMENT, str(out), newline="\r\n")
        written = out.read_bytes()
        self.assertNotEqual(self.expected, written)
        self.assertIn(b"\r\n", written)
        self.assertNotEqual(legacy._sha256(self.expected),
                            legacy._sha256(written))


if __name__ == "__main__":
    unittest.main()
