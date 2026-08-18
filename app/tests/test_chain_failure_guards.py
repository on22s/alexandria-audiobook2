"""A chain that captures an exit code must do something with it.

The re-gate printed COMPLETE and exited 0 after all 67 of its adapters failed,
because it captured `rc=$?` per adapter and only echoed it. Bash discards a
loop iteration's status and `set -e` does not reach inside a loop body, so the
capture was decorative. Two GPU hours were logged as OK.

This does NOT retrofit the older chains - several are historical records of a
run that already happened, and rewriting them would edit the record. It stops
the pattern SPREADING: a chain written from today on either aggregates its
failures or is listed below as a known-legacy exception, which makes the
exception a decision rather than an oversight.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAINS = os.path.join(REPO, "run_chains")

# Chains that captured per-item exit codes without aggregating them, as of
# 2026-08-18. They are frozen history: each one describes a run that already
# happened, and its log is the evidence. Do not add to this list - use
# lib/stage.sh. Removing an entry (by fixing the chain) is always welcome.
LEGACY_UNGUARDED = {
    "chinese_median_speaker.sh", "daisymiller_preflight_proof.sh",
    "decontaminate_library.sh", "determinism_chain.sh", "intervention_chain.sh",
    "medoid_retrain_chain.sh", "moss_vs_lora.sh", "overnight_2026_08_08.sh",
    "overnight_2026_08_09.sh", "pdnc_context_evidence.sh", "ref_audit_chain.sh",
    "regate_reference_text.sh", "replay_dirty_evidence_20260817.sh", "run_rebuild_retrain.sh", "sharp_intervention_chain.sh",
    "morning_20260818.sh", "unseen_books_run_20260818.sh",
    "unseen_books_run_20260818b.sh",
}

CAPTURES_RC = re.compile(r"rc=\$\?|\brc\s*=\s*\$\?")
AGGREGATES = re.compile(r"stage_summary|failed_n|failures=|exit 1|return 1")


class ChainFailureGuardTest(unittest.TestCase):
    def _chains(self):
        for name in sorted(os.listdir(CHAINS)):
            if not name.endswith(".sh"):
                continue
            with open(os.path.join(CHAINS, name), encoding="utf-8") as fh:
                yield name, fh.read()

    def test_new_chains_that_capture_an_exit_code_also_act_on_it(self):
        offenders = [name for name, src in self._chains()
                     if CAPTURES_RC.search(src) and not AGGREGATES.search(src)
                     and name not in LEGACY_UNGUARDED]
        self.assertEqual([], offenders,
                         "these chains record a per-item exit code and never "
                         "look at it, so they will report success while every "
                         "item fails - use run_chains/lib/stage.sh")

    def test_the_legacy_list_does_not_name_chains_that_no_longer_exist(self):
        """A stale exemption silently re-permits the pattern under that name."""
        present = {name for name, _ in self._chains()}
        self.assertEqual(set(), LEGACY_UNGUARDED - present,
                         "exempted chains that are gone; drop them from the list")

    def test_the_legacy_list_does_not_exempt_a_chain_that_is_already_fixed(self):
        # An exemption that is no longer needed hides the fact that the debt
        # was paid, and makes the list look larger than the problem.
        still_unguarded = {name for name, src in self._chains()
                           if CAPTURES_RC.search(src) and not AGGREGATES.search(src)}
        needless = LEGACY_UNGUARDED - still_unguarded
        self.assertEqual(set(), needless,
                         "these are guarded now - remove them from "
                         "LEGACY_UNGUARDED")


if __name__ == "__main__":
    unittest.main()


# Chains that skip work when an artifact already exists. The n1200 respelling
# block proved that skipping on EXISTENCE is not the same as skipping on
# COMPLETION: it was killed at 1129 of 1200 terms, left a file that looked
# finished, and every later run would have skipped it forever.
SKIP_ON_EXISTENCE = re.compile(r"\[\s*-e\s+\"?\$\{?out|\[\s*-f\s+\"?\$\{?out|"
                               r"\[\s*-e\s+\"\$out\"\s*\]")
CHECKS_COMPLETION = re.compile(r"status.*complete|completeness|candidates_considered")


class ChainIdempotencyTest(unittest.TestCase):
    """Re-running a chain must do nothing, and must know what "done" means.

    Every pipeline guide gives the same advice for proving idempotency: run it
    twice and assert the second run changes nothing. lib/stage.sh has that test
    directly; this one covers the part a twice-run cannot see - what the chain
    BELIEVES finished work looks like.

    A chain that skips on existence will skip a truncated artifact forever, and
    the truncation is biased rather than merely small wherever items are
    ordered. That is not hypothetical: respelling_e_row__ay_n1200.json is in
    this repository at 1129 of 1200 terms.
    """

    def _chains(self):
        for name in sorted(os.listdir(CHAINS)):
            if name.endswith(".sh"):
                with open(os.path.join(CHAINS, name), encoding="utf-8") as fh:
                    yield name, fh.read()

    def test_a_chain_that_skips_on_an_artifact_also_checks_it_finished(self):
        offenders = []
        for name, src in self._chains():
            if not SKIP_ON_EXISTENCE.search(src):
                continue
            if not CHECKS_COMPLETION.search(src):
                offenders.append(name)
        self.assertEqual(
            [], offenders,
            "these chains treat an artifact's existence as completion; a run "
            "killed mid-way leaves a file that looks finished and is skipped "
            "forever. Check status == complete, as morning_20260818.sh does.")

    def test_the_completion_check_is_not_satisfied_by_the_word_alone(self):
        """Guards this test: a chain merely mentioning 'complete' in prose
        must not pass. Keeps the pattern honest about what it matches."""
        self.assertIsNone(CHECKS_COMPLETION.search("# this run is complete-ish"))
        self.assertIsNotNone(CHECKS_COMPLETION.search("d.get('status')=='complete'"))
