"""A chain's --books names must be names the harness can find gold for.

`three_pass_vs_single.py` builds its answer key path straight from the book
name: `app/fixtures/attribution_gold_<book>.json`. On 2026-08-21 the 5.3 chain
passed `--books TheGambler ...` - the PDNC *directory* spelling - while the
fixtures are `attribution_gold_pdnc_thegambler.json`. The 10h stage claimed its
slot at 07:45:20Z and died at 07:45:20Z with FileNotFoundError, and the queue
moved on. Nothing about the failure was visible until the log was read.

The mistake costs a stage slot, not a wrong number, which is why nothing else
here catches it: the run is refused, not corrupted. This file checks the two
names line up before the card is committed.
"""
import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAINS = sorted(glob.glob(os.path.join(REPO, "run_chains", "*.sh")))
FIXTURES = os.path.join(REPO, "app", "fixtures")

# `--books a b c \` possibly continued over further backslash-terminated lines.
BOOKS = re.compile(r"--books\s+((?:[^\n\\]|\\\n)+)")


def strip_comments(source):
    """Drop comment lines: this file's own explanation quotes a --books line."""
    return "\n".join(l for l in source.splitlines()
                      if not l.lstrip().startswith("#"))


def books_in(source):
    """-> every book name passed to a --books flag in one chain."""
    out = []
    for match in BOOKS.finditer(strip_comments(source)):
        run = match.group(1).replace("\\\n", " ")
        for word in run.split():
            if word.startswith("-") or word.startswith("$") or '"' in word:
                break
            out.append(word)
    return out


class ChainBookFixtureTest(unittest.TestCase):
    def test_every_books_name_has_a_gold_fixture(self):
        missing = []
        for path in CHAINS:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            if "three_pass_vs_single" not in source:
                continue
            for book in books_in(source):
                fixture = os.path.join(
                    FIXTURES, "attribution_gold_%s.json" % book)
                if not os.path.exists(fixture):
                    missing.append("%s: --books %s -> no %s" % (
                        os.path.basename(path), book,
                        os.path.basename(fixture)))
        self.assertEqual([], missing, "\n".join(missing))

    def test_the_parser_reads_a_continued_books_list(self):
        """The 5.3 chain's list spans two lines; a one-line regex misses half."""
        source = (
            '    --books pdnc_thegambler pdnc_thesignofthefour \\\n'
            '            pdnc_ahandfulofdust \\\n'
            '    --inputs "$inputs" --work "$work"\n')
        self.assertEqual(
            ["pdnc_thegambler", "pdnc_thesignofthefour", "pdnc_ahandfulofdust"],
            books_in(source))

    def test_the_old_spelling_would_have_been_caught(self):
        """The exact line that died, against the fixtures actually present."""
        source = '    --books TheGambler TheSignOfTheFour\n'
        found = books_in(source)
        self.assertEqual(["TheGambler", "TheSignOfTheFour"], found)
        for book in found:
            self.assertFalse(os.path.exists(os.path.join(
                FIXTURES, "attribution_gold_%s.json" % book)))
