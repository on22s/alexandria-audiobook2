"""Every path GOALS.md cites as evidence must actually be in the repository.

WHY THIS EXISTS. On 2026-09-04 GOALS 2.7 cited "the nine
library_fidelity_seed_*_n20_full75.json" while only six were committed - the
other three sat untracked on one machine. Every index check passed the whole
time, because the indexes count TRACKED files and an artifact that was never
committed is invisible to them by construction. "Are the indexes current?" and
"do the indexes cover what the document claims?" are different questions, and
nothing was asking the second.

The same hour turned up the other shape of it: two promotion receipts cited as
`promotion_backups/...` that live at `ab_test_runtime/promotion_backups/...`.
The files existed; the citation pointed at nothing. A reader checking the claim
would have concluded the evidence was gone.

WHAT COUNTS AS SATISFIED. Tracked, or a directory/glob with at least one
tracked file under it, or deliberately gitignored - the blinded listening key
is excluded on purpose, since committing it would break the blinding, and that
is a correct exclusion rather than a missing artifact.

WHAT IS DELIBERATELY NOT CHECKED. Citations whose first segment is not a
top-level entry of this repository: `/api/config` is an HTTP route,
`agent/asr-hybrid-cjk` a branch, `gasmichel/UAR_scene` someone else's GitHub
repo. A first draft of this check flagged all of them and had a false-positive
rate of 14 in 15, which is worse than no check at all - a test nobody believes
gets disabled.
"""
import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CITATION = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./*-]*/[A-Za-z0-9_./*-]+)`")


def _git(*args):
    return subprocess.run(("git", "-C", REPO) + args,
                          capture_output=True, text=True, timeout=120)


def tracked_files():
    out = _git("ls-files")
    if out.returncode != 0:
        raise unittest.SkipTest("git unavailable")
    return set(out.stdout.split())


def top_level_entries():
    return {name.split("/")[0] for name in os.listdir(REPO)}


def cited_paths():
    text = open(os.path.join(REPO, "GOALS.md"), encoding="utf-8").read()
    return sorted(set(CITATION.findall(text)))


class GoalsEvidence(unittest.TestCase):
    def setUp(self):
        self.tracked = tracked_files()
        self.tops = top_level_entries()

    def _relevant(self, path):
        """Only paths rooted in a real top-level entry are repository paths."""
        return path.split("/")[0] in self.tops

    def _satisfied(self, path):
        full = os.path.join(REPO, path)
        if "*" in path:
            import glob
            return any(os.path.relpath(h, REPO) in self.tracked
                       for h in glob.glob(full))
        if path.rstrip("/") + "/" in {t.rsplit("/", 1)[0] + "/" for t in self.tracked}:
            return True
        if os.path.isdir(full):
            prefix = path.rstrip("/") + "/"
            return any(t.startswith(prefix) for t in self.tracked)
        if path in self.tracked:
            return True
        # A deliberate exclusion is not a missing artifact.
        return _git("check-ignore", "-q", path).returncode == 0

    def test_every_cited_evidence_path_is_in_the_repository(self):
        missing = [p for p in cited_paths()
                   if self._relevant(p) and not self._satisfied(p)]
        self.assertEqual([], missing,
                         "GOALS.md cites paths that are neither tracked nor "
                         "deliberately ignored:\n  " + "\n  ".join(missing))

    def test_a_citation_at_the_wrong_prefix_is_caught(self):
        """The promotion-receipt bug: the file exists, the citation does not
        point at it. Skipped citations must not hide a real file sitting
        somewhere else under a different prefix."""
        by_name = {}
        for t in self.tracked:
            by_name.setdefault(os.path.basename(t), []).append(t)
        wrong = []
        for p in cited_paths():
            if self._relevant(p) or "*" in p:
                continue
            hits = by_name.get(os.path.basename(p), [])
            if len(hits) == 1 and not p.endswith(("/",)):
                wrong.append("%s -> actually at %s" % (p, hits[0]))
        self.assertEqual([], wrong,
                         "GOALS.md cites a path that does not exist while the "
                         "file sits elsewhere:\n  " + "\n  ".join(wrong))

    def test_the_checker_finds_the_paths_it_is_meant_to_check(self):
        """Rule 21: a checker that silently matches nothing always passes.
        GOALS cites dozens of artifacts; if this drops near zero the regex
        has broken and the suite would go green on no evidence at all."""
        relevant = [p for p in cited_paths() if self._relevant(p)]
        self.assertGreater(len(relevant), 15,
                           "only %d citations resolved - the citation regex "
                           "has probably stopped matching" % len(relevant))


if __name__ == "__main__":
    unittest.main()
