"""Conventions an experiment script must follow, checked without running it.

Every class here is a mistake actually made in this repository, not a rule
invented for tidiness:

- `provenance()` called with no argument. Hit on 2026-08-21 writing
  riqua_fixture.py: `TypeError: provenance() missing 1 required positional
  argument: 'script_file'`, thrown only after the reader had done all its work,
  so the artifact was never written.
- A chain looping over arms while its guard checks only one of them. The
  explicit_hint chain asserted `explicit_hint in PROMPT_VARIANTS` while running
  five arms, so a checkout missing a NEWER variant would sail past and run the
  control under the new arm's name.
- A fixture whose entries lack the keys every reader assumes.
"""
import ast
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPERIMENTS = os.path.join(ROOT, "experiments")
FIXTURES = os.path.join(ROOT, "fixtures")
CHAINS = os.path.join(os.path.dirname(ROOT), "run_chains")


def experiment_paths():
    return [os.path.join(EXPERIMENTS, n) for n in sorted(os.listdir(EXPERIMENTS))
            if n.endswith(".py") and not n.startswith("_")]


class ProvenanceArityTests(unittest.TestCase):
    """provenance() takes the calling script's __file__. Calls without it
    raise TypeError at the END of a run, after the work is done."""

    def test_every_provenance_call_passes_an_argument(self):
        bad = []
        for path in experiment_paths():
            with open(path, encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read())
                except SyntaxError as exc:      # reported by another test
                    self.fail("%s does not parse: %s" % (os.path.basename(path), exc))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "provenance"
                        and not node.args and not node.keywords):
                    bad.append("%s:%d" % (os.path.basename(path), node.lineno))
        self.assertEqual(bad, [], "provenance() called with no argument at: %s"
                         % bad)

    def test_the_check_catches_a_planted_call(self):
        tree = ast.parse("from x import provenance\nd = provenance()\n")
        found = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "provenance" and not n.args and not n.keywords]
        self.assertEqual(len(found), 1)


class ChainArmGuardTests(unittest.TestCase):
    """A chain that runs N arms must refuse a checkout missing ANY of them."""

    def _chains(self):
        if not os.path.isdir(CHAINS):
            return []
        return [os.path.join(CHAINS, n) for n in sorted(os.listdir(CHAINS))
                if n.endswith(".sh")]

    def test_every_looped_arm_is_named_in_the_guard(self):
        offenders = []
        for path in self._chains():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if "PROMPT_VARIANTS" not in text:
                continue
            loops = re.findall(r"for\s+variant\s+in\s+([^;\n]+?)\s*;\s*do", text)
            for loop in loops:
                # `for variant in $ARMS` is the single-source form and is fine:
                # the guard reads the same variable.
                if loop.strip().startswith("$"):
                    continue
                arms = [a for a in loop.split() if a and not a.startswith("$")]
                for arm in arms:
                    if arm not in text.split("for variant")[0]:
                        offenders.append("%s runs %r but its guard never "
                                         "names it" % (os.path.basename(path), arm))
        self.assertEqual(offenders, [], "\n  ".join(offenders))


class FixtureIntegrityTests(unittest.TestCase):
    """Fixtures are read by every attribution experiment. A malformed one
    fails deep inside a GPU run rather than here."""

    REQUIRED = ("id", "line")

    def _fixtures(self):
        if not os.path.isdir(FIXTURES):
            return []
        return [os.path.join(FIXTURES, n) for n in sorted(os.listdir(FIXTURES))
                if n.endswith(".json")]

    def test_every_fixture_parses(self):
        for path in self._fixtures():
            with open(path, encoding="utf-8") as fh:
                try:
                    json.load(fh)
                except Exception as exc:
                    self.fail("%s is not valid JSON: %s"
                              % (os.path.basename(path), exc))

    def test_attribution_fixtures_have_entries_with_the_expected_keys(self):
        checked = 0
        for path in self._fixtures():
            name = os.path.basename(path)
            if not name.startswith("attribution_gold"):
                continue
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or "entries" not in data:
                continue
            entries = data["entries"]
            self.assertIsInstance(entries, list, name)
            if not entries:
                continue
            checked += 1
            for key in self.REQUIRED:
                self.assertIn(key, entries[0],
                              "%s entry 0 has no %r" % (name, key))
        self.assertGreater(checked, 0, "no attribution fixtures were checked - "
                                       "this test would pass vacuously")

    def test_no_fixture_entry_id_is_duplicated(self):
        for path in self._fixtures():
            name = os.path.basename(path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                continue
            entries = data.get("entries")
            if not isinstance(entries, list) or not entries:
                continue
            ids = [e.get("id") for e in entries if isinstance(e, dict)]
            ids = [i for i in ids if i is not None]
            self.assertEqual(len(ids), len(set(ids)),
                             "%s has duplicate entry ids - a scorer keyed on "
                             "id would silently drop rows" % name)
