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


class ParagraphStructureTest(unittest.TestCase):
    """A block element ends a paragraph, so it must leave a BLANK line.

    The extractor emitted one newline per block, so paragraph boundaries were
    invisible to everything that keys on "\\n\\n" - the dialogue span map, which
    refuses to let a quote cross a paragraph break, and chunking, which splits
    on them. It looked fine on publishers who leave an empty <p> between
    paragraphs and produced 162 breaks where another extractor found 3,830 on
    publishers who do not. The words were all present; the shape was gone.
    """

    def _text(self, html):
        sys.path.insert(0, os.path.join(REPO, "app"))
        from routers.script import _HTMLTextExtractor
        extractor = _HTMLTextExtractor()
        extractor.feed(html)
        return extractor.get_text()

    def test_paragraphs_are_separated_by_a_blank_line(self):
        out = self._text("<p>First paragraph.</p><p>Second paragraph.</p>")
        self.assertIn("\n\n", out)
        self.assertEqual(2, len([p for p in out.split("\n\n") if p.strip()]))

    def test_a_line_break_stays_a_single_newline(self):
        # <br> breaks a line INSIDE a paragraph; promoting it would split one
        # spoken line into two and let the span map treat each half separately.
        out = self._text("<p>First line.<br/>Second line.</p>")
        self.assertIn("First line.\nSecond line.", out.replace("\n\n", "\n", 1))

    def test_headings_and_list_items_also_end_a_paragraph(self):
        out = self._text("<h1>Chapter</h1><li>One</li><li>Two</li>")
        self.assertEqual(3, len([p for p in out.split("\n\n") if p.strip()]))
