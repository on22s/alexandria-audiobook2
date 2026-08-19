"""A skip must ask whether the artifact is FINISHED, not whether it exists.

`respelling_e_row__ay_n1200.json` sits in this repository at 1129 of 1200
terms with no `status` field. Terms are ordered by book count, so the missing
tail is the rarest words: a chain that skips on existence alone would skip
that arm forever, on a biased subset, and print SKIP as though it were done.

Six chains define `artifact_complete()` for exactly this reason. On 2026-08-19
one of them - e_row_arms_20260817.sh - defined the helper at line 38 and never
called it, keeping the bare `-e` test it was written to replace. That is the
class this file guards: a fix that was written and not wired up.
"""
import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAINS = sorted(glob.glob(os.path.join(REPO, "run_chains", "*.sh")))


def source_of(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ChainSkipGuardTest(unittest.TestCase):
    def test_a_chain_that_defines_the_completeness_helper_calls_it(self):
        unused = []
        for path in CHAINS:
            source = source_of(path)
            if "artifact_complete()" not in source:
                continue
            calls = re.findall(r'(?<!\w)artifact_complete\s+"', source)
            if not calls:
                unused.append(os.path.basename(path))
        self.assertEqual([], unused,
                         "these define artifact_complete and never call it, "
                         "which means their skip is still the bare existence "
                         "test the helper was written to replace: %s" % unused)

    def test_no_chain_skips_on_existence_alone(self):
        """`[ -e "$out" ] && continue` with no completeness check beside it."""
        offenders = []
        for path in CHAINS:
            for number, line in enumerate(source_of(path).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # A skip is an existence test that leads to `continue` on the
                # same line, with nothing else consulted.
                if not re.search(r'\[\s*-[ef]\s+"\$\{?\w+\}?"\s*\]', stripped):
                    continue
                if "continue" not in stripped:
                    continue
                if "artifact_complete" in stripped or "complete" in stripped:
                    continue
                offenders.append("%s:%d %s" % (os.path.basename(path), number,
                                               stripped[:70]))
        self.assertEqual([], offenders,
                         "an artifact that exists is not an artifact that "
                         "finished:\n  " + "\n  ".join(offenders))

    def test_the_known_partial_artifact_would_be_rejected(self):
        """Rule 21: check the guard against a case whose answer is known.

        respelling_e_row__ay_n1200.json really is short - 1129 of 1200 - so any
        chain's helper must call it incomplete. If this file is ever completed
        or removed, this test should be re-pointed, not deleted.
        """
        import json
        partial = os.path.join(REPO, "ab_test_runtime", "experiments",
                               "respelling_e_row__ay_n1200.json")
        if not os.path.exists(partial):
            self.skipTest("the known-partial artifact is gone")
        with open(partial, encoding="utf-8") as handle:
            doc = json.load(handle)
        results, considered = doc.get("results"), doc.get("candidates_considered")
        self.assertIsInstance(results, list)
        self.assertLess(len(results), considered,
                        "this fixture is only useful while it is incomplete")


if __name__ == "__main__":
    unittest.main()
