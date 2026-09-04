"""A row must be able to say which prompt produced it, and a probe must be
able to re-ask only the batches that disagreed.

WHAT THIS IS FOR. On 2026-09-04 two Qwen3-14B evaluations that shared a base
model, a book and greedy decoding disagreed on 3 of 133 base rows:

    mushoku16-00923   NANAHOSHI vs HITOGAMI
    mushoku16-00927   NANAHOSHI vs HITOGAMI
    mushoku16-00352   SYLPHY    vs NORN

Two questions follow, and neither could be answered from the artifacts.
Were the runs asked the SAME question? `prompt_sha256` is in every row's
schema and was NULL in every row ever written, because nothing passed a
prompt to record.add - so an inspection comparing them compares None to None
and reports agreement. And could the disagreement be re-tested cheaply? Not
without re-running whole books, because the evaluator could not name a subset.
"""
import unittest

from experiments.manifest import ExperimentRecord


def _rec():
    r = ExperimentRecord.__new__(ExperimentRecord)
    r.rows, r.meta, r.started = [], {}, 0.0
    return r


class BatchIdentity(unittest.TestCase):
    def test_a_hash_may_be_supplied_without_the_prompt_text(self):
        """The caller that knows the batch identity is the client; the caller
        that writes the row never holds the prompt."""
        r = _rec()
        r.add("base", "b:1", "line", "A", "A", True, prompt_sha256="deadbeef")
        self.assertEqual("deadbeef", r.rows[0]["prompt_sha256"])

    def test_the_prompt_text_still_works(self):
        r = _rec()
        r.add("base", "b:1", "line", "A", "A", True, prompt="hello")
        self.assertIsNotNone(r.rows[0]["prompt_sha256"])
        self.assertEqual(5, r.rows[0]["prompt_chars"])

    def test_neither_given_is_still_null(self):
        """Unchanged for every caller that passes nothing."""
        r = _rec()
        r.add("base", "b:1", "line", "A", "A", True)
        self.assertIsNone(r.rows[0]["prompt_sha256"])

    def test_comparing_two_nulls_must_not_read_as_agreement(self):
        """The trap this whole change exists to remove. An inspection that
        compares prompt hashes across two runs reported '133/133 identical'
        when every value on both sides was None."""
        a = _rec(); b = _rec()
        a.add("base", "b:1", "l", "A", "X", False)
        b.add("base", "b:1", "l", "A", "Y", False)
        ha, hb = a.rows[0]["prompt_sha256"], b.rows[0]["prompt_sha256"]
        self.assertEqual(ha, hb)          # both None - "identical"
        self.assertIsNone(ha)             # ...and therefore meaningless
        # A real comparison must require the hash to EXIST before comparing.
        self.assertFalse(ha is not None and ha == hb)


if __name__ == "__main__":
    unittest.main()
