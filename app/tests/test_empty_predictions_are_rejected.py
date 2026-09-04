"""An arm that predicted nothing is an inference failure until proven otherwise.

WHAT THIS PINS. On 2026-09-04 an FP8 diagnostic wrote 766 rows, both arms 0
correct, every `predicted` None, no error recorded, and `meta.validation` of
"ok". Inference never ran: the model could not fetch
`kernels-community/finegrained-fp8` with Hugging Face forced offline, so 428
batches failed before a token and each was retried four times. The artifact
read as a model result - "the adapter answers nothing" - which is a claim about
a model made by a missing dependency.

The check that catches this already existed. It was opt-in, and the run that
needed it did not ask.
"""
import unittest

from experiments.manifest import ExperimentRecord


def _record(rows, **meta):
    rec = ExperimentRecord.__new__(ExperimentRecord)
    rec.rows = rows
    rec.meta = {"git": {"harness_sha256": "x"}, "model": "m", **meta}
    rec.started = 0.0
    return rec


class EmptyPredictions(unittest.TestCase):
    def _problems(self, rows, contract=None):
        """No try/except here on purpose. An earlier draft swallowed the
        exception and called skipTest, so all four tests reported OK while
        proving nothing - the same shape of fallback this whole change exists
        to remove, written into its own test."""
        return _record(rows).validate(contract or {})

    def test_an_all_empty_arm_is_rejected_without_being_asked(self):
        """The regression: this passed because nobody set the flag."""
        rows = [{"arm": "tuned", "id": i, "predicted": None, "correct": False,
                 "in_candidates": True} for i in range(20)]
        problems = self._problems(rows)
        self.assertTrue(any("every prediction is empty" in p for p in problems),
                        problems)

    def test_one_real_prediction_is_enough_to_pass(self):
        rows = [{"arm": "tuned", "id": i, "predicted": None, "correct": False,
                 "in_candidates": True} for i in range(19)]
        rows.append({"arm": "tuned", "id": 19, "predicted": "SUBARU",
                     "correct": True, "in_candidates": True})
        problems = self._problems(rows)
        self.assertFalse(any("every prediction is empty" in p for p in problems),
                         problems)

    def test_an_arm_that_does_not_predict_is_left_alone(self):
        """crossbook_normalization compares raw against normalized TEXT; its
        rows carry no `predicted` key and it is not an inference failure."""
        rows = [{"arm": "raw", "id": i, "chars": 10, "correct": False,
                 "in_candidates": True} for i in range(6)]
        problems = self._problems(rows)
        self.assertFalse(any("every prediction is empty" in p for p in problems),
                         problems)

    def test_a_run_may_still_opt_out_explicitly(self):
        """Deliberate is fine; silent is not."""
        rows = [{"arm": "tuned", "id": i, "predicted": None, "correct": False,
                 "in_candidates": True} for i in range(20)]
        problems = self._problems(rows, {"require_any_prediction": False})
        self.assertFalse(any("every prediction is empty" in p for p in problems),
                         problems)


if __name__ == "__main__":
    unittest.main()
