"""The scorer must not consume a run that was cut short.

The chain learned to skip incomplete artifacts; the READERS had not. Every
scorer, the results index and the audit would still have taken a file killed at
1129 of 1200 terms and reported its rate as though it covered the sample - and
one such file is committed in this repository.

Truncation here is biased rather than merely small: terms are taken in
book-count order, so the missing tail is exactly the rarest words, which are
the ones a pronunciation lexicon exists for.

Snakemake raises IncompleteFilesException and makes you pass
--rerun-incomplete; Spark expects readers to check _SUCCESS rather than the
data files. This is the same contract with a --allow-partial escape.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from experiments import pair_e_row

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments", "pair_e_row.py")
# audit_experiment_artifacts lives at the repo root, not under app/.
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _row(term, plain=False, respelled=True):
    return {"term": term, "kana": "ア", "respelling": "ah", "books": 1,
            "series": 1, "plain_heard": "", "plain_recovers_word": plain,
            "plain_closeness": 0.0, "plain_scattered": 0.0,
            "respelled_heard": "", "respelled_recovers_word": respelled,
            "respelled_closeness": 1.0, "respelled_scattered": 1.0,
            "helps": True, "hurts": False, "closeness_delta": 1.0}


class CompletenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _artifact(self, name, **doc):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def test_an_explicit_partial_status_is_believed(self):
        p = self._artifact("a.json", status="partial", results=[], candidates_considered=10)
        self.assertEqual("partial", pair_e_row.completeness(p))

    def test_counts_decide_when_the_status_field_is_absent(self):
        # Artifacts written before the field exists must still be judged.
        p = self._artifact("b.json", results=[1, 2], candidates_considered=5)
        self.assertEqual("partial", pair_e_row.completeness(p))
        q = self._artifact("c.json", results=[1, 2, 3], candidates_considered=3)
        self.assertEqual("complete", pair_e_row.completeness(q))

    def test_neither_source_gives_unknown_rather_than_a_guess(self):
        p = self._artifact("d.json", results=[1, 2])
        self.assertEqual("unknown", pair_e_row.completeness(p))


class RefusalTest(unittest.TestCase):
    """Driven through the CLI, because the refusal is the CLI's contract."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.baseline = self._write("base.json", "complete", 3)
        self.partial = self._write("arm.json", "partial", 3, written=2)

    def _write(self, name, status, requested, written=None):
        path = os.path.join(self.tmp.name, name)
        rows = [_row(f"t{i}") for i in range(written if written is not None else requested)]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"status": status, "candidates_considered": requested,
                       "results": rows}, fh)
        return path

    def _run(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args],
                              capture_output=True, text=True, timeout=120)

    def test_a_partial_arm_is_refused_by_default(self):
        r = self._run(self.partial, "--baseline", self.baseline)
        self.assertNotEqual(0, r.returncode)
        self.assertIn("refusing to score a partial artifact", r.stderr + r.stdout)

    def test_the_refusal_says_why_the_subset_is_biased_not_merely_small(self):
        r = self._run(self.partial, "--baseline", self.baseline)
        self.assertIn("rarest", r.stderr + r.stdout)

    def test_allow_partial_scores_it_and_labels_every_output(self):
        r = self._run(self.partial, "--baseline", self.baseline, "--allow-partial")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("ARM IS PARTIAL", r.stdout)

    def test_a_complete_arm_scores_without_a_warning(self):
        complete = self._write("full.json", "complete", 3)
        r = self._run(complete, "--baseline", self.baseline)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertNotIn("PARTIAL", r.stdout)


if __name__ == "__main__":
    unittest.main()


class SharedCompletenessTest(unittest.TestCase):
    """One definition of "did this run finish", used by every reader.

    The chain, the scorer, the rescorer and the structural audit all ask it.
    Two copies would drift the way every other duplicated question in this
    repo has (Rule 15) - and this one decides whether a number gets cited.
    """

    def test_pair_e_row_uses_the_shared_definition(self):
        from experiments import manifest
        self.assertIs(pair_e_row.completeness, manifest.completeness)

    def test_the_audit_records_completeness_for_every_row(self):
        """A null would be a fourth state no reader handles - including the
        rows that bail early on an unreadable file."""
        import audit_experiment_artifacts as audit
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "broken.json"), "w") as fh:
                fh.write("{")
            with open(os.path.join(tmp, "listy.json"), "w") as fh:
                fh.write("[]")
            with open(os.path.join(tmp, "part.json"), "w") as fh:
                json.dump({"results": [1], "candidates_considered": 9}, fh)
            rows = audit.build_audit(tmp)["artifacts"]
        by_name = {r["artifact"]: r for r in rows}
        self.assertEqual("unknown", by_name["broken.json"]["completeness"])
        self.assertEqual("unknown", by_name["listy.json"]["completeness"])
        self.assertEqual("partial", by_name["part.json"]["completeness"])
        self.assertTrue(all(r.get("completeness") for r in rows))
