"""A bare % in an argparse help string breaks --help.

argparse formats help through `%`, so "wrong 47.1% of the time" becomes the
conversion `% o` and raises `TypeError: %o format: an integer is required, not
dict`. The script still RUNS - the expansion only happens when help is
printed - so it merges cleanly and then fails the first time someone asks what
its flags do. That happened on 2026-08-20 to two_stage_attribution's
--quote-type, whose help quoted a percentage.

Checked statically rather than by running `<script> --help` 186 times, which
costs 27 seconds against a 62-second suite. The AST tells us the same thing.
"""
import ast
import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEARCH = [os.path.join(REPO, "app", "experiments", "*.py"),
          os.path.join(REPO, "app", "*.py"),
          os.path.join(REPO, "*.py")]
# %% is escaped, %(name)s is argparse's own substitution. Anything else is a
# conversion argparse will try to apply to its params dict.
LEGAL = re.compile(r"%%|%\(\w+\)[sdrfg]")


def offending_percents(text):
    """-> the % signs in `text` that argparse will try to expand."""
    stripped = LEGAL.sub("", text)
    return "%" in stripped


def help_strings(path):
    """-> [(lineno, value)] for every add_argument(help=...) literal."""
    with open(path, encoding="utf-8") as handle:
        try:
            tree = ast.parse(handle.read(), filename=path)
        except SyntaxError:
            return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None)
        if name != "add_argument":
            continue
        for keyword in node.keywords:
            if keyword.arg != "help":
                continue
            value = keyword.value
            parts = []
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts = [value.value]
            elif isinstance(value, ast.JoinedStr):
                parts = [v.value for v in value.values
                         if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            if parts:
                found.append((value.lineno, "".join(parts)))
    return found


class ArgparseHelpStringTest(unittest.TestCase):
    def test_no_help_string_contains_an_unescaped_percent(self):
        offenders = []
        for pattern in SEARCH:
            for path in sorted(glob.glob(pattern)):
                for lineno, text in help_strings(path):
                    if offending_percents(text):
                        offenders.append("%s:%d %s"
                                         % (os.path.relpath(path, REPO),
                                            lineno, text[:60]))
        self.assertEqual([], offenders,
                         "argparse expands help through %%; write %%%% for a "
                         "literal percent, or --help raises TypeError:\n  "
                         + "\n  ".join(offenders))

    def test_the_check_recognises_the_shapes_it_must_allow(self):
        """Rule 21: it has to accept argparse's own syntax, or it will just
        force everyone to delete useful percentages from their help."""
        self.assertFalse(offending_percents("rescues 10.2%% of failed terms"))
        self.assertFalse(offending_percents("defaults to %(default)s"))
        self.assertFalse(offending_percents("no percentages here at all"))

    def test_the_check_catches_the_real_case(self):
        self.assertTrue(offending_percents("are wrong 47.1% of the time"))
        self.assertTrue(offending_percents("a bare % sign"))


if __name__ == "__main__":
    unittest.main()
