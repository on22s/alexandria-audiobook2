"""A corpus file too damaged to read must stop work, not flavour it.

index18 carries 6,662 replacement characters - 1.4% of its text against a 0.5%
gate - and no quotation mark or apostrophe survives. The bytes are literally
EF BF BD, so the file was written after a lossy decode and the original
characters are gone. 32 experiment artifacts name that book, and GOALS.md
quotes per-book figures from it.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments", "audit_source_encoding.py")


class SourceEncodingAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "audit.json")

    def _write(self, name, text):
        with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _run(self):
        return subprocess.run(
            [sys.executable, SCRIPT, "--inputs", self.tmp.name, "--out", self.out],
            capture_output=True, text=True, timeout=120)

    def test_a_clean_book_passes(self):
        self._write("clean.txt", "He said, “Good evening.” " * 200)
        result = self._run()
        self.assertEqual(0, result.returncode, result.stdout)
        rows = json.load(open(self.out))["results"]
        self.assertTrue(rows[0]["passes_gate"])

    def test_a_damaged_book_fails_and_stops_the_chain(self):
        """Non-zero on purpose: continuing past it measures an encoding bug
        more precisely."""
        self._write("broken.txt", "He said, �Good evening.� " * 200)
        result = self._run()
        self.assertEqual(1, result.returncode)
        self.assertIn("fail the encoding gate", result.stdout)

    def test_the_report_names_what_has_to_be_rechecked(self):
        self._write("broken.txt", "�" * 50 + "x" * 100)
        self._run()
        row = json.load(open(self.out))["results"][0]
        self.assertIn("artifacts_naming_this_book", row)
        self.assertIn("re-extract", row["remedy"])

    def test_a_few_stray_replacements_do_not_condemn_a_book(self):
        # The gate is a share, not a count: one mangled character in a novel
        # is not a reason to throw away every measurement made from it.
        self._write("mostly_fine.txt", "�" + "clean text here. " * 500)
        self.assertEqual(0, self._run().returncode)
