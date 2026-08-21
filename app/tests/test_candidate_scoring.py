"""Scoring candidates instead of generating a name, and why not this way.

SIG (arXiv 2312.14590) reads its answer out as the highest generation
probability over enumerated candidates rather than by direct generation. That
removes the list-order mechanism #383 measured (67.2%, p = 1.3e-23) instead of
randomising it, which is what `shuffled_roster` does.

Measured before building it: 53% of PDNC candidates share a first token - 16
share `MR` in Pride and Prejudice - so a one-step score compares honorifics,
not people. That is worse than the bias it replaces, because it is systematic
rather than merely biased. The correct implementation scores the whole
candidate sequence, at one forward pass per candidate per quote.

Everything here runs without a server. The parts that need one refuse rather
than degrade, because a scorer that quietly fell back to generation would
report a number for an experiment that did not happen.
"""
import unittest

from experiments.candidate_scoring import (collisions, decide, first_token_of,
                                           read_logprobs, score_candidates)


class FirstTokenTest(unittest.TestCase):
    def test_the_leading_word_is_taken(self):
        self.assertEqual("MR.", first_token_of("MR. DARCY"))
        self.assertEqual("ELIZABETH", first_token_of("ELIZABETH"))

    def test_blank_input_yields_blank(self):
        self.assertEqual("", first_token_of(""))
        self.assertEqual("", first_token_of(None))
        self.assertEqual("", first_token_of("   "))


class CollisionTest(unittest.TestCase):
    def test_honorifics_collide_which_is_the_whole_finding(self):
        groups = collisions(["MR. BENNET", "MR. DARCY", "ELIZABETH"])
        self.assertEqual({"MR": ["MR. BENNET", "MR. DARCY"]}, groups)

    def test_distinct_leading_words_do_not_collide(self):
        self.assertEqual({}, collisions(["ELIZABETH", "JANE", "LYDIA"]))

    def test_a_single_candidate_is_not_a_collision(self):
        self.assertEqual({}, collisions(["MR. DARCY"]))


class ReadLogprobsTest(unittest.TestCase):
    def _choice(self, top):
        return {"logprobs": {"content": [{"top_logprobs": top}]}}

    def test_the_expected_shape_is_read(self):
        out = read_logprobs(self._choice([{"token": "MRS", "logprob": -0.2},
                                          {"token": "MR", "logprob": -1.4}]))
        self.assertEqual({"MRS": -0.2, "MR": -1.4}, out)

    def test_every_other_shape_is_none_rather_than_a_guess(self):
        """None makes the caller refuse; a guess makes it invent an arm."""
        for bad in ({}, {"logprobs": {}}, {"logprobs": {"content": []}},
                    self._choice([]), self._choice([{"token": "A"}]),
                    self._choice([{"logprob": -1.0}])):
            self.assertIsNone(read_logprobs(bad), bad)


class ScoreTest(unittest.TestCase):
    LP = {"MRS": -0.2, "MR": -1.4, "ELIZ": -3.0}

    def test_the_best_scoring_candidate_wins(self):
        self.assertEqual(("MRS. BENNET", None),
                         decide(self.LP, ["MRS. BENNET", "MR. BENNET"]))

    def test_a_candidate_outside_the_returned_alternatives_is_absent(self):
        """The endpoint returns a truncated list, so missing is not improbable."""
        self.assertEqual([("MRS. BENNET", -0.2)],
                         score_candidates({"MRS": -0.2}, ["MRS. BENNET", "JANE"]))

    def test_no_visible_candidate_declines_rather_than_picking(self):
        winner, why = decide({"ZZZ": -0.1}, ["JANE", "LYDIA"])
        self.assertIsNone(winner)
        self.assertIn("no candidate", why)

    def test_an_exact_tie_declines_rather_than_taking_the_first(self):
        """Taking the first would reintroduce list order through the back door."""
        winner, why = decide({"JANE": -1.0, "LYDIA": -1.0}, ["JANE", "LYDIA"])
        self.assertIsNone(winner)
        self.assertIn("tie", why)

    def test_colliding_candidates_score_identically_which_is_the_problem(self):
        """Both MR. names read the same token; the score cannot separate them."""
        scored = score_candidates({"MR": -0.5}, ["MR. BENNET", "MR. DARCY"])
        self.assertEqual([-0.5, -0.5], [s for _, s in scored])
        self.assertIsNone(decide({"MR": -0.5}, ["MR. BENNET", "MR. DARCY"])[0])
